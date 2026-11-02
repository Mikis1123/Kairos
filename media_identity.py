"""Gate 0: odolná identita média – nezávislá na písmenu disku a názvu složky.

Dřív byla jediným klíčem absolutní cesta (media_items.file_path UNIQUE). Jiné písmeno
externího disku (E: -> F:), přejmenovaná složka nebo přesun v Průzkumníku = „nové“ fotky
a staré řádky (alba, tváře, oblíbené, ruční poloha) osiřely.

Každá položka má teď navíc:
  volume_guid   \\\\?\\Volume{…}\\ svazku (nemění se s písmenem disku; síť = „unc:\\\\server\\share“)
  vol_rel_path  cesta relativně ke kořeni svazku (Fotky\\2023\\IMG_1.HEIC)
  fingerprint   „velikost:xxh3_64 prvních 64 kB“ – obsahový otisk, levný (jedno čtení 64 kB)
  availability  NULL = na disku | 'offline' = svazek odpojený | 'missing' = svazek je, soubor ne
                | 'cloud' = jen v cloudu (cloud_files)

file_path zůstává pracovním klíčem pro zbytek aplikace (náhledy, prohlížeč, Live Photos) –
tento modul ho jen udržuje platný:
  * reconcile() při startu: jiné písmeno svazku -> přepsat cestu (volume_guid + vol_rel_path);
    odpojený svazek -> 'offline' (nic se nemaže, alba / tváře zůstávají);
  * scanner při importu: nový soubor, jehož otisk (+ datum EXIF) odpovídá chybějící položce
    -> rekey() přepíše cestu existujícího řádku místo vložení duplicity (tiché znovupřipojení).
"""
import os
import sys
import threading

try:
    import xxhash
    _xxh3 = xxhash.xxh3_64_hexdigest
except ImportError:                      # bez xxhash (vývoj) – stejná délka, jiný prefix
    import hashlib
    _xxh3 = None

    def _blake(data):
        return "b2" + hashlib.blake2b(data, digest_size=7).hexdigest()

import cloud_files

HEAD_BYTES = 64 * 1024
OFFLINE, MISSING, CLOUD = "offline", "missing", "cloud"
_SIG_TABLES = ("image_embeddings", "siglip_embeddings", "video_frame_embeddings", "media_ocr")


# ── otisk obsahu ─────────────────────────────────────────────────────────────

def head_hash(path):
    """xxh3_64 prvních 64 kB (hex), nebo None (nečitelné)."""
    try:
        with open(path, "rb") as f:
            data = f.read(HEAD_BYTES)
    except OSError:
        return None
    return _xxh3(data) if _xxh3 else _blake(data)


def fingerprint(path, size=None):
    """„velikost:hash“ – nikdy neotevírá zástupný soubor cloudu (vrátí None)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if cloud_files.stat_is_placeholder(st):
        return None
    h = head_hash(path)
    return f"{st.st_size if size is None else int(size)}:{h}" if h else None


# ── svazky ───────────────────────────────────────────────────────────────────

_vol_cache = {}
_vol_lock = threading.Lock()


def _win_volume(path):
    """(volume_guid, kořen svazku) pro cestu, bez přístupu k souboru samotnému."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(1024)
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    if not k32.GetVolumePathNameW(path, buf, 1024):
        return None, None
    root = buf.value
    if root.startswith("\\\\") and not root.startswith("\\\\?\\"):
        return "unc:" + root.rstrip("\\").lower(), root          # síťová sdílená složka
    gbuf = ctypes.create_unicode_buffer(64)
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    if k32.GetVolumeNameForVolumeMountPointW(root, gbuf, 64):
        return gbuf.value.lower(), root
    return None, root                                          # namapovaná síť (Z:) apod.


def volume_of(path):
    """(volume_guid | None, kořen svazku | None). Výsledek se pamatuje podle písmena disku."""
    if not path or sys.platform != "win32":
        return None, None
    drive = os.path.splitdrive(os.path.abspath(path))[0].lower()
    with _vol_lock:
        if drive in _vol_cache:
            return _vol_cache[drive]
    try:
        if not os.path.exists(drive + "\\" if drive and not drive.startswith("\\\\") else drive or path):
            return None, None                                  # odpojený disk – necachovat
        res = _win_volume(path)
    except Exception:
        res = (None, None)
    if not drive.startswith("\\\\"):
        with _vol_lock:
            _vol_cache[drive] = res
    return res


def forget_volumes():
    """Po připojení / odpojení disku (WM_DEVICECHANGE) nebo v testech."""
    with _vol_lock:
        _vol_cache.clear()


def identity_of(path):
    """(volume_guid, vol_rel_path) – relativní cesta ke kořeni svazku."""
    guid, root = volume_of(path)
    if not root:
        return None, None
    ap = os.path.normpath(os.path.abspath(path))
    rel = ap[len(root):] if ap.lower().startswith(root.lower()) else None
    return guid, (rel.lstrip("\\/") if rel is not None else None)


