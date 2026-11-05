"""Přehrávač videa mimo GUI vlákno.

Změřeno (Qt 6.11, FFmpeg backend, HEVC z iPhonu): QMediaPlayer dělá těžkou práci ve vlákně,
ve kterém žije – setSource na už použitém přehrávači ukončí starou relaci (~120–200 ms),
první play() po načtení staví dekodér, renderery a zvukový výstup (~90–400 ms). V GUI vlákně
to bylo zamrznutí 0,3–0,6 s při každém otevření videa.

ThreadedPlayer je náhrada QMediaPlayer + QAudioOutput se stejným rozhraním, jaké používá
prohlížeč: skutečný přehrávač žije ve vlastním QThread, příkazy (play / pause / setSource /
setPosition …) se tam posílají frontou a GUI vlákno se vrací okamžitě. Snímky jdou přímo
z rendereru do QVideoSink výstupu (QML VideoOutput) jako dřív.

Stav (playbackState / position / duration / mediaStatus) drží proxy v GUI vlákně:
  * příkaz ho nastaví hned „opticky“ – rychlé přepínání play/pause tak vždy čte záměr,
    ne zpožděný stav dekodéru, a ikona se nepřeklápí tam a zpět;
  * hlášení z vlákna nesou pořadové číslo posledního provedeného příkazu – zastaralá
    (z doby před novějším příkazem) se zahodí, aktuální stav přepíše optiku.
"""
import atexit
import itertools

from PyQt6.QtCore import QObject, QThread, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

_PS = QMediaPlayer.PlaybackState
_MS = QMediaPlayer.MediaStatus
SHUTDOWN_WAIT_MS = 3000

_live = {}                     # id -> (QThread, _PlayerCore); drží je, dokud vlákno neskončí
_ids = itertools.count(1)


class _PlayerCore(QObject):
    """Žije v pracovním vlákně: vlastní QMediaPlayer + QAudioOutput, provádí příkazy."""
    report = pyqtSignal(int, int, int, "qint64", "qint64")    # seq, stav, status, pozice, délka
    frame_error = pyqtSignal(str)
    dispose_requested = pyqtSignal()   # vysílá GUI vlákno -> fronta pracovního vlákna

    def __init__(self):
        super().__init__()
        self.player = None
        self.audio = None
        self.seq = 0
        self.tracks_off = set()        # {"audio", "video"} switched off for the current source
        self.dispose_requested.connect(self._dispose)

    @pyqtSlot(int, str, object)
    def run(self, seq, name, arg):
        self.seq = max(self.seq, seq)
        if name == "setup":
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.audio.setVolume(arg)
            self.player.setAudioOutput(self.audio)
            self.player.playbackStateChanged.connect(self._emit)
            self.player.mediaStatusChanged.connect(self._emit)
            self.player.positionChanged.connect(self._emit)
            self.player.durationChanged.connect(self._emit)
            self.player.mediaStatusChanged.connect(self._on_status)
            self.player.errorOccurred.connect(lambda _e, s: self.frame_error.emit(s))
            return
        if self.player is None:
            return
        if name == "setVideoSink":
            self.player.setVideoSink(arg)
        elif name == "setTrackEnabled":
            kind, on = arg
            (self.tracks_off.discard if on else self.tracks_off.add)(kind)
            self._apply_tracks(kind)
            return
        elif name == "setSource":
            self.tracks_off.clear()    # Qt resets the active tracks with every new source too
            self.player.setSource(arg)
        elif name in ("setMuted", "setVolume"):
            getattr(self.audio, name)(arg)
            return
        elif arg is None:
            getattr(self.player, name)()
        else:
            getattr(self.player, name)(arg)
        self._emit()

    def _on_status(self, status):
        if status == _MS.LoadedMedia and self.tracks_off:
            self._apply_tracks()           # a choice made before loading would be overwritten

    def _apply_tracks(self, only=None):
        p = self.player
        if p is None or p.mediaStatus() in (_MS.NoMedia, _MS.LoadingMedia, _MS.InvalidMedia):
            return                         # applied on LoadedMedia
        for kind in (only,) if only else ("audio", "video"):
            setter = p.setActiveAudioTrack if kind == "audio" else p.setActiveVideoTrack
            current = p.activeAudioTrack() if kind == "audio" else p.activeVideoTrack()
            want = -1 if kind in self.tracks_off else 0
            if current != want:
                setter(want)

    def _emit(self, *_):
        p = self.player
        if p is not None:
            self.report.emit(self.seq, p.playbackState().value, p.mediaStatus().value,
                             p.position(), p.duration())

    @pyqtSlot()
    def _dispose(self):
        from PyQt6 import sip
        player, audio, self.player, self.audio = self.player, self.audio, None, None
        if player is not None:
            try:
                player.stop()
                player.setSource(QUrl())
                sip.delete(player)      # zničit ve vlastním vlákně (časovače backendu)
                sip.delete(audio)
            except RuntimeError:
                pass
        self.thread().quit()


