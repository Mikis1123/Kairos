"""Founder audit 2026-11-03: console hygiene, startup hand-off, badges, journeys/map polish,
settings sheet + wheel guard, viewer mini-map/people, duplicates compare."""
import os
import re
import threading
import time

import pytest

import fs_atomic

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


# ── 1. Atomic replace on Windows ─────────────────────────────────────────────────────────────

def test_unique_tmp_keeps_extension_and_differs_per_thread(tmp_path):
    target = str(tmp_path / "thumb_2434.jpg")
    a = fs_atomic.unique_tmp(target)
    out = []
    t = threading.Thread(target=lambda: out.append(fs_atomic.unique_tmp(target)))
    t.start()
    t.join()
    assert a.endswith(".tmp.jpg") and out[0].endswith(".tmp.jpg")
    assert a != out[0]


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode semantics")
def test_replace_survives_a_transient_reader_lock(tmp_path):
    target = tmp_path / "thumb_1.jpg"
    target.write_bytes(b"old")
    tmp = fs_atomic.unique_tmp(str(target))
    with open(tmp, "wb") as f:
        f.write(b"new")
    reader = open(target, "rb")            # Python opens without FILE_SHARE_DELETE -> WinError 5
    threading.Timer(0.15, reader.close).start()
    t0 = time.perf_counter()
    assert fs_atomic.replace(tmp, str(target))
    assert time.perf_counter() - t0 >= 0.1
    assert target.read_bytes() == b"new"
    assert not os.path.exists(tmp)


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode semantics")
def test_replace_gives_up_cleanly_on_a_permanent_lock(tmp_path):
    target = tmp_path / "thumb_2.jpg"
    target.write_bytes(b"old")
    tmp = fs_atomic.unique_tmp(str(target))
    with open(tmp, "wb") as f:
        f.write(b"new")
    with open(target, "rb"):
        assert fs_atomic.replace(tmp, str(target), delays=(0.01, 0.01)) is False
    assert target.read_bytes() == b"old"       # old thumbnail intact
    assert not os.path.exists(tmp)             # no stranded temp file


def test_replace_does_not_retry_non_transient_errors(tmp_path):
    slept = []
    assert fs_atomic.replace(str(tmp_path / "missing.tmp.jpg"), str(tmp_path / "x.jpg"),
                             sleep=slept.append) is False
    assert slept == []


def test_writers_use_the_safe_replace():
    for name in ("scanner.py", "preview_cache.py", "cv_io.py", "video_proxy.py", "card_media.py"):
        src = _read(name)
        assert "fs_atomic.replace(" in src, name
    assert '+ ".tmp.jpg"' not in _read("scanner.py")


def test_make_thumbnail_with_locked_target_does_not_raise(tmp_path, capsys):
    if os.name != "nt":
        pytest.skip("Windows share-mode semantics")
    from PIL import Image
    import scanner
    src = tmp_path / "a.jpg"
    Image.new("RGB", (1200, 900), (200, 80, 40)).save(src)
    out = tmp_path / "thumb_9.jpg"
    out.write_bytes(b"old")
    reader = open(out, "rb")
    threading.Timer(0.1, reader.close).start()
    assert scanner.make_thumbnail(str(src), str(out), "photo", None)
    assert "WinError" not in capsys.readouterr().out
    assert not [p for p in os.listdir(tmp_path) if ".tmp" in p]


def test_accent_classic_is_translated_everywhere():
    import i18n
    for lang in ("cs", "en", "de", "fr", "es", "ja", "zh-CN"):
        i18n.set_language(lang, retranslate=False)
        assert i18n.tr("accent-classic") != "accent-classic", lang
    i18n.set_language("cs", retranslate=False)


# ── 2. Startup hand-off + badges ─────────────────────────────────────────────────────────────

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    import sys
    _app = QApplication.instance() or QApplication(sys.argv)   # Chromium potřebuje argv[0]
    import main  # noqa: F401,E402 – jako test_fidelity_days: main (příznaky Chromia) hned po QApplication
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False

qt = pytest.mark.skipif(not HAS_QT, reason="Qt není dostupné")


