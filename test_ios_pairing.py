"""iOS Phase 2, desktop side: QR encoder, Pair iPhone sheet, Settings › iPhone sync.

The QR encoder is verified by DECODING its output with OpenCV (the same library the app already
ships). A wrong table or placement would fail on every mask, not just on a detector-unfriendly one.
The pairing sheet runs against fakes and against a real SyncServer (plain HTTP on port 0): the
PIN shown in the sheet is posted to /v1/pair, and the sheet must notice the new device.
"""
import http.client
import json
import os
import random
import shutil
import string
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtWidgets import QApplication

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
_app = QApplication.instance() or QApplication(sys.argv)

import config as cfg  # noqa: E402
import database  # noqa: E402
import qr_code  # noqa: E402
import sync_server as ss  # noqa: E402
import phone_pairing as pp  # noqa: E402

try:
    import cv2
except ImportError:                                   # pragma: no cover
    cv2 = None

URI = ("kairos://pair?h=192.168.100.200&p=52790&fp=" + "ab" * 32
       + "&lib=6f1c2b8e-4d5a-4e2f-9b7c-0123456789ab&pin=048213&v=1")


def _matrix_to_gray(matrix, scale):
    n, q = len(matrix), qr_code.QUIET_ZONE
    a = np.full(((n + 2 * q) * scale,) * 2, 255, np.uint8)
    for y, row in enumerate(matrix):
        for x, v in enumerate(row):
            if v:
                a[(y + q) * scale:(y + q + 1) * scale, (x + q) * scale:(x + q + 1) * scale] = 0
    return a


def _decode(gray):
    dets = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        dets.append(cv2.QRCodeDetectorAruco())
    for d in dets:
        text, *_ = d.detectAndDecode(gray)
        if text:
            return text
    return ""


def _decode_matrix(matrix):
    for scale in (5, 3, 8):                            # detectors have favourite module sizes
        text = _decode(_matrix_to_gray(matrix, scale))
        if text:
            return text
    return ""


def _qimage_to_gray(img):
    from PyQt6.QtGui import QImage
    img = img.convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width()].copy()


# ── QR encoder ───────────────────────────────────────────────────────────────

class TestQrEncoder(unittest.TestCase):
    def test_capacity_matches_the_standard(self):
        # ISO/IEC 18004 table 7, byte mode
        self.assertEqual(qr_code.capacity(1, qr_code.ECC_MEDIUM), 14)
        self.assertEqual(qr_code.capacity(1, qr_code.ECC_LOW), 17)
        self.assertEqual(qr_code.capacity(10, qr_code.ECC_MEDIUM), 213)
        self.assertEqual(qr_code.capacity(40, qr_code.ECC_LOW), 2953)
        self.assertEqual(qr_code.capacity(40, qr_code.ECC_HIGH), 1273)

    def test_symbol_size_and_version_choice(self):
        m = qr_code.encode("x" * 14)
        self.assertEqual((len(m), qr_code.version_of(m)), (21, 1))
        self.assertEqual(qr_code.version_of(qr_code.encode("x" * 15)), 2)
        with self.assertRaises(qr_code.DataTooLong):
            qr_code.encode("x" * 3000, qr_code.ECC_LOW)

    def test_finder_patterns_and_timing(self):
        m = qr_code.encode(URI)
        n = len(m)
        for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):
            ring = [m[y0][x0 + i] for i in range(7)]
            self.assertTrue(all(ring))
            self.assertTrue(m[y0 + 3][x0 + 3])
        self.assertEqual([m[6][x] for x in range(8, n - 8)], [x % 2 == 0 for x in range(8, n - 8)])
        self.assertTrue(m[n - 8][8], "dark module")

    @unittest.skipIf(cv2 is None, "OpenCV not installed")
    def test_pairing_link_decodes(self):
        m = qr_code.encode(URI)
        self.assertLessEqual(qr_code.version_of(m), 10, "the pairing QR stays small enough to scan across a desk")
        self.assertEqual(_decode_matrix(m), URI)

    @unittest.skipIf(cv2 is None, "OpenCV not installed")
    def test_every_version_decodes(self):
        rnd = random.Random(18004)
        failures = []
        for ver in range(1, 41):
            ecl = (qr_code.ECC_MEDIUM, qr_code.ECC_LOW, qr_code.ECC_QUARTILE, qr_code.ECC_HIGH)[ver % 4]
            lo = qr_code.capacity(ver - 1, ecl) + 1 if ver > 1 else 1
            text = "".join(rnd.choice(string.ascii_letters + string.digits + ":/?&=.")
                           for _ in range(rnd.randint(lo, qr_code.capacity(ver, ecl))))
            m = qr_code.encode(text, ecl)
            self.assertEqual(qr_code.version_of(m), ver)
            if _decode_matrix(m) != text:
                failures.append((ver, ecl))
        self.assertEqual(failures, [])

    @unittest.skipIf(cv2 is None, "OpenCV not installed")
    def test_utf8_payload(self):
        self.assertEqual(_decode_matrix(qr_code.encode("Kairos · Šumava")), "Kairos · Šumava")

    @unittest.skipIf(cv2 is None, "OpenCV not installed")
    def test_qimage_render_decodes(self):
        img = qr_code.to_qimage(qr_code.encode(URI), scale=6)
        n = len(qr_code.encode(URI))
        self.assertEqual(img.width(), (n + 8) * 6)
        self.assertEqual(_decode(_qimage_to_gray(img)), URI)


