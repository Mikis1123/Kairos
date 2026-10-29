"""Fáze 1 – Hledání 2.0: plánovač dotazu, fazety, dvě pásma, slovník, původ embeddingů.

Kalibrace na skutečné knihovně se ověřuje skriptem eval/eval_search.py (zlatá sada);
tady jsou deterministické testy chování bez modelu.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest import mock

import numpy as np

import ai_engine
import database
import search_engine as se
from search_lexicon import english_for


def _vocab(items, places=None, people=None):
    places = places or {}
    for it in items.values():
        for name in (it.city, it.event_place):
            if name:
                places.setdefault(name, name.title())
    return se.Vocab(places, people or {}, items)


def _item(mid, city="", event="", day="2026-07-10", mtype="photo", fav=False, face=False, persons=()):
    return se.Item(mid, se.fold(city), se.fold(event), day, mtype, fav, face, frozenset(persons))


class TestNormalization(unittest.TestCase):

    def test_fold(self):
        self.assertEqual(se.fold("Horažďovice"), "horazdovice")
        self.assertEqual(se.fold("Horazd'ovice"), "horazdovice")
        self.assertEqual(se.tokens("Kašperské Hory!"), ["kasperske", "hory"])

    def test_czech_inflection_matches(self):
        for q, c in (("kasperskych", "kasperske"), ("hor", "hory"), ("rude", "ruda"),
                     ("zelezne", "zelezna"), ("susici", "susice")):
            self.assertTrue(se._same_word(q, c), (q, c))

    def test_short_prefix_is_not_a_match(self):
        self.assertFalse(se._same_word("hory", "horazdovice"))
        self.assertFalse(se._same_word("les", "lesnice"))


class TestPlanner(unittest.TestCase):

    def setUp(self):
        self.vocab = _vocab({1: _item(1, city="Kasperske Hory"), 2: _item(2, city="Zelezna Ruda"),
                             3: _item(3, city="Horazd'ovice")},
                            people={"jana novakova": {7}})
        self.today = date(2026, 10, 29)

    def plan(self, q):
        return se.plan_query(q, self.vocab, today=self.today)

    def test_place_in_any_case_form(self):
        for q in ("Kašperské Hory", "v Kašperských Horách", "kasperske hory"):
            p = self.plan(q)
            self.assertEqual(p.places, ["kasperske hory"], q)
            self.assertEqual(p.text, "", q)

    def test_common_word_is_not_a_place(self):
        p = self.plan("hory")
        self.assertEqual(p.places, [])
        self.assertEqual(p.text, "hory")

    def test_day_month_year(self):
        self.assertEqual(self.plan("10. července").days, {"2026-07-10"})
        self.assertEqual(self.plan("10. 7. 2025").days, {"2025-07-10"})
        self.assertEqual(self.plan("12. prosince").days, {"2025-12-12"}, "budoucí den = loni")
        self.assertEqual(self.plan("červenec 2026").months, {7})
        self.assertEqual(self.plan("červenec 2026").years, {2026})
        self.assertEqual(self.plan("léto").months, {6, 7, 8})
        self.assertEqual(self.plan("včera").days, {"2026-10-28"})

    def test_type_flag_people_and_remainder(self):
        p = self.plan("videa u jezera")
        self.assertEqual(p.media_types, {"video"})
        self.assertEqual(p.text, "u jezera", "zbytek v původním zápisu s diakritikou")
        self.assertTrue(self.plan("oblíbené").favorites)
        self.assertTrue(self.plan("lidi").people)
        self.assertEqual(self.plan("lidi").text, "")
        self.assertEqual(self.plan("fotky z Kašperských Hor").text, "", "jen výplňová slova")

    def test_named_person(self):
        p = self.plan("Jana Nováková u vody")
        self.assertEqual(p.person_ids, {7})
        self.assertEqual(p.text, "u vody")


class TestRunSearch(unittest.TestCase):

    def setUp(self):
        self.items = {
            1: _item(1, city="Kasperske Hory", event="Kasperske Hory", mtype="video"),
            2: _item(2, city="Hartmanice", event="Kasperske Hory"),
            3: _item(3, city="Frauenau", event="Frauenau", face=True),
            4: _item(4, city="Frauenau", event="Frauenau", day="2026-07-11", face=True),
            5: _item(5, city="Frauenau", event="Frauenau", day="2026-07-11"),
        }
        self.vocab = _vocab(self.items)
        self.universe = [5, 4, 3, 2, 1]
        self.calls = []

    def semantic(self, conf, poss=()):
        def fn(text, pool, emb, faces):
            self.calls.append((text, list(pool), faces))
            return [m for m in conf if m in pool], [m for m in poss if m in pool]
        return fn

    def run_q(self, q, fn=None):
        return se.run_search(q, self.universe, {}, vocab=self.vocab, semantic_fn=fn or self.semantic([]),
                             today=date(2026, 10, 29))

    def test_facet_only_needs_no_model(self):
        res = self.run_q("Kašperské Hory")
        self.assertEqual(res.confident, [1], "pořízeno v místě = jisté")
        self.assertEqual(res.possible, [2], "jen výlet pojmenovaný po místě = možné")
        self.assertEqual(self.calls, [], "fazety bez modelu")
        self.assertFalse(res.semantic)

    def test_facets_filter_semantic_pool(self):
        res = self.run_q("videa Kašperské Hory")
        self.assertEqual(res.confident, [1])
        res = self.run_q("11. července u jezera", self.semantic([5, 3, 4]))
        self.assertEqual(self.calls[-1][1], [5, 4], "obraz jen v rámci fazety dne")
        self.assertEqual(res.confident, [5, 4])

    def test_people_word_uses_faces(self):
        res = self.run_q("lidi", self.semantic([4], [5]))
        self.assertEqual(res.confident, [4, 3], "tvář = jistá shoda, řazeno podle obrazu")
        self.assertEqual(res.possible, [5])
        self.assertEqual(self.calls[-1][0], "lidé")

    def test_people_with_text_gets_face_bonus_set(self):
        self.run_q("lidi u jezera", self.semantic([3]))
        text, pool, faces = self.calls[-1]
        self.assertEqual(text, "lidé u jezera")
        self.assertEqual(faces, {3, 4})

    def test_universe_limits_results(self):
        self.universe = [5]
        res = self.run_q("Frauenau")
        self.assertEqual(res.ids, [5], "hledá se jen v aktuální kolekci")

    def test_empty_inputs(self):
        self.assertTrue(se.run_search("", self.universe, {}, vocab=self.vocab).is_empty())
        self.assertTrue(se.run_search("les", [], {}, vocab=self.vocab).is_empty())


class TestLexicon(unittest.TestCase):

    def test_english_for_inflected_words(self):
        e = lambda w: english_for(w.split(), se.fold, se._same_word)
        self.assertEqual(e("les"), "forest")
        self.assertEqual(e("jezera"), "lake")
        self.assertEqual(e("mraků"), "cloud")
        self.assertEqual(e("rozhledna"), "observation tower")
        self.assertEqual(e("xyzzy"), "")

    def test_query_vector_ensembles_english(self):
        seen = []

        class Model:
            def encode(self, texts, **_):
                seen.append(list(texts))
                return np.eye(4, dtype=np.float32)[:len(texts)]
        v = ai_engine.query_vector("les", Model())
        self.assertEqual(seen[-1], ["les", "forest"])
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=5)
        self.assertAlmostEqual(float(v[0]), float(v[1]), places=5, msg="oba texty stejnou vahou")


class TestTiers(unittest.TestCase):

    def test_possible_capped(self):
        zi = np.full(100, 1.5, np.float32)
        zi[0] = 3.0
        conf, poss = ai_engine.tier_scores(zi, np.full(100, 1.2, np.float32), zi, 100)
        self.assertEqual(conf, [0])
        self.assertEqual(len(poss), ai_engine.SEARCH_POSSIBLE_MAX)

    def test_thresholds_grow_with_library_size(self):
        zi = np.array([2.7, 0.0], np.float32)
        zl = np.array([2.0, 0.0], np.float32)
        self.assertEqual(ai_engine.tier_scores(zi, zl, zi, 300)[0], [0])
        self.assertEqual(ai_engine.tier_scores(zi, zl, zi, 100000)[0], [], "100k fotek: přísnější")


class TestEmbeddingProvenance(unittest.TestCase):
    """Embeddingy fotek byly v knihovně posunuté o desítky id -> nový podpis zdroje + přepočet."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_test_emb_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        c = database.get_connection()
        for i, (path, size, mtime) in enumerate((("a.jpg", 10, 100.4), ("b.jpg", 20, 200.0),
                                                  ("c.jpg", 30, 300.0), ("d.jpg", 40, 400.0)), 1):
            c.execute("INSERT INTO media_items (id, file_path, file_size, file_mtime, media_type) VALUES (?,?,?,?,?)",
                      (i, path, size, mtime, "photo"))
        sig = lambda p, s, m: f"{p}|{s}|{int(m)}"
        c.execute("INSERT INTO image_embeddings (media_id, embedding, source_sig) VALUES (2, x'00', ?)",
                  (sig("b.jpg", 20, 200.0),))                                   # platný
        c.execute("INSERT INTO image_embeddings (media_id, embedding) VALUES (3, x'00')")   # bez podpisu
        c.execute("INSERT INTO image_embeddings (media_id, embedding, source_sig) VALUES (4, x'00', ?)",
                  (sig("x.jpg", 40, 400.0),))                                   # z jiného souboru
        c.commit()
        c.close()

    def tearDown(self):
        database.DB_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_unsigned_and_foreign_are_refreshed(self):
        rows = database.get_media_needing_embedding()
        self.assertEqual(sorted(r[0] for r in rows), [1, 3, 4])
        sig = {r[0]: r[4] for r in rows}
        self.assertEqual(sig[1], "a.jpg|10|100")
        database.save_image_embedding(1, b"\x01", sig[1])
        self.assertEqual(sorted(r[0] for r in database.get_media_needing_embedding()), [3, 4])

    def test_changed_file_invalidates(self):
        c = database.get_connection()
        c.execute("UPDATE media_items SET file_mtime = 999 WHERE id = 2")
        c.commit()
        c.close()
        self.assertIn(2, [r[0] for r in database.get_media_needing_embedding()])

    def test_motion_clip_excluded(self):
        c = database.get_connection()
        c.execute("INSERT INTO media_items (id, file_path, media_type) VALUES (5, 'clip.mov', 'video')")
        c.execute("UPDATE media_items SET paired_video_path = 'clip.mov' WHERE id = 1")
        c.commit()
        c.close()
        self.assertNotIn(5, [r[0] for r in database.get_media_needing_embedding()])