class ThreadedAudio(QObject):
    """Zvukový výstup přehrávače (proxy QAudioOutput) – hlasitost a ztlumení."""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner
        self._volume = 0.8
        self._muted = False

    def volume(self):
        return self._volume

    def setVolume(self, value):
        self._volume = max(0.0, min(1.0, float(value)))
        self._owner._send("setVolume", self._volume, bump=False)

    def isMuted(self):
        return self._muted

    def setMuted(self, muted):
        self._muted = bool(muted)
        self._owner._send("setMuted", self._muted, bump=False)


class ThreadedPlayer(QObject):
    """Rozhraní QMediaPlayer (co používá prohlížeč), skutečný přehrávač ve vlastním vlákně."""
    positionChanged = pyqtSignal("qint64")
    durationChanged = pyqtSignal("qint64")
    playbackStateChanged = pyqtSignal(object)
    mediaStatusChanged = pyqtSignal(object)
    errorOccurred = pyqtSignal(str)
    _command = pyqtSignal(int, str, object)

    def __init__(self, parent=None, volume=0.8):
        super().__init__(parent)
        self._seq = 0
        self._intent = None               # požadovaný stav, dokud ho přehrávač nepotvrdí
        self._state = _PS.StoppedState
        self._status = _MS.NoMedia
        self._position = 0
        self._duration = 0
        self._rate = 1.0
        self._source = QUrl()
        self._output = None
        self._sink = None
        self.audio = ThreadedAudio(self)
        self.audio._volume = float(volume)
        self.commands = 0                 # počet odeslaných příkazů (testy / ladění)

        reap()
        key = next(_ids)
        thread = QThread()
        thread.setObjectName(f"KairosVideo{key}")
        core = _PlayerCore()
        core.moveToThread(thread)
        self._command.connect(core.run)
        core.report.connect(self._on_report)
        core.frame_error.connect(self._on_error)
        _live[key] = (thread, core)
        self._key = key
        self._acked = 0
        thread.start()
        self._command.emit(0, "setup", float(volume))
        # zničení proxy (zavřený prohlížeč) ukončí i vlákno – lambda bez odkazu na self
        self.destroyed.connect(lambda *_: _dispose_key(key))

    # ── příkazy ──
    def _send(self, name, arg=None, bump=True):
        if bump:
            self._seq += 1
        self.commands += 1
        try:
            self._command.emit(self._seq, name, arg)
        except RuntimeError:
            pass                          # proxy už zničena (zavírání aplikace)

    def _want(self, state):
        self._intent = state
        self._set_state(state)

    def play(self):
        if not self._source.isEmpty():
            self._want(_PS.PlayingState)
        self._send("play")

    def pause(self):
        if not self._source.isEmpty():
            self._want(_PS.PausedState)
        self._send("pause")

    def stop(self):
        self._want(_PS.StoppedState)
        self._set_position(0)
        self._send("stop")

    def setSource(self, url):
        url = QUrl(url)
        self._source = url
        self._want(_PS.StoppedState)
        self._set_status(_MS.NoMedia if url.isEmpty() else _MS.LoadingMedia)
        self._set_position(0)
        self._set_duration(0)
        self._send("setSource", url)

    def setPosition(self, pos):
        pos = max(0, int(pos))
        if self._duration > 0:
            pos = min(pos, self._duration)
        self._set_position(pos)
        self._send("setPosition", pos)

    def setPlaybackRate(self, rate):
        self._rate = float(rate)
        self._send("setPlaybackRate", self._rate, bump=False)

    def setAudioTrackEnabled(self, on):
        """False = play the current source without its audio track (until the next setSource).

        Live Photo clips: Qt 6.11's FFmpeg backend slaves video to the audio clock and holds
        it back ~12 % (37.5 ms instead of 33.3 ms per frame, measured); video alone is exact."""
        self._send("setTrackEnabled", ("audio", bool(on)), bump=False)

    def setVideoTrackEnabled(self, on):
        """False = audio only (no HEVC decode) for the current source."""
        self._send("setTrackEnabled", ("video", bool(on)), bump=False)

    def setVideoOutput(self, output):
        self._output = output
        self._sink = _sink_of(output)
        self._send("setVideoSink", self._sink, bump=False)

    def shutdown(self):
        """Okamžité ukončení vlákna (konec aplikace / testy)."""
        _dispose_key(self._key, wait=True)

    # ── stav (GUI vlákno, bez čekání) ──
    def playbackState(self):
        return self._state

    def mediaStatus(self):
        return self._status

    def position(self):
        return self._position

    def duration(self):
        return self._duration

    def playbackRate(self):
        return self._rate

    def source(self):
        return QUrl(self._source)

    def videoOutput(self):
        return self._output

    def videoSink(self):
        return self._sink

    def audioOutput(self):
        return self.audio

    def isBusy(self):
        """Pracovní vlákno ještě neprovedlo všechny příkazy."""
        return self._acked < self._seq

    # ── hlášení z vlákna ──
    def _on_report(self, seq, state, status, pos, dur):
        self._acked = seq
        if seq != self._seq:
            return                        # novější příkaz ještě čeká – optika platí
        state, status = _PS(state), _MS(status)
        self._set_duration(dur)
        self._set_status(status)
        # Záměr (play/pause) platí, dokud ho přehrávač nepotvrdí: Qt během načítání média
        # play() odloží a hlásí Stopped – bez tohohle by ikona i plánování blikaly
        # Playing -> Stopped -> Playing. Neplatné médium / konec záměr ruší.
        if self._intent is not None:
            if state == self._intent or status in (_MS.InvalidMedia, _MS.EndOfMedia):
                self._intent = None
            else:
                state = self._state
        self._set_state(state)
        self._set_position(pos)

    def _on_error(self, message):
        self._intent = None               # chyba = záměr se nesplní, platí skutečný stav
        self.errorOccurred.emit(message)

    def _set_state(self, state):
        if state != self._state:
            self._state = state
            self.playbackStateChanged.emit(state)

    def _set_status(self, status):
        if status != self._status:
            self._status = status
            self.mediaStatusChanged.emit(status)

    def _set_position(self, pos):
        pos = max(0, int(pos))
        if pos != self._position:
            self._position = pos
            self.positionChanged.emit(pos)

    def _set_duration(self, dur):
        dur = max(0, int(dur))
        if dur != self._duration:
            self._duration = dur
            self.durationChanged.emit(dur)


