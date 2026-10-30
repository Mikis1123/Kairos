"""Sprint 2 – XMP sidecary: zápis, čtení, zachování cizího obsahu, obnova do DB."""
import os
import sqlite3
import tempfile
import unittest

import xmp_sync

LIGHTROOM_SIDECAR = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/" xmp:Rating="3">
   <dc:subject><rdf:Bag><rdf:li>dovolená</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE media_items (id INTEGER PRIMARY KEY, file_path TEXT, is_favorite INTEGER DEFAULT 0,
                                  latitude REAL, longitude REAL, raw_metadata_json TEXT, gps_source TEXT);
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, is_confirmed INTEGER DEFAULT 0);
        CREATE TABLE face_instances (id INTEGER PRIMARY KEY, media_id INTEGER, person_id INTEGER,
                                     bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL);
    """)
    return conn


class TestXmpFormat(unittest.TestCase):
    def test_round_trip(self):
        st = {"favorite": True, "gps": {"lat": 50.0875, "lon": -14.4213, "source": "borrowed"},
              "faces": [{"name": "Anna", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25}]}
        back = xmp_sync.parse(xmp_sync.build(st, dims=(4032, 3024)))
        self.assertTrue(back["favorite"])
        self.assertEqual(back["rating"], "5")
        self.assertAlmostEqual(back["gps"]["lat"], 50.0875, places=6)
        self.assertAlmostEqual(back["gps"]["lon"], -14.4213, places=6)
        self.assertEqual(back["gps"]["source"], "borrowed")
        f = back["faces"][0]
        self.assertEqual(f["name"], "Anna")
        for k, v in (("x", 0.1), ("y", 0.2), ("w", 0.3), ("h", 0.25)):
            self.assertAlmostEqual(f[k], v, places=5)

    def test_mwg_regions_use_centre_and_normalized_unit(self):
        text = xmp_sync.build({"favorite": False, "gps": None,
                               "faces": [{"name": "Petr", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.4}]})
        self.assertIn('stArea:x="0.300000"', text)
        self.assertIn('stArea:y="0.400000"', text)
        self.assertIn('stArea:unit="normalized"', text)
        self.assertIn("<mwg-rs:Type>Face</mwg-rs:Type>", text)
        self.assertIn("PersonInImage", text)
        self.assertTrue(text.startswith("<?xpacket"))

    def test_gps_text_format(self):
        self.assertEqual(xmp_sync.gps_to_xmp(50.5, True), "50,30.000000N")
        self.assertEqual(xmp_sync.gps_to_xmp(-0.25, False), "0,15.000000W")
        self.assertAlmostEqual(xmp_sync.gps_from_xmp("50,30.0N"), 50.5)
        self.assertAlmostEqual(xmp_sync.gps_from_xmp("0,15,0W"), -0.25)

    def test_foreign_content_and_rating_preserved(self):
        st = {"favorite": True, "gps": None, "faces": [{"name": "Eva", "x": 0, "y": 0, "w": 0.1, "h": 0.1}]}
        text = xmp_sync.build(st, LIGHTROOM_SIDECAR)
        self.assertIn("dovolená", text, "cizí klíčová slova zůstanou")
        back = xmp_sync.parse(text)
        self.assertEqual(back["rating"], "3", "hodnocení z Lightroomu oblíbení nepřepíše")
        self.assertTrue(back["favorite"])
        # odebrání všech dat Kairos: cizí obsah zůstane, náš zmizí
        text2 = xmp_sync.build(xmp_sync.empty_state(), text)
        self.assertIn("dovolená", text2)
        self.assertNotIn("<mwg-rs:Regions", text2)
        self.assertEqual(xmp_sync.parse(text2)["faces"], [])
        self.assertEqual(xmp_sync.parse(text2)["rating"], "3")
        self.assertFalse(xmp_sync.parse(text2)["favorite"])

    def test_owned_rating_removed_on_unfavorite(self):
        text = xmp_sync.build({"favorite": True, "gps": None, "faces": []})
        self.assertIsNone(xmp_sync.build(xmp_sync.empty_state(), text), "nic nezbylo -> sidecar se smaže")

    def test_invalid_xml(self):
        self.assertIsNone(xmp_sync.parse("<nope"))


class TestXmpFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.photo = os.path.join(self.dir.name, "IMG_1.HEIC")
        open(self.photo, "wb").close()

    def tearDown(self):
        self.dir.cleanup()

    def test_sidecar_name_avoids_live_photo_collision(self):
        self.assertEqual(xmp_sync.sidecar_path(self.photo), self.photo + ".xmp")

    def test_no_file_without_curated_data(self):
        self.assertEqual(xmp_sync.write_sidecar(self.photo, xmp_sync.empty_state()), "skipped")
        self.assertFalse(os.path.exists(self.photo + ".xmp"))

    def test_write_then_remove(self):
        st = xmp_sync.empty_state()
        st["favorite"] = True
        self.assertEqual(xmp_sync.write_sidecar(self.photo, st), "written")
        self.assertEqual(xmp_sync.write_sidecar(self.photo, st), "skipped", "beze změny se nepřepisuje")
        self.assertTrue(xmp_sync.read_sidecar(self.photo)["favorite"])
        self.assertEqual(xmp_sync.write_sidecar(self.photo, xmp_sync.empty_state()), "removed")
        self.assertFalse(os.path.exists(self.photo + ".xmp"))

    def test_unreadable_foreign_file_is_left_alone(self):
        with open(self.photo + ".xmp", "wb") as f:
            f.write(b"\xff\xfe\x00binary")
        st = xmp_sync.empty_state()
        st["favorite"] = True
        self.assertEqual(xmp_sync.write_sidecar(self.photo, st), "skipped")

    def test_db_sync_and_restore_after_database_loss(self):
        conn = make_db()
        conn.execute("INSERT INTO media_items (id, file_path, is_favorite, latitude, longitude, gps_source, "
                     "raw_metadata_json) VALUES (1, ?, 1, 49.2, 16.6, 'borrowed', '{\"ImageWidth\": 400, \"ImageHeight\": 300}')",
                     (self.photo,))
        conn.execute("INSERT INTO people (id, name) VALUES (5, 'Anna'), (6, 'Neznámá osoba 3')")
        conn.execute("INSERT INTO face_instances VALUES (10, 1, 5, 0.1, 0.1, 0.2, 0.3), (11, 1, 6, 0.6, 0.1, 0.2, 0.3)")
        self.assertEqual(xmp_sync.sync_media(conn, [1])["written"], 1)
        st = xmp_sync.read_sidecar(self.photo)
        self.assertEqual([f["name"] for f in st["faces"]], ["Anna"], "nepojmenované osoby se nezapisují")

        # databáze ztracena: nový import + detekce tváří s mírně jinými rámečky
        fresh = make_db()
        fresh.execute("INSERT INTO media_items (id, file_path) VALUES (7, ?)", (self.photo,))
        self.assertEqual(xmp_sync.restore_media(fresh, [7]), 1)
        fav, lat, lon, src = fresh.execute("SELECT is_favorite, latitude, longitude, gps_source FROM media_items").fetchone()
        self.assertEqual(fav, 1)
        self.assertAlmostEqual(lat, 49.2, places=5)
        self.assertAlmostEqual(lon, 16.6, places=5)
        self.assertEqual(src, "borrowed")
        fresh.execute("INSERT INTO people (id, name) VALUES (1, 'Neznámá osoba 1')")
        fresh.execute("INSERT INTO face_instances VALUES (20, 7, 1, 0.11, 0.1, 0.19, 0.31), (21, 7, NULL, 0.6, 0.1, 0.2, 0.3)")
        self.assertEqual(xmp_sync.apply_faces(fresh, 7), 1)
        name = fresh.execute("SELECT p.name, p.is_confirmed FROM face_instances f JOIN people p ON p.id = f.person_id "
                             "WHERE f.id = 20").fetchone()
        self.assertEqual(name, ("Anna", 1))
        self.assertIsNone(fresh.execute("SELECT person_id FROM face_instances WHERE id = 21").fetchone()[0])
        self.assertEqual(xmp_sync.apply_faces(fresh, 7), 0, "opakování nic nemění")

    def test_restore_never_overrides_db(self):
        st = xmp_sync.empty_state()
        st["gps"] = {"lat": 1.0, "lon": 2.0, "source": "manual"}
        xmp_sync.write_sidecar(self.photo, st)
        conn = make_db()
        conn.execute("INSERT INTO media_items (id, file_path, latitude, longitude) VALUES (1, ?, 10, 20)", (self.photo,))
        self.assertEqual(xmp_sync.restore_media(conn, [1]), 0)
        self.assertEqual(conn.execute("SELECT latitude FROM media_items").fetchone()[0], 10)

    def test_writer_coalesces_and_flushes(self):
        path = os.path.join(self.dir.name, "lib.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE media_items (id INTEGER PRIMARY KEY, file_path TEXT, is_favorite INTEGER,
                                      latitude REAL, longitude REAL, raw_metadata_json TEXT);
            CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE face_instances (id INTEGER PRIMARY KEY, media_id INTEGER, person_id INTEGER,
                                         bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL);""")
        conn.execute("INSERT INTO media_items (id, file_path, is_favorite) VALUES (1, ?, 1)", (self.photo,))
        conn.commit()
        conn.close()
        w = xmp_sync.SidecarWriter(lambda: sqlite3.connect(path))
        w.mark([1, 1, 1])
        self.assertEqual(w.pending(), 1)
        self.assertEqual(w.flush(), {"written": 1, "removed": 0, "skipped": 0})
        self.assertEqual(w.pending(), 0)
        self.assertTrue(os.path.exists(self.photo + ".xmp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
