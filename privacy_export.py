"""Čistý soukromý export (Stage 4): kopie fotek a videí bez polohy a jmen osob, ověřená ExifToolem.

Originály se nikdy nemění – ExifTool zapisuje jen do kopií ve stagingu (skrytá složka vedle cíle),
hotový export se na konec přesune (složka) nebo zabalí (ZIP) najednou. Zrušení / chyba po sobě
staging smaže.

Postup na každou kopii:
  1. odstranění (strip_args): GPS*, místopis (město / země / LocationShown…), oblasti tváří
     (MWG / Microsoft regions), PersonInImage, klíčová slova se jmény osob; volitelně zařízení
     a autor (Make / Model / sériová čísla / MakerNotes / Artist …)
  2. ověření (scan_violations): ExifTool -a -G0:1 -ee3 přečte VŠECHNY značky včetně vnořených
     (časová metadata videí – GPS stopy dronů a akčních kamer) a hledá, co tam být nesmí,
     včetně jmen osob v libovolné textové hodnotě
  3. náprava: nalezené značky se smažou cíleně (seznam = odebrat jen dané položky), video se
     případně přebalí ffmpegem bez datových stop a metadat; pak znovu ověření
  4. co ani potom neprojde, do exportu NEJDE (fail closed) a zpráva to vyjmenuje

Webová prezentace: index.html bez jediného odkazu ven (inline CSS / JS), fotky jako JPEG
s barevným profilem, ale bez EXIF; videa jako ověřené kopie.

Skryté položky (Stage 3) se neexportují nikdy – ani když je o ně někdo požádá přímo.
"""
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from paths import EXIFTOOL_PATH, FFMPEG_PATH

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".m2ts"}
WEB_VIDEO_EXTS = {".mp4", ".m4v", ".mov"}
WEB_EDGE = 2048
WEB_THUMB = 480
_SKIP_FAMILIES = {"SourceFile", "ExifTool", "File", "System", "Composite"}
_LOCATION_TAGS = {"city", "country", "countrycode", "country-primarylocationname", "country-primarylocationcode",
                  "state", "province-state", "sub-location", "sublocation", "location", "locationname",
                  "worldregion", "countryname", "provincestate"}
_PEOPLE_GROUPS = ("xmp-mwg-rs", "xmp-mp", "xmp-mpreg", "xmp-acdsee-rs", "xmp-mpri")
_CAMERA_TAGS = {"make", "model", "lensmake", "lensmodel", "lens", "lensid", "lensinfo", "software",
                "hostcomputer", "ownername", "cameraownername", "artist", "creator", "by-line", "author",
                "copyright", "rights", "uniquecameramodel", "localizedcameramodel", "androidmanufacturer",
                "androidmodel", "com.android.manufacturer", "com.android.model", "contentidentifier",
                "imageuniqueid", "documentid", "originaldocumentid", "instanceid"}


class ExportError(Exception):
    pass


class ExportCancelled(ExportError):
    pass


@dataclass
class ExportOptions:
    strip_gps: bool = True            # GPS + místopis
    strip_people: bool = True         # jména osob a oblasti tváří
    strip_camera: bool = False        # zařízení, objektiv, sériová čísla, autor
    neutral_names: bool = True        # soubory podle data (původní názvy můžou prozradit víc)
    include_live: bool = True         # pohybové video Live Photo vedle fotky
    layout: str = "files"             # files | web
    package: str = "folder"           # folder | zip
    title: str = "Export"

    @property
    def strips_anything(self):
        return self.strip_gps or self.strip_people or self.strip_camera


@dataclass
class ExportResult:
    path: str = ""
    exported: int = 0                 # položek (fotka + její pohybové video = 1)
    files: int = 0
    verified: int = 0                 # souborů ověřených ExifToolem bez nálezu
    remediated: int = 0               # souborů, které prošly až po nápravě
    excluded: list = field(default_factory=list)       # [(název, důvod)]
    skipped: list = field(default_factory=list)        # [(název, důvod)] – chybějící / skryté

    def summary(self):
        parts = [f"{self.exported} {_cz(self.exported, 'položka', 'položky', 'položek')}"]
        if self.verified:
            parts.append(f"ověřeno ExifToolem: {self.verified} {_cz(self.verified, 'soubor', 'soubory', 'souborů')}")
        if self.excluded:
            parts.append(f"vyřazeno: {len(self.excluded)}")
        return " · ".join(parts)


