"""Viewport-prioritised thumbnail decode queue (Sprint 2, 2026-11-05).

The grid used to hand every decode straight to a QThreadPool, which runs jobs first in, first
out. During a fast fling the queue filled with rows the user had already flown past. Those
decodes ran first and the rows now on screen waited behind them, so tiles stayed grey until the
fling settled and the stale jobs were cleared. Measured with eval/bench_massive_scale.py
(20 000 items, wheel flings, throttled CPU): 99.6 % of visible tiles were empty during the
flings.

Now the decode work and the threads are decoupled:

* Jobs wait here, keyed by item id. The pool only receives tiny "drain" runnables. Each drain,
  when a worker thread picks it up, pops the job that is BEST RIGHT NOW. The priority is computed
  at pop time against the latest viewport, not when the job was queued.
* Priority: visible rows nearest the viewport centre first, then rows AHEAD in the scroll
  direction (the predictive prefetch), then a short margin behind.
* A viewport update drops every job outside the keep window at once (the rows scrolled away).
  Those ids are returned so the model can forget them as "loading". If they ever come back on
  screen, the next paint re-requests them.

Pure Python, thread-safe, no Qt: unit-tested in test_sprint_ux.py.

The jobs sit in a heap. Priorities are recomputed once per viewport update on the GUI thread,
and a worker's pop is a heappop. A worker must never scan every job under the lock: probing
showed that each new tile's paint waited on exactly that lock.
"""
import heapq
import itertools
import threading

AHEAD_SCREENS_REST = 1.0      # predictive prefetch beyond the leading edge, in viewport heights
AHEAD_SCREENS_FLING = 2.0     # a fling travels further before it settles
BEHIND_SCREENS = 0.5          # kept behind the trailing edge (a small reversal stays warm)


class DecodeQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}               # item_id -> (row, job, seq)
        self._heap = []               # (priority, seq, item_id); stale entries skipped on pop
        self._seq = itertools.count()
        self._view = None             # (first, last, direction, ahead_items, behind_items)
        self.served = 0               # jobs handed to a worker (stats / tests)
        self.dropped = 0              # jobs discarded because their row left the keep window

    def __len__(self):
        with self._lock:
            return len(self._jobs)

    def __contains__(self, item_id):
        with self._lock:
            return item_id in self._jobs

    # ── viewport ──
    def set_viewport(self, first, last, direction=0, fast=False):
        """The rows on screen and the scroll direction (+1 down, -1 up, 0 resting). Returns the
        ids of jobs dropped because their row is outside the new keep window."""
        first, last = int(first), int(last)
        if last < first:
            return []
        span = last - first + 1
        ahead = int(round(span * (AHEAD_SCREENS_FLING if fast else AHEAD_SCREENS_REST)))
        behind = int(round(span * BEHIND_SCREENS))
        view = (first, last, int(direction), ahead, behind)
        with self._lock:
            moved = view != self._view
            self._view = view
            lo, hi = self._window_locked()
            gone = [i for i, (row, _job, _s) in self._jobs.items() if row < lo or row > hi]
            for i in gone:
                del self._jobs[i]
            self.dropped += len(gone)
            if not moved and not gone:
                return []                         # nothing moved: the heap's priorities hold
            prio = self._priority_locked
            self._heap = [(prio(row), seq, i) for i, (row, _job, seq) in self._jobs.items()]
            heapq.heapify(self._heap)
        return gone

    def window(self):
        """(lo, hi) rows worth decoding now (the keep window); None before the first viewport."""
        with self._lock:
            return self._window_locked() if self._view is not None else None

    def prefetch_rows(self):
        """Rows to request ahead of the viewport, nearest to the leading edge first."""
        with self._lock:
            if self._view is None:
                return []
            first, last, direction, ahead, behind = self._view
            lo, hi = self._window_locked()
        if direction < 0:
            return list(range(first - 1, lo - 1, -1))
        if direction > 0:
            return list(range(last + 1, hi + 1))
        out = []                                    # resting: alternate below / above
        for k in range(1, max(last + 1 - lo, hi - last) + 1):
            if last + k <= hi:
                out.append(last + k)
            if first - k >= lo:
                out.append(first - k)
        return out

    def _window_locked(self):
        first, last, direction, ahead, behind = self._view
        if direction > 0:
            return first - behind, last + ahead
        if direction < 0:
            return first - ahead, last + behind
        return first - ahead, last + ahead

    def priority(self, row):
        with self._lock:
            return self._priority_locked(row)

    def _priority_locked(self, row):
        if self._view is None:
            return float(row)                       # nothing known yet: top of the grid first
        first, last, direction, ahead, behind = self._view
        if first <= row <= last:
            return abs(row - (first + last) / 2.0)  # on screen: centre outwards
        span = last - first + 1
        if row > last:
            d, leading = row - last, direction >= 0
        else:
            d, leading = first - row, direction <= 0
        # ahead of the scroll: right after the visible rows; behind: after everything ahead
        return span + d if leading else span + ahead + d

    # ── jobs ──
    def submit(self, item_id, row, job):
        """Queues (or replaces) the decode for item_id at grid row `row`."""
        row = int(row)
        seq = next(self._seq)
        with self._lock:
            self._jobs[item_id] = (row, job, seq)
            heapq.heappush(self._heap, (self._priority_locked(row), seq, item_id))

    def discard(self, item_id):
        with self._lock:
            return self._jobs.pop(item_id, None) is not None

    def pop(self):
        """The best job for a worker thread now (None = nothing left)."""
        with self._lock:
            heap, jobs = self._heap, self._jobs
            while heap:
                _p, seq, item_id = heapq.heappop(heap)
                entry = jobs.get(item_id)
                if entry is not None and entry[2] == seq:
                    del jobs[item_id]
                    self.served += 1
                    return entry[1]
            return None

    def clear(self):
        """Drops everything; returns the dropped ids."""
        with self._lock:
            gone = list(self._jobs)
            self._jobs.clear()
            self._heap.clear()
            self.dropped += len(gone)
            return gone

    def pending_rows(self):
        with self._lock:
            return sorted(row for row, _job, _s in self._jobs.values())
