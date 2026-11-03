"""Windows-safe atomic file replacement.

os.replace() on Windows fails with WinError 5 (access denied) / 32 (sharing violation) while
another handle holds the *target* open without FILE_SHARE_DELETE – a grid loader thread decoding
the previous thumbnail, Windows Defender scanning the fresh file, the Search indexer, a viewer.
Those locks are momentary, so the correct fix is a short bounded retry with backoff, then a
clean failure that leaves the old file intact and removes the temp file (never a traceback
in the console, never a stranded *.tmp.jpg).

Writers must also use a per-thread unique temp name (``unique_tmp``) so two producers of the
same target (scanner upgrade pass + on-demand rebuild) never fight over one temp file.
"""
import errno
import os
import threading
import time

# Total worst-case wait ≈ 20+40+80+160+320+400+400 ms ≈ 1.4 s – long enough to outlast an AV
# scan of an 840 px JPEG, short enough not to stall a scan pass noticeably.
_DELAYS = (0.02, 0.04, 0.08, 0.16, 0.32, 0.4, 0.4)
_RETRYABLE_WINERR = {5, 32, 33}          # access denied, sharing violation, lock violation
_RETRYABLE_ERRNO = {errno.EACCES, errno.EBUSY, errno.EPERM}


def unique_tmp(target, suffix=".tmp"):
    """`thumb_1.jpg` -> `thumb_1.<pid>-<tid>.tmp.jpg` (keeps the real extension last, so tools
    such as ffmpeg still infer the container from the name)."""
    root, ext = os.path.splitext(target)
    return f"{root}.{os.getpid()}-{threading.get_ident()}{suffix}{ext}"


def _retryable(exc):
    win = getattr(exc, "winerror", None)
    if win is not None:
        return win in _RETRYABLE_WINERR
    return getattr(exc, "errno", None) in _RETRYABLE_ERRNO


def replace(tmp, target, delays=_DELAYS, sleep=time.sleep):
    """os.replace(tmp, target) with retry on transient Windows locks.

    Returns True on success. On a permanent failure returns False, removes ``tmp`` and leaves
    ``target`` untouched. Non-transient errors (missing tmp, bad path) also return False."""
    for attempt in range(len(delays) + 1):
        try:
            os.replace(tmp, target)
            return True
        except OSError as exc:
            if not _retryable(exc) or attempt == len(delays):
                break
            sleep(delays[attempt])
    discard(tmp)
    return False


def discard(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