def _grid(n=30, loaded=True):
    from unittest import mock
    from PyQt6.QtGui import QColor, QImage, QPixmap
    import main
    host = main.QWidget()
    host.resize(800, 600)
    grid = main.AppleGridView(mock.Mock(), host)
    grid.setGeometry(0, 0, 800, 600)
    recs = [(i, f"p{i}.jpg", f"C:/x/p{i}.jpg", ("live_photo", "video", "photo")[i % 3], None, None,
             "2024-01-01 10:00", 0) for i in range(n)]
    grid.photo_model.set_records(recs)
    # žádné skutečné dekódování v testu
    grid.photo_model._start_worker = lambda item_id, rec: grid.photo_model.loading_ids.add(item_id)
    if loaded:
        for i in range(n):
            img = QImage(360, 270, QImage.Format.Format_RGB32)
            img.fill(QColor(40 + i * 5, 90, 140))
            grid.photo_model.ram_cache.put(i, QPixmap.fromImage(img))
    host.show()
    _app.processEvents()
    return host, grid


@qt
def test_first_frame_waits_for_visible_thumbnails():
    from PyQt6.QtGui import QColor, QImage, QPixmap
    host, grid = _grid(loaded=False)
    fired = []
    grid.firstFrameRendered.connect(lambda: fired.append(1))
    grid.doItemsLayout()
    grid.viewport().grab()
    _app.processEvents()
    assert not grid.visible_tiles_ready() and fired == []
    for i in range(30):
        img = QImage(360, 270, QImage.Format.Format_RGB32)
        img.fill(QColor(10, 10, 10))
        grid.photo_model.ram_cache.put(i, QPixmap.fromImage(img))
    grid.viewport().grab()
    _app.processEvents()
    assert fired == [1]
    grid.viewport().grab()               # jen jednou
    _app.processEvents()
    assert fired == [1]
    host.deleteLater()


@qt
def test_empty_library_counts_as_ready():
    host, grid = _grid(n=0)
    assert grid.visible_tiles_ready()
    host.deleteLater()


@qt
def test_splash_cross_dissolves_into_the_window():
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QWidget
    import motion
    import splash
    motion.set_reduce_motion(False)

    class Src(QObject):
        ready = pyqtSignal()

    src = Src()
    win = QWidget()
    win.setWindowOpacity(0.0)
    win.show()
    s = splash.KairosSplash("dark")
    s.show()
    s.hand_off(win, src.ready, timeout_ms=60000)
    _app.processEvents()
    assert win.windowOpacity() == 0.0, "okno čeká skryté, dokud mřížka není hotová"
    src.ready.emit()
    seen = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.5 and win.windowOpacity() < 1.0:
        _app.processEvents()
        seen.append(round(win.windowOpacity(), 2))
        time.sleep(0.005)
    assert win.windowOpacity() == 1.0
    assert any(0.05 < v < 0.95 for v in seen), "prolnutí, ne skok"
    s.finish(win)                        # idempotentní
    win.close()


@qt
def test_splash_timeout_reveals_window_even_without_a_grid_frame():
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QWidget
    import motion
    import splash
    motion.set_reduce_motion(True)

    class Src(QObject):
        ready = pyqtSignal()

    src = Src()
    win = QWidget()
    win.setWindowOpacity(0.0)
    s = splash.KairosSplash("dark")
    s.hand_off(win, src.ready, timeout_ms=30)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.0 and win.windowOpacity() < 1.0:
        _app.processEvents()
        time.sleep(0.005)
    motion.set_reduce_motion(False)
    assert win.windowOpacity() == 1.0


def test_main_hands_off_on_first_frame():
    src = _read("main.py")
    assert "splash.hand_off(window, grid.firstFrameRendered)" in src
    assert "window.setWindowOpacity(0.0)" in src


@qt
def test_badges_are_glass_icons_without_text():
    from unittest import mock
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    import main
    img = QImage(200, 200, QImage.Format.Format_ARGB32)
    for kind in ("live_photo", "video"):
        img.fill(0)
        p = QPainter(img)
        with mock.patch.object(QPainter, "drawText", side_effect=AssertionError("text badge")):
            main.ApplePhotoDelegate._paint_badges(p, QRectF(0, 0, 200, 200), {"type": kind})
        p.end()
        disc = main.ApplePhotoDelegate.badge_rect(QRectF(0, 0, 200, 200))
        assert disc.right() > 170 and disc.top() < 10 and disc.width() <= 22   # malý, vpravo nahoře
        # něco se nakreslilo v kroužku, nic mimo roh
        assert img.pixelColor(int(disc.center().x()), int(disc.center().y())).alpha() > 0
        assert img.pixelColor(30, 30).alpha() == 0