# ── Pair iPhone sheet (fakes) ────────────────────────────────────────────────

class FakeCredentials:
    def __init__(self):
        self.devices = []
        self.active = False
        self.cancelled = 0

    def pairing_active(self):
        return self.active

    def cancel_pairing(self):
        self.active = False
        self.cancelled += 1


class FakeServer:
    name = "STUDIO-PC"

    def __init__(self):
        self.credentials = FakeCredentials()
        self.offers = 0

    def begin_pairing(self):
        self.offers += 1
        self.credentials.active = True
        pin = "123456" if self.offers == 1 else "654321"
        return {"pin": pin, "uri": URI.replace("048213", pin), "expires_s": 120}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _pump():
    for _ in range(5):
        QCoreApplication.processEvents()


class TestPairSheet(unittest.TestCase):
    def sheet(self, srv, **kw):
        clock = Clock()
        dlg = pp.PairPhoneDialog(service=lambda: srv, clock=clock, **kw)
        self.addCleanup(dlg.deleteLater)
        return dlg, clock

    def test_helpers(self):
        self.assertEqual(pp.format_pin("048213"), "048 213")
        self.assertEqual(pp.format_pin("12"), "12")
        self.assertEqual(pp.address_of(URI), "192.168.100.200:52790")
        self.assertEqual(pp.address_of("kairos://pair?v=1"), "")

    def test_shows_pin_qr_and_countdown(self):
        srv = FakeServer()
        dlg, clock = self.sheet(srv)
        self.assertEqual(dlg.state, dlg.CODE)
        self.assertEqual(dlg.lbl_pin.text(), "123 456")
        self.assertFalse(dlg.lbl_qr.pixmap().isNull())
        self.assertIn("192.168.100.200:52790", dlg.lbl_address.text())
        self.assertIn("STUDIO-PC", dlg.lbl_address.text())
        clock.t += 30
        dlg.poll()
        self.assertIn("90", dlg.lbl_status.text())

    @unittest.skipIf(cv2 is None, "OpenCV not installed")
    def test_qr_on_screen_is_the_offer(self):
        srv = FakeServer()
        dlg, _ = self.sheet(srv)
        gray = _qimage_to_gray(dlg.lbl_qr.pixmap().toImage())
        self.assertEqual(_decode(gray), URI.replace("048213", "123456"))

    def test_expiry_then_new_code(self):
        srv = FakeServer()
        dlg, clock = self.sheet(srv)
        clock.t += 121
        dlg.poll()
        self.assertEqual(dlg.state, dlg.EXPIRED)
        self.assertTrue(dlg.lbl_qr.pixmap().isNull(), "an expired code must not stay scannable")
        self.assertFalse(dlg.btn_new.isHidden())
        self.assertFalse(srv.credentials.active, "the server-side PIN is cancelled too")
        dlg.new_code()
        self.assertEqual(dlg.state, dlg.CODE)
        self.assertEqual(dlg.lbl_pin.text(), "654 321")
        self.assertTrue(dlg.btn_new.isHidden())

    def test_wrong_pins_lock_the_code(self):
        srv = FakeServer()
        dlg, _ = self.sheet(srv)
        srv.credentials.active = False                 # 5 wrong guesses burnt it
        dlg.poll()
        self.assertEqual(dlg.state, dlg.LOCKED)
        self.assertFalse(dlg.btn_new.isHidden())

    def test_new_device_means_paired(self):
        srv = FakeServer()
        srv.credentials.devices = [{"id": "old", "name": "Old iPhone", "paired_at": "2026-01-01T10:00:00"}]
        dlg, _ = self.sheet(srv)
        dlg.poll()
        self.assertEqual(dlg.state, dlg.CODE, "an already paired device is not a new pairing")
        srv.credentials.devices.append({"id": "new", "name": "Miki's iPhone", "paired_at": "2026-11-06T12:00:00"})
        dlg.poll()
        self.assertEqual(dlg.state, dlg.PAIRED)
        self.assertIn("Miki's iPhone", dlg.lbl_status.text())
        self.assertFalse(dlg._timer.isActive())

    def test_re_pairing_the_same_phone_is_detected(self):
        srv = FakeServer()
        srv.credentials.devices = [{"id": "p1", "name": "iPhone", "paired_at": "2026-01-01T10:00:00"}]
        dlg, _ = self.sheet(srv)
        srv.credentials.devices = [{"id": "p1", "name": "iPhone", "paired_at": "2026-11-06T12:00:00"}]
        dlg.poll()
        self.assertEqual(dlg.state, dlg.PAIRED)

    def test_closing_cancels_a_live_pin(self):
        srv = FakeServer()
        dlg, _ = self.sheet(srv)
        dlg.reject()
        self.assertEqual(srv.credentials.cancelled, 1)
        self.assertFalse(srv.credentials.active)

    def test_starts_the_bridge_when_off(self):
        started = {}
        dlg = pp.PairPhoneDialog(service=lambda: None, start_async=lambda on_ready: started.update(cb=on_ready),
                                 clock=Clock())
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg.state, dlg.STARTING)
        srv = FakeServer()
        t = threading.Thread(target=started["cb"], args=(srv,))   # start_async calls back on its worker
        t.start()
        t.join()
        _pump()
        self.assertEqual(dlg.state, dlg.CODE)
        self.assertEqual(dlg.lbl_pin.text(), "123 456")

    def test_bridge_that_cannot_start(self):
        started = {}
        dlg = pp.PairPhoneDialog(service=lambda: None, start_async=lambda on_ready: started.update(cb=on_ready),
                                 clock=Clock())
        self.addCleanup(dlg.deleteLater)
        started["cb"](None)
        _pump()
        self.assertEqual(dlg.state, dlg.FAILED)
        self.assertIn(str(ss.DEFAULT_PORT), dlg.lbl_status.text())

    def test_late_ready_after_close_is_ignored(self):
        started = {}
        srv = FakeServer()
        dlg = pp.PairPhoneDialog(service=lambda: None, start_async=lambda on_ready: started.update(cb=on_ready),
                                 clock=Clock())
        self.addCleanup(dlg.deleteLater)
        dlg.reject()
        started["cb"](srv)
        _pump()
        self.assertEqual(srv.offers, 0, "no PIN may be issued for a closed sheet")