def _cz(n, one, few, many):
    return one if n == 1 else few if 2 <= n <= 4 else many


def _hidden_startupinfo():
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return info
    return None


def exiftool_available(path=None):
    return os.path.exists(path or EXIFTOOL_PATH)


# ── výběr položek ────────────────────────────────────────────────────────────

def _named(name):
    n = (name or "").strip()
    return bool(n) and not n.lower().startswith(("neznámá osoba", "unknown"))


def person_names(conn):
    """Jména všech pojmenovaných osob – hledají se v metadatech každé kopie (i cizí štítky
    z jiných aplikací můžou nést jméno osoby, kterou Kairos zná)."""
    try:
        rows = conn.execute("SELECT name FROM people").fetchall()
    except Exception:
        return []
    return sorted({r[0].strip() for r in rows if _named(r[0])}, key=str.casefold)


def collect(conn, media_ids):
    """Exportovatelné položky v daném pořadí: jen viditelné (ne koš, ne Skryté)."""
    ids = [int(i) for i in dict.fromkeys(media_ids)]
    if not ids:
        return [], []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()}
    hidden = "COALESCE(is_hidden, 0)" if "is_hidden" in cols else "0"
    trashed = "COALESCE(is_trashed, 0)" if "is_trashed" in cols else "0"
    place = "place_city, place_country" if "place_city" in cols else "NULL, NULL"
    rows = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        for r in conn.execute(f"""SELECT id, file_path, file_name, media_type, date_taken, paired_video_path,
                                         {place}, {hidden}, {trashed}
                                  FROM media_items WHERE id IN ({','.join('?' * len(part))})""", part):
            rows[r[0]] = r
    people = {}
    try:
        for mid, name in conn.execute("""SELECT f.media_id, p.name FROM face_instances f JOIN people p
                                         ON p.id = f.person_id"""):
            if _named(name):
                people.setdefault(mid, set()).add(name.strip())
    except Exception:
        pass
    try:
        import locked_folder
        is_hidden_path = locked_folder.is_hidden_path
    except Exception:
        is_hidden_path = lambda _p: False                       # noqa: E731
    items, skipped = [], []
    for mid in ids:
        r = rows.get(mid)
        if r is None:
            continue
        _, path, name, mtype, date, paired, city, country, is_hidden, is_trashed = r
        label = name or os.path.basename(path or "")
        if is_hidden or is_hidden_path(path):
            skipped.append((label, "skrytá položka"))
            continue
        if is_trashed:
            skipped.append((label, "v koši"))
            continue
        if not path or not os.path.isfile(path):
            skipped.append((label, "soubor nenalezen"))
            continue
        if paired and (not os.path.isfile(paired) or is_hidden_path(paired)):
            paired = None
        items.append({"id": mid, "path": path, "name": label, "type": mtype or _kind(path), "date": date,
                      "paired": paired, "city": city, "country": country,
                      "people": sorted(people.get(mid, ()), key=str.casefold)})
    return items, skipped


def _kind(path):
    return "video" if os.path.splitext(path)[1].lower() in VIDEO_EXTS else "photo"


# ── názvy souborů ────────────────────────────────────────────────────────────

def _fold(text):
    t = unicodedata.normalize("NFKD", str(text)).casefold()
    return "".join(c for c in t if not unicodedata.combining(c))


def _name_patterns(names):
    out = []
    for n in names:
        f = _fold(n).strip()
        if len(f) >= 3:
            out.append(re.compile(r"(?<![0-9a-z])" + re.escape(f) + r"(?![0-9a-z])"))
    return out


def mentions_name(text, patterns):
    if not patterns or text is None:
        return False
    f = _fold(text)
    return any(p.search(f) for p in patterns)


def _safe_stem(stem):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" .")
    return s[:120] or "soubor"


