"""XMP sidecary vedle originálů – kurátorská data přežijí ztrátu databáze (Sprint 2).

Co se zapisuje (jen standardní XMP, čitelné v digiKamu, Lightroomu, darktable, Bridge):
  * oblíbené       -> xmp:Rating = 5 (jen pokud hodnocení nepatří jinému programu, viz níže)
  * jména tváří    -> mwg-rs:Regions (Metadata Working Group, normalizovaný střed + rozměr)
                      + Iptc4xmpExt:PersonInImage (seznam jmen)
  * opravy polohy  -> exif:GPSLatitude / GPSLongitude (jen opravená/vypůjčená poloha,
                      původní GPS ze souboru se neopakuje) + kairos:GPSSource

Soubor: <název>.<přípona>.xmp (konvence digiKam / darktable). Lightroomové IMG_1383.xmp by
kolidovalo: iPhone má IMG_1383.HEIC i IMG_1383.MOV se stejným základem.

Cizí sidecar (existující obsah z jiného programu) se NEPŘEPISUJE: upraví se jen vlastnosti, které
Kairos spravuje, ostatní uzly zůstanou. Hodnocení: Kairos smí měnit xmp:Rating jen když ho sám
zapsal (kairos:RatingOwned) nebo když žádné nebylo – hodnocení 3 z Lightroomu oblíbení nepřebije.

Směr dat: DB -> sidecar při každé změně (SidecarWriter, na pozadí s odkladem); sidecar -> DB
při importu nových položek (restore_media) a po detekci tváří (apply_faces).
"""
import json
import os
import threading
import xml.etree.ElementTree as ET

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
    "stDim": "http://ns.adobe.com/xap/1.0/sType/Dimensions#",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "kairos": "https://kairos.app/ns/1.0/",
}
for _p, _u in NS.items():
    ET.register_namespace(_p, _u)


def q(tag):
    p, local = tag.split(":")
    return "{%s}%s" % (NS[p], local)


# Vlastnosti, které Kairos spravuje (při zápisu se odstraní a zapíšou znovu)
MANAGED = [q("xmp:Rating"), q("mwg-rs:Regions"), q("Iptc4xmpExt:PersonInImage"),
           q("exif:GPSLatitude"), q("exif:GPSLongitude"), q("kairos:Favorite"),
           q("kairos:RatingOwned"), q("kairos:GPSSource")]
FAVORITE_RATING = 5
CORRECTED_GPS = ("borrowed", "manual")       # zdroje polohy, které do sidecaru patří


def sidecar_path(file_path):
    return file_path + ".xmp"


# ── GPS v zápisu XMP: "50,5.12345N" (stupně, desetinné minuty, světová strana) ──
def gps_to_xmp(value, is_lat):
    ref = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    v = abs(float(value))
    deg = int(v)
    return f"{deg},{(v - deg) * 60:.6f}{ref}"


def gps_from_xmp(text):
    if not text:
        return None
    s = text.strip()
    ref = s[-1].upper() if s[-1].isalpha() else ""
    body = s[:-1] if ref else s
    parts = [float(p) for p in body.split(",") if p.strip()]
    if not parts:
        return None
    v = parts[0] + (parts[1] / 60 if len(parts) > 1 else 0) + (parts[2] / 3600 if len(parts) > 2 else 0)
    return -v if ref in ("S", "W") else v


# ── stav jedné položky ─────────────────────────────────────────────────────────
def empty_state():
    return {"favorite": False, "faces": [], "gps": None}


def state_is_empty(st):
    return not st["favorite"] and not st["faces"] and not st["gps"]


def _description(root):
    rdf = root.find(q("rdf:RDF")) if root.tag != q("rdf:RDF") else root
    if rdf is None:
        rdf = ET.SubElement(root, q("rdf:RDF"))
    desc = rdf.find(q("rdf:Description"))
    if desc is None:
        desc = ET.SubElement(rdf, q("rdf:Description"), {q("rdf:about"): ""})
    return desc