# ── Pair iPhone sheet against a real bridge ──────────────────────────────────

class TestPairSheetLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-pair-")
        self._old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.srv = ss.SyncServer(host="127.0.0.1", port=0, state_dir=os.path.join(self.tmp, "sync"),
                                 packs_dir=os.path.join(self.tmp, "packs"), use_tls=False, name="TEST-PC")

    def tearDown(self):
        self.srv.shutdown()
        database.DB_PATH = self._old_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post_pair(self, pin):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        try:
            c.request("POST", "/v1/pair", json.dumps({"pin": pin, "device_name": "Test iPhone",
                                                      "device_id": "dev-1"}),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            c.close()

    def test_pin_from_the_sheet_pairs_the_phone(self):
        dlg = pp.PairPhoneDialog(service=lambda: self.srv)
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg.state, dlg.CODE)
        self.assertIn("h=127.0.0.1", dlg.offer["uri"])
        pin = dlg.lbl_pin.text().replace(" ", "")
        self.assertEqual(self.post_pair("000000" if pin != "000000" else "111111")[0], 403)
        status, body = self.post_pair(pin)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("token"))
        dlg.poll()
        self.assertEqual(dlg.state, dlg.PAIRED)
        self.assertIn("Test iPhone", dlg.lbl_status.text())
        self.assertEqual([d["name"] for d in ss.paired_devices(os.path.join(self.tmp, "sync"))], ["Test iPhone"])

    def test_closing_the_sheet_kills_the_pin(self):
        dlg = pp.PairPhoneDialog(service=lambda: self.srv)
        self.addCleanup(dlg.deleteLater)
        pin = dlg.lbl_pin.text().replace(" ", "")
        dlg.reject()
        status, body = self.post_pair(pin)
        self.assertEqual((status, body.get("error")), (403, "no_pairing"))