class TestLoadVocab(unittest.TestCase):

    def test_vocab_from_db(self):
        tmp = tempfile.mkdtemp(prefix="omni_test_vocab_")
        saved = database.DB_PATH
        try:
            database.DB_PATH = os.path.join(tmp, "photos.db")
            database.init_db()
            c = database.get_connection()
            c.execute("INSERT INTO events (id, title, location_name) VALUES (1, 'Výlet', 'Kasperske Hory')")
            c.execute("INSERT INTO media_items (id, file_path, media_type, place_city, event_id, date_taken) "
                      "VALUES (1, 'a.jpg', 'photo', 'Hartmanice', 1, '2026:07:10 10:00:00')")
            c.execute("INSERT INTO people (id, name) VALUES (5, 'Jana'), (6, 'Neznámá osoba')")
            c.execute("INSERT INTO face_instances (media_id, person_id, embedding) VALUES (1, 5, x'00')")
            c.commit()
            v = se.load_vocab(c)
            c.close()
            self.assertEqual(set(v.places), {"hartmanice", "kasperske hory"})
            self.assertEqual(v.people, {"jana": {5}}, "neznámé osoby nejsou jména")
            it = v.items[1]
            self.assertEqual((it.day, it.has_face, it.person_ids), ("2026-07-10", True, frozenset({5})))
        finally:
            database.DB_PATH = saved
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
