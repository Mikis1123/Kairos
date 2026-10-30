"""Sprint 1 – mapa bez děr: rastrový podklad pod vektory, film bez přenačtení zdrojů,
zpomalení zoomu na hranicích úrovní dlaždic.

Testy NESAHAJÍ na síť ani na photos.db (stránka s podstrčeným vektorovým stylem, síť blokovaná).
"""
import json
import sys
import unittest

HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    from test_patch11_maps import FIXTURE_STYLE, ShownMapPage, wait
    from test_phase2_film import FILM_TRIP

# Spy na Style._reloadSource: které zdroje MapLibre při změně stylu přenačetl
RELOAD_SPY = """(function () {
  window.__reloads = [];
  if (!map.style.__spied) {
    var o = map.style._reloadSource.bind(map.style);
    map.style._reloadSource = function (id) { window.__reloads.push(id); return o(id); };
    map.style.__spied = true;
  }
  return 0; })()"""


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestZeroVoidMap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1400, 900))
        cls.m.wait_style()
        cls.m.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})")
        cls.m.wait_style()

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code):
        return self.m.js(code)

    def tearDown(self):
        self.js("window.endJourney(true); 0")

    def layer_ids(self):
        return self.js("map.getStyle().layers.map(function (l) { return l.id; })")

    # ── rastrový podklad ──
    def test_underlay_sits_between_background_and_vector_fills(self):
        ids = self.layer_ids()
        self.assertIn("underlay", ids)
        self.assertLess(ids.index("background"), ids.index("underlay"))
        self.assertLess(ids.index("underlay"), ids.index("park"), "podklad musí být pod vektorovými plochami")
        src = self.js("(function () { var s = map.getStyle().sources.underlay; return [s.type, s.maxzoom, s.tiles[0]]; })()")
        self.assertEqual(src[0], "raster")
        self.assertEqual(src[1], 9)
        self.assertIn("World_Light_Gray_Base", src[2])

    def test_underlay_blends_into_land_colour(self):
        # pevnina Esri (239) přes pozadí Liberty #F8F4F0 při dané průhlednosti: rozdíl ≤ 5 úrovní
        a = self.js("UNDERLAY_OPACITY")
        for bg in (0xF8, 0xF4, 0xF0):
            self.assertLessEqual(abs(a * 239 + (1 - a) * bg - bg), 5)

    def test_underlay_holds_in_film_and_fades_in_detail_outside(self):
        rest = self.js("map.getPaintProperty('underlay', 'raster-opacity')")
        self.assertIsInstance(rest, list, "mimo film průhlednost podle zoomu (pobřeží z9 v detailu)")
        self.assertTrue(self.js(f"playJourney({json.dumps(FILM_TRIP)})"))
        self.js("journeyPause(); 0")
        self.assertEqual(self.js("map.getPaintProperty('underlay', 'raster-opacity')"), self.js("UNDERLAY_OPACITY"))
        self.js("window.endJourney(true); 0")
        self.assertIsInstance(self.js("map.getPaintProperty('underlay', 'raster-opacity')"), list)

    def test_underlay_can_be_switched_off(self):
        self.js("PERF.map_underlay = false; setVectorStyle(" + json.dumps(FIXTURE_STYLE) + "); 0")
        self.m.wait_style()
        try:
            self.assertNotIn("underlay", self.layer_ids())
        finally:
            self.js("PERF.map_underlay = true; setVectorStyle(" + json.dumps(FIXTURE_STYLE) + "); 0")
            self.m.wait_style()

    # ── film bez přenačtení zdroje ──
    def test_film_start_does_not_reload_vector_source(self):
        self.js(RELOAD_SPY)
        self.assertTrue(self.js(f"playJourney({json.dumps(FILM_TRIP)})"))
        self.js("journeyPause(); map.triggerRepaint(); 0")
        wait(500)                      # pár snímků: Style.update provede naplánovaná přenačtení
        reloads = self.js("window.__reloads")
        self.assertNotIn("openmaptiles", reloads, f"start filmu přenačetl vektory: {reloads}")
        self.assertNotIn("dem-shade", reloads)
        faded = self.js("filmFaded()")
        self.assertIn("poi_r1", faded)
        self.assertIn("road_shield", faded)
        self.assertEqual(self.js("map.getPaintProperty('poi_r1', 'text-opacity')"), 0)
        self.assertEqual(self.js("map.getLayoutProperty('poi_r1', 'visibility')"), None, "viditelnost zůstává")

    def test_film_end_restores_label_opacity(self):
        before = self.js("[map.getPaintProperty('poi_r1', 'text-opacity'), map.getPaintProperty('poi_r1', 'icon-opacity')]")
        self.assertTrue(self.js(f"playJourney({json.dumps(FILM_TRIP)})"))
        self.js("journeyPause(); window.endJourney(true); 0")
        after = self.js("[map.getPaintProperty('poi_r1', 'text-opacity'), map.getPaintProperty('poi_r1', 'icon-opacity')]")
        self.assertEqual(before, after)
        self.assertEqual(self.js("filmFaded()"), [])
        ids = self.layer_ids()
        self.assertIn("relief", ids)
        self.assertLess(ids.index("relief"), ids.index("relief-film"), "reliéf se vrátí na své místo")

    # ── zpomalení na hranicích úrovní ──
    def test_detent_is_monotone_and_slows_level_crossings(self):
        r = self.js("""(function () {
          var z = [], dt = CAM_DT;
          for (var t = 0; t <= 20; t += dt) z.push(t < 4 ? 13.4 : t < 12 ? 13.4 - 4.9 * Math.pow(Math.sin(Math.PI * (t - 4) / 8), 2) : 13.4);
          var keep = ZOOM_DETENT; ZOOM_DETENT = ZOOM_DETENT_ON;
          var w = detentZoom(z), rev = 0, maxDev = 0, at = [], plan = [];
          for (var k = 1; k < z.length; k++) {
            var a = z[k] - z[k - 1], b = w[k] - w[k - 1];
            if (a * b < 0) rev++;
            maxDev = Math.max(maxDev, Math.abs(w[k] - z[k]));
            var mid = (z[k] + z[k - 1]) / 2;
            if (Math.abs(a) / dt > 0.3 && Math.abs(mid - Math.round(mid)) < 0.12) { plan.push(Math.abs(a)); at.push(Math.abs(b)); }
          }
          var sum = function (v) { return v.reduce(function (s, x) { return s + x; }, 0); };
          ZOOM_DETENT = keep;
          return { rev: rev, maxDev: maxDev, ratio: sum(at) / sum(plan), still: w[3] - z[3] };
        })()""")
        self.assertEqual(r["rev"], 0, "zoom se nesmí vracet")
        self.assertLess(r["maxDev"], 0.12)
        self.assertLess(r["ratio"], 0.7, "přechod úrovně musí být pomalejší")
        self.assertEqual(r["still"], 0, "kamera v okamžiku drží plánovaný zoom")

    def test_detent_off_by_default(self):
        self.assertEqual(self.js("ZOOM_DETENT"), 0, "bez naměřeného přínosu – viz komentář v map_vault.html")

    def test_detent_can_be_disabled(self):
        self.assertEqual(self.js("(function () { var a = ZOOM_DETENT; ZOOM_DETENT = 0; var r = detentZoom([1, 1.5, 2.2, 3]); "
                                 "ZOOM_DETENT = a; return r; })()"), [1, 1.5, 2.2, 3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