def plan_names(items, opts, patterns=()):
    """{media_id: stem} unikátní v rámci exportu. Neutrální = 2024-05-03_14-22-33; původní název
    se nahradí neutrálním i tehdy, když obsahuje jméno osoby a jména se mají odstranit."""
    used, out = set(), {}
    for i, it in enumerate(items, 1):
        orig = os.path.splitext(os.path.basename(it["path"]))[0]
        neutral = opts.neutral_names or (opts.strip_people and mentions_name(orig, patterns))
        if neutral:
            d = (it.get("date") or "").strip()
            m = re.match(r"(\d{4})[-:](\d{2})[-:](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", d)
            stem = f"{m[1]}-{m[2]}-{m[3]}_{m[4]}-{m[5]}-{m[6]}" if m else f"Kairos_{i:05d}"
        else:
            stem = _safe_stem(orig)
        base, n = stem, 2
        while stem.casefold() in used:
            stem = f"{base}_{n}"
            n += 1
        used.add(stem.casefold())
        out[it["id"]] = stem
    return out


# ── ExifTool ─────────────────────────────────────────────────────────────────

def strip_args(opts, names=()):
    """Argumenty odstranění (ExifTool, -TAG= se zástupnými znaky maže ve všech skupinách)."""
    args = []
    if opts.strip_gps:
        args += ["-GPS*=", "-XMP-iptcExt:LocationShown*=", "-XMP-iptcExt:LocationCreated*=",
                 "-City=", "-Country*=", "-State=", "-Province-State=", "-Sub-location=", "-Location*=",
                 "-XMP-iptcCore:Location=", "-XMP-photoshop:State=", "-XMP-iptcCore:CountryCode="]
    if opts.strip_people:
        args += ["-XMP-mwg-rs:all=", "-XMP-MP:all=", "-XMP-MPReg:all=", "-XMP-acdsee-rs:all=",
                 "-PersonInImage*="]
        for n in names:
            args += [f"-XMP-dc:Subject-={n}", f"-IPTC:Keywords-={n}", f"-XMP-lr:HierarchicalSubject-=People|{n}",
                     f"-XMP-lr:HierarchicalSubject-=Lidé|{n}", f"-XMP-lr:HierarchicalSubject-=Osoby|{n}",
                     f"-XMP-microsoft:LastKeywordXMP-={n}", f"-XMP-mediapro:People-={n}"]
    if opts.strip_camera:
        args += ["-makernotes:all=", "-Make=", "-Model=", "-*Serial*=", "-Lens*=", "-Software=",
                 "-HostComputer=", "-OwnerName=", "-CameraOwnerName=", "-Artist=", "-XMP-dc:Creator=",
                 "-IPTC:By-line=", "-Author=", "-UniqueCameraModel=", "-LocalizedCameraModel=",
                 "-AndroidManufacturer=", "-AndroidModel=", "-ContentIdentifier=", "-ImageUniqueID=",
                 "-XMP-xmpMM:all="]
    return args


