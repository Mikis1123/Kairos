"""Sprint 3 – text na fotkách (OCR Windows) a hledání slovem."""
import unittest

import ocr_engine
import search_engine as se


class TestOcrFilter(unittest.TestCase):
    def test_texture_noise_dropped(self):
        for noise in (["sgFF"], ["zg:FI"], ["i,w"], ["liv"], ["lll", "III"], ["6,7,"]):
            self.assertEqual(ocr_engine.clean_text(noise), "", noise)

    def test_real_text_kept(self):
        self.assertEqual(ocr_engine.clean_text(["PETRONAS"]), "PETRONAS")
        self.assertEqual(ocr_engine.clean_text(["VSTUP", "ZAKÁZÁN"]), "VSTUP ZAKÁZÁN")
        self.assertEqual(ocr_engine.clean_text(["Kašperské", "Hory,", "1290"]), "Kašperské Hory 1290")


class TestOcrSearch(unittest.TestCase):
    def vocab(self):
        v = se.Vocab({}, {}, {})
        v.ocr = {1: tuple(se.tokens("VSTUP ZAKÁZÁN")), 2: tuple(se.tokens("Kašperské Hory náměstí")),
                 3: tuple(se.tokens("PETRONAS"))}
        return v

    def test_all_words_must_be_read(self):
        v = self.vocab()
        self.assertEqual(se.ocr_matches("vstup zakázán", v, [1, 2, 3]), [1])
        self.assertEqual(se.ocr_matches("zakázáno vstup", v, [1, 2, 3]), [1], "skloňování")
        self.assertEqual(se.ocr_matches("kasperske namesti", v, [3, 2, 1]), [2], "bez diakritiky")
        self.assertEqual(se.ocr_matches("petro", v, [1, 2, 3]), [3], "začátek slova od 4 písmen")
        self.assertEqual(se.ocr_matches("vstup les", v, [1, 2, 3]), [])
        self.assertEqual(se.ocr_matches("na", v, [1, 2, 3]), [], "jen výplňová slova")
        self.assertEqual(se.ocr_matches("vstup", v, [2, 3]), [], "jen v poolu")

    def test_ocr_hits_lead_the_results(self):
        v = self.vocab()
        v.items = {i: se.Item(i, "", "", "", "photo", False, False, frozenset()) for i in (1, 2, 3, 4)}
        res = se.run_search("petronas", [1, 2, 3, 4], {}, vocab=v,
                            semantic_fn=lambda t, pool, emb, faces: ([4, 3], [2]))
        self.assertEqual(res.confident[:2], [3, 4])
        self.assertIn("text na fotce", res.labels)


@unittest.skipUnless(ocr_engine.available(), "OCR Windows není k dispozici")
class TestWindowsOcr(unittest.TestCase):
    def test_reads_rendered_sign(self):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (900, 260), "white")
        try:
            font = ImageFont.truetype("arial.ttf", 52)
        except OSError:
            self.skipTest("bez písma Arial")
        ImageDraw.Draw(img).text((40, 90), "VSTUP ZAKÁZÁN", fill="black", font=font)
        self.assertIn("VSTUP", ocr_engine.recognize(img).upper())


if __name__ == "__main__":
    unittest.main(verbosity=2)