def mounted_volumes():
    """{volume_guid: [kořeny]} všech právě připojených svazků (FindFirstVolume)."""
    out = {}
    if sys.platform != "win32":
        return out
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.FindFirstVolumeW.restype = wintypes.HANDLE
        k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
        k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
        k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
        k32.GetVolumePathNamesForVolumeNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                                         ctypes.POINTER(wintypes.DWORD)]
        name = ctypes.create_unicode_buffer(64)
        h = k32.FindFirstVolumeW(name, 64)
        if h in (None, wintypes.HANDLE(-1).value):
            return out
        try:
            while True:
                guid = name.value
                paths_buf = ctypes.create_unicode_buffer(2048)
                ret = wintypes.DWORD(0)
                roots = []
                if k32.GetVolumePathNamesForVolumeNameW(guid, paths_buf, 2048, ctypes.byref(ret)):
                    raw = ctypes.wstring_at(ctypes.addressof(paths_buf), max(0, ret.value - 1))
                    roots = [r for r in raw.split("\0") if r]
                if roots:
                    out[guid.lower()] = roots
                if not k32.FindNextVolumeW(h, name, 64):
                    break
        finally:
            k32.FindVolumeClose(h)
    except Exception:
        pass
    return out


# ── přepis cesty (znovupřipojení) ────────────────────────────────────────────

def rekey(cur, media_id, new_path, fp=None):
    """Přesune existující položku na novou cestu – id, alba, tváře, oblíbené i vektory
    zůstávají. False = cílová cesta už patří jiné položce (nic se nezmění)."""
    new_path = os.path.normpath(new_path)
    row = cur.execute("SELECT file_path FROM media_items WHERE id = ?", (media_id,)).fetchone()
    if not row:
        return False
    old = row[0]
    if old == new_path:
        return True
    clash = cur.execute("SELECT id FROM media_items WHERE file_path = ? AND id != ?",
                        (new_path, media_id)).fetchone()
    if clash:
        return False
    guid, rel = identity_of(new_path)
    cur.execute("""UPDATE media_items SET file_path = ?, file_name = ?, volume_guid = COALESCE(?, volume_guid),
                   vol_rel_path = COALESCE(?, vol_rel_path), fingerprint = COALESCE(?, fingerprint),
                   availability = NULL WHERE id = ?""",
                (new_path, os.path.basename(new_path), guid, rel, fp, media_id))
    if old:
        cur.execute("UPDATE media_items SET paired_video_path = ? WHERE paired_video_path = ?", (new_path, old))
        # podpis zdroje vektorů začíná cestou (database.EMBEDDING_SIG_SQL) – bez přepisu by se
        # po přesunu všechno přepočítávalo (CLIP, SigLIP, snímky videí, OCR)
        prefix = old + "|"
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in _SIG_TABLES:
            if t in tables:
                cur.execute(f"UPDATE {t} SET source_sig = ? || substr(source_sig, ?) "
                            f"WHERE media_id = ? AND substr(source_sig, 1, ?) = ?",
                            (new_path + "|", len(prefix) + 1, media_id, len(prefix), prefix))
    return True


def _date_key(value):
    s = str(value or "").strip().replace("T", " ")
    if len(s) >= 10 and s[4] == ":" and s[7] == ":":
        s = s.replace(":", "-", 2)
    return s[:19]


class MissingIndex:
    """Položky, jejichž soubor chybí – kandidáti na znovupřipojení. Sestaví se líně
    (jen když sken našel neznámé soubory)."""

    def __init__(self, rows, exists=os.path.exists):
        self.by_fp, self.by_name = {}, {}
        for mid, path, name, size, fp, date in rows:
            if path and exists(path):
                continue
            entry = (mid, date)
            if fp:
                self.by_fp.setdefault(fp, []).append(entry)
            if name and size is not None:            # starší položky bez otisku: název + velikost
                self.by_name.setdefault((name.lower(), int(size)), []).append(entry)
        self.claimed = set()

    @classmethod
    def load(cls, conn, exists=os.path.exists):
        rows = conn.execute("SELECT id, file_path, file_name, file_size, fingerprint, date_taken "
                            "FROM media_items").fetchall()
        return cls(rows, exists)

    def __bool__(self):
        return bool(self.by_fp or self.by_name)

    def candidates(self, path, size, fp):
        """Kandidáti (id, datum) pro nový soubor: shoda otisku, jinak název + velikost."""
        found = list(self.by_fp.get(fp, [])) if fp else []
        if not found:
            found = [e for e in self.by_name.get((os.path.basename(path).lower(), int(size)), [])]
        return [e for e in found if e[0] not in self.claimed]

    def choose(self, cands, date_taken):
        """Jediný kandidát se shodným datem pořízení (EXIF) – nebo None (nejisté = nová položka)."""
        d = _date_key(date_taken)
        same = [mid for mid, cd in cands if _date_key(cd) == d] if d else []
        if len(same) == 1:
            pick = same[0]
        elif not d and len(cands) == 1:
            pick = cands[0][0]
        else:
            return None
        self.claimed.add(pick)
        return pick