# ── 4. Settings: sheet, wheel guard, (i) ─────────────────────────────────────────────────────

def _wheel(widget, dy=-120):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    pos = QPointF(widget.rect().center())
    ev = QWheelEvent(pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, dy),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, ev)


@qt
def test_wheel_over_unfocused_slider_scrolls_the_page_instead():
    from PyQt6.QtWidgets import QComboBox, QScrollArea, QSlider, QVBoxLayout, QWidget
    import ui_controls
    area = QScrollArea()
    area.resize(300, 200)
    page = QWidget()
    lay = QVBoxLayout(page)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(50)
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    lay.addWidget(slider)
    lay.addWidget(combo)
    for _ in range(30):
        lay.addWidget(QComboBox())
    area.setWidget(page)
    area.setWidgetResizable(True)
    assert ui_controls.guard_wheel(page) >= 32
    assert ui_controls.guard_wheel(page) == 0          # idempotentní
    area.show()
    _app.processEvents()
    sb = area.verticalScrollBar()
    before = sb.value()
    _wheel(slider)
    _wheel(combo)
    assert slider.value() == 50 and combo.currentIndex() == 0, "hodnota se kolečkem nezměnila"
    assert sb.value() > before, "stránka se posunula"
    assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus
    # po kliknutí (fokus myší) kolečko prvek ovládá
    from PyQt6.QtTest import QTest
    QTest.mouseClick(combo, Qt.MouseButton.LeftButton)          # otevře a zavře seznam
    combo.hidePopup()
    slider.setFocus(Qt.FocusReason.OtherFocusReason)            # fokus od programu: stále chráněno
    _wheel(slider)
    assert slider.value() == 50
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, slider.rect().center())
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, slider.rect().center())
    v = slider.value()
    _wheel(slider)
    if slider.hasFocus():
        assert slider.value() != v, "po kliknutí kolečko prvek ovládá"
    area.close()


@qt
def test_info_dot_carries_rich_wrapped_tooltip():
    import ui_controls
    dot = ui_controls.InfoDot("a < b\nsecond line")
    assert "&lt;" in dot.toolTip() and "<br>" in dot.toolTip() and dot.toolTip().startswith("<qt>")
    assert dot.accessibleDescription() == "a < b\nsecond line"
    sw = ui_controls.ToggleSwitch("Label")
    sw.resize(300, 30)
    sw.set_info("Explain")
    assert sw.info_dot is not None and sw.info_dot.info == "Explain"


def _settings_env(tmp_path, monkeypatch):
    import config as cfg
    import database
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "photos.db"))
    database.init_db()
    monkeypatch.setattr(cfg, "CONFIG_FILE", str(tmp_path / "config.json"))


