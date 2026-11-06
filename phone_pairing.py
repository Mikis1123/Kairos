"""Pair iPhone sheet (iOS Phase 2): QR code + 6-digit PIN for the Kairos iPhone app.

Opened from Předvolby › Pokročilé › Synchronizace s iPhonem. The LAN bridge (sync_server.py)
issues a single-use PIN that is valid for 120 s and allows 5 tries. The QR holds the full
`kairos://pair?h=&p=&fp=&lib=&pin=&v=` link, so the phone pins the certificate fingerprint
without typing anything. Typing the PIN is the fallback when the camera can't read the code.

States: starting (the bridge binds on a worker thread) -> code (QR, PIN, countdown) ->
paired (a new device appears in the bridge's credentials) | expired | locked (5 wrong PINs)
-> "New code". Closing the sheet cancels a live PIN, so no pairing window stays open.
Nothing here blocks the GUI thread: the bind runs in sync_server.start_async, and the poll is a
500 ms timer that reads in-memory state.
"""
from i18n import tr
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLayout, QVBoxLayout

import qr_code
import sync_server
import tokens
from ui_controls import KButton

POLL_MS = 500
QR_MODULE_PX = 5               # whole pixels: crisp at 100 % and 200 %; v9-10 code = ~330 px
TEXT_WIDTH = 340


def format_pin(pin):
    """'482913' -> '482 913' (easier to read across a desk)."""
    pin = str(pin or "")
    return f"{pin[:3]} {pin[3:]}" if len(pin) == 6 else pin


def address_of(uri):
    """host:port from a kairos://pair link, for the caption under the code."""
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(uri).query)
    host, port = q.get("h", [""])[0], q.get("p", [""])[0]
    return f"{host}:{port}" if host else ""