def _sink_of(output):
    """QVideoSink výstupu: QML VideoOutput (vlastnost), QGraphicsVideoItem / QVideoWidget (metoda)."""
    if output is None:
        return None
    if isinstance(output, QVideoSink):
        return output
    sink_fn = getattr(output, "videoSink", None)
    if callable(sink_fn):
        return sink_fn()
    sink = output.property("videoSink")
    return sink if isinstance(sink, QVideoSink) else None


def _dispose_key(key, wait=False):
    """Ukončí vlákno přehrávače (uvolnění relace proběhne v něm, ne v GUI)."""
    entry = _live.get(key)
    if entry is None:
        return
    thread, core = entry
    if thread.isRunning() and not getattr(core, "_disposing", False):
        core._disposing = True
        core.dispose_requested.emit()
    if wait:
        _join(key)


def _join(key):
    entry = _live.pop(key, None)
    if entry is None:
        return
    thread, _core = entry
    if not thread.wait(SHUTDOWN_WAIT_MS):
        thread.quit()
        thread.wait(500)


def reap():
    """Odebere z registru vlákna, která už doběhla (volá se průběžně)."""
    for key, (thread, _core) in list(_live.items()):
        if thread.isFinished():
            _live.pop(key, None)


def live_threads():
    reap()
    return len(_live)


def shutdown_all():
    """Ukončí všechna vlákna přehrávačů (shutdown_app / atexit) – běžící QThread při
    zániku interpretu = qFatal."""
    for key in list(_live):
        _dispose_key(key)
    for key in list(_live):
        _join(key)


atexit.register(shutdown_all)