@qt
def test_settings_has_no_hint_walls_and_guards_wheels(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QComboBox, QLabel, QSlider
    _settings_env(tmp_path, monkeypatch)
    from settings_dialog import SettingsDialog
    import ui_controls
    dlg = SettingsDialog()
    import i18n
    moved = ["settings-folders-hint", "settings-appearance-hint", "settings-music-hint", "settings-tile-size-hint",
             "settings-trash-hint", "settings-locked-hint", "settings-guide-hint", "settings-caches-hint",
             "settings-face-threshold-hint"]
    texts = {l.text() for l in dlg.findChildren(QLabel)}
    walls = [k for k in moved if i18n.tr(k) in texts]
    assert walls == [], walls
    dots = dlg.findChildren(ui_controls.InfoDot)
    assert len(dots) >= 15
    assert all(d.info for d in dots)
    for w in dlg.findChildren(QSlider) + dlg.findChildren(QComboBox):
        assert w.property("_wheel_guarded"), w
    dlg.deleteLater()


@qt
def test_settings_opens_as_in_window_sheet(tmp_path, monkeypatch):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QMainWindow
    _settings_env(tmp_path, monkeypatch)
    from settings_dialog import SettingsDialog
    import ui_sheets
    win = QMainWindow()
    win.resize(1200, 860)
    win.show()
    _app.processEvents()
    dlg = SettingsDialog(win, as_sheet=True)
    assert not dlg.isWindow(), "není samostatné okno"
    assert isinstance(dlg.sheet, ui_sheets.PanelSheet) and dlg.sheet.parentWidget() is win
    seen = {}

    def probe():
        seen["visible"] = dlg.sheet.isVisible()
        seen["card"] = dlg.sheet.card.geometry()
        dlg.save_and_close()

    QTimer.singleShot(400, probe)
    result = dlg.exec()
    assert result == 1
    assert seen["visible"]
    card = seen["card"]
    assert card.width() <= 720 and card.height() <= 820 and win.rect().contains(card)
    win.close()


@qt
def test_settings_sheet_cancel_by_clicking_scrim(tmp_path, monkeypatch):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QMainWindow
    _settings_env(tmp_path, monkeypatch)
    from settings_dialog import SettingsDialog
    win = QMainWindow()
    win.resize(1200, 860)
    win.show()
    dlg = SettingsDialog(win, as_sheet=True)
    QTimer.singleShot(300, lambda: dlg.sheet.request_close())
    assert dlg.exec() == 0
    win.close()


def test_main_presents_settings_as_sheet():
    assert "SettingsDialog(self, as_sheet=True)" in _read("main.py")


# ── 5. Viewer mini-map + people ──────────────────────────────────────────────────────────────

def test_mini_map_uses_the_main_map_style_and_tile_store():
    mini = _read("mini_map.html")
    assert "styles/dark" not in mini and "map_style.js" in mini
    assert "MS.standard(" in mini and "transformRequest" in mini and "TILE_STORE_CONFIG" in mini
    assert "#FF3B30" not in mini, "žádná červená kapka"
    assert "here-pulse" in mini and "prefers-reduced-motion" in mini
    main_page = _read("map_vault.html")
    assert '<script src="map_style.js"></script>' in main_page
    assert "MS.localize(" in main_page and "var pastelize = MS.pastelize" in main_page
    assert "map_style.js" in _read("app.spec")
    viewer_src = _read("viewer.py")
    assert "self.page_setup(m.page())" in viewer_src and "setAccent" in viewer_src
    assert "viewer.page_setup = attach_tile_store" in _read("main.py")


@qt
def test_face_chips_wrap_and_carry_names(tmp_path):
    from PyQt6.QtGui import QImage, QColor
    from PyQt6.QtWidgets import QWidget
    import ui_controls
    import viewer
    crop = str(tmp_path / "f.jpg")
    img = QImage(90, 90, QImage.Format.Format_RGB32)
    img.fill(QColor("#a87"))
    img.save(crop)
    host = QWidget()
    host.resize(240, 200)
    flow = ui_controls.FlowLayout(host, spacing=6)
    chips = [viewer.FaceChip(crop, "Jana Nováková"), viewer.FaceChip(None, None),
             viewer.FaceChip(crop, "A very long person name that must be elided nicely")]
    for c in chips:
        flow.addWidget(c)
    host.show()
    _app.processEvents()
    assert chips[0]._avatar is not None and chips[1]._avatar is None and not chips[1].known
    assert all(c.width() <= viewer.FaceChip.MAX_W for c in chips)
    assert chips[2]._text.endswith("…")
    ys = {c.y() for c in chips}
    assert len(ys) >= 2, "zalamují se do řádků, ne jedna přeplněná řada"
    assert not chips[1].grab().isNull()
    host.deleteLater()


# ── 6. Duplicates ────────────────────────────────────────────────────────────────────────────

def test_compare_facts_marks_the_better_side():
    import main
    assert main.compare_facts((4032, 3024, 3_000_000, "HEIC"), (2016, 1512, 900_000, "JPG")) == \
        {"a": ["res", "size"], "b": []}
    assert main.compare_facts((100, 100, 1000, "JPG"), (100, 100, 1001, "JPG")) == \
        {"a": ["same_res"], "b": ["same_res"]}
    assert main.compare_facts((None, None, None, None), (100, 100, 5, "JPG")) == {"a": [], "b": []}


@qt
def test_duplicate_cards_badges_and_split_mode(tmp_path, monkeypatch):
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QLabel
    import database
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "p.db"))
    database.init_db()
    import main
    paths = []
    for i, (w, h) in enumerate(((1200, 900), (600, 450))):
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor(200, 100, 50 + i * 80))
        f = str(tmp_path / f"d{i}.jpg")
        img.save(f, "JPG", 90)
        paths.append(f)
    view = main.DuplicateReviewView([(1, paths[0], 2, paths[1], 1)])
    view.resize(1100, 760)
    view.show()
    view.wait_for_images(8000)
    _app.processEvents()
    assert view.facts[0][:2] == (1200, 900) and view.facts[1][:2] == (600, 450)
    badges_a = [l.text() for l in view.card_a.findChildren(QLabel) if l.objectName() == "dupBadge"]
    badges_b = [l.text() for l in view.card_b.findChildren(QLabel) if l.objectName() == "dupBadge"]
    import i18n
    assert i18n.tr("dup-badge-recommended") in badges_a and i18n.tr("dup-badge-higher-res") in badges_a
    assert badges_b == []
    assert "1200 × 900" in view.card_a.facts.text()
    # karty mají stín a zaoblené obrazy
    assert view.card_a.graphicsEffect() is not None and view.lbl_a.radius > 0
    # posuvník: S přepne, dělítko se táhne
    from PyQt6.QtTest import QTest
    QTest.keyClick(view, Qt.Key.Key_S)
    assert view.compare_stack.currentIndex() == 1 and view.mode_switch.current_key() == "split"
    _app.processEvents()
    sv = view.split_view
    r = sv.image_rect()
    QTest.mouseClick(sv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     r.topLeft().toPoint() + main.QPoint(int(r.width() * 0.25), int(r.height() / 2)))
    assert abs(sv.split - 0.25) < 0.02
    assert not sv.grab().isNull()
    QTest.keyClick(view, Qt.Key.Key_S)
    assert view.compare_stack.currentIndex() == 0
    view.close()


