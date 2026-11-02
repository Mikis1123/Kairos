"""Regresní testy pro Patch 11 – dávka F: vektorové mapy (MapLibre GL + OpenFreeMap).
Batch 6: stránka je čistý MapLibre GL 5 (zeměkoule), bez Leafletu – testy stránky přepsané.

Spuštění (ve složce projektu):   python -m pytest test_patch11_maps.py -v

Pokrývá:
  Politika    – OpenFreeMap a AWS terén povoleny (jen přesné hosty), MapLibre přibalený lokálně
                s ověřeným SHA-256, žádné CDN.
  Styly       – Základní: české názvy (name:cs), pastelové lesy/parky, 3D budovy bez null výšek;
                Satelitní: snímky Esri + vektorové cesty, hranice a názvy; Turistická: rastr.
  Stránka     – s podstrčeným stylem vektor, bez stylu / bez sítě rastrová záloha; vždy zeměkoule
                a zdroj terénu; piny, shluky a klik fungují.
  Profil      – trvalý profil mapy s diskovou HTTP cache a filtrem povolených serverů.

Testy NESAHAJÍ na síť (vše mimo file/qrc/data/blob se blokuje).
"""
import base64
import hashlib
import json
import os
import sys
import unittest

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QUrl
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    import main
    import paths

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor", "maplibre")

# MapLibre GL JS 5.24.0 z npm (tarball ověřen proti dist.integrity registru)
VENDORED_SHA256 = {
    "maplibre-gl.js": "RamwepGJzlYFTGIKlHzPQeKR5YyV6bYVM7dAqqZe5cs=",
    "maplibre-gl.css": "qx5w1Z7EBGW65+cDDaLzzPKBM/1QLmK9WY7vut/XpzI=",
}

NAME_EN = ["case", ["has", "name:nonlatin"],
           ["concat", ["get", "name:latin"], "\n", ["get", "name:nonlatin"]],
           ["coalesce", ["get", "name_en"], ["get", "name"]]]

