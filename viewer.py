from i18n import tr
import os
import webbrowser
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QFrame,
    QInputDialog, QLineEdit, QStyle, QAbstractButton
)
from PyQt6.QtCore import Qt, QUrl, pyqtSignal, QSize, QTimer, QEvent, QPoint, QRect, QRectF, QThreadPool
from PyQt6.QtGui import QPixmap, QImage, QKeyEvent, QPainter, QColor, QPainterPath, QIcon, QFont, QPen
from PyQt6.QtMultimedia import QMediaPlayer
import media_engine
from PyQt6.QtWebEngineWidgets import QWebEngineView
from viewer_hud import (MediaCanvas, GlassPanel, HudIconButton, HudAutoHider, VIDEO_REVEAL_DELAY_MS,
                        load_image_file, ImagePipeline, VideoScrubber, quiet_ffmpeg_logs,
                        quiet_qt_ffmpeg_noise)
import governor
import perf_profile
import scrub_strip
import video_proxy
import viewer_motion
import viewer_zoom
from ui_sheets import InWindowDialog   # Kairos: dotazy jako list v okně
import tokens
from moments_view import czech_day

from preview_cache import get_preview

MEDIA_LOAD_DELAY_MS = 150    # při listování načíst video / Live Photo až po krátké pauze
PRELOAD_DELAY_MS = 400       # Patch 11: sousední video do záložního přehrávače až po ustálení
PAUSED_PRELOAD_DELAY_MS = 2500  # po pauze až po delším klidu (pauza ≠ konec práce s videem)
SEEK_COALESCE_MS = 60        # rychlé skoky ±10 s se sloučí do jednoho přesunu
STRIP_START_DELAY_MS = 700   # Patch 14: náhledový pás až po rozjezdu přehrávání
STRIP_TOLERANCE_MS = 1200    # snímek pásu dál než tohle od cíle = pás tam ještě není (tvoří se)
SCRUB_REVEAL_TIMEOUT_MS = 600  # po puštění: přesný snímek nepřišel -> odkrýt video i tak
from database import get_connection, toggle_favorite
from paths import RESOURCE_DIR, resolve_face_path
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage

MINI_MAP_PAGE = "mini_map.html"          # vedle map_vault.html v RESOURCE_DIR

def _iso_date(dt):
    """EXIF '2025:06:14 10:22:01' i ISO '2025-06-14 10:22' -> '2025-06-14'."""
    s = str(dt or "")
    return s[:10].replace(":", "-") if len(s) >= 10 else ""


def _clock(dt):
    s = str(dt or "")
    return s[11:16] if len(s) >= 16 and s[13] == ":" else ""


def _fnum(value):
    try:
        return f"ƒ/{float(value):g}" if value else ""
    except (TypeError, ValueError):
        return f"ƒ/{value}"


def _exposure(value):
    """0.004 -> '1/250 s', '1/250' -> '1/250 s', 2 -> '2 s'."""
    if not value:
        return ""
    s = str(value).strip()
    if "/" in s:
        return s if s.endswith("s") else f"{s} s"
    try:
        x = float(s)
    except ValueError:
        return s
    return f"1/{round(1 / x)} s" if 0 < x < 1 else f"{x:g} s"


def _caption(text):
    t = tokens.palette("dark")
    lbl = QLabel(text.upper())
    lbl.setStyleSheet(f"font-size: 10.5px; font-weight: 600; letter-spacing: 0.6px; color: {t['text_tertiary']}; "
                      f"background: transparent; margin-top: 12px;")
    return lbl


def _section(title):
    box = QWidget()
    box.setStyleSheet("background: transparent;")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(3)
    cap = _caption(title)
    cap.setStyleSheet(cap.styleSheet().replace("margin-top: 12px;", "margin-bottom: 3px;"))
    lay.addWidget(cap)
    return box


def _value(text, strong=False, muted=False):
    t = tokens.palette("dark")
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    color = t["text_tertiary"] if muted else t["text"]
    weight = 600 if strong else 400
    lbl.setStyleSheet(f"font-size: 13px; font-weight: {weight}; color: {color}; background: transparent;")
    return lbl


def _chip_row(values):
    t = tokens.palette("dark")
    row = QWidget()
    row.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 6, 0, 0)
    lay.setSpacing(6)
    for v in values:
        chip = QLabel(v)
        chip.setStyleSheet(f"font-size: 12px; font-weight: 500; color: {t['text']}; background: {t['surface']}; "
                           f"border: 1px solid {t['hairline_strong']}; border-radius: 7px; padding: 3px 8px;")
        lay.addWidget(chip)
    lay.addStretch()
    return row


try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass
import raw_decode  # noqa: E402  ProRAW / camera RAW through every Image.open (raw_decode.py)
raw_decode.register()


def load_hi_res_pixmap(file_path):
    """Plné rozlišení se správnou orientací (HEIC/DNG přes Pillow, JPEG s EXIF otočením)."""
    img = load_image_file(file_path)
    return QPixmap.fromImage(img) if not img.isNull() else QPixmap()


class ClickableSlider(QSlider):
    """Umožňuje okamžitý skok na časové ose kliknutím kamkoliv na lištu."""
    slider_clicked = pyqtSignal(int)
    mouse_released = pyqtSignal(int)      # vždy po puštění tlačítka (i když se táhlo mimo úchyt)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            val = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(event.position().x()), self.width()
            )
            self.setValue(val)
            self.slider_clicked.emit(val)
            event.accept()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_released.emit(self.value())


class FaceChip(QAbstractButton):
    """Founder audit: osoba v panelu Info jako moderní štítek – avatar 28 px v kroužku, jméno
    vedle (13 px, oříznuté), zaoblené pozadí, při najetí jemné zvýraznění a prstenec zvýraznění.
    Neznámá osoba: čárkovaný obrys a „+“, ať je jasné, že se dá pojmenovat."""
    H = 34
    AV = 26
    MAX_W = 196

    def __init__(self, crop_path=None, name=None, parent=None):
        super().__init__(parent)
        self.person_name = name or ""
        self.known = bool(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(name if name else tr("viewer-unknown-person-tooltip"))
        self.setAccessibleName(name if name else tr("viewer-unknown-person-tooltip"))
        self._hover = 0.0
        self._kbd_focus = False
        self._avatar = self._make_avatar(crop_path)
        self._label = name or tr("viewer-unknown")
        f = self.font()
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold if self.known else QFont.Weight.Normal)
        self.setFont(f)
        fm = self.fontMetrics()
        room = self.MAX_W - (4 + self.AV + 8 + 12)
        self._text = fm.elidedText(self._label, Qt.TextElideMode.ElideRight, room)
        self.setFixedSize(4 + self.AV + 8 + fm.horizontalAdvance(self._text) + 12, self.H)

    def _make_avatar(self, crop_path):
        d = self.AV * 2                               # HiDPI: dvojnásobné rozlišení
        if not crop_path:
            return None
        src = QImage(crop_path)
        if src.isNull():
            return None
        import imaging                                # Lanczos3: ostrý čip i ze 384 px ořezu
        side = min(src.width(), src.height())
        src = src.copy((src.width() - side) // 2, (src.height() - side) // 2, side, side)
        pix = QPixmap.fromImage(imaging.scale_qimage(src, d, d))
        out = QPixmap(d, d)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(0, 0, d, d)
        p.setClipPath(clip)
        p.drawPixmap((d - pix.width()) // 2, (d - pix.height()) // 2, pix)
        p.end()
        out.setDevicePixelRatio(2.0)
        return out

    def enterEvent(self, e):
        self._hover = 1.0
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = 0.0
        self.update()
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self._kbd_focus = e.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._kbd_focus = False
        self.update()
        super().focusOutEvent(e)

    def sizeHint(self):
        return self.size()

    def paintEvent(self, _):
        t = tokens.palette("dark")
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2
        bg = tokens.qcolor(t["surface_2"] if self._hover else t["surface"])
        p.setBrush(bg)
        pen = QPen(tokens.qcolor(t["hairline_strong"]), 1)
        if not self.known:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, radius, radius)
        av = QRectF(4, (self.H - self.AV) / 2, self.AV, self.AV)
        if self._avatar is not None:
            p.drawPixmap(av, self._avatar, QRectF(self._avatar.rect()))
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tokens.qcolor(t["surface_2"]))
            p.drawEllipse(av)
            p.setPen(tokens.qcolor(t["text_secondary"]))
            f = QFont(self.font())
            f.setPixelSize(12)
            f.setWeight(QFont.Weight.DemiBold)
            p.setFont(f)
            glyph = "".join(w[0] for w in self.person_name.split()[:2]).upper() if self.known else "+"
            p.drawText(av, Qt.AlignmentFlag.AlignCenter, glyph)
        if self._hover or self._kbd_focus:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(tokens.qcolor(t["accent"]), 1.6))
            p.drawEllipse(av.adjusted(-1.2, -1.2, 1.2, 1.2))
        p.setFont(self.font())
        p.setPen(tokens.qcolor(t["text"] if self.known else t["text_secondary"]))
        text_r = QRectF(av.right() + 8, 0, self.width() - av.right() - 8 - 10, self.H)
        p.drawText(text_r, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self._text)
        p.end()