# ── 3. Journeys + map interaction (skutečná stránka map_vault.html) ──────────────────────────

def _fid_trip():
    from test_fidelity_days import TestDayChaptersPage
    return TestDayChaptersPage.trip(None)


@pytest.fixture(scope="module")
def mappage():
    if not HAS_QT:
        pytest.skip("Qt není dostupné")
    import main  # noqa: F401  (MapBridge)
    from test_patch11_maps import ShownMapPage
    m = ShownMapPage((1000, 700))
    yield m
    m.js("if (journey.active) window.endJourney(true); reduceMotion = false; 0")
    m.close()


def _wait_js(m, cond, ms=4000):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < ms / 1000:
        if m.js(cond):
            return True
        _app.processEvents()
        time.sleep(0.03)
    return False


def test_hub_trip_route_draws_each_place_pair_once_and_fades_the_past(mappage):
    m = mappage
    import json
    m.js("reduceMotion = true; 0")
    assert m.js(f"playJourney({json.dumps(_fid_trip())})")
    m.js("seekStop(5); 0")
    assert _wait_js(m, "journey.curLeg >= 5")
    lay = m.js("getRouteLayout()")
    done = lay["done"]
    # legs 1-4 projeté; leg 1 (Kolinec→Kašperské) a leg 2 (zpět) = stejná dvojice -> jen jednou
    assert len(done) == 3, done
    newest = min(done, key=lambda p: p["age"])
    oldest = max(done, key=lambda p: p["age"])
    assert newest["o"] == 1 and newest["glow"] == 1
    assert oldest["o"] < 0.5, "starší den bledne"
    assert m.js("map.getPaintProperty('j-done', 'line-opacity')") == ["get", "o"]
    assert m.js("JSON.stringify(map.getFilter('j-glow'))") == '["==",["get","glow"],1]'
    m.js("window.endJourney(true); 0")


def test_route_ahead_is_a_faint_hint_without_duplicates(mappage):
    m = mappage
    import json
    m.js("reduceMotion = true; 0")
    assert m.js(f"playJourney({json.dumps(_fid_trip())})")
    lay = m.js("getRouteLayout()")
    assert lay["legs"] == 5
    assert len(lay["ahead"]) <= 4, "návrat stejnou cestou se nekreslí dvakrát"
    assert all(p["o"] <= 0.5 for p in lay["ahead"])
    m.js("window.endJourney(true); 0")


