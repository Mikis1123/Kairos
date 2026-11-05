"""Latest-wins background loads: zero-stall GUI thread for library reads.

The deferred start-up services (people, events, search vectors, SigLIP coverage) and the
daemon's refresh signals ran their SQLite reads on the GUI thread. The vector tables are tens
of MB, so a frame could stall for hundreds of ms right when the window appeared or the daemon
finished a batch.

BackgroundLoads.run(key, fn, on_done) runs fn() on a small dedicated pool (it doesn't compete
with the thumbnail decoders) and delivers on_done(result) on the GUI thread. Each key keeps a
generation: when a newer load for the same key starts, the older result is dropped, so a burst
of daemon signals never renders stale data. fn must open its own DB connection (sqlite3
connections are per thread). An exception in fn is printed and delivered as on_error(e) when
one is given; it never reaches a Qt slot as an exception.
"""
import time
import traceback

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

# Eager mode (tests, like Celery's task_always_eager): run() executes fn inline and delivers at
# once. conftest turns it on, so a read can never outlive a test's temporary database and the
# old synchronous expectations hold. test_ui_fluidity covers the threaded path with SYNC off.
SYNC = False


class _Signals(QObject):
    done = pyqtSignal(str, int, object, object, float)      # key, gen, result, error, ms


class _Job(QRunnable):
    def __init__(self, key, gen, fn, signals):
        super().__init__()
        self.key, self.gen, self.fn, self.signals = key, gen, fn, signals

    def run(self):
        t = time.perf_counter()
        result, error = None, None
        try:
            result = self.fn()
        except Exception as e:                    # noqa: BLE001 – delivered, never raised in a slot
            error = e
            traceback.print_exc()
        try:
            self.signals.done.emit(self.key, self.gen, result, error, (time.perf_counter() - t) * 1000.0)
        except RuntimeError:                      # the window closed while the read ran
            pass


class BackgroundLoads(QObject):
    """Keyed, latest-wins background reads delivered on the owner's (GUI) thread."""

    def __init__(self, parent=None, threads=2):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(1, int(threads)))
        self._gen = {}
        self._handlers = {}
        self.timings = {}                         # key -> last worker time (ms), for benches/logs
        self._signals = _Signals(self)
        self._signals.done.connect(self._deliver)

    def run(self, key, fn, on_done, on_error=None):
        gen = self._gen.get(key, 0) + 1
        self._gen[key] = gen
        self._handlers[key] = (gen, on_done, on_error)
        job = _Job(key, gen, fn, self._signals)
        if SYNC:
            job.run()                              # queued signal: deliver now, not next loop pass
            self._flush_eager(key, gen)
        else:
            self.pool.start(job)
        return gen

    def pending(self, key):
        return key in self._handlers

    def cancel(self, key):
        """Drop whatever is in flight for key (its result is ignored)."""
        self._gen[key] = self._gen.get(key, 0) + 1
        self._handlers.pop(key, None)

    def wait(self, msecs=-1):
        """Tests / shutdown: wait for the pool, then deliver what finished."""
        self.pool.waitForDone(msecs)

    def _flush_eager(self, key, gen):
        from PyQt6.QtCore import QCoreApplication
        if self._handlers.get(key, (None,))[0] == gen:
            QCoreApplication.sendPostedEvents(self, 0)     # the queued done signal for this job

    def _deliver(self, key, gen, result, error, ms):
        handler = self._handlers.get(key)
        if handler is None or handler[0] != gen or gen != self._gen.get(key):
            return                                 # superseded: a newer load is in flight
        del self._handlers[key]
        self.timings[key] = ms
        _, on_done, on_error = handler
        try:
            if error is not None:
                if on_error is not None:
                    on_error(error)
            else:
                on_done(result)
        except Exception:                          # never let it reach qFatal
            traceback.print_exc()