# ── sync_server service hooks ────────────────────────────────────────────────

class TestServiceHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-hooks-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(ss.set_remote_change_handler, None)

    def test_remote_handler_reaches_running_and_later_services(self):
        class Srv:
            on_remote_change = None
        fake = Srv()
        with mock.patch.object(ss, "_service", fake):
            handler = mock.Mock()
            ss.set_remote_change_handler(handler)
            self.assertIs(fake.on_remote_change, handler)
        self.assertIs(ss._remote_handler, handler)

    def test_device_list_and_forget(self):
        state = os.path.join(self.tmp, "sync")
        os.makedirs(state)
        creds = ss.Credentials(state)
        creds.begin_pairing()
        pin = creds.begin_pairing()
        token, dev = creds.pair(pin, "Anna's iPhone", "d1")
        listed = ss.paired_devices(state)
        self.assertEqual([(d["id"], d["name"]) for d in listed], [("d1", "Anna's iPhone")])
        self.assertNotIn("token_sha256", listed[0], "token hashes stay inside sync_server")
        self.assertTrue(ss.forget_device("d1", state))
        self.assertEqual(ss.paired_devices(state), [])
        self.assertIsNone(ss.Credentials(state).authenticate(token))


# ── Settings › iPhone sync ───────────────────────────────────────────────────