def run_exiftool(args, files=(), exiftool=None, timeout=900):
    """ExifTool s argumenty v UTF-8 souboru (-@): jména osob s diakritikou a cesty s Unicode
    projdou beze změny, délka příkazové řádky nehraje roli. Vrací (returncode, stdout, stderr)."""
    exe = exiftool or EXIFTOOL_PATH
    fd, argfile = tempfile.mkstemp(suffix=".args", prefix="kairos-et-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for a in list(args) + [os.path.abspath(p) for p in files]:
                f.write(str(a).replace("\n", " ") + "\n")
        r = subprocess.run([exe, "-charset", "filename=utf8", "-@", argfile], capture_output=True,
                           startupinfo=_hidden_startupinfo(), timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    finally:
        try:
            os.remove(argfile)
        except OSError:
            pass


def read_metadata(files, exiftool=None):
    """{abspath_casefold: {"Family0:Family1:Tag": value}} – všechny značky, i duplicitní (-a) a
    vnořené (-ee3, např. GPS stopa ve videu)."""
    if not files:
        return {}
    _code, out, _err = run_exiftool(["-j", "-a", "-G0:1", "-s", "-ee3", "-charset", "iptc=utf8", "-api",
                                     "LargeFileSupport=1"], files, exiftool)
    try:
        data = json.loads(out) if out.strip() else []
    except ValueError:
        data = []
    return {os.path.normcase(os.path.abspath(d.get("SourceFile", ""))): d for d in data if isinstance(d, dict)}


def _values(v):
    if isinstance(v, list):
        return [x for x in v]
    return [v]


def scan_violations(meta, opts, patterns=()):
    """[(klíč, kategorie, hodnota)] značek, které v ověřené kopii být nesmí. meta = jeden objekt
    z read_metadata (klíče Family0:Family1:Tag nebo Family0:Tag)."""
    out = []
    for key, value in meta.items():
        parts = key.split(":")
        if len(parts) < 2 or parts[0] in _SKIP_FAMILIES:
            continue
        tag = parts[-1]
        tl = tag.casefold()
        group = parts[-2].casefold() if len(parts) >= 3 else parts[0].casefold()
        cat = None
        if opts.strip_gps and ("gps" in tl or tl in _LOCATION_TAGS or tl.startswith("location")):
            cat = "poloha"
        elif opts.strip_people and (group.startswith(_PEOPLE_GROUPS) or "personinimage" in tl
                                    or tl.startswith("region")):
            cat = "osoby"
        elif opts.strip_camera and (tl in _CAMERA_TAGS or "serial" in tl or parts[0] == "MakerNotes"):
            cat = "zařízení"
        elif opts.strip_people and patterns and any(
                isinstance(x, str) and mentions_name(x, patterns) for x in _values(value)):
            cat = "jméno osoby"
        if cat:
            out.append((key, cat, value))
    return out


def remediation_args(violations, patterns=()):
    """Cílené smazání nalezených značek. U seznamů (klíčová slova) se odeberou jen položky se
    jménem osoby, zbytek zůstane. Composite značky se odvozují – zmizí se zdrojem."""
    args, makernotes = [], False
    for key, cat, value in violations:
        parts = key.split(":")
        if parts[0] == "Composite":
            continue
        if parts[0] == "MakerNotes":
            makernotes = True
            continue
        spec = f"{parts[-2]}:{parts[-1]}" if len(parts) >= 3 else key
        if isinstance(value, list) and cat == "jméno osoby":
            args += [f"-{spec}-={x}" for x in value if isinstance(x, str) and mentions_name(x, patterns)]
        else:
            args.append(f"-{spec}=")
    if makernotes:
        args.append("-makernotes:all=")
    return list(dict.fromkeys(args))


def _remux_clean(path, ffmpeg=None):
    """Video bez datových stop (GPS telemetrie) a bez kontejnerových metadat; obraz a zvuk se
    jen kopírují. Vrací True, když se podařilo."""
    exe = ffmpeg or FFMPEG_PATH
    if not os.path.exists(exe):
        return False
    root, ext = os.path.splitext(path)
    tmp = root + ".remux" + ext
    cmd = [exe, "-v", "error", "-y", "-i", path, "-map", "0:v?", "-map", "0:a?", "-c", "copy",
           "-map_metadata", "-1", "-map_chapters", "-1", "-dn", "-sn"]
    if ext.lower() in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]
    try:
        r = subprocess.run(cmd + [tmp], capture_output=True, startupinfo=_hidden_startupinfo(), timeout=1800)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            raise OSError(r.stderr.decode("utf-8", "replace")[-300:])
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[Export] přebalení videa selhalo: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def clean_and_verify(files, opts, names, exiftool=None, ffmpeg=None, progress=None):
    """Odstraní metadata z kopií a ověří je. Vrací (čisté, opravené, {cesta: [nálezy]}) –
    soubory v posledním slovníku neprošly ani po nápravě a nesmí do exportu."""
    patterns = _name_patterns(names)
    files = [os.path.abspath(f) for f in files]
    if not files:
        return [], [], {}
    args = strip_args(opts, names)
    if args:
        for i in range(0, len(files), 200):
            run_exiftool(["-q", "-q", "-m", "-overwrite_original", "-charset", "iptc=utf8", *args], files[i:i + 200],
                         exiftool)
            if progress:
                progress(min(1.0, (i + 200) / len(files)) * 0.5)
    meta = read_metadata(files, exiftool)
    bad = {}
    for f in files:
        m = meta.get(os.path.normcase(f))
        if m is None:
            bad[f] = [("ExifTool", "nečitelné", "soubor nejde ověřit")]
            continue
        v = scan_violations(m, opts, patterns)
        if v:
            bad[f] = v
    if progress:
        progress(0.75)
    fixed = []
    for f, v in list(bad.items()):
        if v and v[0][0] == "ExifTool":
            continue
        fix = remediation_args(v, patterns)
        if fix:
            run_exiftool(["-q", "-q", "-m", "-overwrite_original", "-charset", "iptc=utf8", *fix], [f], exiftool)
        again = scan_violations(read_metadata([f], exiftool).get(os.path.normcase(f), {}), opts, patterns)
        if again and _kind(f) == "video" and _remux_clean(f, ffmpeg):
            again = scan_violations(read_metadata([f], exiftool).get(os.path.normcase(f), {}), opts, patterns)
        if not again:
            fixed.append(f)
            del bad[f]
        else:
            bad[f] = again
    if progress:
        progress(1.0)
    clean = [f for f in files if f not in bad and f not in fixed]
    return clean, fixed, bad


# ── webová prezentace ────────────────────────────────────────────────────────

def _open_image(path):
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    img = Image.open(path)
    icc = img.info.get("icc_profile")
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img, icc


def _fallback_preview(path, media_type):
    try:
        import preview_cache
        return preview_cache.get_preview(path, touch=False) or preview_cache.build_preview(path, media_type)
    except Exception:
        return None


def render_web_image(src, dst, edge=WEB_EDGE, media_type="photo"):
    """JPEG pro web: orientace z EXIF zapečená, barevný profil zachovaný, žádná jiná metadata.
    Vrací (šířka, výška) nebo None."""
    from PIL import Image
    try:
        img, icc = _open_image(src)
    except Exception:
        alt = _fallback_preview(src, media_type)        # RAW / formát, který PIL neumí: náhled aplikace
        if not alt:
            return None
        img, icc = _open_image(alt)
    img.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    kw = {"quality": 88, "optimize": True, "progressive": True}
    if icc:
        kw["icc_profile"] = icc
    img.convert("RGB").save(dst, "JPEG", **kw)
    return img.size


def video_poster(src, dst, edge=WEB_EDGE, ffmpeg=None):
    exe = ffmpeg or FFMPEG_PATH
    if os.path.exists(exe):
        for at in ("1", "0"):
            try:
                subprocess.run([exe, "-v", "error", "-y", "-ss", at, "-i", src, "-frames:v", "1",
                                "-vf", f"scale='min({edge},iw)':-2", "-map_metadata", "-1", dst],
                               capture_output=True, startupinfo=_hidden_startupinfo(), timeout=120)
            except Exception:
                continue
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                break
    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
        alt = _fallback_preview(src, "video")
        if not alt:
            return None
        render_web_image(alt, dst, edge)
    try:
        from PIL import Image
        with Image.open(dst) as im:
            size = im.size
        render_web_image(dst, dst, edge)                 # znovu uložit = jistě bez metadat
        return size
    except Exception:
        return None


_MONTHS = ("ledna", "února", "března", "dubna", "května", "června", "července", "srpna", "září",
           "října", "listopadu", "prosince")


def czech_date(date_text):
    m = re.match(r"(\d{4})[-:](\d{2})[-:](\d{2})", (date_text or "").strip())
    if not m:
        return ""
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if not 1 <= mo <= 12:
        return ""
    return f"{d}. {_MONTHS[mo - 1]} {y}"


def caption(item, opts):
    bits = [czech_date(item.get("date"))]
    if not opts.strip_gps:
        bits.append(", ".join(x for x in (item.get("city"), item.get("country")) if x))
    if not opts.strip_people and item.get("people"):
        bits.append(", ".join(item["people"]))
    return " · ".join(b for b in bits if b)


def build_showcase_html(title, entries):
    """Jedna samostatná stránka: mřížka náhledů + prohlížeč (šipky, Esc, swipe). Nic se nenačítá
    zvenku – funguje z disku, z USB i z rozbaleného ZIPu."""
    data = json.dumps(entries, ensure_ascii=False).replace("</", "<\\/")
    t = html.escape(title or "Fotky")
    count = len(entries)
    sub = f"{count} {_cz(count, 'položka', 'položky', 'položek')}"
    return _SHOWCASE.replace("{{TITLE}}", t).replace("{{SUB}}", html.escape(sub)).replace("{{DATA}}", data)


_SHOWCASE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<meta name="referrer" content="no-referrer">
<title>{{TITLE}}</title>
<style>
:root{color-scheme:dark;--bg:#0e0f11;--fg:#f2f2f4;--muted:#9a9ca3;--tile:#1a1b1f}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.4 "Segoe UI Variable","Segoe UI",system-ui,-apple-system,sans-serif}
header{padding:40px 28px 18px}h1{margin:0;font-size:30px;font-weight:650;letter-spacing:-.01em}
header p{margin:6px 0 0;color:var(--muted)}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:4px;padding:0 24px 40px}
.t{position:relative;aspect-ratio:1;background:var(--tile);border:0;padding:0;cursor:zoom-in;overflow:hidden;border-radius:6px}
.t img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .35s cubic-bezier(.2,.8,.2,1)}
.t:hover img,.t:focus-visible img{transform:scale(1.035)}.t:focus-visible{outline:2px solid #6aa2ff;outline-offset:2px}
.badge{position:absolute;right:8px;bottom:8px;background:rgba(0,0,0,.55);color:#fff;font-size:12px;padding:2px 7px;border-radius:10px}
#lb{position:fixed;inset:0;background:rgba(6,6,8,.96);display:none;align-items:center;justify-content:center;flex-direction:column}
#lb.on{display:flex}#lb img,#lb video{max-width:94vw;max-height:84vh;object-fit:contain;border-radius:4px}
#cap{color:var(--muted);margin-top:14px;min-height:20px;text-align:center;padding:0 20px}
.nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.08);color:#fff;border:0;
width:48px;height:48px;border-radius:24px;font-size:24px;cursor:pointer}.nav:hover{background:rgba(255,255,255,.16)}
#prev{left:18px}#next{right:18px}#close{position:absolute;top:16px;right:18px;transform:none}
footer{color:var(--muted);text-align:center;padding:0 0 30px;font-size:12px}
</style>
</head>
<body>
<header><h1>{{TITLE}}</h1><p>{{SUB}}</p></header>
<main id="g"></main>
<div id="lb" role="dialog" aria-modal="true" aria-label="Prohlížeč">
  <button class="nav" id="close" aria-label="Zavřít">&#x2715;</button>
  <button class="nav" id="prev" aria-label="Předchozí">&#x2039;</button>
  <div id="stage"></div><div id="cap"></div>
  <button class="nav" id="next" aria-label="Další">&#x203A;</button>