# ── kontrola dostupnosti při startu ──────────────────────────────────────────

def reconcile(conn, exists=os.path.exists, volumes=None, should_stop=None):
    """Dostupnost všech položek: přepis cesty po změně písmena disku, 'offline' u odpojených
    svazků, 'cloud' u zástupných souborů, doplnění volume_guid / vol_rel_path. Nikdy nic
    nemaže. Vrací statistiku. conn = připojení (commit dělá volající).

    Nejdřív se jen zjišťuje (bez zámku zápisu – pomalý síťový disk nesmí blokovat UI), změny
    se zapíšou na konci najednou. Kořen disku / sdílené složky se ověří jednou; když chybí,
    jednotlivé soubory na něm se už nezkoušejí (mrtvé SMB = sekundy na každý dotaz)."""
    volumes = mounted_volumes() if volumes is None else volumes
    stats = {"checked": 0, "online": 0, "offline": 0, "missing": 0, "cloud": 0, "remapped": 0,
             "backfilled": 0, "offline_volumes": {}}
    rows = conn.execute("SELECT id, file_path, volume_guid, vol_rel_path, availability "
                        "FROM media_items").fetchall()
    conn.commit()                       # žádná otevřená čtecí transakce během zjišťování
    root_ok = {}

    def root_present(p):
        d = os.path.splitdrive(p)[0]
        if d not in root_ok:
            root_ok[d] = exists(d + os.sep) if d else True
        return root_ok[d]

    states, backfill, remaps = [], [], []
    for mid, path, guid, rel, avail in rows:
        if should_stop and should_stop():
            break
        stats["checked"] += 1
        state = None
        gone_volume = bool(guid and not guid.startswith("unc:") and volumes and guid not in volumes)
        present = bool(path) and not gone_volume and root_present(path) and exists(path)
        if present:
            if cloud_files.is_placeholder(path):
                state = CLOUD
            if not guid:
                g, r = identity_of(path)
                if g or r:
                    backfill.append((g, r, mid))
        else:
            moved = None
            if guid and rel and guid in volumes:
                for root in volumes[guid]:
                    cand = os.path.join(root, rel)
                    if exists(cand):
                        moved = cand
                        break
            if moved:
                remaps.append((mid, moved, avail))
                continue
            state = OFFLINE if (gone_volume or (path and not root_present(path))) else MISSING
            if state == OFFLINE:
                key = guid or os.path.splitdrive(path or "")[0]
                stats["offline_volumes"][key] = stats["offline_volumes"].get(key, 0) + 1
        stats[state or "online"] += 1
        if state != avail:
            states.append((state, mid))
    cur = conn.cursor()
    if backfill:
        cur.executemany("UPDATE media_items SET volume_guid = ?, vol_rel_path = ? WHERE id = ?", backfill)
        stats["backfilled"] = len(backfill)
    if states:
        cur.executemany("UPDATE media_items SET availability = ? WHERE id = ?", states)
    for mid, moved, avail in remaps:
        if rekey(cur, mid, moved):
            stats["remapped"] += 1
            stats["online"] += 1
        else:                               # cílová cesta patří jiné položce – nechat chybět
            stats["missing"] += 1
            if avail != MISSING:
                cur.execute("UPDATE media_items SET availability = ? WHERE id = ?", (MISSING, mid))
    return stats


def backfill_fingerprints(conn, should_stop=None, limit=None, exists=os.path.exists):
    """Otisky položek, které ho ještě nemají (knihovny ze starší verze). Jen soubory na disku,
    nikdy zástupné soubory cloudu. Vrací počet. Čte 64 kB na soubor."""
    rows = conn.execute("SELECT id, file_path, file_size FROM media_items WHERE fingerprint IS NULL "
                        "AND availability IS NULL" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    done, updates = 0, []
    for mid, path, size in rows:
        if should_stop and should_stop():
            break
        if not path or not exists(path):
            continue
        fp = fingerprint(path)
        if fp:
            updates.append((fp, mid))
            done += 1
        if len(updates) >= 200:
            conn.executemany("UPDATE media_items SET fingerprint = ? WHERE id = ?", updates)
            conn.commit()
            updates.clear()
    if updates:
        conn.executemany("UPDATE media_items SET fingerprint = ? WHERE id = ?", updates)
        conn.commit()
    return done
