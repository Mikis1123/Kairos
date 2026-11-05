"""Stage 3: složka Skryté 🔒 – úroveň 1 (bez UI; Qt část je v locked_ui.py).

Co úroveň 1 chrání: skryté fotky a videa nejsou v knihovně, hledání, tvářích, na mapě, ve
vzpomínkách ani v sestřizích; jejich miniatury leží na disku jen šifrovaně (AES-256-GCM)
a klíč je v paměti jen po dobu odemčení (Windows Hello nebo heslo). Originály zůstávají ve
svých složkách beze změny – úroveň 1 nebrání tomu, kdo otevře složku s fotkami v Průzkumníku
nebo spustí kód pod stejným účtem Windows (to by byla úroveň 2: šifrování originálů).

Klíč knihovny (32 B, náhodný) je v vault.json zabalený dvakrát:
  * heslem – scrypt (N 2^15, r 8, p 1) -> AES-GCM; špatné heslo = neprojde ověření tagu,
  * DPAPI účtu Windows – uvolní se jen po ověření Windows Hello (UserConsentVerifier).
Skrytí (zašifrování miniatury) klíč potřebuje, ale nezobrazuje nic skrytého – vezme ho tiše
přes DPAPI a hned zapomene. Zobrazení skrytého vyžaduje odemčení.

Soubor miniatury: MAGIC | nonce 12 B | šifrovaný JPEG + tag; AAD = „thumb:<id>“ (zašifrovaná
miniatura jedné položky se nedá podstrčit jiné).
"""
from i18n import tr
import base64
import ctypes
import glob
import json
import os
import secrets
import threading
import time

import paths

VAULT_VERSION = 1
VAULT_FILE = "vault.json"
THUMB_MAGIC = b"KVT1"
NONCE_LEN = 12
KEY_LEN = 32
PASSCODE_MIN = 6
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
DPAPI_ENTROPY = b"Kairos/Skryte/v1"
KEY_CHECK_AAD = b"kairos-vault-key-v1"
# Po FREE_ATTEMPTS špatných heslech čekání 30 s, pak se zdvojuje (strop 15 min); trvá i po restartu
FREE_ATTEMPTS = 5
LOCKOUT_BASE_S = 30
LOCKOUT_MAX_S = 900
AUTO_LOCK_IDLE_S = 120


class VaultError(Exception):
    pass


class VaultLocked(VaultError):
    """Operace potřebuje klíč a složka je zamčená (a tichý DPAPI klíč není k dispozici)."""


class LockedOut(VaultError):
    """Příliš mnoho špatných hesel – další pokus až za `seconds`."""

    def __init__(self, seconds):
        super().__init__(tr("locked-retry-in", value=int(seconds)))
        self.seconds = int(seconds)


# ── registr skrytých cest (stráže diskových cache: náhledy, pásy, proxy) ─────────────────

_hidden_lock = threading.Lock()
_hidden_keys = set()


def _norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def set_hidden_paths(path_list):
    global _hidden_keys
    keys = {_norm(p) for p in path_list if p}
    with _hidden_lock:
        _hidden_keys = keys


def add_hidden_paths(path_list):
    with _hidden_lock:
        _hidden_keys.update(_norm(p) for p in path_list if p)


def remove_hidden_paths(path_list):
    with _hidden_lock:
        _hidden_keys.difference_update(_norm(p) for p in path_list if p)


def is_hidden_path(path):
    """Je originál ve složce Skryté? (z libovolného vlákna; prázdný registr = rychlé False)"""
    if not path or not _hidden_keys:
        return False
    return _norm(path) in _hidden_keys


def load_hidden_paths():
    """Naplní registr z DB (při startu aplikace)."""
    import database
    set_hidden_paths(database.hidden_paths())
    return len(_hidden_keys)


# ── DPAPI (crypt32) ──────────────────────────────────────────────────────────────────────

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _dpapi(data, protect):
    if os.name != "nt":
        raise VaultError("DPAPI is only available on Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _keep1 = _blob(data)
    ent, _keep2 = _blob(DPAPI_ENTROPY)
    out = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise VaultError(f"DPAPI selhalo ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_protect(data):
    return _dpapi(data, True)


def dpapi_unprotect(blob):
    return _dpapi(blob, False)


# ── Windows Hello (UserConsentVerifier) ──────────────────────────────────────────────────

HELLO_VERIFIED = "verified"
HELLO_CANCELED = "canceled"
HELLO_UNAVAILABLE = "unavailable"
HELLO_FAILED = "failed"


def _run_async(coro_factory):
    import asyncio
    return asyncio.run(coro_factory())


def hello_available():
    """Je Windows Hello (PIN / otisk / obličej) pro tento účet nastavené? Blokuje ~desítky ms."""
    try:
        from winrt.windows.security.credentials.ui import (UserConsentVerifier,
                                                            UserConsentVerifierAvailability)
    except Exception:
        return False
    try:
        async def check():
            return await UserConsentVerifier.check_availability_async()
        return _run_async(check) == UserConsentVerifierAvailability.AVAILABLE
    except Exception:
        return False


def _raise_hello_dialog(stop):
    """Dialog Zabezpečení Windows se z desktopové aplikace někdy otevře pod oknem: přivést ho
    do popředí (smíme – požadavek přišel z aktivního okna)."""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + 4.0
    while not stop.is_set() and time.monotonic() < deadline:
        hwnd = user32.FindWindowW("Credential Dialog Xaml Host", None)
        if hwnd:
            user32.SetForegroundWindow(hwnd)
            return
        time.sleep(0.05)


def hello_verify(message):
    """Ověří uživatele přes Windows Hello. Blokuje (volat mimo GUI vlákno). Vrací HELLO_*."""
    try:
        from winrt.windows.security.credentials.ui import (UserConsentVerifier,
                                                            UserConsentVerificationResult as R)
    except Exception:
        return HELLO_UNAVAILABLE
    stop = threading.Event()
    threading.Thread(target=_raise_hello_dialog, args=(stop,), daemon=True).start()
    try:
        async def verify():
            return await UserConsentVerifier.request_verification_async(message)
        result = _run_async(verify)
    except Exception:
        return HELLO_FAILED
    finally:
        stop.set()
    if result == R.VERIFIED:
        return HELLO_VERIFIED
    if result == R.CANCELED:
        return HELLO_CANCELED
    if result in (R.DEVICE_NOT_PRESENT, R.NOT_CONFIGURED_FOR_USER, R.DISABLED_BY_POLICY):
        return HELLO_UNAVAILABLE
    return HELLO_FAILED


# ── trezor ───────────────────────────────────────────────────────────────────────────────

def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    return base64.b64decode(s.encode("ascii"))


def _aesgcm(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(bytes(key))


def _derive(passcode, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p).derive(passcode.encode("utf-8"))


def shred_file(path):
    """Přepíše soubor nulami a smaže ho (best effort – SSD / stínové kopie mohou data držet dál)."""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b", buffering=0) as f:
            f.write(b"\0" * size)
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        os.remove(path)
        return True
    except OSError:
        return False


class Vault:
    """Klíč složky Skryté a šifrované miniatury. Bezpečné z více vláken."""

    def __init__(self, root=None, clock=time.time):
        self.root = root or paths.VAULT_DIR
        self._clock = clock
        self._lock = threading.RLock()
        self._key = None                  # bytearray jen po dobu odemčení
        self._aead = None
        self._listeners = []

    # ── stav ──
    @property
    def meta_path(self):
        return os.path.join(self.root, VAULT_FILE)

    @property
    def thumb_dir(self):
        return os.path.join(self.root, "t")

    def thumb_path(self, media_id):
        return os.path.join(self.thumb_dir, f"{int(media_id)}.kvt")

    def is_configured(self):
        return os.path.exists(self.meta_path)

    @property
    def is_unlocked(self):
        return self._key is not None

    def add_lock_listener(self, fn):
        self._listeners.append(fn)

    def _read_meta(self):
        with open(self.meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_meta(self, meta):
        os.makedirs(self.root, exist_ok=True)
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.meta_path)

    # ── nastavení / odemčení / zamčení ──
    def setup(self, passcode):
        """Založí trezor s novým klíčem (jen jednou) a nechá ho odemčený."""
        if self.is_configured():
            raise VaultError(tr("locked-error-already-set-up"))
        check_passcode(passcode)
        key = bytearray(secrets.token_bytes(KEY_LEN))
        meta = {"version": VAULT_VERSION, "created": int(self._clock()), "failures": 0, "locked_until": 0}
        meta.update(self._wrap_with_passcode(key, passcode))
        try:
            meta["dpapi"] = _b64(dpapi_protect(bytes(key)))
        except VaultError:
            meta["dpapi"] = None          # bez DPAPI: odemyká jen heslo, skrytí vyžaduje odemčení
        self._write_meta(meta)
        self._set_key(key)

    @staticmethod
    def _wrap_with_passcode(key, passcode):
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(NONCE_LEN)
        kek = _derive(passcode, salt)
        wrapped = _aesgcm(kek).encrypt(nonce, bytes(key), KEY_CHECK_AAD)
        return {"kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": _b64(salt)},
                "pass_nonce": _b64(nonce), "pass_wrapped": _b64(wrapped)}

    def lockout_remaining(self):
        try:
            meta = self._read_meta()
        except (OSError, ValueError):
            return 0
        return max(0, int(meta.get("locked_until", 0) - self._clock()))

    def unlock_with_passcode(self, passcode):
        """True = odemčeno. Špatné heslo = False (a počítadlo); během prodlevy LockedOut."""
        from cryptography.exceptions import InvalidTag
        with self._lock:
            meta = self._read_meta()
            wait = meta.get("locked_until", 0) - self._clock()
            if wait > 0:
                raise LockedOut(wait)
            kdf = meta["kdf"]
            kek = _derive(passcode or "", _unb64(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
            try:
                key = _aesgcm(kek).decrypt(_unb64(meta["pass_nonce"]), _unb64(meta["pass_wrapped"]),
                                           KEY_CHECK_AAD)
            except InvalidTag:
                meta["failures"] = int(meta.get("failures", 0)) + 1
                over = meta["failures"] - FREE_ATTEMPTS
                if over >= 0:
                    meta["locked_until"] = self._clock() + min(LOCKOUT_MAX_S, LOCKOUT_BASE_S * (2 ** over))
                self._write_meta(meta)
                return False
            if meta.get("failures") or meta.get("locked_until"):
                meta["failures"], meta["locked_until"] = 0, 0
                self._write_meta(meta)
            self._set_key(bytearray(key))
            return True

    def os_key_available(self):
        try:
            return bool(self._read_meta().get("dpapi"))
        except (OSError, ValueError):
            return False

    def _dpapi_key(self):
        meta = self._read_meta()
        if not meta.get("dpapi"):
            raise VaultLocked(tr("locked-error-no-account-key"))
        try:
            key = dpapi_unprotect(_unb64(meta["dpapi"]))
        except VaultError as e:
            raise VaultLocked(str(e))
        if len(key) != KEY_LEN:
            raise VaultLocked(tr("locked-error-bad-account-key"))
        return bytearray(key)

    def unlock_with_os(self):
        """Po úspěšném ověření Windows Hello: uvolní klíč z DPAPI."""
        with self._lock:
            self._set_key(self._dpapi_key())
            return True

    def change_passcode(self, old, new):
        check_passcode(new)
        with self._lock:
            if not self.unlock_with_passcode(old):
                return False
            meta = self._read_meta()
            meta.update(self._wrap_with_passcode(self._key, new))
            self._write_meta(meta)
            return True

    def _set_key(self, key):
        self._key = key
        self._aead = _aesgcm(key)

    def lock(self):
        """Zapomene klíč (přepíše ho nulami) a dá vědět posluchačům (UI vyčistí RAM cache)."""
        with self._lock:
            was = self._key is not None
            if self._key is not None:
                for i in range(len(self._key)):
                    self._key[i] = 0
            self._key, self._aead = None, None
        if was:
            for fn in list(self._listeners):
                try:
                    fn()
                except Exception as e:
                    print(f"[Skryté] posluchač zamčení: {e}")
        return was

    # ── šifrované miniatury ──
    def _seal_with(self, aead, media_id, data):
        nonce = secrets.token_bytes(NONCE_LEN)
        blob = THUMB_MAGIC + nonce + aead.encrypt(nonce, bytes(data), f"thumb:{int(media_id)}".encode())
        os.makedirs(self.thumb_dir, exist_ok=True)
        target = self.thumb_path(media_id)
        tmp = f"{target}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, target)
        return target

    def seal_thumbnail(self, media_id, data):
        """Zašifruje JPEG miniatury. Zamčeno: klíč tiše z DPAPI (skrytí nic neodhaluje)."""
        self._ensure_thumb_render_version()          # staré pečeti pryč dřív, než přibude nová
        with self._lock:
            aead = self._aead
        if aead is None:
            key = self._dpapi_key()
            aead = _aesgcm(key)
            for i in range(len(key)):
                key[i] = 0
        return self._seal_with(aead, media_id, data)

    def _ensure_thumb_render_version(self):
        """Zapečetěné miniatury jiné verze pipeline / jiného kontraktu barev (scanner.
        thumb_version_tag) se smažou – vytvoří se znovu z originálu při dalším zobrazení.
        Značka RENDER obsahuje jen číslo verze a otisk profilu displeje (nic soukromého)."""
        try:
            import scanner
            tag = scanner.thumb_version_tag()          # levné; mění se i po změně v Předvolbách
            if getattr(self, "_thumb_render_tag", None) == tag:
                return
            marker = os.path.join(self.thumb_dir, "RENDER")
            try:
                with open(marker, encoding="utf-8") as f:
                    current = f.read().strip()
            except OSError:
                current = None
            if current != tag and os.path.isdir(self.thumb_dir):
                for e in os.scandir(self.thumb_dir):
                    if e.name.endswith(".kvt"):
                        try:
                            os.remove(e.path)
                        except OSError:
                            pass
            if current != tag:
                os.makedirs(self.thumb_dir, exist_ok=True)
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(tag)
            self._thumb_render_tag = tag
        except Exception:
            pass

    def open_thumbnail(self, media_id):
        """Dešifrovaný JPEG, nebo None (chybí / poškozený). Zamčeno: VaultLocked."""
        aead = self._aead
        if aead is None:
            raise VaultLocked(tr("locked-error-locked"))
        self._ensure_thumb_render_version()
        try:
            with open(self.thumb_path(media_id), "rb") as f:
                blob = f.read()
        except OSError:
            return None
        if not blob.startswith(THUMB_MAGIC) or len(blob) < len(THUMB_MAGIC) + NONCE_LEN + 16:
            return None
        nonce = blob[len(THUMB_MAGIC):len(THUMB_MAGIC) + NONCE_LEN]
        try:
            return aead.decrypt(nonce, blob[len(THUMB_MAGIC) + NONCE_LEN:], f"thumb:{int(media_id)}".encode())
        except Exception:
            return None

    def delete_thumbnail(self, media_id):
        try:
            os.remove(self.thumb_path(media_id))
        except OSError:
            pass


def check_passcode(passcode):
    if not passcode or len(passcode) < PASSCODE_MIN:
        raise VaultError(tr("locked-error-passcode-min", PASSCODE_MIN=PASSCODE_MIN))


_vault = None


def get():
    global _vault
    if _vault is None:
        _vault = Vault()
    return _vault


# ── skrytí / odkrytí (DB + soubory) ──────────────────────────────────────────────────────

def cache_files_for(source):
    """Všechny diskové cache odvozené z originálu: náhledy (všech verzí), pás časové osy,
    proxy videa a jeho značka, klip karty Cesty."""
    import preview_cache
    import video_proxy
    import card_media
    out = glob.glob(glob.escape(preview_cache.base_path(source)) + ".*")
    out += [video_proxy.proxy_path(source), video_proxy._marker_path(source), card_media.highlight_path(source)]
    return [p for p in out if os.path.exists(p)]


def purge_caches(sources):
    n = 0
    for src in sources:
        if src:
            for p in cache_files_for(src):
                n += bool(shred_file(p))
    return n


def hide_items(media_ids, vault=None):
    """Přesune položky do složky Skryté: zašifruje miniatury, smaže jejich nešifrované cache,
    odvozená data (DB) a ořezy tváří. Vrací počet skrytých. Bez klíče VaultLocked (nic se nestane)."""
    import database
    vault = vault or get()
    if not vault.is_configured():
        raise VaultLocked(tr("locked-error-not-set-up"))
    ids = sorted({int(i) for i in media_ids})
    if not ids:
        return 0
    conn = database.get_connection()
    try:
        marks = ",".join("?" * len(ids))
        pre = conn.execute(f"SELECT id, thumbnail_path FROM media_items WHERE id IN ({marks}) "
                           f"AND COALESCE(is_hidden, 0) = 0 AND COALESCE(is_trashed, 0) = 0", ids).fetchall()
    finally:
        conn.close()
    # 1) nejdřív zašifrovat (pád uprostřed = položka zůstane viditelná, nic se neztratí)
    for mid, thumb in pre:
        if thumb and os.path.exists(thumb):
            with open(thumb, "rb") as f:
                vault.seal_thumbnail(mid, f.read())
    # 2) příznak + odvozená data v jedné transakci
    res = database.hide_media([mid for mid, _ in pre])
    rows = res["rows"]
    sources = [p for r in rows for p in (r[1], r[2]) if p]
    add_hidden_paths(sources)
    # 3) nešifrované stopy pryč: miniatury, náhledy / pásy / proxy, ořezy tváří
    for _mid, thumb in pre:
        if thumb and os.path.exists(thumb):
            shred_file(thumb)
    purge_caches(sources)
    import micro_previews                       # Sprint 2: the grid's micro previews too
    micro_previews.purge_ids([r[0] for r in rows])
    for crop in res["crops"]:
        if crop and os.path.exists(crop):
            shred_file(crop)
    if res["people"]:
        try:
            import face_cluster
            face_cluster.refresh_person_covers(res["people"])
        except Exception as e:
            print(f"[Skryté] portréty osob: {e}")
    return len(rows)


def unhide_items(media_ids, vault=None):
    """Vrátí položky do knihovny. Miniatura se dešifruje zpět do cache miniatur (jinak ji
    scanner vytvoří znovu), tváře / vektory / OCR dopočítá démon. Vyžaduje odemčení."""
    import database
    vault = vault or get()
    if not vault.is_unlocked:
        raise VaultLocked(tr("locked-error-locked"))
    rows = database.unhide_media(media_ids)
    if not rows:
        return 0
    thumb_dir = paths.THUMB_DIR
    updates, sources = [], []
    for mid, fp, _mtype in rows:
        sources.append(fp)
        data = vault.open_thumbnail(mid)
        if data:
            os.makedirs(thumb_dir, exist_ok=True)
            target = os.path.join(thumb_dir, f"thumb_{int(mid)}.jpg")
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
            updates.append((target, mid))
        vault.delete_thumbnail(mid)
    if updates:
        with database.db_transaction() as conn:
            conn.executemany("UPDATE media_items SET thumbnail_path = ? WHERE id = ?", updates)
    conn = database.get_connection()
    try:
        marks = ",".join("?" * len(rows))
        paired = [p for (p,) in conn.execute(f"SELECT paired_video_path FROM media_items WHERE id IN ({marks})",
                                             [r[0] for r in rows]) if p]
    finally:
        conn.close()
    remove_hidden_paths(sources + paired)
    return len(rows)


def startup_sweep(vault=None):
    """Po pádu uprostřed skrytí: nešifrované miniatury a cache skrytých položek smazat,
    zašifrované miniatury bez skryté položky (odkrytí přerušené / položka smazaná) uklidit."""
    import database
    vault = vault or get()
    conn = database.get_connection()
    try:
        hidden = conn.execute("SELECT id, file_path, paired_video_path FROM media_items WHERE is_hidden = 1").fetchall()
    finally:
        conn.close()
    hidden_ids = {r[0] for r in hidden}
    removed = 0
    for mid, _fp, _pv in hidden:
        plain = os.path.join(paths.THUMB_DIR, f"thumb_{int(mid)}.jpg")
        if os.path.exists(plain):
            removed += bool(shred_file(plain))
    removed += purge_caches([p for r in hidden for p in (r[1], r[2]) if p])
    import micro_previews                       # Sprint 2: no micro preview of a hidden item
    micro_previews.purge_ids(sorted(hidden_ids))
    for path in glob.glob(os.path.join(vault.thumb_dir, "*.kvt")):
        try:
            mid = int(os.path.splitext(os.path.basename(path))[0])
        except ValueError:
            continue
        if mid not in hidden_ids:
            try:
                os.remove(path)
            except OSError:
                pass
    set_hidden_paths([p for r in hidden for p in (r[1], r[2]) if p])
    return removed


def render_sealed_thumbnail(media_id, file_path, media_type, paired_video, vault=None):
    """Miniatura skryté položky, která ji v trezoru nemá: vytvoří se v paměti z originálu
    (nikdy nešifrovaně na disk), zašifruje a vrátí JPEG. None = nejde (chybí soubor)."""
    import scanner
    vault = vault or get()
    data = scanner.render_thumbnail_bytes(file_path, media_type, paired_video)
    if data:
        vault.seal_thumbnail(media_id, data)
    return data