</div>
<footer>Vytvořeno v aplikaci Kairos · funguje offline</footer>
<script id="data" type="application/json">{{DATA}}</script>
<script>
(function(){
var items=JSON.parse(document.getElementById('data').textContent),g=document.getElementById('g'),
lb=document.getElementById('lb'),stage=document.getElementById('stage'),cap=document.getElementById('cap'),cur=-1,sx=null;
items.forEach(function(it,i){var b=document.createElement('button');b.className='t';b.setAttribute('aria-label',it.c||('Položka '+(i+1)));
var im=document.createElement('img');im.loading='lazy';im.decoding='async';im.src=it.t;im.alt='';b.appendChild(im);
if(it.v){var s=document.createElement('span');s.className='badge';s.textContent='\\u25B6 video';b.appendChild(s);}
b.onclick=function(){show(i)};g.appendChild(b);});
function show(i){cur=(i+items.length)%items.length;var it=items[cur];stage.innerHTML='';var el;
if(it.v){el=document.createElement('video');el.src=it.v;el.poster=it.f;el.controls=true;el.autoplay=true;el.playsInline=true;}
else{el=document.createElement('img');el.src=it.f;el.alt=it.c||'';}
stage.appendChild(el);cap.textContent=it.c||'';lb.classList.add('on');document.body.style.overflow='hidden';
var n=items[(cur+1)%items.length];if(n&&!n.v){(new Image()).src=n.f;}}
function hide(){lb.classList.remove('on');stage.innerHTML='';document.body.style.overflow='';
var t=g.children[cur];if(t)t.focus();}
document.getElementById('prev').onclick=function(){show(cur-1)};document.getElementById('next').onclick=function(){show(cur+1)};
document.getElementById('close').onclick=hide;lb.onclick=function(e){if(e.target===lb||e.target===stage)hide()};
document.addEventListener('keydown',function(e){if(!lb.classList.contains('on'))return;
if(e.key==='Escape')hide();else if(e.key==='ArrowRight')show(cur+1);else if(e.key==='ArrowLeft')show(cur-1);});
lb.addEventListener('touchstart',function(e){sx=e.touches[0].clientX},{passive:true});
lb.addEventListener('touchend',function(e){if(sx===null)return;var dx=e.changedTouches[0].clientX-sx;sx=null;
if(Math.abs(dx)>50)show(cur+(dx<0?1:-1));});
})();
</script>
</body>
</html>
"""


# ── celý export ──────────────────────────────────────────────────────────────

def _unique_path(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{root} ({n}){ext}"):
        n += 1
    return f"{root} ({n}){ext}"


def default_name(title):
    stem = _safe_stem(title or "Export")
    return f"{stem} – {datetime.now().strftime('%Y-%m-%d')}"


def export(items, opts, dest, names=(), progress=None, cancelled=None, exiftool=None, ffmpeg=None):
    """Provede export. dest: u package='folder' NOVÁ složka (vytvoří se; existující název dostane
    číslo), u 'zip' cesta k .zip. Vrací ExportResult. Originály se jen čtou."""
    exe = exiftool or EXIFTOOL_PATH
    if opts.strips_anything and not exiftool_available(exe):
        raise ExportError("ExifTool není k dispozici – bez ověření se soukromý export neprovede")
    if not items:
        raise ExportError("Není co exportovat")
    sources = {os.path.normcase(os.path.abspath(p)) for it in items for p in (it["path"], it.get("paired")) if p}

    def tick(stage_from, stage_to, frac):
        if progress:
            progress(stage_from + (stage_to - stage_from) * max(0.0, min(1.0, frac)))

    def check():
        if cancelled and cancelled():
            raise ExportCancelled("Export zrušen")

    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".kairos-export-", dir=parent)
    result = ExportResult()
    try:
        web = opts.layout == "web"
        media_dir = os.path.join(staging, "media") if web else staging
        thumbs_dir = os.path.join(staging, "thumbs")
        os.makedirs(media_dir, exist_ok=True)
        if web:
            os.makedirs(thumbs_dir, exist_ok=True)
        patterns = _name_patterns(names)
        stems = plan_names(items, opts, patterns)
        produced = {}                    # media_id -> {"main": cesta, "motion": cesta|None, "poster": …}
        for i, it in enumerate(items):
            check()
            stem = stems[it["id"]]
            src = it["path"]
            ext = os.path.splitext(src)[1].lower()
            entry = {"main": None, "motion": None, "poster": None, "thumb": None, "size": None}
            if web and it["type"] != "video":
                dst = os.path.join(media_dir, stem + ".jpg")
                entry["size"] = render_web_image(src, dst, WEB_EDGE, it["type"])
                if entry["size"]:
                    entry["main"] = dst
            elif web and ext not in WEB_VIDEO_EXTS:
                dst = os.path.join(media_dir, stem + ".jpg")       # prohlížeč by video nepřehrál: jen snímek
                entry["size"] = video_poster(src, dst, WEB_EDGE, ffmpeg)
                if entry["size"]:
                    entry["main"] = dst
            else:
                dst = os.path.join(media_dir, stem + ext)
                shutil.copyfile(src, dst)
                entry["main"] = dst
                if web:
                    poster = os.path.join(media_dir, stem + ".poster.jpg")
                    entry["size"] = video_poster(src, poster, WEB_EDGE, ffmpeg)
                    entry["poster"] = poster if entry["size"] else None
                    entry["video"] = True
            if entry["main"] is None:
                result.excluded.append((it["name"], "nelze převést pro web"))
                continue
            if web:
                show = entry["poster"] if entry.get("video") else entry["main"]
                if show:
                    thumb = os.path.join(thumbs_dir, stem + ".jpg")
                    entry["thumb"] = thumb if render_web_image(show, thumb, WEB_THUMB) else None
            if opts.include_live and it.get("paired") and not web:
                mext = os.path.splitext(it["paired"])[1].lower() or ".mov"
                mdst = os.path.join(media_dir, stem + mext)
                if os.path.normcase(os.path.abspath(mdst)) != os.path.normcase(os.path.abspath(entry["main"])):
                    shutil.copyfile(it["paired"], mdst)
                    entry["motion"] = mdst
            produced[it["id"]] = entry
            tick(0.0, 0.45, (i + 1) / len(items))

        all_files = [p for e in produced.values() for p in (e["main"], e["motion"], e["poster"], e["thumb"]) if p]
        for p in all_files:              # pojistka: staging nikdy nesmí ukazovat na originál
            if os.path.normcase(os.path.abspath(p)) in sources:
                raise ExportError("Cílová složka se překrývá se zdrojovými soubory")
        check()
        bad = {}
        if exiftool_available(exe):
            clean, fixed, bad = clean_and_verify(all_files, opts, names, exe, ffmpeg,
                                                 lambda f: tick(0.45, 0.85, f))
            result.verified = len(clean) + len(fixed)
            result.remediated = len(fixed)
        check()
        norm = lambda p: os.path.normcase(os.path.abspath(p))        # noqa: E731
        bad_by = {norm(p): v for p, v in bad.items()}
        rel = lambda p: os.path.relpath(p, staging).replace(os.sep, "/") if p else ""   # noqa: E731
        entries = []
        by_id = {it["id"]: it for it in items}
        for mid, e in produced.items():
            it = by_id[mid]
            found = bad_by.get(norm(e["main"]))
            if found is not None:
                cats = sorted({c for _k, c, _v in found})
                result.excluded.append((it["name"], "metadata nejdou odstranit (" + ", ".join(cats) + ")"))
                for k in ("main", "motion", "poster", "thumb"):
                    if e[k] and os.path.exists(e[k]):
                        os.remove(e[k])
                continue
            for k in ("motion", "poster", "thumb"):
                if e[k] and norm(e[k]) in bad_by:
                    os.remove(e[k])
                    e[k] = None
            result.exported += 1
            result.files += sum(1 for k in ("main", "motion", "poster") if e[k])
            if web:
                show = e["poster"] if e.get("video") else e["main"]
                entries.append({"f": rel(show), "t": rel(e["thumb"] or show), "v": rel(e["main"]) if e.get("video") else "",
                                "c": caption(it, opts), "w": (e["size"] or (0, 0))[0], "h": (e["size"] or (0, 0))[1]})
        if result.exported == 0:
            raise ExportError("Žádná položka neprošla ověřením – export se nevytvořil")
        if web:
            with open(os.path.join(staging, "index.html"), "w", encoding="utf-8", newline="\n") as f:
                f.write(build_showcase_html(opts.title, entries))
        check()
        tick(0.85, 0.9, 1.0)
        if opts.package == "zip":
            target = _unique_path(dest if dest.lower().endswith(".zip") else dest + ".zip")
            part = target + ".part"
            files = []
            for root, _dirs, fnames in os.walk(staging):
                for fn in fnames:
                    files.append(os.path.join(root, fn))
            folder_name = os.path.splitext(os.path.basename(target))[0]
            with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for n, fp in enumerate(sorted(files)):
                    check()
                    arc = folder_name + "/" + os.path.relpath(fp, staging).replace(os.sep, "/")
                    comp = zipfile.ZIP_DEFLATED if fp.endswith((".html", ".json")) else zipfile.ZIP_STORED
                    z.write(fp, arc, compress_type=comp)
                    tick(0.9, 1.0, (n + 1) / max(1, len(files)))
            os.replace(part, target)
            shutil.rmtree(staging, ignore_errors=True)
            result.path = target
        else:
            target = _unique_path(dest)
            os.replace(staging, target)
            result.path = target
        tick(0.0, 1.0, 1.0)
        return result
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        for leftover in (dest + ".part", (dest if dest.lower().endswith(".zip") else dest + ".zip") + ".part"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise
