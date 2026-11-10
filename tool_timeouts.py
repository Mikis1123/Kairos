"""Deadlines for external tools (ffmpeg / ffprobe / exiftool).

A corrupt or truncated video can make ffmpeg stall without ever exiting. A one-shot call with no
timeout then blocks the background scanner (and its Stop button) forever. Two tools:

  * the *_TIMEOUT_S constants, passed as `timeout=` to every one-shot `subprocess.run`
    (subprocess.run kills the child and raises TimeoutExpired when the deadline passes);
  * StallWatchdog for long-running streaming children (proxy transcode, scrub strip): they are
    allowed to run for as long as they like, but not to make no progress. The idle clock does not
    advance while the governor has the process suspended for the user's interaction (`paused`).
"""
import threading
import time

FFMPEG_ONESHOT_TIMEOUT_S = 60      # one frame / one thumbnail
FFMPEG_PROBE_TIMEOUT_S = 30        # header probe (ffmpeg -i)
FFMPEG_GENERATE_TIMEOUT_S = 300    # synthesising + encoding a whole music track
STALL_S = 120                      # no output for this long (not counting paused time) = stuck


class StallWatchdog(threading.Thread):
    """Kills `proc` when `kick()` has not been called for `idle_s` seconds of un-paused time.
    `fired` tells the owner why the process died. Stop it with `stop()` (idempotent)."""

    def __init__(self, proc, idle_s=STALL_S, paused=None, poll_s=0.5, clock=time.monotonic):
        super().__init__(name="tool-watchdog", daemon=True)
        self.proc = proc
        self.idle_s = float(idle_s)
        self.poll_s = float(poll_s)
        self._paused = paused
        self._clock = clock
        self._idle = 0.0
        self._last = clock()
        self._done = threading.Event()
        self.fired = False
        self.start()

    def kick(self):
        self._idle = 0.0

    def stop(self):
        self._done.set()

    def run(self):
        while not self._done.wait(self.poll_s):
            now = self._clock()
            dt, self._last = now - self._last, now
            try:
                paused = bool(self._paused()) if self._paused is not None else False
            except Exception:                       # noqa: BLE001 - a broken probe must not kill ffmpeg
                paused = False
            if not paused:
                self._idle += dt
            if self.proc.poll() is not None:
                return
            if self._idle > self.idle_s:
                self.fired = True
                try:
                    self.proc.kill()
                except OSError:
                    pass
                return