def test_day_switch_hands_over_without_tearing_down_the_film(mappage):
    m = mappage
    import json
    m.js("reduceMotion = false; 0")
    assert m.js(f"playJourney({json.dumps(_fid_trip())})")
    m.js("window.__stops = 0; window.__looks = 0;"
         " window.__origStop = stopJourney; stopJourney = function (s) { window.__stops++; return window.__origStop(s); };"
         " window.__origLook = filmLook; filmLook = function (on) { window.__looks++; return window.__origLook(on); }; 0")
    try:
        assert m.js("journeyPlayDay(2)")
        st = m.js("({ day: journey.day, switched: !!journey.switched, intro: film.segs[0].kind, dur: film.segs[0].dur,"
                  " stops: window.__stops, looks: window.__looks, pinsHidden: map.getLayoutProperty('pins', 'visibility') })")
        assert st["day"] == 2 and st["switched"]
        assert st["stops"] == 0, "žádné ukončení Cesty mezi dny (terén, rozlišení, piny, obloha)"
        assert st["looks"] == 0, "vzhled filmu zůstává"
        assert st["intro"] == "intro" and abs(st["dur"] - 2.6) < 0.6, "kratší let van Wijk nad nový den"
        assert m.js("!!map.getLayer('j-done') && !!map.getLayer('j-all')")
    finally:
        m.js("stopJourney = window.__origStop; filmLook = window.__origLook; window.endJourney(true); 0")


def test_leaving_the_journey_still_restores_the_map(mappage):
    m = mappage
    import json
    m.js("reduceMotion = true; 0")
    assert m.js(f"playJourney({json.dumps(_fid_trip())})")
    m.js("journeyPlayDay(0); journeyPlayDay(2); 0")
    m.js("window.endJourney(true); 0")
    assert m.js("!journey.active && !document.body.classList.contains('journey')")
    assert m.js("map.getLayoutProperty('pins', 'visibility') !== 'none'")


def test_pin_clusters_cross_fade(mappage):
    m = mappage
    res = m.js("""(function () {
      var f = function (img, x) { return { type: 'Feature', properties: { img: img, count: 1, stay: 0 },
                                           geometry: { type: 'Point', coordinates: [x, 50] } }; };
      var oldG = { type: 'FeatureCollection', features: [f('a', 14), f('b', 15)] };
      var newG = { type: 'FeatureCollection', features: [f('a', 14), f('c', 16), f('d', 17)] };
      var leaving = markStaying(oldG, newG);
      return { leaving: leaving.map(function (x) { return x.properties.img; }),
               stay: newG.features.map(function (x) { return x.properties.stay; }),
               expr: PIN_OPACITY };
    })()""")
    assert res["leaving"] == ["b"]
    assert res["stay"] == [1, 0, 0]
    # staying pins at 1, fresh ones fade through per-feature state (constant paint, no re-layout)
    assert res["expr"] == ["case", ["==", ["get", "stay"], 1], 1, ["coalesce", ["feature-state", "op"], 1]]
    assert "crossFadePins(leaving)" in _read("map_vault.html")


def test_orbit_gestures(mappage):
    m = mappage
    m.js("map.jumpTo({ center: [14.4, 50.08], zoom: 12, bearing: 0, pitch: 0 }); 0")
    assert m.js("map.dragRotate.isEnabled() && map.touchZoomRotate.isEnabled()")
    m.js("orbitBy(30, 20); 0")
    assert abs(m.js("map.getBearing()") - 30) < 0.5 and abs(m.js("map.getPitch()") - 20) < 0.5
    m.js("orbitBy(0, 500); 0")
    assert m.js("map.getPitch()") <= m.js("MAX_PITCH") + 0.01, "náklon s horní mezí"
    b0 = m.js("map.getBearing()")
    m.js("map.getCanvasContainer().dispatchEvent(new WheelEvent('wheel', { deltaX: 40, deltaY: 0, shiftKey: true,"
         " bubbles: true, cancelable: true })); 0")
    assert abs(m.js("map.getBearing()") - b0 - 10) < 0.5, "Shift + posun dvěma prsty = kroužení"
    m.js("map.jumpTo({ bearing: 0, pitch: 0 }); 0")