def _prop(desc, tag):
    """Hodnota vlastnosti zapsané jako atribut i jako prvek (exiftool píše obojí)."""
    full = q(tag)
    if full in desc.attrib:
        return desc.attrib[full]
    el = desc.find(full)
    return el.text if el is not None else None


def _all_descriptions(root):
    return list(root.iter(q("rdf:Description")))


def _find_prop(root, tag):
    for d in _all_descriptions(root):
        v = _prop(d, tag)
        if v is not None:
            return v
    return None


def parse(xml_text):
    """Sidecar -> stav Kairos (+ pomocné příznaky). Neplatné XML -> None."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    st = empty_state()
    rating = _find_prop(root, "xmp:Rating")
    fav = _find_prop(root, "kairos:Favorite")
    try:
        st["favorite"] = fav == "1" if fav is not None else (rating is not None and float(rating) >= FAVORITE_RATING)
    except ValueError:
        st["favorite"] = False
    st["rating"] = rating
    st["rating_owned"] = _find_prop(root, "kairos:RatingOwned") == "1"
    for li in root.iter(q("rdf:li")):
        name = li.find(q("mwg-rs:Name"))
        area = li.find(q("mwg-rs:Area"))
        kind = li.find(q("mwg-rs:Type"))
        if name is None or area is None or not (name.text or "").strip():
            continue
        if kind is not None and (kind.text or "Face") != "Face":
            continue
        try:
            cx, cy = float(area.get(q("stArea:x"))), float(area.get(q("stArea:y")))
            w, h = float(area.get(q("stArea:w"))), float(area.get(q("stArea:h")))
        except (TypeError, ValueError):
            continue
        st["faces"].append({"name": name.text.strip(), "x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h})
    lat, lon = gps_from_xmp(_find_prop(root, "exif:GPSLatitude")), gps_from_xmp(_find_prop(root, "exif:GPSLongitude"))
    if lat is not None and lon is not None:
        st["gps"] = {"lat": lat, "lon": lon, "source": _find_prop(root, "kairos:GPSSource") or "xmp"}
    return st


def read_sidecar(file_path):
    path = sidecar_path(file_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse(f.read())
    except (OSError, UnicodeDecodeError):
        return None


def _new_root():
    root = ET.Element(q("x:xmpmeta"))
    rdf = ET.SubElement(root, q("rdf:RDF"))
    ET.SubElement(rdf, q("rdf:Description"), {q("rdf:about"): ""})
    return root


def _strip_managed(root):
    for d in _all_descriptions(root):
        for full in MANAGED:
            d.attrib.pop(full, None)
            for el in d.findall(full):
                d.remove(el)


def _has_foreign_content(root):
    for d in _all_descriptions(root):
        if any(k != q("rdf:about") for k in d.attrib) or len(d):
            return True
    return False


def build(st, existing_xml=None, dims=None):
    """Stav -> text sidecaru. existing_xml = cizí obsah, který se zachová. Vrací None, pokud
    po odebrání vlastností Kairos nezbude nic (sidecar se pak smaže)."""
    root, prev = None, None
    if existing_xml:
        try:
            root = ET.fromstring(existing_xml)
            prev = parse(existing_xml)
        except ET.ParseError:
            root = None
    if root is None:
        root = _new_root()
    if root.tag == q("rdf:RDF"):                # holé rdf:RDF bez obálky x:xmpmeta
        wrap = ET.Element(q("x:xmpmeta"))
        wrap.append(root)
        root = wrap
    _strip_managed(root)
    desc = _description(root)

    # hodnocení: jen pokud nepatří jinému programu
    foreign_rating = prev and prev.get("rating") is not None and not prev.get("rating_owned")
    if foreign_rating:
        ET.SubElement(desc, q("xmp:Rating")).text = prev["rating"]
    elif st["favorite"]:
        ET.SubElement(desc, q("xmp:Rating")).text = str(FAVORITE_RATING)
        ET.SubElement(desc, q("kairos:RatingOwned")).text = "1"
    if st["favorite"]:
        ET.SubElement(desc, q("kairos:Favorite")).text = "1"

    faces = [f for f in st["faces"] if f.get("name")]
    if faces:
        regions = ET.SubElement(desc, q("mwg-rs:Regions"), {q("rdf:parseType"): "Resource"})
        if dims and dims[0] and dims[1]:
            ET.SubElement(regions, q("mwg-rs:AppliedToDimensions"),
                          {q("stDim:w"): str(int(dims[0])), q("stDim:h"): str(int(dims[1])), q("stDim:unit"): "pixel"})
        bag = ET.SubElement(ET.SubElement(regions, q("mwg-rs:RegionList")), q("rdf:Bag"))
        for f in faces:
            li = ET.SubElement(bag, q("rdf:li"), {q("rdf:parseType"): "Resource"})
            ET.SubElement(li, q("mwg-rs:Name")).text = f["name"]
            ET.SubElement(li, q("mwg-rs:Type")).text = "Face"
            ET.SubElement(li, q("mwg-rs:Area"), {
                q("stArea:x"): f"{f['x'] + f['w'] / 2:.6f}", q("stArea:y"): f"{f['y'] + f['h'] / 2:.6f}",
                q("stArea:w"): f"{f['w']:.6f}", q("stArea:h"): f"{f['h']:.6f}", q("stArea:unit"): "normalized"})
        people = ET.SubElement(ET.SubElement(desc, q("Iptc4xmpExt:PersonInImage")), q("rdf:Bag"))
        for name in sorted({f["name"] for f in faces}):
            ET.SubElement(people, q("rdf:li")).text = name
    if st["gps"]:
        ET.SubElement(desc, q("exif:GPSLatitude")).text = gps_to_xmp(st["gps"]["lat"], True)
        ET.SubElement(desc, q("exif:GPSLongitude")).text = gps_to_xmp(st["gps"]["lon"], False)
        ET.SubElement(desc, q("kairos:GPSSource")).text = st["gps"].get("source") or "manual"

    if not _has_foreign_content(root):
        return None
    ET.indent(root, space=" ")
    body = ET.tostring(root, encoding="unicode")
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + body + '\n<?xpacket end="w"?>\n')


def write_sidecar(file_path, st, dims=None):
    """Zapíše (nebo smaže) sidecar. Vrací 'written' | 'removed' | 'skipped'."""
    path = sidecar_path(file_path)
    existing = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        except (OSError, UnicodeDecodeError):
            return "skipped"                    # nečitelný cizí soubor nepřepisovat
    elif state_is_empty(st):
        return "skipped"                        # bez kurátorských dat žádný nový soubor
    text = build(st, existing, dims)
    if text is None:
        try:
            os.remove(path)
            return "removed"
        except OSError:
            return "skipped"
    if existing is not None and existing == text:
        return "skipped"
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)                   # atomicky – rozepsaný sidecar nikdy nezůstane
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return "skipped"
    return "written"


# ── DB -> stav ─────────────────────────────────────────────────────────────────
def _is_named(name):
    n = (name or "").strip()
    # i18n-ok: DB default person name check
    return bool(n) and not n.lower().startswith(("neznámá osoba", "unknown"))


def _dims(raw_json):
    try:
        m = json.loads(raw_json or "{}")
    except ValueError:
        return None
    w = m.get("ImageWidth") or m.get("ExifImageWidth")
    h = m.get("ImageHeight") or m.get("ExifImageHeight")
    try:
        return (int(w), int(h)) if w and h else None
    except (TypeError, ValueError):
        return None


def _has_column(conn, table, col):
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def collect(conn, media_ids):
    """{media_id: (file_path, stav, rozměry)} z databáze."""
    ids = list(dict.fromkeys(int(i) for i in media_ids))
    out = {}
    if not ids:
        return out
    gps_col = _has_column(conn, "media_items", "gps_source")
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        marks = ",".join("?" * len(chunk))
        cols = "id, file_path, COALESCE(is_favorite, 0), latitude, longitude, raw_metadata_json"
        cols += ", gps_source" if gps_col else ", NULL"
        for mid, path, fav, lat, lon, raw, src in conn.execute(
                f"SELECT {cols} FROM media_items WHERE id IN ({marks})", chunk):
            st = empty_state()
            st["favorite"] = bool(fav)
            if src in CORRECTED_GPS and lat is not None and lon is not None:
                st["gps"] = {"lat": lat, "lon": lon, "source": src}
            out[mid] = (path, st, _dims(raw))
        for mid, name, x, y, w, h in conn.execute(f"""
                SELECT f.media_id, p.name, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h
                FROM face_instances f JOIN people p ON p.id = f.person_id
                WHERE f.media_id IN ({marks}) AND f.bbox_w > 0""", chunk):
            if mid in out and _is_named(name) and None not in (x, y, w, h):
                out[mid][1]["faces"].append({"name": name.strip(), "x": x, "y": y, "w": w, "h": h})
    return out


def sync_media(conn, media_ids):
    """Zapíše sidecary pro dané položky. Vrací počty {'written': n, 'removed': n, 'skipped': n}."""
    counts = {"written": 0, "removed": 0, "skipped": 0}
    for mid, (path, st, dims) in collect(conn, media_ids).items():
        if not path or not os.path.exists(path):
            counts["skipped"] += 1
            continue
        counts[write_sidecar(path, st, dims)] += 1
    return counts


def media_for_person(conn, person_id):
    return [r[0] for r in conn.execute("SELECT DISTINCT media_id FROM face_instances WHERE person_id = ?", (person_id,))]


# ── sidecar -> DB ──────────────────────────────────────────────────────────────
def restore_media(conn, media_ids):
    """Nově importované položky: oblíbené a opravená poloha ze sidecarů. Tváře až po detekci
    (apply_faces). Nikdy nepřepíše hodnotu, kterou už databáze má. Vrací počet obnovených."""
    restored = 0
    gps_col = _has_column(conn, "media_items", "gps_source")
    ids = [int(i) for i in media_ids]
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT id, file_path, COALESCE(is_favorite, 0), latitude FROM media_items "
                            f"WHERE id IN ({marks})", chunk).fetchall()
        for mid, path, fav, lat in rows:
            if not path or not os.path.exists(sidecar_path(path)):
                continue
            st = read_sidecar(path)
            if not st:
                continue
            changed = False
            if st["favorite"] and not fav:
                conn.execute("UPDATE media_items SET is_favorite = 1 WHERE id = ?", (mid,))
                changed = True
            if st["gps"] and lat is None:
                if gps_col:
                    conn.execute("UPDATE media_items SET latitude = ?, longitude = ?, gps_source = ? WHERE id = ?",
                                 (st["gps"]["lat"], st["gps"]["lon"], st["gps"]["source"], mid))
                else:
                    conn.execute("UPDATE media_items SET latitude = ?, longitude = ? WHERE id = ?",
                                 (st["gps"]["lat"], st["gps"]["lon"], mid))
                changed = True
            restored += changed
    return restored


def _iou(a, b):
    ax2, ay2, bx2, by2 = a["x"] + a["w"], a["y"] + a["h"], b["x"] + b["w"], b["y"] + b["h"]
    iw = max(0.0, min(ax2, bx2) - max(a["x"], b["x"]))
    ih = max(0.0, min(ay2, by2) - max(a["y"], b["y"]))
    inter = iw * ih
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


FACE_MATCH_IOU = 0.4


def apply_faces(conn, media_id):
    """Jména ze sidecaru na detekované tváře (podle překryvu rámečků). Tvář s už pojmenovanou
    osobou se nemění. Chybějící osoba se založí jako potvrzená. Vrací počet přiřazených tváří."""
    row = conn.execute("SELECT file_path FROM media_items WHERE id = ?", (media_id,)).fetchone()
    if not row or not row[0] or not os.path.exists(sidecar_path(row[0])):
        return 0
    st = read_sidecar(row[0])
    if not st or not st["faces"]:
        return 0
    faces = conn.execute("""
        SELECT f.id, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, p.name
        FROM face_instances f LEFT JOIN people p ON p.id = f.person_id
        WHERE f.media_id = ? AND f.bbox_w > 0""", (media_id,)).fetchall()
    has_confirmed = _has_column(conn, "people", "is_confirmed")
    assigned, used = 0, set()
    for want in st["faces"]:
        best, best_iou = None, FACE_MATCH_IOU
        for fid, x, y, w, h, pname in faces:
            if fid in used or None in (x, y, w, h):
                continue
            iou = _iou(want, {"x": x, "y": y, "w": w, "h": h})
            if iou >= best_iou:
                best, best_iou, best_name = fid, iou, pname
        if best is None:
            continue
        used.add(best)
        if _is_named(best_name):
            continue                              # databáze už ví – sidecar ji nepřebije
        prow = conn.execute("SELECT id FROM people WHERE name = ? ORDER BY id LIMIT 1", (want["name"],)).fetchone()
        if prow:
            pid = prow[0]
        else:
            if has_confirmed:
                cur = conn.execute("INSERT INTO people (name, is_confirmed) VALUES (?, 1)", (want["name"],))
            else:
                cur = conn.execute("INSERT INTO people (name) VALUES (?)", (want["name"],))
            pid = cur.lastrowid
        conn.execute("UPDATE face_instances SET person_id = ? WHERE id = ?", (pid, best))
        assigned += 1
    return assigned


# ── zapisovač na pozadí ─────────────────────────────────────────────────────────
class SidecarWriter:
    """Fronta položek ke zápisu: změny se slučují (přejmenování osoby = stovky fotek) a zapisují
    po DEBOUNCE_S ve vlákně, UI nečeká na disk. flush() při ukončení aplikace."""
    DEBOUNCE_S = 1.5

    def __init__(self, connect, enabled=lambda: True):
        self._connect = connect
        self._enabled = enabled
        self._dirty = set()
        self._lock = threading.Lock()
        self._timer = None
        self.last_counts = None

    def mark(self, media_ids):
        if not self._enabled():
            return
        with self._lock:
            self._dirty.update(int(i) for i in media_ids if i is not None)
            if self._timer is None:
                self._timer = threading.Timer(self.DEBOUNCE_S, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def mark_person(self, person_id):
        if not self._enabled() or person_id is None:
            return
        conn = self._connect()
        try:
            self.mark(media_for_person(conn, person_id))
        finally:
            conn.close()

    def flush(self):
        with self._lock:
            ids, self._dirty = list(self._dirty), set()
            if self._timer is not None:
                self._timer.cancel()
            self._timer = None
        if not ids:
            return None
        conn = self._connect()
        try:
            self.last_counts = sync_media(conn, ids)
        except Exception as e:                  # zápis sidecaru nesmí shodit aplikaci
            print(f"XMP sidecar: {e}")
        finally:
            conn.close()
        return self.last_counts

    def pending(self):
        with self._lock:
            return len(self._dirty)


# ── jediný zapisovač aplikace (main.install_sidecars); bez instalace (testy) nic nedělá ──
_writer = None


def install(connect, enabled=lambda: True):
    global _writer
    _writer = SidecarWriter(connect, enabled)
    return _writer


def uninstall():
    global _writer
    w, _writer = _writer, None
    if w is not None:
        w.flush()


def touch(media_ids):
    if _writer is not None:
        _writer.mark(media_ids)


def touch_person(*person_ids):
    if _writer is not None:
        for pid in person_ids:
            _writer.mark_person(pid)


def touch_faces(face_ids):
    """Položky, na kterých leží dané tváře (přesun tváře / vrácení sloučení)."""
    ids = [int(f) for f in face_ids if f is not None]
    if _writer is None or not ids:
        return
    conn = _writer._connect()
    try:
        media = []
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            media += [r[0] for r in conn.execute(
                f"SELECT DISTINCT media_id FROM face_instances WHERE id IN ({','.join('?' * len(chunk))})", chunk)]
    finally:
        conn.close()
    _writer.mark(media)
