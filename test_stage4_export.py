"""Stage 4 – čistý soukromý export (privacy_export.py): odstranění polohy a jmen osob ověřené
skutečným ExifToolem na kopiích, ZIP a offline webová prezentace. Originály se nesmí změnit."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile

import privacy_export as pe
from paths import EXIFTOOL_PATH, FFMPEG_PATH

HAS_ET = os.path.exists(EXIFTOOL_PATH)
HAS_FF = os.path.exists(FFMPEG_PATH)
NAME = "Jana Nováková"


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tag(path, args):
    code, _out, err = pe.run_exiftool(["-q", "-overwrite_original", "-charset", "iptc=utf8", *args], [path])
    assert code == 0, err


def make_tagged_jpeg(path):
    from PIL import Image
    Image.new("RGB", (96, 64), (200, 120, 40)).save(path, quality=90)
    _tag(path, ["-GPSLatitude=50.08", "-GPSLatitudeRef=N", "-GPSLongitude=14.42", "-GPSLongitudeRef=E",
                "-XMP:GPSLatitude=50.08", "-XMP:City=Praha", "-IPTC:City=Praha", "-XMP:Country=Česko",
                "-XMP-iptcExt:LocationShownCity=Praha", f"-XMP-iptcExt:PersonInImage={NAME}",
                f"-XMP-dc:Subject={NAME}", "-XMP-dc:Subject+=moře", f"-IPTC:Keywords={NAME}",
                f"-XMP-lr:HierarchicalSubject=People|{NAME}", "-XMP-mwg-rs:RegionAppliedToDimensionsW=96",
                "-XMP-mwg-rs:RegionAppliedToDimensionsH=64", "-XMP-mwg-rs:RegionAppliedToDimensionsUnit=pixel",
                f"-XMP-mwg-rs:RegionName={NAME}", "-XMP-mwg-rs:RegionType=Face",
                f"-XMP-dc:Description=Narozeniny – {NAME} a dort", "-Make=Apple", "-Model=iPhone 15",
                "-SerialNumber=SN12345", "-DateTimeOriginal=2024:05:03 14:22:33", "-Orientation#=6"])


def make_tagged_mp4(path):
    subprocess.run([FFMPEG_PATH, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path], check=True, capture_output=True)
    _tag(path, ["-Keys:GPSCoordinates=50.08 14.42", "-UserData:GPSCoordinates=50.08 14.42",
                "-XMP:GPSLatitude=50.08", "-Keys:Make=Apple"])


def all_tags(path):
    return pe.read_metadata([path]).get(os.path.normcase(os.path.abspath(path)), {})


class TestPureRules(unittest.TestCase):
    def test_scan_flags_gps_location_regions_and_names(self):
        opts = pe.ExportOptions()
        pats = pe._name_patterns([NAME])
        meta = {"SourceFile": "x", "File:System:FileName": f"{NAME}.jpg",
                "EXIF:GPS:GPSLatitude": "50", "XMP:XMP-photoshop:City": "Praha",
                "XMP:XMP-iptcExt:LocationShownCity": "Praha", "XMP:XMP-mwg-rs:RegionName": NAME,
                "XMP:XMP-dc:Subject": ["moře", "jana novakova"], "EXIF:IFD0:Make": "Apple",
                "Composite:GPSPosition": "50 14", "XMP:XMP-dc:Description": "Dort"}
        found = {k: c for k, c, _v in pe.scan_violations(meta, opts, pats)}
        self.assertEqual(found, {"EXIF:GPS:GPSLatitude": "poloha", "XMP:XMP-photoshop:City": "poloha",
                                 "XMP:XMP-iptcExt:LocationShownCity": "poloha",
                                 "XMP:XMP-mwg-rs:RegionName": "osoby", "XMP:XMP-dc:Subject": "jméno osoby"},
                         "jméno i bez diakritiky; systémové a Composite skupiny se neposuzují; Make zůstává")
        cam = {k for k, _c, _v in pe.scan_violations(meta, pe.ExportOptions(strip_gps=False, strip_people=False,
                                                                           strip_camera=True))}
        self.assertEqual(cam, {"EXIF:IFD0:Make"})

    def test_name_match_is_word_bounded(self):
        pats = pe._name_patterns(["Eva", "Jo"])               # „Jo“ je moc krátké, nehledá se
        self.assertTrue(pe.mentions_name("S Evou? Ne – s EVA!", pats))
        self.assertFalse(pe.mentions_name("Evangelium, Jordánsko", pats))

    def test_remediation_removes_only_offending_list_items(self):
        pats = pe._name_patterns([NAME])
        args = pe.remediation_args([("XMP:XMP-dc:Subject", "jméno osoby", ["moře", NAME]),
                                    ("EXIF:GPS:GPSLatitude", "poloha", "50"),
                                    ("Composite:GPSPosition", "poloha", "50 14"),
                                    ("MakerNotes:Apple:SerialNumber", "zařízení", "1")], pats)
        self.assertEqual(args, [f"-XMP-dc:Subject-={NAME}", "-GPS:GPSLatitude=", "-makernotes:all="])

    def test_neutral_names_unique_and_name_safe(self):
        items = [{"id": 1, "path": r"C:\f\IMG_1.JPG", "date": "2024-05-03 14:22:33"},
                 {"id": 2, "path": r"C:\f\IMG_2.JPG", "date": "2024-05-03 14:22:33"},
                 {"id": 3, "path": rf"C:\f\{NAME} u moře.jpg", "date": None}]
        self.assertEqual(pe.plan_names(items, pe.ExportOptions()),
                         {1: "2024-05-03_14-22-33", 2: "2024-05-03_14-22-33_2", 3: "Kairos_00003"})
        keep = pe.plan_names(items, pe.ExportOptions(neutral_names=False), pe._name_patterns([NAME]))
        self.assertEqual(keep[1], "IMG_1")
        self.assertEqual(keep[3], "Kairos_00003", "název se jménem osoby se i tak nahradí")

    def test_collect_never_returns_hidden_or_trashed(self):
        d = tempfile.mkdtemp()
        try:
            paths = []
            for i in range(4):
                p = os.path.join(d, f"{i}.jpg")
                open(p, "wb").write(b"x")
                paths.append(p)
            conn = sqlite3.connect(":memory:")
            conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, file_path TEXT, file_name TEXT,
                            media_type TEXT, date_taken TEXT, paired_video_path TEXT, place_city TEXT,
                            place_country TEXT, is_hidden INTEGER, is_trashed INTEGER)""")
            conn.executemany("INSERT INTO media_items VALUES (?, ?, ?, 'photo', NULL, NULL, NULL, NULL, ?, ?)",
                             [(1, paths[0], "a", 0, 0), (2, paths[1], "b", 1, 0), (3, paths[2], "c", 0, 1),
                              (4, os.path.join(d, "gone.jpg"), "d", 0, 0)])
            conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("CREATE TABLE face_instances (media_id INTEGER, person_id INTEGER)")
            conn.executemany("INSERT INTO people VALUES (?, ?)", [(1, NAME), (2, "Neznámá osoba 3")])
            conn.executemany("INSERT INTO face_instances VALUES (?, ?)", [(1, 1), (1, 2)])
            items, skipped = pe.collect(conn, [2, 1, 3, 4])
            self.assertEqual([it["id"] for it in items], [1])
            self.assertEqual(items[0]["people"], [NAME])
            self.assertEqual({r for _n, r in skipped}, {"skrytá položka", "v koši", "soubor nenalezen"})
            self.assertEqual(pe.person_names(conn), [NAME])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_showcase_has_no_external_references(self):
        page = pe.build_showcase_html("Léto </script>", [{"f": "media/a.jpg", "t": "thumbs/a.jpg", "v": "",
                                                         "c": "3. května 2024 </script>", "w": 1, "h": 1}])
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("</script>\",", page, "data nesmí ukončit <script>")
        self.assertIn("Léto &lt;/script&gt;", page)


@unittest.skipUnless(HAS_ET and HAS_FF, "ExifTool / ffmpeg nejsou k dispozici")
class TestRealExport(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kairos-test-export-")
        self.src = os.path.join(self.root, "src")
        os.makedirs(self.src)
        self.jpg = os.path.join(self.src, f"{NAME} narozeniny.jpg")
        self.mp4 = os.path.join(self.src, "IMG_0002.mp4")
        self.mov = os.path.join(self.src, "IMG_0001_live.mp4")
        make_tagged_jpeg(self.jpg)
        make_tagged_mp4(self.mp4)
        make_tagged_mp4(self.mov)
        self.before = {p: _sha(p) for p in (self.jpg, self.mp4, self.mov)}
        self.items = [
            {"id": 1, "path": self.jpg, "name": "a", "type": "photo", "date": "2024-05-03 14:22:33",
             "paired": self.mov, "city": "Praha", "country": "Česko", "people": [NAME]},
            {"id": 2, "path": self.mp4, "name": "b", "type": "video", "date": "2024-05-04 09:00:00",
             "paired": None, "city": "Brno", "country": "Česko", "people": []},
        ]
        self.out = os.path.join(self.root, "out")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _assert_clean(self, files, camera=False):
        pats = pe._name_patterns([NAME])
        opts = pe.ExportOptions(strip_camera=camera)
        for f in files:
            tags = all_tags(f)
            self.assertTrue(tags, f"ExifTool soubor přečetl: {f}")
            self.assertEqual(pe.scan_violations(tags, opts, pats), [], f)
            blob = json.dumps({k: v for k, v in tags.items() if not k.startswith(("File:", "SourceFile"))},
                              ensure_ascii=False)
            self.assertNotIn("Nováková", blob)
            self.assertNotIn("Praha", blob)

    def _assert_originals_untouched(self):
        for p, h in self.before.items():
            self.assertEqual(_sha(p), h, f"originál změněn: {p}")
        self.assertIn("50 deg", json.dumps(all_tags(self.jpg)), "originál si GPS nechává")

    def test_folder_export_strips_and_verifies(self):
        res = pe.export(self.items, pe.ExportOptions(title="Léto"), os.path.join(self.out, "Léto"), [NAME])
        self.assertEqual((res.exported, res.files, res.excluded), (2, 3, []))
        self.assertEqual(res.verified, 3)
        files = sorted(os.listdir(res.path))
        self.assertEqual(files, ["2024-05-03_14-22-33.jpg", "2024-05-03_14-22-33.mp4", "2024-05-04_09-00-00.mp4"])
        self._assert_clean([os.path.join(res.path, f) for f in files])
        kept = all_tags(os.path.join(res.path, files[0]))
        self.assertEqual(kept.get("XMP:XMP-dc:Subject"), "moře", "ostatní klíčová slova zůstanou")
        self.assertEqual(kept.get("EXIF:IFD0:Make"), "Apple", "zařízení se bez volby nemaže")
        self.assertEqual(kept.get("EXIF:ExifIFD:DateTimeOriginal"), "2024:05:03 14:22:33")
        self.assertEqual(kept.get("EXIF:IFD0:Orientation"), "Rotate 90 CW")
        self._assert_originals_untouched()
        self.assertEqual([n for n in os.listdir(self.out) if n.startswith(".kairos-export-")], [], "staging uklizen")

    def test_camera_option_and_description_with_name(self):
        res = pe.export(self.items[:1], pe.ExportOptions(strip_camera=True, include_live=False),
                        os.path.join(self.out, "x"), [NAME])
        f = os.path.join(res.path, os.listdir(res.path)[0])
        self._assert_clean([f], camera=True)
        tags = all_tags(f)
        self.assertNotIn("EXIF:IFD0:Make", tags)
        self.assertNotIn("XMP:XMP-dc:Description", tags, "popis se jménem osoby odstraněn nápravou")
        self.assertGreaterEqual(res.remediated, 1)

    def test_zip_package(self):
        res = pe.export(self.items, pe.ExportOptions(package="zip"), os.path.join(self.out, "Léto"), [NAME])
        self.assertTrue(res.path.endswith(".zip") and os.path.isfile(res.path))
        unz = os.path.join(self.root, "unz")
        with zipfile.ZipFile(res.path) as z:
            names = z.namelist()
            self.assertTrue(all(n.startswith("Léto/") for n in names), names)
            z.extractall(unz)
        files = [os.path.join(dp, f) for dp, _d, fs in os.walk(unz) for f in fs]
        self.assertEqual(len(files), 3)
        self._assert_clean(files)
        self.assertFalse(os.path.exists(res.path + ".part"))

    def test_web_showcase(self):
        res = pe.export(self.items, pe.ExportOptions(layout="web", title="Léto 2024"), os.path.join(self.out, "w"), [NAME])
        self.assertEqual(res.exported, 2)
        page = open(os.path.join(res.path, "index.html"), encoding="utf-8").read()
        self.assertNotIn("Nováková", page)
        self.assertNotIn("Praha", page, "místo se při odstranění polohy nezobrazí ani v popisku")
        self.assertIn("3. května 2024", page)
        data = json.loads(page.split('type="application/json">')[1].split("</script>")[0])
        self.assertEqual(data[0]["f"], "media/2024-05-03_14-22-33.jpg")
        self.assertEqual(data[1]["v"], "media/2024-05-04_09-00-00.mp4")
        for e in data:
            for k in ("f", "t", "v"):
                if e[k]:
                    self.assertTrue(os.path.isfile(os.path.join(res.path, e[k])), e[k])
        from PIL import Image
        with Image.open(os.path.join(res.path, data[0]["f"])) as im:
            self.assertEqual(im.size, (64, 96), "orientace z EXIF zapečená")
            self.assertNotIn("exif", im.info)
        files = [os.path.join(dp, f) for dp, _d, fs in os.walk(res.path) for f in fs if not f.endswith(".html")]
        self._assert_clean(files)

    def test_people_kept_when_option_off(self):
        res = pe.export(self.items[:1], pe.ExportOptions(strip_people=False, include_live=False, neutral_names=False),
                        os.path.join(self.out, "p"), [NAME])
        f = os.path.join(res.path, os.listdir(res.path)[0])
        self.assertEqual(os.path.basename(f), f"{NAME} narozeniny.jpg")
        self.assertEqual(all_tags(f).get("XMP:XMP-iptcExt:PersonInImage"), NAME, "diakritika beze změny")
        self.assertNotIn("EXIF:GPS:GPSLatitude", all_tags(f))

    def test_unremovable_metadata_is_excluded_fail_closed(self):
        orig = pe.strip_args
        orig_fix = pe.remediation_args
        try:
            pe.strip_args = lambda opts, names=(): []                   # simulace: nic nejde smazat
            pe.remediation_args = lambda v, p=(): []
            with self.assertRaises(pe.ExportError):
                pe.export(self.items[:1], pe.ExportOptions(include_live=False), os.path.join(self.out, "f"), [NAME])
        finally:
            pe.strip_args, pe.remediation_args = orig, orig_fix
        self.assertFalse(os.path.exists(os.path.join(self.out, "f")))
        self.assertEqual(os.listdir(self.out), [], "po selhání žádný staging")

    def test_cancel_cleans_up(self):
        with self.assertRaises(pe.ExportCancelled):
            pe.export(self.items, pe.ExportOptions(), os.path.join(self.out, "c"), [NAME], cancelled=lambda: True)
        self.assertEqual(os.listdir(self.out), [])

    def test_missing_exiftool_refuses(self):
        with self.assertRaises(pe.ExportError):
            pe.export(self.items, pe.ExportOptions(), os.path.join(self.out, "m"), exiftool=os.path.join(self.root, "nic.exe"))


if __name__ == "__main__":
    unittest.main()