class MediaViewerWidget(QWidget):
    web_profile_factory = None                     # instance: () -> trvalý profil mapy (main.create_media_viewer)
    page_setup = None                              # (QWebEnginePage) -> None: úložiště dlaždic (main.attach_tile_store)
    close_requested = pyqtSignal()
    favorite_changed_signal = pyqtSignal(int, int)
    fullscreen_toggled = pyqtSignal(bool)          # hlavní okno přepne celou obrazovku
    slideshow_requested = pyqtSignal(list, int)    # (záznamy, index) -> prezentace

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("omni_fixed_dark", True)   # prohlížeč zůstává tmavý i ve světlém motivu (theme.py)
        self.media_list = []
        self.current_index = 0
        self.is_live_playing = False
        self.is_fullscreen = False
        self._is_video = False
        self._is_live = False
        self._awaiting_first_video_frame = False
        self.playback_speeds = [1.0, 1.5, 2.0, 0.5]
        self.current_speed_idx = 0

        self.setObjectName("MediaViewer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#MediaViewer { background-color: #000000; }
            QWidget { color: #F5F5F7; font-family: -apple-system, 'SF Pro Display', 'Segoe UI', sans-serif; }
            QLabel { background: transparent; }
            QLabel#HudTitle { font-size: 14px; font-weight: 600; color: #F5F5F7; }
            QLabel#HudCounter { font-size: 11px; color: #AEAEB2; }
            QLabel#HudTime { font-size: 12px; color: #D1D1D6; font-family: 'Segoe UI', sans-serif; }
            QPushButton {
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 9px;
                padding: 6px 14px;
                color: #FFFFFF;
                font-weight: 600;
                font-size: 13px;
            }
            QPushButton:hover { background-color: rgba(255, 255, 255, 0.16); border-color: rgba(255, 255, 255, 0.25); }
            QPushButton#HudBack, QPushButton#HudSpeed {
                background: transparent; border: none; border-radius: 15px; padding: 4px 10px;
                color: #FFFFFF; font-size: 13px; font-weight: 600;
            }
            QPushButton#HudBack:hover, QPushButton#HudSpeed:hover { background: rgba(255, 255, 255, 0.12); }
            QSlider { background: transparent; }
            QSlider::groove:horizontal { height: 4px; background: rgba(255, 255, 255, 0.22); border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #FFFFFF; border-radius: 2px; }
            QSlider::handle:horizontal {
                background: #FFFFFF; border: none; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
            }
        """)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # 1. PLÁTNO: fotka i video v jedné scéně (QGraphicsVideoItem, žádný nativní QVideoWidget)
        self.canvas = MediaCanvas(self)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.canvas.double_clicked.connect(self.toggle_fullscreen)
        self.image_view = self.canvas          # původní název
        self.main_layout.addWidget(self.canvas, 1)
        # Polish P5: stažení fotky dolů = zavření do dlaždice; hero otevření z mřížky
        self.transition_provider = lambda media_id: (None, None, 0.0)   # hlavní okno: snímek mřížky + cíl
        self.dismiss = viewer_motion.DismissController(self, self.canvas.viewport())

        # Boční Info panel
        self.info_wrap = QWidget()
        wrap_layout = QVBoxLayout(self.info_wrap)
        wrap_layout.setContentsMargins(0, 12, 12, 12)
        self.info_panel = QFrame()
        self.info_panel.setFixedWidth(340)
        self.info_panel.setStyleSheet(f"""
            QFrame#infoFrame {{
                background-color: {tokens.palette("dark")["bg_sunken"]};
                border: 1px solid {tokens.palette("dark")["hairline_strong"]};
                border-radius: {tokens.RADIUS["lg"]}px;
            }}
        """)
        self.info_panel.setObjectName("infoFrame")
        wrap_layout.addWidget(self.info_panel)
        self.info_layout = QVBoxLayout(self.info_panel)
        self.info_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.info_layout.setContentsMargins(18, 18, 18, 18)
        self.info_layout.setSpacing(6)

        # Kairos dávka 4: datum jako titulek, pod ním čas a soubor; oddíly s malým nadpisem
        t = tokens.palette("dark")
        self.lbl_info_hdr = QLabel("")
        self.lbl_info_hdr.setWordWrap(True)
        self.lbl_info_hdr.setStyleSheet(f"font-family: '{tokens.display_family()}'; font-size: 17px; "
                                        f"font-weight: 600; color: {t['text']}; background: transparent;")
        self.info_layout.addWidget(self.lbl_info_hdr)
        self.lbl_info_sub = QLabel("")
        self.lbl_info_sub.setWordWrap(True)
        self.lbl_info_sub.setStyleSheet(f"font-size: 12px; color: {t['text_secondary']}; background: transparent;")
        self.info_layout.addWidget(self.lbl_info_sub)

        self.meta_container = QWidget()
        self.meta_container.setStyleSheet("background: transparent;")
        self.meta_layout = QVBoxLayout(self.meta_container)
        self.meta_layout.setContentsMargins(0, 6, 0, 0)
        self.meta_layout.setSpacing(14)
        self.info_layout.addWidget(self.meta_container)

        self.lbl_faces_hdr = _caption(tr("nav-people"))
        self.info_layout.addWidget(self.lbl_faces_hdr)

        self.faces_container = QWidget()
        self.faces_container.setStyleSheet("background: transparent;")
        # Founder audit: štítky osob (avatar + jméno) se zalamují – žádná přeplněná řada kroužků
        from ui_controls import FlowLayout
        self.faces_layout = FlowLayout(self.faces_container, spacing=6, v_spacing=6)
        self.faces_layout.setContentsMargins(0, 4, 0, 4)
        self.info_layout.addWidget(self.faces_container)

        self.lbl_map_hdr = _caption(tr("viewer-place-taken"))
        self.info_layout.addWidget(self.lbl_map_hdr)

        # Minimapa se vytvoří až při prvním zobrazení polohy v panelu Info (_ensure_mini_map):
        # stránka QWebEngine stojí ~1 s v GUI vlákně a většina prohlížení panel Info neotevře.
        self.mini_map = None
        self._mini_map_ready = False
        self._mini_place = None              # (lat, lon) čekající na načtení stránky minimapy
        self._mini_map_index = self.info_layout.count()

        self.btn_gmap = QPushButton(tr("viewer-open-google-maps"))
        self.btn_gmap.clicked.connect(self.open_google_maps)
        self.info_layout.addWidget(self.btn_gmap)

        self.main_layout.addWidget(self.info_wrap)
        self.info_wrap.setVisible(False)

        # 2. PŘEHRÁVAČ – výstup do položky scény
        # Patch 11: dva přehrávače, když plátno nabízí dva výstupy. Neaktivní drží předem
        # načtené sousední video (setSource + pause = dekodér běží, 1. snímek je hotový),
        # takže start po listování / otevření z mřížky je jen prohození vrstev.
        # Video engine: každý přehrávač žije ve vlastním vlákně (media_engine) – otevření,
        # ukončení relace i stavba dekodéru už neblokují GUI; play/pause se vrací hned.
        outputs = self.canvas.video_outputs or [None]
        self._players, self._audios = [], []
        for out in outputs:
            player = media_engine.ThreadedPlayer(self, volume=0.8)
            player.setVideoOutput(out)          # QML VideoOutput (GPU) / záložně QGraphicsVideoItem
            self._players.append(player)
            self._audios.append(player.audio)
        self._active = 0
        self._preloaded_path = None
        self._preload_ready = False
        self._last_step = 1
        self._preload_timer = QTimer(self)        # sousední video až po ustálení listování
        self._preload_timer.setSingleShot(True)
        self._preload_timer.setInterval(PRELOAD_DELAY_MS)
        self._preload_timer.timeout.connect(self._preload_neighbor)
        # Skoky ±10 s / klávesy: rychlé stisky se slučují do jednoho přesunu dekodéru (každý
        # setPosition u 4K HEVC dekóduje od klíčového snímku – deset za sebou = sekundy zamrznutí)
        self._seek_target = None
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(SEEK_COALESCE_MS)
        self._seek_timer.timeout.connect(self._flush_seek)
        # Patch 12: plynulé tažení časové osy + tichá konzole (FFmpeg varování u Dolby Vision)
        # Patch 14: při tažení náhledy z pásu (okamžitě), přesný 4K snímek až kurzor zastaví
        self._scrubber = VideoScrubber(lambda: self.player, lambda: self.audio_output, self,
                                       preview_fn=self._show_strip_frame)
        self._strips = scrub_strip.manager()
        self._strip_timer = QTimer(self)          # pás až po rozjezdu videa (nebrzdit 1. snímek)
        self._strip_timer.setSingleShot(True)
        self._strip_timer.setInterval(STRIP_START_DELAY_MS)
        self._strip_timer.timeout.connect(lambda: self._request_strip(priority=False))
        self._reveal_after_scrub = False
        quiet_ffmpeg_logs()
        quiet_qt_ffmpeg_noise()
        # Odkrýt video až s PRVNÍM PLATNÝM SNÍMKEM (do té doby drží obraz plakát / předchozí
        # fotka) – žádné černé bliknutí. Dřívější odhad „pozice >= 55 ms“ zůstává jen záložně.
        self._media_gen = 0
        self._media_gen_pending = -1
        self._current_photo_path = None
        self._media_timer = QTimer(self)          # odložené načtení videa při listování
        self._media_timer.setSingleShot(True)
        self._media_timer.setInterval(MEDIA_LOAD_DELAY_MS)
        self._media_timer.timeout.connect(self._start_media)
        self._images = ImagePipeline(self._display_size, capacity=8, threads=2, parent=self)
        self._images.loaded.connect(self._on_image_loaded)
        # Stage 2: originál v plném rozlišení pro přiblížení (jedno vlákno, jen aktuální fotka)
        self._full_pool = QThreadPool(self)
        self._full_pool.setMaxThreadCount(1)
        self.canvas.zoom.full_res_wanted.connect(self._load_full_res)
        self._video_sink = self.player.videoSink()
        # signály neaktivního (přednačítajícího) přehrávače se do UI nepouštějí
        for i, player in enumerate(self._players):
            sink = player.videoSink()
            if sink is not None:
                sink.videoFrameChanged.connect(lambda f, i=i: self._on_sink_frame(i, f))
            player.positionChanged.connect(lambda v, i=i: i == self._active and self.on_position_changed(v))
            player.durationChanged.connect(lambda v, i=i: i == self._active and self.on_duration_changed(v))
            player.mediaStatusChanged.connect(lambda s, i=i: i == self._active and self.on_media_status_changed(s))
            player.playbackStateChanged.connect(lambda s, i=i: i == self._active and self._on_playback_state(s))

        self._build_hud()
        self.current_lat = None
        self.current_lon = None

    # ── Plovoucí skleněný HUD ─────────────────────────────────────────────────

    def _build_hud(self):
        backdrop = self.canvas          # sklo se počítá ze scény (MediaCanvas.snapshot)

        # Horní kapsle: zpět, název, akce
        self.top_hud = GlassPanel(self.canvas, backdrop, radius=20)
        top = QHBoxLayout(self.top_hud)
        top.setSpacing(6)
        top.setContentsMargins(GlassPanel.SHADOW + 6, GlassPanel.SHADOW + 4,
                               GlassPanel.SHADOW + 8, GlassPanel.SHADOW + 4)
        self.btn_back = QPushButton(tr("nav-back"))
        self.btn_back.setObjectName("HudBack")
        self.btn_back.setToolTip(tr("nav-back-tooltip"))
        self.btn_back.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.clicked.connect(self.request_close)
        top.addWidget(self.btn_back)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("HudTitle")
        self.lbl_counter = QLabel()
        self.lbl_counter.setObjectName("HudCounter")
        titles.addWidget(self.lbl_title)
        titles.addWidget(self.lbl_counter)
        top.addLayout(titles)
        top.addStretch(1)
        self.btn_fav = HudIconButton("heart", tr("viewer-favorite-tooltip"))
        self.btn_fav.clicked.connect(self.handle_toggle_favorite)
        self.btn_live = HudIconButton("live", tr("viewer-play-live-tooltip"))
        self.btn_live.setCheckable(True)
        self.btn_live.clicked.connect(self.toggle_live_playback)
        self.btn_slideshow = HudIconButton("slideshow", tr("viewer-slideshow-tooltip"))
        self.btn_slideshow.clicked.connect(self.start_slideshow)
        self.btn_info = HudIconButton("info", tr("viewer-info-tooltip"))
        self.btn_info.setCheckable(True)
        self.btn_info.clicked.connect(self.toggle_info_panel)
        self.btn_fullscreen = HudIconButton("fullscreen", tr("viewer-fullscreen-tooltip"))
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        for b in (self.btn_fav, self.btn_live, self.btn_slideshow, self.btn_info, self.btn_fullscreen):
            top.addWidget(b)

        # Spodní kapsle videa: čas, posuvník, přehrávání, hlasitost, rychlost
        self.scrubber_panel = GlassPanel(self.canvas, backdrop, radius=22)
        hud_vbox = QVBoxLayout(self.scrubber_panel)
        hud_vbox.setContentsMargins(GlassPanel.SHADOW + 16, GlassPanel.SHADOW + 8,
                                    GlassPanel.SHADOW + 16, GlassPanel.SHADOW + 8)
        hud_vbox.setSpacing(4)
        slider_row = QHBoxLayout()
        slider_row.setSpacing(10)
        self.lbl_time_cur = QLabel("00:00")
        self.lbl_time_cur.setObjectName("HudTime")
        self.lbl_time_cur.setFixedWidth(44)
        self.lbl_time_cur.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.timeline_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.timeline_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Patch 12: tažení přes VideoScrubber (omezené přesuny, ztlumeno, přesně až po puštění)
        self.timeline_slider.sliderMoved.connect(self.scrub_to)
        self.timeline_slider.slider_clicked.connect(self.scrub_to)
        self.timeline_slider.mouse_released.connect(self.end_scrub)
        self.lbl_time_total = QLabel("00:00")
        self.lbl_time_total.setObjectName("HudTime")
        self.lbl_time_total.setFixedWidth(44)
        slider_row.addWidget(self.lbl_time_cur)
        slider_row.addWidget(self.timeline_slider, 1)
        slider_row.addWidget(self.lbl_time_total)
        hud_vbox.addLayout(slider_row)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(6)
        self.btn_mute = HudIconButton("volume", tr("viewer-mute-tooltip"))
        self.btn_mute.clicked.connect(self.toggle_mute)
        self.volume_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(84)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.valueChanged.connect(self.on_volume_changed)
        self.volume_slider.slider_clicked.connect(self.on_volume_changed)
        self.btn_skip_back = HudIconButton("skip_back", tr("viewer-back-10"))
        self.btn_skip_back.clicked.connect(lambda: self.skip_seconds(-10000))
        self.btn_play_pause = HudIconButton("pause", tr("viewer-play-pause"), size=44)
        self.btn_play_pause.clicked.connect(self.toggle_play_pause)
        self.btn_skip_fwd = HudIconButton("skip_fwd", tr("viewer-forward-10"))
        self.btn_skip_fwd.clicked.connect(lambda: self.skip_seconds(10000))
        self.btn_speed = QPushButton("1.0×")
        self.btn_speed.setObjectName("HudSpeed")
        self.btn_speed.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_speed.clicked.connect(self.cycle_playback_speed)
        self.btn_video_fullscreen = HudIconButton("fullscreen", tr("viewer-fullscreen-f11"))
        self.btn_video_fullscreen.clicked.connect(self.toggle_fullscreen)
        controls_row.addWidget(self.btn_mute)
        controls_row.addWidget(self.volume_slider)
        controls_row.addStretch(1)
        controls_row.addWidget(self.btn_skip_back)
        controls_row.addWidget(self.btn_play_pause)
        controls_row.addWidget(self.btn_skip_fwd)
        controls_row.addStretch(1)
        controls_row.addWidget(self.btn_speed)
        controls_row.addWidget(self.btn_video_fullscreen)
        hud_vbox.addLayout(controls_row)

        # Šipky předchozí / další na okrajích
        self.prev_hud = GlassPanel(self.canvas, backdrop, radius=22)
        self.next_hud = GlassPanel(self.canvas, backdrop, radius=22)
        self.btn_prev = HudIconButton("prev", tr("viewer-previous"), size=44)
        self.btn_next = HudIconButton("next", tr("viewer-next"), size=44)
        self.btn_prev.clicked.connect(self.show_prev)
        self.btn_next.clicked.connect(self.show_next)
        for panel, btn in ((self.prev_hud, self.btn_prev), (self.next_hud, self.btn_next)):
            lay = QHBoxLayout(panel)
            lay.setContentsMargins(GlassPanel.SHADOW, GlassPanel.SHADOW, GlassPanel.SHADOW, GlassPanel.SHADOW)
            lay.addWidget(btn)

        self.hud_panels = (self.top_hud, self.scrubber_panel, self.prev_hud, self.next_hud)
        self.hud = HudAutoHider(self.canvas.mouse_surfaces(), self._wanted_panels, parent=self)
        self.hud.track(*self.hud_panels)
        self.canvas.installEventFilter(self)       # změna velikosti -> přeskládat panely

        # Sklo se přepočítá jen při změně obrazu (nová fotka, velikost, konec prolínání);
        # během přehrávání videa je HUD čistě průsvitný – žádné snímkování 4× za sekundu
        self._backdrop_debounce = QTimer(self)
        self._backdrop_debounce.setSingleShot(True)
        self._backdrop_debounce.setInterval(30)
        self._backdrop_debounce.timeout.connect(self._refresh_backdrops)
        self.canvas.content_changed.connect(self._backdrop_debounce.start)

    def _wanted_panels(self):
        panels = [self.top_hud]
        if self._is_video:
            panels.append(self.scrubber_panel)
        if len(self.media_list) > 1:
            if self.current_index > 0:
                panels.append(self.prev_hud)
            if self.current_index < len(self.media_list) - 1:
                panels.append(self.next_hud)
        return panels

    def _layout_hud(self):
        cw, ch = self.canvas.width(), self.canvas.height()
        m = 14 - GlassPanel.SHADOW
        top_h = self.top_hud.sizeHint().height()
        self.top_hud.setGeometry(m, m, max(200, cw - 2 * m), top_h)
        sh = self.scrubber_panel.sizeHint()
        sw = min(860, max(sh.width(), cw - 120))
        self.scrubber_panel.setGeometry((cw - sw) // 2, ch - sh.height() - m - 6, sw, sh.height())
        for panel, left in ((self.prev_hud, True), (self.next_hud, False)):
            ps = panel.sizeHint()
            x = m if left else cw - ps.width() - m
            panel.setGeometry(x, (ch - ps.height()) // 2, ps.width(), ps.height())
        for panel in self.hud_panels:
            panel.raise_()
        self._refresh_backdrops()

    def _refresh_backdrops(self):
        for panel in self.hud_panels:
            panel.refresh_backdrop()

    def eventFilter(self, obj, event):
        if obj is self.canvas and event.type() == QEvent.Type.Resize:
            self._layout_hud()
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._layout_hud()
        self.hud.enabled = True
        self.hud.poke()

    def hideEvent(self, event):
        self.hud.stop()
        self._backdrop_debounce.stop()
        self.dismiss.cancel()                  # rozpracované gesto / přechod nesmí přežít zavření
        super().hideEvent(event)

    # ── Polish P5: fyzický prohlížeč ──────────────────────────────────────────

    def current_media_id(self):
        if not self.media_list or not 0 <= self.current_index < len(self.media_list):
            return None
        return int(self.media_list[self.current_index][0])

    def can_drag_dismiss(self):
        """Stáhnout dolů jde statickou fotku v okně (ne video, pás časové osy, celou obrazovku)."""
        pm = self.canvas.displayed_pixmap()
        return (bool(self.media_list) and not self.is_fullscreen and not self.canvas.is_video_shown()
                and not self.canvas.is_showing_scrub_frame() and not self.canvas.zoom.is_zoomed()
                and pm is not None and not pm.isNull())

    def photo_area(self):
        """Plocha plátna v souřadnicích prohlížeče (sem se fotka vejde při otevření)."""
        vp = self.canvas.viewport()
        if vp.width() <= 0 or vp.height() <= 0:
            return QRectF()
        return QRectF(QRect(vp.mapTo(self, QPoint(0, 0)), vp.size()))

    def photo_rect(self):
        """Kde je fotka právě vidět (souřadnice prohlížeče)."""
        item = self.canvas.photo_item
        pm = item.pixmap()
        if pm.isNull():
            return QRectF()
        size = pm.deviceIndependentSize() * item.scale()
        top_left = self.canvas.viewport().mapTo(self, item.pos().toPoint())
        return QRectF(top_left.x(), top_left.y(), size.width(), size.height())

    def play_hero_open(self, image, from_rect, radius, backdrop):
        return self.dismiss.play_open(image, from_rect, radius, backdrop)

    def _sync_play_icon(self, *_):
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play_pause.set_icon("pause" if playing else "play")
        # Sklo jen nad statickým obrazem; nad videem (i pozastaveným) čistě průsvitná kapsle
        frosted = not playing and not self.canvas.is_video_shown()
        for panel in self.hud_panels:
            panel.set_frosted(frosted)
        if frosted:
            self._backdrop_debounce.start()     # pauza = jeden snímek skla z aktuálního obrazu

    def _update_fav_icon(self, is_fav):
        self.btn_fav.set_icon("heart_fill" if int(is_fav or 0) == 1 else "heart")
        self.btn_fav.setToolTip(tr("viewer-unfavorite-tooltip") if int(is_fav or 0) == 1 else tr("viewer-favorite-tooltip"))

    # ── Celá obrazovka a prezentace ───────────────────────────────────────────

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.is_fullscreen)

    def set_fullscreen(self, on: bool):
        on = bool(on)
        if on == self.is_fullscreen:
            return
        self.is_fullscreen = on
        for b in (self.btn_fullscreen, self.btn_video_fullscreen):
            b.set_icon("fullscreen_exit" if on else "fullscreen")
        if self.receivers(self.fullscreen_toggled) > 0:
            self.fullscreen_toggled.emit(on)       # vložený v hlavním okně
        elif self.isWindow():
            self.showFullScreen() if on else self.showNormal()

    def start_slideshow(self):
        if not self.media_list:
            return
        self.cleanup_playback()
        self.slideshow_requested.emit(list(self.media_list), int(self.current_index))

    def set_media_data(self, media_list, start_index=0):
        self.media_list = media_list
        self.current_index = start_index
        self.load_current_item()

    def forget_media(self):
        """Stage 3 (zamčení Skrytých): zapomenout seznam i všechny dekódované snímky v paměti."""
        self.cleanup_playback()
        self.media_list = []
        self.current_index = 0
        self._current_photo_path = None
        self._images._cache.clear()
        self._full_pool.clear()
        self.canvas.clear_image()           # i pyramida originálu (zoom.set_photo)

    def request_close(self):
        self.canvas.zoom.reset(animate=False)       # zavření (hero do dlaždice) z vešlé fotky
        self.cleanup_playback()
        self.close_requested.emit()

    def _load_full_res(self):
        """Přiblížení fotky: originál v plném rozlišení na pozadí (pyramida), pak ostré detaily."""
        zoom = self.canvas.zoom
        path = getattr(zoom, "path", None)
        if not path or not os.path.exists(path):
            return
        job = viewer_zoom.FullResJob(path, zoom.token)
        job.signals.done.connect(lambda p, token, levels: zoom.set_full_levels(token, levels))
        self._full_pool.clear()                     # čekající originály dřívějších fotek už nejsou potřeba
        self._full_pool.start(job)

    def closeEvent(self, event):
        # Zavření křížkem okna nesmí nechat hrát zvuk videa / Live Photo
        self.cleanup_playback()
        super().closeEvent(event)

    # ── Patch 11: aktivní přehrávač + předem načtené video ────────────────────

    @property
    def player(self):
        return self._players[self._active]

    @property
    def audio_output(self):
        return self._audios[self._active]

    def can_preload(self):
        return len(self._players) > 1

    def _on_sink_frame(self, index, frame):
        if index == self._active:
            if self._scrubber.active and frame.isValid():
                self._scrubber.frame_ready()  # dekodér dohnal cíl -> může se poslat další
                if self._scrubber.show_live:  # přesný snímek po zastavení kurzoru (ne zastaralý)
                    self.canvas.show_video(True)
            elif self._reveal_after_scrub and frame.isValid():
                self._finish_scrub_reveal()   # přesný snímek po puštění nahradí náhled z pásu
            self._on_video_frame(frame)
        elif frame.isValid() and self._preloaded_path:
            self._preload_ready = True

    def _on_playback_state(self, state):
        self._sync_play_icon()
        # přehrávání = práce na pozadí stojí (démon, pás, proxy) – video má celé GPU i CPU
        governor.get().set_active("video", state == QMediaPlayer.PlaybackState.PlayingState)
        # Během přehrávání se nepřednačítá (výměna zdroje stojí ~100 ms+ GUI vlákna = trhnutí).
        # Pauza není ustálení: uživatel často hned pokračuje nebo táhne časovou osu (scrubber sám
        # pozastavuje) – dřív setSource souseda padl 400 ms po pauze přímo doprostřed tažení /
        # dalšího stisku mezerníku a GUI zamrzlo. Po pauze až po delším klidu, při tažení nikdy.
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._preload_timer.stop()
        elif state == QMediaPlayer.PlaybackState.PausedState:
            if not self._scrubber.active:
                self._schedule_preload(PAUSED_PRELOAD_DELAY_MS)
        else:
            self._schedule_preload()

    def _schedule_preload(self, delay_ms=PRELOAD_DELAY_MS):
        if self.can_preload() and self.media_list:
            self._preload_timer.start(delay_ms)

    def _preload_blocked(self):
        """Uživatel s videem právě pracuje -> výměna zdroje souseda by zasekla GUI."""
        return (self._scrubber.active or self._seek_timer.isActive()
                or self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)

    def _preload_neighbor(self):
        """Sousední video ve směru listování (pak opačném) do neaktivního přehrávače."""
        if not self.isVisible():
            return                              # zavřený prohlížeč nic nepřednačítá
        if self._preload_blocked():
            return                              # znovu ho naplánuje další pauza / zastavení
        for step in (self._last_step, -self._last_step):
            i = self.current_index + step
            if 0 <= i < len(self.media_list):
                rec = self.media_list[i]
                if rec[3] == "video" and rec[2] and os.path.exists(rec[2]):
                    self.preload(rec[2])
                    return

    def preload(self, path):
        """Načte video do neaktivního přehrávače a pozastaví na 1. snímku. Vrací True, když
        je (nebo už bylo) načteno. Nikdy během přehrávání – výměna zdroje by trhla videem."""
        if not self.can_preload() or not path:
            return False
        if path == self._preloaded_path:
            return True
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            return False
        source = self._playback_path(path)
        if self.player.source().toLocalFile() and os.path.normcase(
                self.player.source().toLocalFile()) == os.path.normcase(source):
            return False                       # už je v aktivním přehrávači
        spare = self._players[1 - self._active]
        self._audios[1 - self._active].setMuted(True)   # zvuk až po prohození
        self._preloaded_path = path
        self._preload_ready = False
        spare.setSource(QUrl.fromLocalFile(source))
        spare.pause()
        return True

    def _swap_to_preloaded(self):
        """Prohodí aktivní přehrávač za ten s předem načteným videem (bez setSource)."""
        old = self.player
        old.stop()
        muted, volume = self.audio_output.isMuted(), self.audio_output.volume()
        self._active = 1 - self._active
        self.canvas.set_active_output(self._active)
        self._video_sink = self.player.videoSink()
        self.audio_output.setVolume(volume)
        self.audio_output.setMuted(muted)
        self._preloaded_path = None
        ready, self._preload_ready = self._preload_ready, False
        # jeho durationChanged přišel, když byl neaktivní (UI ho zahodilo) -> osa ze skutečného stavu
        self._sync_timeline()
        return ready

    def _on_video_frame(self, frame):
        if not self._awaiting_first_video_frame or not frame.isValid():
            return
        self._awaiting_first_video_frame = False
        gen = self._media_gen
        # krátce počkat, až zakrytý QQuickWidget snímek vykreslí, pak jen prohodit vrstvy
        QTimer.singleShot(VIDEO_REVEAL_DELAY_MS, lambda: self._reveal_video(gen))

    def _reveal_video(self, gen):
        if gen == self._media_gen and (self._is_video or self.is_live_playing):
            self.canvas.show_video(True)

    def cleanup_playback(self):
        """Úplné uvolnění přehrávače (odchod z prohlížeče, zavření okna)."""
        self._halt_playback()                # zpožděná odkrytí / načtení se zahodí
        self._preload_timer.stop()
        self._preloaded_path = None
        self._preload_ready = False
        for player in self._players:
            player.setSource(QUrl())

    def open_google_maps(self):
        if self.current_lat is not None and self.current_lon is not None:
            webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={self.current_lat},{self.current_lon}")

    def toggle_info_panel(self):
        is_visible = self.btn_info.isChecked()
        self.info_wrap.setVisible(is_visible)
        if is_visible and self.media_list:
            item = self.media_list[self.current_index]
            self.update_info_data(int(item[0]))

    def handle_toggle_favorite(self):
        if not self.media_list:
            return
        item = self.media_list[self.current_index]
        media_id = int(item[0])
        new_fav = toggle_favorite(media_id)
        if len(item) > 7:
            # Vlastní seznam musí znát nový stav, jinak po návratu šipkou ukáže staré srdíčko
            self.media_list[self.current_index] = tuple(item[:7]) + (new_fav,) + tuple(item[8:])
        self._update_fav_icon(new_fav)
        self.favorite_changed_signal.emit(media_id, new_fav)

    def update_info_data(self, item_id):
        while self.meta_layout.count():
            child = self.meta_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                while child.layout().count():
                    sub = child.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        while self.faces_layout.count():
            child = self.faces_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        conn = get_connection()
        c = conn.cursor()
        c.execute("""
            SELECT m.file_path, m.media_type, m.date_taken,
                   m.latitude, m.longitude, m.altitude,
                   m.place_city, m.place_country,
                   m.device_make, m.device_model, m.lens_model,
                   m.f_number, m.exposure_time, m.iso, m.focal_length_35mm,
                   e.title
            FROM media_items m
            LEFT JOIN events e ON m.event_id = e.id
            WHERE m.id = ?
        """, (item_id,))
        row = c.fetchone()

        c.execute("""
            SELECT f.id, f.face_crop_path, p.id, p.name 
            FROM face_instances f
            LEFT JOIN people p ON f.person_id = p.id
            WHERE f.media_id = ? AND COALESCE(f.is_valid, 1) = 1
        """, (item_id,))
        faces = c.fetchall()
        conn.close()

        if not row:
            return

        (path, m_type, dt, lat, lon, alt,
         city, country, make, model, lens, fnum, exp, iso_val, focal35,
         event_title) = row

        self.current_lat = lat
        self.current_lon = lon

        when = czech_day(_iso_date(dt))
        clock = _clock(dt)
        self.lbl_info_hdr.setText(when or tr("viewer-date-unknown"))
        self.lbl_info_sub.setText(" · ".join(filter(None, [clock, os.path.basename(path or "")])))

        camera = " ".join(x for x in (make or "", model or "") if x).strip()
        if make and model and str(model).lower().startswith(str(make).lower()):
            camera = str(model)                       # „Apple iPhone…“ místo „Apple Apple iPhone…“
        chips = [c for c in (_fnum(fnum), _exposure(exp), f"ISO {iso_val}" if iso_val else "",
                             f"{int(focal35)} mm" if focal35 else "") if c]
        section = _section(tr("viewer-camera"))
        if camera or lens or chips:
            if camera:
                section.layout().addWidget(_value(camera, strong=True))
            if lens:
                section.layout().addWidget(_value(str(lens), muted=True))
            if chips:
                section.layout().addWidget(_chip_row(chips))
        else:
            section.layout().addWidget(_value(tr("viewer-no-camera"), muted=True))
        self.meta_layout.addWidget(section)

        place = ", ".join(x for x in (city, country) if x)
        section = _section(tr("viewer-place"))
        section.layout().addWidget(_value(place, strong=True) if place
                                   else _value(tr("viewer-location-unknown") if lat is None else tr("viewer-unnamed-place"), muted=True))
        self.meta_layout.addWidget(section)

        if event_title:
            section = _section(tr("viewer-event"))
            # i18n-ok: stored auto event name prefix
            section.layout().addWidget(_value(event_title.replace("Výlet: ", ""), strong=True))
            self.meta_layout.addWidget(section)

        if faces:
            self.lbl_faces_hdr.setVisible(True)
            self.faces_container.setVisible(True)
            for face_id, crop_path, person_id, person_name in faces:
                self.faces_layout.addWidget(self._face_chip(face_id, crop_path, person_id, person_name, item_id))
        else:
            self.lbl_faces_hdr.setVisible(False)
            self.faces_container.setVisible(False)

        if lat is not None and lon is not None:
            self.lbl_map_hdr.setVisible(True)
            self._ensure_mini_map().setVisible(True)
            self.btn_gmap.setVisible(True)
            self._show_mini_place(float(lat), float(lon))
        else:
            self.lbl_map_hdr.setVisible(False)
            if self.mini_map is not None:
                self.mini_map.setVisible(False)
            self.btn_gmap.setVisible(False)

    def _face_chip(self, face_id, crop_path, person_id, person_name, item_id):
        """Štítek osoby: kulatý avatar + jméno v zaobleném „chipu“; klik = přiřadit / přejmenovat."""
        chip = FaceChip(resolve_face_path(crop_path), person_name)
        chip.clicked.connect(lambda checked=False, f_id=face_id, p_id=person_id, p_name=person_name:
                             self.on_face_badge_clicked(f_id, p_id, p_name, item_id))
        return chip

    def _ensure_mini_map(self):
        """Minimapa = mini_map.html (přibalený MapLibre GL, jako hlavní mapa). Načte se jednou;
        další fotky jen posunou pin přes window.showPlace."""
        if self.mini_map is None:
            m = QWebEngineView()
            profile = self.web_profile_factory() if self.web_profile_factory else None
            from web_console import QuietWebPage   # konzole WebGL bez opakovaných hlášení
            # trvalý profil hlavní mapy: sdílená disková cache dlaždic
            m.setPage(QuietWebPage(profile, m, tag="Minimapa") if profile is not None
                      else QuietWebPage(m, tag="Minimapa"))
            m.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            m.setFixedHeight(180)
            # Stránka z file:// (přibalený MapLibre) smí načíst styl a dlaždice povolených serverů
            m.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            m.setStyleSheet("border-radius: 10px; border: 1px solid #2C2C2E; background-color: #E4E8EC;")
            m.page().setBackgroundColor(QColor("#E4E8EC"))      # pozadí Základní – žádný černý záblesk
            if self.page_setup is not None:
                try:
                    self.page_setup(m.page())       # dlaždice přes místní úložiště (jako hlavní mapa)
                except Exception as e:
                    print(f"[Minimapa] úložiště dlaždic: {e}")
            m.loadFinished.connect(self._on_mini_map_loaded)
            m.load(QUrl.fromLocalFile(os.path.join(RESOURCE_DIR, MINI_MAP_PAGE)))
            self.info_layout.insertWidget(self._mini_map_index, m)
            self.mini_map = m
        return self.mini_map

    def _show_mini_place(self, lat, lon):
        self._mini_place = (lat, lon)
        if self._mini_map_ready:
            self.mini_map.page().runJavaScript(f"window.showPlace && window.showPlace({lat!r}, {lon!r});")

    def _on_mini_map_loaded(self, ok):
        self._mini_map_ready = bool(ok)
        if ok and getattr(self, "mini_map", None) is not None:
            import json
            import i18n
            self.mini_map.page().runJavaScript(
                f"window.setStrings && window.setStrings({json.dumps(i18n.page_strings('minimap-'), ensure_ascii=False)},"
                f" {json.dumps(i18n.current())});")
            import motion
            import tokens
            accent = tokens.accent("dark")["accent"]
            self.mini_map.page().runJavaScript(
                f"window.setAccent && window.setAccent({json.dumps(accent)});"
                f" window.setStill && window.setStill({json.dumps(motion.reduced())});")
        if ok and self._mini_place is not None:
            self._show_mini_place(*self._mini_place)

    def on_face_badge_clicked(self, face_id, person_id, current_name, item_id):
        new_name, ok = InWindowDialog.getText(
            self, tr("viewer-assign-person"), tr("viewer-assign-person-body"),
            text=current_name if current_name else "", action_text=tr("viewer-assign"))
        if ok and new_name.strip():
            target_name = new_name.strip()
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT id FROM people WHERE name = ?", (target_name,))
            existing = c.fetchone()
            
            if existing:
                target_person_id = existing[0]
            else:
                c.execute("INSERT INTO people (name) VALUES (?)", (target_name,))
                target_person_id = c.lastrowid
                
            c.execute("UPDATE face_instances SET person_id = ? WHERE id = ?", (target_person_id, face_id))
            conn.commit()
            conn.close()
            import xmp_sync
            xmp_sync.touch_faces([face_id])      # jméno tváře do sidecaru (Sprint 2)
            
            from face_engine import update_person_key_photo
            update_person_key_photo(target_person_id)
            if person_id:
                update_person_key_photo(person_id)
                
            self.update_info_data(item_id)

    def load_current_item(self, defer_media=False):
        """defer_media=True (listování šipkami): obraz se ukáže hned, ale video / Live Photo
        se do přehrávače načte až po krátké pauze. Výměna zdroje přehrávače stojí ~100 ms
        v GUI vlákně (FFmpeg ukončí předchozí relaci) – při rychlém listování se tak neplatí
        za každou položku a nikdy ne uprostřed animace."""
        if not self.media_list:
            return

        self._halt_playback()
        item = self.media_list[self.current_index]

        item_id = item[0]
        name = item[1]
        path = item[2]
        m_type = item[3]
        paired_video = item[4]
        thumb = item[5]
        is_fav = item[7] if len(item) > 7 else 0

        self.lbl_title.setText(name or (os.path.basename(path) if path else ""))
        self.lbl_counter.setText(f"{self.current_index + 1} / {len(self.media_list)}")
        self._update_fav_icon(is_fav)
        self._reset_timeline()                     # osa vlevo a prázdná, i když hraje dál stará (Patch 13)

        if self.btn_info.isChecked():
            self.update_info_data(int(item_id))

        # Stav podle typu položky – NE podle isVisible() panelů (ty se po 2 s samy skrývají)
        self._is_video = bool(m_type == "video" and path and os.path.exists(path))
        self._is_live = bool(not self._is_video and m_type == "live_photo"
                             and paired_video and os.path.exists(paired_video))
        self.btn_live.setVisible(self._is_live)

        self._current_photo_path = None
        # Stage 2: nová položka = vešlá; přiblížit jde jen fotku (rozměr originálu z hlavičky, ~5 ms)
        zoomable = bool(not self._is_video and path and os.path.exists(path))
        self.canvas.zoom.set_photo(path if zoomable else None,
                                   viewer_zoom.original_size(path) if zoomable else None)
        self.canvas.zoom.set_enabled(zoomable)
        if self._is_video:
            # Plakát drží obraz, dokud video nevykreslí 1. snímek -> žádné černé bliknutí.
            # Patch 11: plakát v rozlišení náhledu (2048 px, z preview cache), ne rozmazaná miniatura
            poster = get_preview(path) or (thumb if thumb and os.path.exists(thumb) else None)
            if poster:
                self.canvas.set_image(QPixmap(poster))
            else:
                self.canvas.clear_image()
            self.canvas.show_video(False)
        else:
            self.load_image_to_view(path, thumb)
        self._prefetch_neighbors()

        if self._is_video or self._is_live:
            self._media_gen_pending = self._media_gen
            preloaded = bool(self._is_video and self._preloaded_path and
                             os.path.normcase(self._preloaded_path) == os.path.normcase(path))
            if defer_media and not preloaded:      # předem načtené video nic nestojí -> hned
                self._media_timer.start()
            else:
                self._start_media()
        self._schedule_preload()
        self._sync_play_icon()
        self.hud.sync()
        QTimer.singleShot(0, self._refresh_backdrops)

    def _start_media(self):
        """Načte video / pohybové video Live Photo aktuální položky do přehrávače."""
        if not self.media_list or self._media_gen_pending != self._media_gen:
            return                                   # mezitím jiná položka
        item = self.media_list[self.current_index]
        if self._is_video:
            self._strip_timer.start()                # náhledový pás – odložený, viz _request_strip
            self._awaiting_first_video_frame = True
            if self._preloaded_path and os.path.normcase(self._preloaded_path) == os.path.normcase(item[2]):
                if self._swap_to_preloaded():
                    # 1. snímek už leží v (teď odkrytém) výstupu – odkrýt hned, bez čekání
                    self._awaiting_first_video_frame = False
                    gen = self._media_gen
                    QTimer.singleShot(VIDEO_REVEAL_DELAY_MS, lambda: self._reveal_video(gen))
            else:
                self.player.setSource(QUrl.fromLocalFile(self._playback_path(item[2])))
            self.player.setPlaybackRate(self.playback_speeds[self.current_speed_idx])
            self.player.play()
        elif self._is_live:
            self.player.setSource(QUrl.fromLocalFile(item[4]))
            self.player.setPlaybackRate(1.0)

    @staticmethod
    def _playback_path(path):
        """Co přehrát: originál (HW dekódování), na slabším PC (perf_profile.proxy_playback)
        hotovou 1080p SDR proxy 4K / HDR videa – plynulé přehrávání i bez výkonné grafiky."""
        if path and perf_profile.current().proxy_playback:
            proxy = video_proxy.get_proxy(path)
            if proxy:
                video_proxy.touch(proxy)
                return proxy
        return path

    def _display_size(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return QSize(2560, 1600)
        dpr = screen.devicePixelRatio()
        size = screen.size()
        return QSize(int(size.width() * dpr), int(size.height() * dpr))

    def load_image_to_view(self, path, thumb):
        """Fotka v rozlišení obrazovky: z cache okamžitě, jinak hned náhled a ostrý snímek
        z dekódování na pozadí (HEIC ~300 ms už neblokuje GUI vlákno)."""
        self._current_photo_path = path
        img = self._images.request(path) if path and os.path.exists(path) else None
        if img is not None:
            self.canvas.set_image(QPixmap.fromImage(img))
        elif thumb and os.path.exists(thumb):
            self.canvas.set_image(QPixmap(thumb))
        elif not (path and os.path.exists(path)):
            self.canvas.clear_image()

    def _on_image_loaded(self, path, image):
        if path == self._current_photo_path and not self.canvas.is_video_shown():
            self.canvas.set_image(QPixmap.fromImage(image))

    def _prefetch_neighbors(self):
        """Sousední fotky (←/→) se dekódují dopředu, aby se při listování ukázaly ostré hned."""
        paths = []
        for step in (1, -1, 2):
            i = self.current_index + step
            if 0 <= i < len(self.media_list):
                rec = self.media_list[i]
                if rec[3] != "video" and rec[2]:
                    paths.append(rec[2])
        self._images.prefetch(paths)

    def _halt_playback(self):
        """Rychlé zastavení při přechodu na jinou položku (stop ~0 ms); zdroj se nevyměňuje."""
        self._media_gen += 1
        self._media_timer.stop()
        self._seek_timer.stop()
        self._seek_target = None
        self._scrubber.cancel()
        self._strip_timer.stop()
        self._reveal_after_scrub = False
        self.canvas.end_scrub_frames()
        self.is_live_playing = False
        self._awaiting_first_video_frame = False
        if self.player:
            self.player.stop()
        governor.get().set_active("video", False)
        self.canvas.show_video(False)
        self.btn_live.setChecked(False)


    def toggle_live_playback(self):
        if not self._is_live:
            self.btn_live.setChecked(False)
            return
        if self.is_live_playing:
            self.stop_live_smoothly()
        else:
            self.is_live_playing = True
            self._awaiting_first_video_frame = True
            self.btn_live.setChecked(True)
            self.player.setPosition(0)
            self.player.play()

    def stop_live_smoothly(self):
        self.is_live_playing = False
        self._awaiting_first_video_frame = False
        self.canvas.show_video(False)
        self.btn_live.setChecked(False)
        self.player.pause()

    def toggle_play_pause(self):
        # Jen play()/pause() na stávající relaci – žádný nový zdroj ani pipeline
        self._preload_timer.stop()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self._flush_seek()
            if self.player.duration() > 0 and self.player.position() >= self.player.duration() - 90:
                self.player.setPosition(0)
            self.canvas.show_video(True)
            self.player.play()
        self._sync_play_icon()

    def skip_seconds(self, ms_delta):
        if self.player.duration() <= 0:
            return
        base = self._seek_target if self._seek_target is not None else self.player.position()
        self._seek_target = max(0, min(self.player.duration() - 60, base + ms_delta))
        self._preload_timer.stop()
        self._set_timeline(self._seek_target, self.player.duration())   # osa reaguje hned
        self._seek_timer.start()

    def _flush_seek(self):
        self._seek_timer.stop()
        target, self._seek_target = self._seek_target, None
        if target is not None:
            self.player.setPosition(target)

    def step_frame(self, ms_offset):
        self.player.pause()
        self.canvas.show_video(True)
        new_pos = max(0, min(self.player.duration() - 40, self.player.position() + ms_offset))
        self.player.setPosition(new_pos)
        self._sync_play_icon()

    def set_video_position(self, pos):
        self.canvas.show_video(True)
        self.player.setPosition(pos)

    def scrub_to(self, pos):
        """Tažení / klik na časové ose: náhled z pásu hned, jinak omezené živé přesuny."""
        if not (self._is_video or self.is_live_playing) or self.player.duration() <= 0:
            return                                 # bez známé délky není kam táhnout
        pos = min(max(0, int(pos)), self.player.duration())
        if not self._scrubber.active and self._is_video:
            self._strip_timer.stop()
            self._request_strip()                  # tažení dřív, než se pás začal tvořit
        self._scrubber.move(pos)
        if self._scrubber.show_live:               # bez pásu: snímky z dekodéru (video vrstva)
            self.canvas.show_video(True)
        self.lbl_time_cur.setText(self.format_time(int(pos)))

    def end_scrub(self, pos):
        sent = self._scrubber.end(pos)
        if self.canvas.is_showing_scrub_frame():
            if sent:                               # náhled drží obraz, dokud nepřijde přesný snímek
                self._reveal_after_scrub = True
                gen = self._media_gen
                QTimer.singleShot(SCRUB_REVEAL_TIMEOUT_MS,
                                  lambda: gen == self._media_gen and self._finish_scrub_reveal())
            else:
                self._finish_scrub_reveal()

    # ── Patch 14: náhledový pás ───────────────────────────────────────────────

    def _current_video_path(self):
        if self._is_video and self.media_list:
            return self.media_list[self.current_index][2]
        return None

    def _request_strip(self, priority=True):
        """priority=False (po spuštění videa): pás počká na klid a během přehrávání stojí –
        dřív tu 0,7 s po startu běželo druhé dekódování téhož 4K HDR videa a video se trhalo.
        Tažení časové osy žádá s prioritou (a rozpracovaný pás povýší)."""
        path = self._current_video_path()
        if path:
            self._strips.request(path, priority=priority)

    def _show_strip_frame(self, pos):
        """preview_fn scrubberu: True, když pás má pro pozici snímek a ukázal se."""
        path = self._current_video_path()
        strip = self._strips.get(path) if path else None
        if strip is None or not len(strip):
            return False
        image = strip.frame_near(pos, STRIP_TOLERANCE_MS)
        return image is not None and self.canvas.show_scrub_frame(image)

    def _finish_scrub_reveal(self):
        if not self._reveal_after_scrub and not self.canvas.is_showing_scrub_frame():
            return
        self._reveal_after_scrub = False
        if self._is_video:
            self.canvas.show_video(True)           # nejdřív přesný snímek nahoru…
        self.canvas.end_scrub_frames()             # …pak pod ním vrátit plakát (bez probliknutí)

    def toggle_mute(self):
        muted = not self.audio_output.isMuted()
        self.audio_output.setMuted(muted)
        self.btn_mute.set_icon("mute" if muted else "volume")

    def on_volume_changed(self, val):
        self.audio_output.setVolume(val / 100.0)
        if val == 0:
            self.btn_mute.set_icon("mute")
        else:
            self.audio_output.setMuted(False)
            self.btn_mute.set_icon("volume")

    def cycle_playback_speed(self):
        self.current_speed_idx = (self.current_speed_idx + 1) % len(self.playback_speeds)
        spd = self.playback_speeds[self.current_speed_idx]
        self.player.setPlaybackRate(spd)
        self.btn_speed.setText(f"{spd}×")

    def format_time(self, ms):
        s = max(0, ms // 1000)
        m = s // 60
        s = s % 60
        return f"{m:02d}:{s:02d}"

    def update_time_label(self):
        self._set_timeline(self.player.position(), self.player.duration())

    # ── Patch 13: časová osa vždy podle AKTIVNÍHO přehrávače ──────────────────
    # Dřív se rozsah osy nastavoval jen z durationChanged. Předem načtené video ho ale vyslalo,
    # když byl jeho přehrávač neaktivní (signál se zahodí) – po prohození zůstal rozsah z minula
    # nebo výchozí 0–99 a pozice v ms (např. 3000) se ořízly na maximum: úchyt vpravo a po
    # puštění zpět na konec. Teď se osa při nové položce vynuluje, při prohození přehrávačů
    # srovná podle nového aktivního a hodnoty se vždy ořezávají do 0…délka.

    def _set_timeline(self, pos, dur):
        dur = max(0, int(dur or 0))
        pos = min(max(0, int(pos or 0)), dur)
        slider = self.timeline_slider
        slider.blockSignals(True)
        if slider.maximum() != dur or slider.minimum() != 0:
            slider.setRange(0, dur)
        if not self._scrubber.active:              # při tažení drží úchyt uživatel
            slider.setValue(pos)
            self.lbl_time_cur.setText(self.format_time(pos))
        slider.blockSignals(False)
        self.lbl_time_total.setText(self.format_time(dur))

    def _reset_timeline(self):
        """Nová položka: osa prázdná a vlevo, dokud nový zdroj neohlásí délku."""
        self._set_timeline(0, 0)

    def _sync_timeline(self):
        """Prohození přehrávačů: délka i pozice nového aktivního (už je zná z přednačtení)."""
        self._set_timeline(self.player.position(), self.player.duration())

    def on_position_changed(self, pos):
        # Záloha bez video sinku: odhad prvního vykresleného snímku podle pozice
        if self._awaiting_first_video_frame and self._video_sink is None and pos >= 55:
            self._awaiting_first_video_frame = False
            self.canvas.show_video(True)

        dur = self.player.duration()
        if dur <= 0:
            return                                 # délka ještě neznámá – zbloudilé hlášení
        if dur > 0:
            # U Live Photo přepneme zpět na statickou fotku 75 ms před koncem, aby neproblikla černá
            if self.is_live_playing and pos >= dur - 75:
                self.stop_live_smoothly()
                return
            # U běžného videa zastavíme na posledním snímku (50 ms před koncem), aby obrazovka na konci nezčernala
            elif self._is_video and pos >= dur - 50 and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()

        if self._scrubber.active:
            # positionChanged přijde hned po setPosition (ještě před dekódováním) – za hotový
            # snímek ho bere jen záloha bez video sinku; úchyt mezitím drží uživatel
            if self._video_sink is None:
                self._scrubber.frame_ready()
            return
        if self._seek_target is not None:
            return                                 # sloučený skok čeká – osa už ukazuje cíl
        self._set_timeline(pos, dur)

    def on_duration_changed(self, duration):
        self._set_timeline(self.player.position(), duration)

    def on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.is_live_playing:
                self.stop_live_smoothly()
            self._sync_play_icon()

    def show_prev(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._last_step = -1
            self.load_current_item(defer_media=True)

    def show_next(self):
        if self.current_index < len(self.media_list) - 1:
            self.current_index += 1
            self._last_step = 1
            self.load_current_item(defer_media=True)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()
        video = self._is_video

        if key == Qt.Key.Key_Left:
            if mods & Qt.KeyboardModifier.ShiftModifier and video:
                self.skip_seconds(-10000)
            else:
                self.show_prev()
        elif key == Qt.Key.Key_Right:
            if mods & Qt.KeyboardModifier.ShiftModifier and video:
                self.skip_seconds(10000)
            else:
                self.show_next()
        elif key == Qt.Key.Key_J and video:
            self.skip_seconds(-10000)
        elif key == Qt.Key.Key_L and video:
            self.skip_seconds(10000)
        elif key == Qt.Key.Key_Comma and video:
            self.step_frame(-40)
        elif key == Qt.Key.Key_Period and video:
            self.step_frame(40)
        elif key == Qt.Key.Key_M and video:
            self.toggle_mute()
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal) and self.canvas.zoom.enabled:
            self.canvas.zoom.zoom_by(viewer_zoom.NOTCH_FACTOR ** 2)
        elif key == Qt.Key.Key_Minus and self.canvas.zoom.enabled:
            self.canvas.zoom.zoom_by(viewer_zoom.NOTCH_FACTOR ** -2)
        elif key == Qt.Key.Key_0 and self.canvas.zoom.enabled:
            self.canvas.zoom.reset()
        elif key == Qt.Key.Key_1 and self.canvas.zoom.enabled:
            self.canvas.zoom.zoom_to(self.canvas.zoom.one_to_one_w())
        elif key == Qt.Key.Key_Space:
            if self._is_live:
                self.canvas.zoom.reset(animate=False)
                self.toggle_live_playback()
            elif video:
                self.toggle_play_pause()
        elif key == Qt.Key.Key_F:
            self.handle_toggle_favorite()
        elif key == Qt.Key.Key_S:
            self.start_slideshow()
        elif key == Qt.Key.Key_F11 or (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                                       and mods & Qt.KeyboardModifier.AltModifier):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_I:
            self.btn_info.setChecked(not self.btn_info.isChecked())
            self.toggle_info_panel()
        elif key == Qt.Key.Key_Escape:
            # Esc postupně: přiblížení -> celá obrazovka -> panel Info -> zpět
            if self.canvas.zoom.is_zoomed():
                self.canvas.zoom.reset()
            elif self.is_fullscreen:
                self.set_fullscreen(False)
            elif self.info_panel.isVisible():
                self.btn_info.setChecked(False)
                self.toggle_info_panel()
            else:
                self.request_close()
        else:
            super().keyPressEvent(event)
            return
        event.accept()