# Zmenšená kopie struktury stylu OpenFreeMap „liberty“ (stejné id vrstev a source-layer)
FIXTURE_STYLE = {
    "version": 8,
    "glyphs": "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
    "sprite": "https://tiles.openfreemap.org/sprites/ofm_f384/ofm",
    "sources": {"openmaptiles": {"type": "vector", "url": "https://tiles.openfreemap.org/planet"}},
    "layers": [
        {"id": "background", "type": "background", "paint": {"background-color": "#f8f4f0"}},
        {"id": "park", "type": "fill", "source": "openmaptiles", "source-layer": "park",
         "paint": {"fill-color": "#d8e8c8", "fill-opacity": 0.7, "fill-outline-color": "rgba(95, 208, 100, 1)"}},
        {"id": "landcover_wood", "type": "fill", "source": "openmaptiles", "source-layer": "landcover",
         "paint": {"fill-color": "hsla(98,61%,72%,0.7)", "fill-opacity": 0.4}},
        {"id": "road_motorway", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
         "paint": {"line-color": "#fc8", "line-width": 3}},
        {"id": "road_minor", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
         "paint": {"line-color": "#fff", "line-width": 1, "line-dasharray": [2, 1]}},
        {"id": "road_motorway_casing", "type": "line", "source": "openmaptiles", "source-layer": "transportation",
         "paint": {"line-color": "#e9ac77"}},
        {"id": "boundary_2", "type": "line", "source": "openmaptiles", "source-layer": "boundary",
         "paint": {"line-color": "hsl(248, 7%, 66%)"}},
        {"id": "label_city", "type": "symbol", "source": "openmaptiles", "source-layer": "place",
         "layout": {"text-field": NAME_EN, "text-font": ["Noto Sans Regular"], "icon-image": "dot_9"},
         "paint": {"text-color": "#333"}},
        {"id": "road_shield", "type": "symbol", "source": "openmaptiles", "source-layer": "transportation_name",
         "layout": {"text-field": ["to-string", ["get", "ref"]], "icon-image": "road_2"}},
        {"id": "poi_r1", "type": "symbol", "source": "openmaptiles", "source-layer": "poi",
         "filter": ["all", ["match", ["geometry-type"], ["MultiPoint", "Point"], True, False],
                    [">=", ["get", "rank"], 1], ["<", ["get", "rank"], 7]],
         "layout": {"text-field": NAME_EN, "icon-image": "{class}_11"}},
        {"id": "building-3d", "type": "fill-extrusion", "source": "openmaptiles", "source-layer": "building",
         "minzoom": 14, "filter": ["!", ["has", "hide_3d"]],
         "paint": {"fill-extrusion-base": ["get", "render_min_height"], "fill-extrusion-color": "hsl(35,8%,85%)",
                   "fill-extrusion-height": ["get", "render_height"], "fill-extrusion-opacity": 0.8}},
    ],
}
# Gate 1: názvy míst v jazyce aplikace; samostatně načtená stránka = angličtina
CS_NAME = ["coalesce", ["get", "name:en"], ["get", "name:latin"], ["get", "name"]]


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapPolicy(unittest.TestCase):

    def test_openfreemap_allowed_exact_host(self):
        self.assertIn("tiles.openfreemap.org", main.ALLOWED_WEB_HOSTS)
        self.assertTrue(main.is_allowed_web_url("https://tiles.openfreemap.org/styles/liberty"))
        self.assertTrue(main.is_allowed_web_url("https://tiles.openfreemap.org/planet/20261020/14/8800/5600.pbf"))
        for bad in ("http://tiles.openfreemap.org/styles/liberty",
                    "https://tiles.openfreemap.org.evil.com/styles/liberty",
                    "https://openfreemap.org/styles/liberty"):
            self.assertFalse(main.is_allowed_web_url(bad), bad)

    def test_terrain_host_is_single_bucket(self):
        """Batch 6: 3D terén jen z bucketu AWS Terrain Tiles – ne celé s3.amazonaws.com."""
        self.assertTrue(main.is_allowed_web_url(
            "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/8/138/88.png"))
        for bad in ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/8/138/88.png",
                    "https://evil-bucket.s3.amazonaws.com/x.png",
                    "http://elevation-tiles-prod.s3.amazonaws.com/terrarium/8/138/88.png"):
            self.assertFalse(main.is_allowed_web_url(bad), bad)

    def test_vendored_integrity(self):
        for name, sri in VENDORED_SHA256.items():
            with open(os.path.join(VENDOR, name), "rb") as f:
                digest = base64.b64encode(hashlib.sha256(f.read()).digest()).decode()
            self.assertEqual(digest, sri, name)
        self.assertTrue(os.path.getsize(os.path.join(VENDOR, "LICENSE-maplibre.txt")) > 0)
        with open(os.path.join(VENDOR, "maplibre-gl.js"), encoding="utf-8") as f:
            self.assertIn("v5.24.0", f.read(400), "MapLibre GL 5 (zeměkoule)")
        self.assertFalse(os.path.exists(os.path.join(VENDOR, "leaflet-maplibre-gl.js")), "most do Leafletu už není")

    def test_page_uses_local_libraries(self):
        with open(os.path.join(HERE, "map_vault.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ('src="vendor/maplibre/maplibre-gl.js"', 'href="vendor/maplibre/maplibre-gl.css"',
                       "https://tiles.openfreemap.org/", "'styles/liberty'", "name:cs",
                       "type: GLOBE_PROJECTION", "'vertical-perspective'", "elevation-tiles-prod.s3.amazonaws.com/terrarium"):
            self.assertTrue(needle in html, needle)
        self.assertNotIn("vendor/leaflet", html)
        self.assertNotIn("unpkg.com", html)
        self.assertNotIn("api_key", html.lower())


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapProfile(unittest.TestCase):

    def test_persistent_profile_with_disk_cache(self):
        from PyQt6.QtWebEngineCore import QWebEngineProfile
        profile = main.map_web_profile()
        self.assertIsNotNone(profile)
        self.assertIs(main.map_web_profile(), profile, "jeden profil za běh")
        self.assertFalse(profile.isOffTheRecord())
        self.assertEqual(profile.httpCacheType(), QWebEngineProfile.HttpCacheType.DiskHttpCache)
        self.assertEqual(profile.httpCacheMaximumSize(), main.MAP_HTTP_CACHE_MB * 1024 * 1024)
        self.assertTrue(os.path.normcase(profile.cachePath()).startswith(os.path.normcase(paths.WEB_CACHE_DIR)),
                        "v testech jen v dočasné složce (conftest)")
        self.assertIsInstance(profile._interceptor, main.WebUrlInterceptor)

    def test_shutdown_releases_in_safe_order(self):
        """Ukončení: okno (a stránka mapy) dřív než profil, filtr motivu pryč – jinak asi každé
        třetí zavření aplikace padalo v sip (0xC0000005) při rušení Python obalů."""
        from PyQt6 import sip
        from PyQt6.QtWebEngineCore import QWebEnginePage
        from PyQt6.QtWidgets import QWidget, QVBoxLayout
        import theme
        theme.apply("dark")
        self.assertIsNotNone(theme._filter)
        window = QWidget()
        view = QWebEngineView(window)
        QVBoxLayout(window).addWidget(view)
        profile = main.map_web_profile()
        page = QWebEnginePage(profile, view)
        view.setPage(page)
        window.show()
        _app.processEvents()
        main.shutdown_app(_app, window)
        self.assertTrue(sip.isdeleted(window), "okno zrušeno hned, ne až při ukončení interpretu")
        self.assertTrue(sip.isdeleted(page), "stránka zrušena dřív než profil")
        self.assertTrue(sip.isdeleted(profile))
        self.assertIsNone(main._map_profile)
        self.assertIsNone(theme._filter, "globální filtr událostí odebrán")
        again = main.map_web_profile()                 # další spuštění v témže procesu (testy)
        self.assertIsNotNone(again)
        self.assertFalse(sip.isdeleted(again))


def _offline_interceptor():
    from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

    class Offline(QWebEngineUrlRequestInterceptor):
        def __init__(self):
            super().__init__()
            self.urls = []

        def interceptRequest(self, info):
            url = info.requestUrl()
            self.urls.append(url.toString())
            if url.scheme() not in ("file", "qrc", "data", "blob"):   # blob = workery MapLibre
                info.block(True)
    return Offline()


def _load(page, timeout=15000):
    loop = QEventLoop()
    page.loadFinished.connect(lambda ok: loop.quit())
    QTimer.singleShot(timeout, loop.quit)
    page.load(QUrl.fromLocalFile(os.path.join(HERE, "map_vault.html")))
    loop.exec()


def _js(page, code, timeout=5000):
    loop, out = QEventLoop(), {}
    page.runJavaScript(code, lambda r: (out.__setitem__("r", r), loop.quit()))
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()
    return out.get("r")


class _FakeMain:
    def __init__(self):
        self.opened, self.opened_many, self.layers, self.journeys = [], [], [], []
        self.journeys_closed = 0

    def open_viewer_by_id(self, i):
        self.opened.append(i)

    def open_viewer_for_ids(self, ids):
        self.opened_many.append(ids)

    def set_map_layer(self, name):
        self.layers.append(name)

    def play_journey(self, event_id):
        self.journeys.append(event_id)

    def journey_closed(self):
        self.journeys_closed += 1


class ShownMapPage:
    """Skutečná stránka v zobrazeném okně (mapa vzniká jen na viditelné stránce), síť zablokovaná.
    Sdílí ji i test_batch6_map.py."""

    def __init__(self, size=(900, 600)):
        from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings
        from PyQt6.QtWebChannel import QWebChannel
        self.offline = _offline_interceptor()
        self.profile = QWebEngineProfile()
        self.profile.setUrlRequestInterceptor(self.offline)
        self.page = QWebEnginePage(self.profile)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.view = QWebEngineView()
        self.view.setPage(self.page)
        self.view.resize(*size)
        self.view.show()
        self.fake = _FakeMain()
        self.bridge = main.MapBridge(self.fake)
        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)
        _load(self.page)
        for _ in range(60):
            if self.js("bridge !== null && isMapCreated()"):
                break
            wait(100)
        self.wait_style()

    def js(self, code, timeout=5000):
        return _js(self.page, code, timeout)

    def wait_style(self):
        for _ in range(60):
            if self.js("styleReady === true"):
                return True
            wait(100)
        return False

    def reset_view(self, center=(15.5, 49.8), zoom=7):
        # „; 0“: jumpTo vrací objekt mapy – jeho převod do Pythonu je obří graf objektů
        self.js(f"map.stop(); map.jumpTo({{center: [{center[0]}, {center[1]}], zoom: {zoom}, pitch: 0, bearing: 0}}); "
                f"render(); 0")

    def close(self):
        self.view.hide()
        self.view.deleteLater()
        wait(50)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestVectorMapPage(unittest.TestCase):
    """Skutečná stránka: vektorový styl podstrčený testem, síť zablokovaná."""

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage()

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    @classmethod
    def js(cls, code, timeout=5000):
        return cls.m.js(code, timeout)

    def setUp(self):
        self.m.reset_view()
        wait(50)

    def _inject(self):
        self.assertTrue(self.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})"))
        self.m.wait_style()

    def test_01_offline_falls_back_to_raster(self):
        """Bez sítě se styl nestáhne – Základní zůstane na rastrové záloze, nic nespadne."""
        for _ in range(40):
            if self.js("vectorState") == "failed":
                break
            wait(100)
        self.assertEqual(self.js("vectorState"), "failed")
        self.assertTrue(any("tiles.openfreemap.org/styles/liberty" in u for u in self.m.offline.urls),
                        "styl se zkusil stáhnout")
        self.js("setBaseLayer('outdoor'); setBaseLayer('standard')")
        self.assertEqual(self.js("getBaseLayerEngine()"), "raster")
        self.assertIn("World_Street_Map", self.js("getBaseLayerUrls()")[0])

    def test_02_webgl2_available(self):
        self.assertTrue(self.js("glSupported()"), "QtWebEngine má WebGL 2 (ANGLE/D3D11)")
        self.assertTrue(self.js("isMapCreated()"))

    def test_03_vector_engine_for_standard_and_satellite(self):
        self._inject()
        self.js("setBaseLayer('outdoor')")
        self.assertEqual(self.js("getBaseLayerEngine()"), "raster", "Turistická = OpenTopoMap se stínováním")
        self.js("setBaseLayer('standard')")
        self.assertEqual(self.js("getBaseLayerEngine()"), "vector")
        self.assertEqual(self.js("getBaseLayerUrls()"), ["https://tiles.openfreemap.org/styles/liberty"])
        self.assertEqual(self.js("document.querySelectorAll('.maplibregl-canvas').length"), 1)
        self.js("setBaseLayer('satellite')")
        self.assertEqual(self.js("getBaseLayerEngine()"), "vector")
        urls = self.js("getBaseLayerUrls()")
        self.assertIn("World_Imagery", urls[0])
        self.assertEqual(urls[1], "https://tiles.openfreemap.org/planet")
        self.m.wait_style()
        wait(200)
        self.assertIn("Esri", self.js("document.querySelector('.maplibregl-ctrl-attrib').textContent"))
        # vektorový zdroj se offline nenačte (TileJSON) -> ovládání ho nevypíše; ve stylu ale je
        self.assertIn("OpenFreeMap", self.js("map.getStyle().sources.openmaptiles.attribution"))
        self.js("setBaseLayer('standard')")

    def test_04_standard_style_czech_and_pastel(self):
        self._inject()
        style = self.js("buildVectorStyle('standard')")
        layers = {l["id"]: l for l in style["layers"]}
        self.assertEqual(layers["label_city"]["layout"]["text-field"], CS_NAME, "české názvy")
        self.assertEqual(layers["poi_r1"]["layout"]["text-field"], CS_NAME)
        self.assertEqual(layers["road_shield"]["layout"]["text-field"], ["to-string", ["get", "ref"]],
                         "čísla silnic beze změny")
        self.assertNotIn("name_en", json.dumps(style))
        self.assertLess(layers["park"]["paint"]["fill-opacity"], 0.7, "pastelový park")
        self.assertNotIn("fill-outline-color", layers["park"]["paint"])
        self.assertLess(layers["landcover_wood"]["paint"]["fill-opacity"], 0.4)
        self.assertEqual(FIXTURE_STYLE["layers"][7]["layout"]["text-field"], NAME_EN)

    def test_05_satellite_style_is_imagery_plus_vector_overlay(self):
        self._inject()
        style = self.js("buildVectorStyle('satellite')")
        ids = [l["id"] for l in style["layers"]]
        self.assertEqual(ids[0], "esri-imagery")
        self.assertEqual(style["sources"]["esri"]["type"], "raster")
        self.assertIn("World_Imagery", style["sources"]["esri"]["tiles"][0])
        self.assertEqual(style["sources"]["openmaptiles"]["url"], "https://tiles.openfreemap.org/planet")
        self.assertEqual(set(ids), {"esri-imagery", "road_motorway", "road_minor", "boundary_2", "label_city"},
                         "jen cesty, hranice a názvy – bez ploch, obrysů, štítů a POI")
        by = {l["id"]: l for l in style["layers"]}
        self.assertEqual(by["label_city"]["paint"]["text-color"], "#FFFFFF")
        self.assertNotIn("icon-image", by["label_city"]["layout"])
        self.assertEqual(by["label_city"]["layout"]["text-field"], CS_NAME)
        self.assertNotIn("line-dasharray", by["road_minor"]["paint"])
        self.assertIn("255,214,110", by["road_motorway"]["paint"]["line-color"])

    def test_06_invalid_style_rejected(self):
        self.assertFalse(self.js("setVectorStyle({version: 8})"))
        self.assertFalse(self.js("setVectorStyle(null)"))

    def test_07_pins_work_over_vector_base(self):
        self._inject()
        self.js("setBaseLayer('standard')")
        self.assertEqual(self.js("getBaseLayerEngine()"), "vector")
        self.m.fake.opened.clear()
        markers = [{"id": 31, "lat": 49.03, "lon": 13.49, "name": "IMG_31", "city": "Modrava", "date": "", "thumb": ""},
                   {"id": 32, "lat": 50.08, "lon": 14.43, "name": "IMG_32", "city": "Praha", "date": "", "thumb": ""}]
        self.assertEqual(self.js(f"setMarkers({json.dumps(markers)})"), 2)
        self.m.reset_view((13.9, 49.6), 7)
        self.assertEqual(self.js("getClusterCount()"), 2)
        self.assertEqual(self.js("getPinCount()"), 2, "piny ve vrstvě WebGL")
        self.m.reset_view((13.49, 49.03), 12)
        idx = self.js("getClusters().findIndex(function (c) { return c.ids[0] === 31; })")
        self.js(f"clickCluster({idx}); 0")
        for _ in range(30):
            if self.m.fake.opened:
                break
            wait(50)
        self.assertEqual(self.m.fake.opened, [31])

    def test_09_buildings_3d_without_null_heights(self):
        """Batch 6: nakloněná kamera -> budovy zase 3D; chybějící výška = 0, ne chyba null."""
        self._inject()
        style = self.js("buildVectorStyle('standard')")
        b = next(l for l in style["layers"] if l["id"] == "building-3d")
        self.assertEqual(b["type"], "fill-extrusion")
        self.assertEqual(b["minzoom"], 14)
        self.assertEqual(b["paint"]["fill-extrusion-height"], ["coalesce", ["get", "render_height"], 0])
        self.assertEqual(b["paint"]["fill-extrusion-base"], ["coalesce", ["get", "render_min_height"], 0])
        self.assertEqual(FIXTURE_STYLE["layers"][-1]["paint"]["fill-extrusion-height"], ["get", "render_height"],
                         "vstupní styl se nemění")

    def test_09b_numeric_filters_guarded_against_null(self):
        """POI bez rank: filtr [">=", ["get","rank"], 1] hlásil „Expected value to be of type number,
        but found null instead.“ za každý prvek – stráž typeof ho tiše vyřadí."""
        self._inject()
        for name in ("standard", "satellite"):
            style = self.js(f"buildVectorStyle('{name}')")
            poi = next((l for l in style["layers"] if l["id"] == "poi_r1"), None)
            if poi is None:
                continue                                    # satelit POI nebere
            guard = ["==", ["typeof", ["get", "rank"]], "number"]
            self.assertIn(["all", guard, [">=", ["get", "rank"], 1]], poi["filter"])
            self.assertIn(["all", guard, ["<", ["get", "rank"], 7]], poi["filter"])

    def test_10_missing_sprite_images_get_blank_placeholder(self):
        self._inject()
        code = ("(function(){var before = map.hasImage('recycling');"
                "map.fire('styleimagemissing', {id: 'recycling'});"
                "return [before, map.hasImage('recycling')];})()")
        self.assertEqual(self.js(code), [False, True], "chybějící ikona = průhledný 1×1 px, ne chyba")

    def test_11_trails_only_when_zoomed_in(self):
        # test_08 končí na Turistické -> nejdřív jiný podklad, ať se zdroje opravdu vymění a dlaždice
        # se vyžádají až po vyprázdnění výpisu (dřív test závisel na pořadí a načasování)
        self.js("setBaseLayer('standard'); 0")
        self.m.wait_style()
        self.m.reset_view((15.5, 49.8), 7)
        self.m.offline.urls.clear()
        self.js("setBaseLayer('outdoor')")
        self.m.wait_style()
        self.assertEqual(self.js("map.getStyle().layers.filter(function (l) { return l.type === 'raster'; })"
                                 ".map(function (l) { return l.minzoom || 0; })"),
                         [0, 11, 12], "podklad vždy, pěší od z11, cyklo od z12")

        def requested(needle, timeout_ms=4000):
            for _ in range(timeout_ms // 100):
                if any(needle in u for u in self.m.offline.urls):
                    return True
                wait(100)
            return False

        self.assertTrue(requested("tile.opentopomap.org/"), "podklad se načítá")
        wait(300)
        self.assertFalse([u for u in self.m.offline.urls if "waymarkedtrails" in u],
                         "mapa státu: jen reliéf, žádná síť tras")
        self.m.offline.urls.clear()
        self.m.reset_view((13.5, 49.05), 13)
        self.assertTrue(requested("waymarkedtrails.org/hiking/"), "pěší trasy v krajině")
        self.assertTrue(requested("waymarkedtrails.org/cycling/"), "cyklotrasy v krajině")
        self.js("setBaseLayer('standard')")

    def test_12_built_styles_pass_maplibre_validation(self):
        """Styl po úpravách (české názvy, pastel, 3D budovy, satelit, zeměkoule, terén) MapLibre
        skutečně načte – jediná neplatná hodnota by shodila celý podklad."""
        self._inject()
        for name in ("standard", "satellite", "outdoor"):
            self.js(f"setBaseLayer('{name}'); mapErrors.length = 0; 0")
            self.assertTrue(self.m.wait_style(), f"{name}: styl prošel validací")
            errors = [e for e in (self.js("getMapErrors()") or []) if "layers" in e or "expected" in e.lower()]
            self.assertEqual(errors, [], name)
            self.assertEqual(self.js("map.style.projection.name"), "globe", name)
            self.assertTrue(self.js("!!map.getSource('dem')"), f"{name}: zdroj terénu")
            if name != "outdoor":
                self.assertTrue(self.js("map.getLayer('label_city') !== undefined"), name)
        self.js("setBaseLayer('standard')")

    def test_13_max_zoom_follows_layer(self):
        for name, z in (("outdoor", 17), ("standard", 19), ("topo", 17), ("satellite", 19)):
            self.js(f"setBaseLayer('{name}'); 0")
            self.assertEqual(self.js("map.getMaxZoom()"), z, name)
        self.js("setBaseLayer('standard')")

    def test_08_only_allowed_hosts_requested(self):
        self._inject()
        for name in ("standard", "satellite", "outdoor"):
            self.js(f"setBaseLayer('{name}'); 0")
            self.m.reset_view((13.5, 49.05), 11)
            wait(300)
        remote = [u for u in self.m.offline.urls if not u.startswith(("file:", "qrc:", "data:", "blob:"))]
        self.assertTrue(remote, "podklady se vyžádaly")
        self.assertTrue(all(main.is_allowed_web_url(u) for u in remote),
                        [u for u in remote if not main.is_allowed_web_url(u)])


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestVectorDeferredAndCached(unittest.TestCase):
    """Patch 12: skrytá záložka nevytváří WebGL mapu; uložený styl = vektor hned po startu."""

    def setUp(self):
        from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings
        self.offline = _offline_interceptor()
        self.profile = QWebEngineProfile()            # vlastní (paměťový) localStorage na test
        self.profile.setUrlRequestInterceptor(self.offline)
        self.page = QWebEnginePage(self.profile)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.view = None
        _load(self.page)

    def tearDown(self):
        if self.view is not None:
            self.view.hide()
            self.view.deleteLater()
        else:
            self.page.deleteLater()
        wait(50)

    def js(self, code):
        return _js(self.page, code)

    def _show(self):
        self.view = QWebEngineView()
        self.view.setPage(self.page)
        self.view.resize(800, 500)
        self.view.show()
        for _ in range(50):
            if self.js("isMapCreated()"):
                break
            wait(100)
        wait(200)

    def test_hidden_tab_defers_map_until_shown(self):
        self.assertFalse(self.js("hasSize()"), "stránka bez okna = skrytá záložka")
        self.assertTrue(self.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})"))
        self.assertFalse(self.js("isMapCreated()"))
        self.assertEqual(self.js("getBaseLayerEngine()"), "none")
        self.assertEqual(self.js("document.querySelectorAll('.maplibregl-canvas').length"), 0,
                         "žádný WebGL kontext ani shadery při startu aplikace")
        markers = [{"id": 1, "lat": 49.2, "lon": 13.4, "name": "a", "city": "", "date": "", "thumb": ""}]
        self.assertEqual(self.js(f"setMarkers({json.dumps(markers)})"), 1)
        self.assertEqual(self.js("getClusterCount()"), 1, "shluky se spočítají i bez mapy")
        self.assertFalse(self.js("isFitted()"), "úvodní pohled až na viditelné mapě")
        self._show()
        self.assertTrue(self.js("isMapCreated()"), "po zobrazení se mapa dostaví sama")
        self.assertEqual(self.js("getBaseLayerEngine()"), "vector")
        self.assertTrue(self.js("isFitted()"))

    def test_cached_style_mounts_vector_without_network(self):
        self.js(f"storeStyle({json.dumps(FIXTURE_STYLE)})")
        self._show()
        self.view.page().triggerAction(self.view.page().WebAction.Reload)
        loop = QEventLoop()
        self.page.loadFinished.connect(lambda ok: loop.quit())
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        for _ in range(30):
            if self.js("getBaseLayerEngine()") == "vector":
                break
            wait(100)
        self.assertEqual(self.js("vectorState"), "ready", "styl z minulého spuštění, bez čekání na síť")
        self.assertEqual(self.js("getBaseLayerEngine()"), "vector")
        self.assertEqual(self.js("buildVectorStyle('standard').layers.length"), len(FIXTURE_STYLE["layers"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