class TestSettingsGroup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-settings-sync-")
        self._saved = (database.DB_PATH, cfg.CONFIG_FILE, cfg.LEGACY_CONFIG_FILE)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        cfg.CONFIG_FILE = os.path.join(self.tmp, "config.json")
        cfg.LEGACY_CONFIG_FILE = os.path.join(self.tmp, "legacy.json")
        database.init_db()
        self.devices = [{"id": "d1", "name": "Anna's iPhone", "paired_at": "2026-11-06T09:00:00"}]
        self.calls = []
        patches = [
            mock.patch.object(ss, "service", lambda: None),
            mock.patch.object(ss, "start_async", lambda on_ready=None, **k: self.calls.append("start")),
            mock.patch.object(ss, "stop", lambda: self.calls.append("stop")),
            mock.patch.object(ss, "paired_devices", lambda state_dir=None: list(self.devices)),
            mock.patch.object(ss, "forget_device", self._forget),
            mock.patch.object(ss, "last_error", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        database.DB_PATH, cfg.CONFIG_FILE, cfg.LEGACY_CONFIG_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _forget(self, device_id, state_dir=None):
        self.calls.append(("forget", device_id))
        self.devices = [d for d in self.devices if d["id"] != device_id]
        return True

    def dialog(self):
        from settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        self.addCleanup(dlg.deleteLater)
        return dlg

    def saved(self):
        with open(cfg.CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_group_defaults_off(self):
        dlg = self.dialog()
        self.assertFalse(dlg.chk_sync.isChecked())
        self.assertEqual(dlg.lbl_sync_status.text(), "Vypnuto")
        self.assertEqual(dlg.list_sync_devices.count(), 1)
        self.assertIn("Anna's iPhone", dlg.list_sync_devices.item(0).text())

    def test_switch_acts_and_persists_at_once(self):
        dlg = self.dialog()
        dlg.chk_sync.setChecked(True)
        self.assertEqual(self.calls, ["start"])
        self.assertTrue(self.saved()["sync_server_enabled"], "persisted without pressing OK")
        self.assertEqual(dlg.lbl_sync_status.text(), "Spouští se…")
        dlg.reject()                                    # Cancel does not undo a live switch
        self.assertTrue(self.saved()["sync_server_enabled"])

    def test_switch_off_stops_the_bridge(self):
        cfg.save_config({**cfg.load_config(), "sync_server_enabled": True})
        dlg = self.dialog()
        self.assertTrue(dlg.chk_sync.isChecked())
        dlg.chk_sync.setChecked(False)
        self.assertEqual(self.calls, ["stop"])
        self.assertFalse(self.saved()["sync_server_enabled"])

    def test_ok_keeps_the_switch_state(self):
        dlg = self.dialog()
        dlg.chk_sync.setChecked(True)
        dlg.save_and_close()
        self.assertTrue(self.saved()["sync_server_enabled"])

    def test_running_status_shows_the_address(self):
        class Srv:
            port = 52790
        with mock.patch.object(ss, "service", lambda: Srv()), mock.patch.object(ss, "lan_address", lambda: "10.0.0.7"):
            dlg = self.dialog()
        self.assertIn("10.0.0.7:52790", dlg.lbl_sync_status.text())

    def test_failed_start_is_reported(self):
        cfg.save_config({**cfg.load_config(), "sync_server_enabled": True})
        with mock.patch.object(ss, "last_error", OSError("in use")):
            dlg = self.dialog()
        self.assertIn(str(ss.DEFAULT_PORT), dlg.lbl_sync_status.text())

    def test_pair_button_turns_sync_on_and_opens_the_sheet(self):
        dlg = self.dialog()
        opened = []

        class Sheet:
            def __init__(self, parent):
                opened.append(parent)

            def exec(self):
                return 1
        with mock.patch.object(pp, "PairPhoneDialog", Sheet):
            dlg.btn_pair_phone.click()
        self.assertEqual(opened, [dlg])
        self.assertTrue(dlg.chk_sync.isChecked())
        self.assertEqual(self.calls, ["start"])

    def test_forget_device_after_confirmation(self):
        dlg = self.dialog()
        with mock.patch.object(dlg, "_confirm", return_value=(False, {})):
            dlg.btn_sync_forget.click()
        self.assertEqual(self.calls, [])
        with mock.patch.object(dlg, "_confirm", return_value=(True, {})) as confirm:
            dlg.btn_sync_forget.click()
        self.assertIn("Anna's iPhone", confirm.call_args[0][0])
        self.assertEqual(self.calls, [("forget", "d1")])
        self.assertEqual(dlg.list_sync_devices.count(), 0)
        self.assertEqual(dlg.lbl_sync_devices.text(), "Žádná")
        self.assertTrue(dlg.list_sync_devices.isHidden())


if __name__ == "__main__":
    unittest.main()