class PairPhoneDialog(QDialog):
    STARTING, CODE, PAIRED, EXPIRED, LOCKED, FAILED = range(6)

    _ready = pyqtSignal(object)            # start_async's worker -> GUI thread (queued)

    def __init__(self, parent=None, service=sync_server.service, start_async=sync_server.start_async,
                 clock=time.monotonic):
        super().__init__(parent)
        self._service_fn = service
        self._start_async = start_async
        self._clock = clock
        self.srv = None
        self.offer = None
        self.state = None
        self.paired_name = None
        self._expires = 0.0
        self._known = set()
        self._closed = False
        self.setWindowTitle(tr("pair-title"))
        self.setObjectName("pairPhone")
        self.setModal(True)
        tokens.apply_stylesheet(self, "settings")

        lay = QVBoxLayout(self)
        # exactly the size hint: a squeezed layout let the captions overlap the code
        lay.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(10)
        self.lbl_title = QLabel(tr("pair-title"))
        self.lbl_title.setObjectName("sheetTitle")
        f = self.lbl_title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.lbl_title.setFont(f)
        lay.addWidget(self.lbl_title, 0, Qt.AlignmentFlag.AlignHCenter)
        self.lbl_intro = QLabel(tr("pair-instructions"))
        self.lbl_intro.setWordWrap(True)
        self.lbl_intro.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_intro.setFixedWidth(TEXT_WIDTH)
        lay.addWidget(self.lbl_intro, 0, Qt.AlignmentFlag.AlignHCenter)

        # The code sits on white whatever the theme: scanners need dark modules on light.
        self.lbl_qr = QLabel()
        self.lbl_qr.setObjectName("pairQr")
        self.lbl_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_qr.setStyleSheet("background: #FFFFFF; border-radius: 14px;")
        lay.addWidget(self.lbl_qr, 0, Qt.AlignmentFlag.AlignHCenter)

        self.lbl_pin_caption = QLabel(tr("pair-pin-caption"))
        self.lbl_pin_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.lbl_pin_caption)
        self.lbl_pin = QLabel("")
        self.lbl_pin.setObjectName("pairPin")
        self.lbl_pin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pin.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # the settings stylesheet sets QLabel font-size, which beats setFont(): size it in QSS
        self.lbl_pin.setStyleSheet("QLabel#pairPin { font-size: 30px; font-weight: 700; letter-spacing: 4px; }")
        lay.addWidget(self.lbl_pin)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hint")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setFixedWidth(TEXT_WIDTH)
        lay.addWidget(self.lbl_status, 0, Qt.AlignmentFlag.AlignHCenter)
        self.lbl_address = QLabel("")
        self.lbl_address.setObjectName("hint")
        self.lbl_address.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.lbl_address)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_new = KButton(tr("pair-new-code"), compact=True)
        self.btn_new.clicked.connect(self.new_code)
        self.btn_close = KButton(tr("common-done"), compact=True, variant="primary")
        self.btn_close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_new)
        buttons.addWidget(self.btn_close)
        lay.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)
        self._ready.connect(self._on_ready)

        srv = self._service_fn()
        if srv is not None:
            self._on_ready(srv)
        else:
            self._set_state(self.STARTING)
            self._start_async(on_ready=self._ready.emit)

    # ── states ──
    def _set_state(self, state):
        self.state = state
        code = state == self.CODE
        dim = state in (self.EXPIRED, self.LOCKED)
        self.lbl_qr.setVisible(code or dim)
        self.lbl_pin.setVisible(code)
        self.lbl_pin_caption.setVisible(code)
        self.lbl_qr.setEnabled(code)
        self.btn_new.setVisible(dim)
        if dim:                               # an expired code must not look scannable
            self.lbl_qr.setPixmap(QPixmap())
        if state == self.STARTING:
            self.lbl_status.setText(tr("pair-starting"))
        elif state == self.FAILED:
            self.lbl_status.setText(tr("pair-failed", port=str(sync_server.DEFAULT_PORT)))
        elif state == self.EXPIRED:
            self.lbl_status.setText(tr("pair-expired"))
        elif state == self.LOCKED:
            self.lbl_status.setText(tr("pair-locked"))
        elif state == self.PAIRED:
            self.lbl_status.setText(tr("pair-done", device=self.paired_name or tr("pair-device-fallback")))
            self.lbl_qr.setVisible(False)
            self._timer.stop()

    def _on_ready(self, srv):
        if self._closed:                      # the bind finished after the sheet was closed
            return
        self.srv = srv
        if srv is None:
            self._set_state(self.FAILED)
            return
        self.new_code()

    def new_code(self):
        if self.srv is None:
            return
        self._known = {(d.get("id"), d.get("paired_at")) for d in self.srv.credentials.devices}
        self.offer = self.srv.begin_pairing()
        self._expires = self._clock() + float(self.offer.get("expires_s", sync_server.PIN_TTL_S))
        matrix = qr_code.encode(self.offer["uri"])
        img = qr_code.to_qimage(matrix, scale=QR_MODULE_PX)
        self.lbl_qr.setPixmap(QPixmap.fromImage(img))
        self.lbl_qr.setFixedSize(img.width() + 16, img.height() + 16)
        self.lbl_pin.setText(format_pin(self.offer["pin"]))
        self.lbl_address.setText(tr("pair-address", name=getattr(self.srv, "name", ""),
                                    address=address_of(self.offer["uri"])))
        self._set_state(self.CODE)
        self.poll()
        self._timer.start()

    def remaining_s(self):
        return max(0, int(round(self._expires - self._clock())))

    def poll(self):
        if self.srv is None or self.state != self.CODE:
            return
        for d in self.srv.credentials.devices:
            if (d.get("id"), d.get("paired_at")) not in self._known:
                self.paired_name = d.get("name") or tr("pair-device-fallback")
                self._set_state(self.PAIRED)
                return
        left = self.remaining_s()
        if left <= 0:
            self.srv.credentials.cancel_pairing()
            self._set_state(self.EXPIRED)
            self._timer.stop()
            return
        if not self.srv.credentials.pairing_active():  # 5 wrong guesses burnt the PIN
            self._set_state(self.LOCKED)
            self._timer.stop()
            return
        self.lbl_status.setText(tr("pair-expires", seconds=left))

    def done(self, r):
        self._closed = True
        self._timer.stop()
        if self.srv is not None and self.state == self.CODE:
            self.srv.credentials.cancel_pairing()       # no live PIN after the sheet closes
        super().done(r)
