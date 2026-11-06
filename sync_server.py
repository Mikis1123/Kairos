"""Kairos LAN bridge: the desktop library served to the iOS companion (Phase 1, 2026-11-06).

One small HTTPS + WebSocket server on the local network. Python standard library only, plus
`cryptography` for the self-signed certificate, which is already a dependency (locked_folder).

Security model
  * TLS everywhere. A self-signed EC P-256 certificate is created on first start, and its SHA-256
    fingerprint goes into the pairing QR code. The phone pins exactly that certificate, so no CA is
    involved and a LAN attacker cannot intercept the connection.
  * Pairing: the desktop shows a 6-digit PIN plus a QR code (`kairos://pair?...`). The PIN is
    single-use, lives PIN_TTL_S seconds and allows PIN_MAX_ATTEMPTS guesses. A correct PIN returns a
    256-bit bearer token. Only its SHA-256 is stored (sync/devices.json), so a stolen file does not
    authenticate anyone.
  * Every endpoint except /v1/hello and /v1/pair needs `Authorization: Bearer <token>`.
  * Absolute file paths never leave the machine. Hidden (vault) and trashed items, and the motion
    halves of Live Photos, are never served: to the phone they read as deleted. Syncing the vault
    needs its own key exchange and is out of scope for Phase 1.

Incremental sync
  `sync_log` is a change journal filled by SQLite triggers, so every writer is covered without
  touching any call site (scanner, trips, faces, albums, edits, the vault). Each entity owns
  exactly ONE row, and a change moves it to a new AUTOINCREMENT `seq`. The journal is therefore
  bounded by the number of entities, not the number of writes. UPDATE triggers fire only when a
  synced column really changes (WHEN OLD.x IS NOT NEW.x), so daemon churn such as phash, faces_scanned
  or crop versions costs nothing. The current state of the row is serialized at read time: a row
  that is missing or not visible becomes a tombstone ({"op": "delete"}).

Off the GUI thread, aware of battery and idle state
  The listener and every request run on daemon threads. Bulk transfers (map packs, and originals
  requested with bulk=1) are refused with 503 + Retry-After when power.py says PAUSE (battery saver
  or a low battery). While the user is interacting with the desktop (governor busy), bulk transfers
  are paced. Interactive requests, such as opening one photo on the phone, are never held back.

Run standalone for iOS development:  python sync_server.py [--port 52790]
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import socket
import socketserver
import sqlite3
import ssl
import struct
import threading
import time
import uuid
import http.server
from urllib.parse import parse_qs, urlparse

import database
import paths

PROTOCOL = 1
DEFAULT_PORT = 52790
CHUNK = 256 * 1024
PIN_TTL_S = 120
PIN_MAX_ATTEMPTS = 5
MAX_CONCURRENT = 8                 # HTTP requests in flight (WebSockets are counted separately)
MAX_SOCKETS = 4                    # WebSocket change feeds
PAGE_DEFAULT, PAGE_MAX = 500, 2000
MICRO_BATCH_MAX = 500
WS_KEEPALIVE_S = 20.0
NOTIFY_POLL_S = 1.0                # the change notifier polls MAX(seq), an indexed read
BULK_PACE_S = 0.02                 # per chunk while the desktop user interacts (~12 MB/s cap)
RETRY_AFTER_S = 300
SOCKET_TIMEOUT_S = 30.0
SCHEMA_VERSION = 1                 # sync_log + triggers; bump = triggers are recreated
MICRO_MAGIC = b"KMP1"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ── what is synced ────────────────────────────────────────────────────────────
# Columns whose change is worth a sync. The others (phash, sharpness, faces_scanned, embeddings'
# bookkeeping, raw_metadata_json, ...) are desktop-internal and never wake the journal.
MEDIA_COLS = (
    "file_name", "file_size", "file_mtime", "file_ext", "media_type", "date_taken", "subsec_time",
    "timezone_offset", "latitude", "longitude", "altitude", "compass_heading", "place_city",
    "place_country", "device_make", "device_model", "lens_model", "f_number", "exposure_time", "iso",
    "focal_length", "focal_length_35mm", "content_identifier", "paired_video_path", "is_favorite",
    "event_id", "chapter_id", "is_trashed", "is_hidden", "gps_source", "fingerprint", "availability",
    "thumbnail_path",
)
EVENT_COLS = ("title", "location_name", "start_date", "end_date", "item_count", "cover_media_id",
              "kind", "parent_id", "tint")
PERSON_COLS = ("name", "key_face_id", "is_favorite", "is_confirmed", "cover_face_path")
FACE_COLS = ("media_id", "person_id", "bbox_x", "bbox_y", "bbox_w", "bbox_h", "quality_score", "is_valid")
ALBUM_COLS = ("name", "kind", "parent_id", "position", "sort_mode", "cover_media_id", "query", "filter")
ENTITIES = ("media", "event", "person", "face", "album", "edit")

# On the wire: client-facing names. Paths are replaced by flags (has_thumb / live).
_MEDIA_WIRE = (
    "file_name", "file_size", "file_mtime", "file_ext", "media_type", "date_taken", "subsec_time",
    "timezone_offset", "latitude", "longitude", "altitude", "compass_heading", "place_city",
    "place_country", "device_make", "device_model", "lens_model", "f_number", "exposure_time", "iso",
    "focal_length", "focal_length_35mm", "content_identifier", "is_favorite", "event_id",
    "chapter_id", "gps_source", "fingerprint", "availability",
)

_CTYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
           ".heic": "image/heic", ".heif": "image/heif", ".gif": "image/gif", ".tif": "image/tiff",
           ".tiff": "image/tiff", ".dng": "image/x-adobe-dng", ".mp4": "video/mp4",
           ".mov": "video/quicktime", ".m4v": "video/x-m4v", ".mbtiles": "application/vnd.sqlite3"}


# ── schema: change journal + triggers ────────────────────────────────────────

def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _changed(cols, present):
    cols = [c for c in cols if c in present]
    return " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in cols) or "0"


def _log(entity, expr):
    # DELETE + INSERT, not INSERT OR REPLACE: an outer "INSERT OR IGNORE" would override the
    # trigger's conflict policy (SQLite rule) and silently skip the bump.
    return (f"DELETE FROM sync_log WHERE entity = '{entity}' AND entity_id = {expr}; "
            f"INSERT INTO sync_log (entity, entity_id) VALUES ('{entity}', {expr});")


def _log_select(entity, col, from_where):
    """Bump every entity selected by `SELECT col FROM_WHERE`."""
    sel = f"SELECT {col} {from_where}"
    return (f"DELETE FROM sync_log WHERE entity = '{entity}' AND entity_id IN ({sel}); "
            f"INSERT INTO sync_log (entity, entity_id) SELECT '{entity}', {col} {from_where};")


def _trigger_sql(conn):
    m, e, p = _cols(conn, "media_items"), _cols(conn, "events"), _cols(conn, "people")
    f, a = _cols(conn, "face_instances"), _cols(conn, "albums")
    out = []

    def trig(name, when, table, body, cond=None):
        w = f" WHEN {cond}" if cond else ""
        out.append(f"CREATE TRIGGER IF NOT EXISTS ksync_{name} AFTER {when} ON {table}{w} BEGIN {body} END")

    for table, entity, cols, present in (("media_items", "media", MEDIA_COLS, m),
                                         ("events", "event", EVENT_COLS, e),
                                         ("people", "person", PERSON_COLS, p),
                                         ("face_instances", "face", FACE_COLS, f),
                                         ("albums", "album", ALBUM_COLS, a)):
        trig(f"{entity}_ins", "INSERT", table, _log(entity, "NEW.id"))
        trig(f"{entity}_upd", "UPDATE", table, _log(entity, "NEW.id"), _changed(cols, present))
        trig(f"{entity}_del", "DELETE", table, _log(entity, "OLD.id"))
    # Unhiding / restoring a photo brings back its faces, edit and album membership on the phone
    # (the phone dropped them with the tombstone).
    trig("media_vis", "UPDATE", "media_items",
         _log_select("face", "id", "FROM face_instances WHERE media_id = NEW.id")
         + _log_select("edit", "media_id", "FROM media_edits WHERE media_id = NEW.id")
         + _log_select("album", "album_id", "FROM album_items WHERE media_id = NEW.id"),
         "OLD.is_hidden IS NOT NEW.is_hidden OR OLD.is_trashed IS NOT NEW.is_trashed")
    trig("item_ins", "INSERT", "album_items", _log("album", "NEW.album_id"))
    trig("item_upd", "UPDATE", "album_items", _log("album", "NEW.album_id"),
         "OLD.position IS NOT NEW.position OR OLD.album_id IS NOT NEW.album_id")
    trig("item_del", "DELETE", "album_items", _log("album", "OLD.album_id"))
    trig("edit_ins", "INSERT", "media_edits", _log("edit", "NEW.media_id"))
    trig("edit_upd", "UPDATE", "media_edits", _log("edit", "NEW.media_id"), "OLD.params IS NOT NEW.params")
    trig("edit_del", "DELETE", "media_edits", _log("edit", "OLD.media_id"))
    return out


def ensure_schema(conn):
    """Creates the journal and the triggers, and backfills every existing entity once. Idempotent.
    Called when the server starts, so a library that never enables sync carries no triggers."""
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sync_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "entity TEXT NOT NULL, entity_id INTEGER NOT NULL, UNIQUE (entity, entity_id))")
    row = conn.execute("SELECT value FROM app_meta WHERE key = 'sync_schema'").fetchone()
    if row is not None and row[0] == str(SCHEMA_VERSION):
        return False
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                "AND name LIKE 'ksync_%'").fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for sql in _trigger_sql(conn):
        conn.execute(sql)
    if row is None:                                   # first install: the whole library is "new"
        for entity, col, table in (("event", "id", "events"), ("person", "id", "people"),
                                   ("album", "id", "albums"), ("media", "id", "media_items"),
                                   ("face", "id", "face_instances"), ("edit", "media_id", "media_edits")):
            conn.execute(f"INSERT OR IGNORE INTO sync_log (entity, entity_id) "
                         f"SELECT '{entity}', {col} FROM {table} ORDER BY {col}")
    conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES ('sync_library_id', ?)", (uuid.uuid4().hex,))
    conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('sync_schema', ?)", (str(SCHEMA_VERSION),))
    return True


# ── serialization ────────────────────────────────────────────────────────────

def _chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _qmarks(n):
    return ",".join("?" * n)


def _visible_ids(conn, ids):
    out = set()
    for part in _chunks(ids):
        out.update(r[0] for r in conn.execute(
            f"SELECT m.id FROM media_items m WHERE m.id IN ({_qmarks(len(part))}) "
            f"AND {database.visible_media_sql('m')}", part).fetchall())
    return out


def _rows(conn, sql, ids, key="id"):
    out = {}
    for part in _chunks(ids):
        cur = conn.execute(sql.format(q=_qmarks(len(part))), part)
        names = [d[0] for d in cur.description]
        for r in cur.fetchall():
            d = dict(zip(names, r))
            out[d[key]] = d
    return out


def _media(conn, ids):
    m = _cols(conn, "media_items")
    wanted = ["id", "thumbnail_path", "paired_video_path"] + [c for c in _MEDIA_WIRE if c in m]
    rows = _rows(conn, f"SELECT {', '.join('m.' + c for c in wanted)} FROM media_items m "
                       f"WHERE m.id IN ({{q}}) AND {database.visible_media_sql('m')}", ids)
    out = {}
    for i, r in rows.items():
        d = {c: r.get(c) for c in _MEDIA_WIRE}
        d["id"] = i
        d["is_favorite"] = bool(r.get("is_favorite"))
        d["has_thumb"] = bool(r.get("thumbnail_path"))
        d["live"] = bool(r.get("paired_video_path"))
        out[i] = d
    return out


def _events(conn, ids):
    e = _cols(conn, "events")
    cols = ["id"] + [c for c in EVENT_COLS if c in e]
    rows = _rows(conn, f"SELECT {', '.join(cols)} FROM events WHERE id IN ({{q}})", ids)
    return {i: {c: r.get(c) for c in ("id",) + EVENT_COLS} for i, r in rows.items()}


def _people(conn, ids):
    p = _cols(conn, "people")
    cols = ["id"] + [c for c in ("name", "key_face_id", "is_favorite", "is_confirmed") if c in p]
    rows = _rows(conn, f"SELECT {', '.join(cols)} FROM people WHERE id IN ({{q}})", ids)
    return {i: {"id": i, "name": r.get("name"), "key_face_id": r.get("key_face_id"),
                "is_favorite": bool(r.get("is_favorite")), "is_confirmed": bool(r.get("is_confirmed"))}
            for i, r in rows.items()}


def _faces(conn, ids):
    f = _cols(conn, "face_instances")
    cols = ["fi.id"] + [f"fi.{c}" for c in FACE_COLS if c in f]
    rows = _rows(conn, f"SELECT {', '.join(cols)} FROM face_instances fi JOIN media_items m ON m.id = fi.media_id "
                       f"WHERE fi.id IN ({{q}}) AND COALESCE(fi.is_valid, 1) != 0 "
                       f"AND {database.visible_media_sql('m')}", ids)
    return {i: {"id": i, "media_id": r["media_id"], "person_id": r.get("person_id"),
                "bbox": [r.get("bbox_x"), r.get("bbox_y"), r.get("bbox_w"), r.get("bbox_h")],
                "quality": r.get("quality_score")} for i, r in rows.items()}


def _albums(conn, ids):
    a = _cols(conn, "albums")
    cols = ["id"] + [c for c in ALBUM_COLS + ("created_at",) if c in a]
    rows = _rows(conn, f"SELECT {', '.join(cols)} FROM albums WHERE id IN ({{q}})", ids)
    out = {}
    for i, r in rows.items():
        items = [x[0] for x in conn.execute(
            f"SELECT ai.media_id FROM album_items ai JOIN media_items m ON m.id = ai.media_id "
            f"WHERE ai.album_id = ? AND {database.visible_media_sql('m')} "
            f"ORDER BY COALESCE(ai.position, 1e18), ai.added_at, ai.media_id", (i,)).fetchall()]
        d = {c: r.get(c) for c in ("id",) + ALBUM_COLS + ("created_at",)}
        d["items"] = items
        out[i] = d
    return out


def _edits(conn, ids):
    rows = _rows(conn, f"SELECT e.media_id, e.params, e.updated_at FROM media_edits e "
                       f"JOIN media_items m ON m.id = e.media_id "
                       f"WHERE e.media_id IN ({{q}}) AND {database.visible_media_sql('m')}", ids, key="media_id")
    out = {}
    for i, r in rows.items():
        try:
            params = json.loads(r["params"])
        except (TypeError, ValueError):
            continue
        out[i] = {"media_id": i, "params": params, "updated_at": r.get("updated_at")}
    return out


_SERIALIZERS = {"media": _media, "event": _events, "person": _people, "face": _faces,
                "album": _albums, "edit": _edits}


def library_id(conn):
    row = conn.execute("SELECT value FROM app_meta WHERE key = 'sync_library_id'").fetchone()
    return row[0] if row else ""


def changes(conn, since=0, limit=PAGE_DEFAULT):
    """One page of the journal: {"library_id", "cursor", "more", "reset", "changes": [...]}.
    `since` is the cursor of the previous page (0 = everything). A cursor ahead of the journal
    (the desktop restored a backup) returns reset=True: the phone must drop its replica."""
    limit = max(1, min(int(limit), PAGE_MAX))
    since = max(0, int(since))
    conn.execute("BEGIN")                       # one snapshot: journal and rows agree
    try:
        top = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_log").fetchone()[0]
        if since > top:
            return {"protocol": PROTOCOL, "library_id": library_id(conn), "cursor": 0, "more": True,
                    "reset": True, "changes": []}
        log = conn.execute("SELECT seq, entity, entity_id FROM sync_log WHERE seq > ? ORDER BY seq LIMIT ?",
                           (since, limit + 1)).fetchall()
        more = len(log) > limit
        log = log[:limit]
        by_entity = {}
        for _seq, entity, eid in log:
            by_entity.setdefault(entity, []).append(eid)
        data = {ent: (_SERIALIZERS[ent](conn, ids) if ent in _SERIALIZERS else {})
                for ent, ids in by_entity.items()}
        out = []
        for seq, entity, eid in log:
            d = data.get(entity, {}).get(eid)
            if d is None:
                out.append({"seq": seq, "entity": entity, "id": eid, "op": "delete"})
            else:
                out.append({"seq": seq, "entity": entity, "id": eid, "op": "upsert", "data": d})
        return {"protocol": PROTOCOL, "library_id": library_id(conn),
                "cursor": log[-1][0] if log else since, "more": more, "reset": False, "changes": out}
    finally:
        conn.execute("COMMIT")


def pack_micro(items):
    """[(id, jpeg bytes)] -> b"KMP1" + (u32 id, u32 length, bytes)*, big-endian."""
    parts = [MICRO_MAGIC]
    for i, data in items:
        parts.append(struct.pack(">II", int(i), len(data)))
        parts.append(bytes(data))
    return b"".join(parts)


def unpack_micro(blob):
    if blob[:4] != MICRO_MAGIC:
        raise ValueError("bad micro frame")
    out, pos = [], 4
    while pos < len(blob):
        i, n = struct.unpack_from(">II", blob, pos)
        pos += 8
        out.append((i, blob[pos:pos + n]))
        pos += n
    return out


# ── credentials ──────────────────────────────────────────────────────────────

def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path, data):
    import fs_atomic
    tmp = fs_atomic.unique_tmp(path)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    fs_atomic.replace(tmp, path)


def ensure_certificate(state_dir):
    """(cert_path, key_path, sha256 fingerprint of the DER certificate as hex)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    cert_path, key_path = os.path.join(state_dir, "server_cert.pem"), os.path.join(state_dir, "server_key.pem")
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        os.makedirs(state_dir, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Kairos Desktop")])
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - _dt.timedelta(days=1)).not_valid_after(now + _dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        with open(key_path, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        with open(cert_path, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(cert_path, "rb") as fh:
        der = ssl.PEM_cert_to_DER_cert(fh.read().decode("ascii"))
    return cert_path, key_path, hashlib.sha256(der).hexdigest()


class Credentials:
    """Paired devices (token hashes) and the one active pairing offer."""

    def __init__(self, state_dir, clock=time.monotonic):
        self.path = os.path.join(state_dir, "devices.json")
        self._clock = clock
        self._lock = threading.Lock()
        self._pin_hash = None
        self._pin_expires = 0.0
        self._attempts = 0
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.devices = list(json.load(fh).get("devices", []))
        except (OSError, ValueError):
            self.devices = []

    def _save(self):
        _write_json(self.path, {"devices": self.devices})

    def begin_pairing(self):
        pin = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self._pin_hash = _sha(pin)
            self._pin_expires = self._clock() + PIN_TTL_S
            self._attempts = 0
        return pin

    def cancel_pairing(self):
        with self._lock:
            self._pin_hash = None

    def pairing_active(self):
        with self._lock:
            return self._pin_hash is not None and self._clock() < self._pin_expires

    def pair(self, pin, device_name, device_id):
        """Bearer token, or raises PermissionError(reason)."""
        with self._lock:
            if self._pin_hash is None or self._clock() >= self._pin_expires:
                self._pin_hash = None
                raise PermissionError("no_pairing")
            self._attempts += 1
            if not hmac.compare_digest(_sha(str(pin or "")), self._pin_hash):
                if self._attempts >= PIN_MAX_ATTEMPTS:
                    self._pin_hash = None
                raise PermissionError("bad_pin")
            self._pin_hash = None                     # single use
            token = secrets.token_urlsafe(32)
            dev_id = str(device_id or uuid.uuid4().hex)[:64]
            self.devices = [d for d in self.devices if d.get("id") != dev_id]
            self.devices.append({"id": dev_id, "name": str(device_name or "iPhone")[:80],
                                 "token_sha256": _sha(token),
                                 "paired_at": _dt.datetime.now().isoformat(timespec="seconds")})
            self._save()
            return token, dev_id

    def authenticate(self, token):
        if not token:
            return None
        h = _sha(token)
        with self._lock:
            hit = None
            for d in self.devices:                # every entry is compared, constant time each
                if hmac.compare_digest(h, d.get("token_sha256", "")):
                    hit = d
            return hit

    def revoke(self, device_id):
        with self._lock:
            n = len(self.devices)
            self.devices = [d for d in self.devices if d.get("id") != device_id]
            if len(self.devices) != n:
                self._save()
                return True
            return False


# ── power / idle gates ───────────────────────────────────────────────────────

def _power_state():
    try:
        import power
        mon = power.get()
        st = mon.state()
        if not st.on_battery:
            return "ac", True
        return ("saver" if st.saver else "battery"), mon.action(True) != power.PAUSE
    except Exception:                       # noqa: BLE001 – a broken probe must not block sync
        return "unknown", True


def _user_busy():
    try:
        import governor
        return governor.get().busy()
    except Exception:                       # noqa: BLE001
        return False


def lan_address():
    """The address the phone should dial (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ── server ───────────────────────────────────────────────────────────────────

class _HttpError(Exception):
    def __init__(self, status, code, headers=None):
        super().__init__(code)
        self.status, self.code, self.headers = status, code, headers or {}


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError, OSError)):
            return                          # dropped Wi-Fi, an unpinned client hanging up: normal
        super().handle_error(request, client_address)


class SyncServer:
    def __init__(self, host="0.0.0.0", port=DEFAULT_PORT, db_path=None, state_dir=None,
                 packs_dir=None, micro_source=None, use_tls=True, name=None):
        self.db_path = db_path or database.DB_PATH
        self.state_dir = state_dir or os.path.join(paths.DATA_DIR, "sync")
        self.packs_dir = packs_dir or paths.MAP_PACKS_DIR
        self.micro_source = micro_source          # callable(ids) -> [(id, bytes)]; None = micro store
        self.name = name or socket.gethostname()
        os.makedirs(self.state_dir, exist_ok=True)
        self.credentials = Credentials(self.state_dir)
        # GUI hook: called (from a request thread) with (kind, media_id) after the phone wrote
        # an edit / favourite. The desktop marshals it to the GUI thread itself.
        self.on_remote_change = None
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            with conn:
                ensure_schema(conn)
            self.library_id = library_id(conn)
        finally:
            conn.close()
        self.use_tls = use_tls
        self.fingerprint = ""
        self._ctx = None
        if use_tls:
            cert, key, self.fingerprint = ensure_certificate(self.state_dir)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
            self._ctx = ctx
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._sockets = threading.BoundedSemaphore(MAX_SOCKETS)
        self._seq_cond = threading.Condition()
        self._top_seq = self._read_top()
        self._stop = threading.Event()
        self._httpd = _Server((host, port), self._handler_class())
        self._httpd.sync_server = self
        self.host, self.port = host, self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, name="sync-http", daemon=True).start()
        threading.Thread(target=self._notifier, name="sync-notify", daemon=True).start()

    # ── db ──
    def connect_ro(self):
        uri = "file:" + self.db_path.replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def connect_rw(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _read_top(self):
        try:
            conn = self.connect_ro()
            try:
                return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_log").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    def _notifier(self):
        while not self._stop.wait(NOTIFY_POLL_S):
            top = self._read_top()
            if top != self._top_seq:
                with self._seq_cond:
                    self._top_seq = top
                    self._seq_cond.notify_all()

    def wait_change(self, seen, timeout):
        with self._seq_cond:
            if self._top_seq == seen and not self._stop.is_set():
                self._seq_cond.wait(timeout)
            return self._top_seq

    # ── pairing ──
    def begin_pairing(self, host=None):
        """{"pin", "uri", "expires_s"} for the desktop UI: the PIN to show and the QR payload."""
        pin = self.credentials.begin_pairing()
        h = host or (lan_address() if self.host in ("0.0.0.0", "") else self.host)
        uri = (f"kairos://pair?h={h}&p={self.port}&fp={self.fingerprint}&lib={self.library_id}"
               f"&pin={pin}&v={PROTOCOL}")
        return {"pin": pin, "uri": uri, "expires_s": PIN_TTL_S}

    def shutdown(self):
        self._stop.set()
        with self._seq_cond:
            self._seq_cond.notify_all()
        self._httpd.shutdown()
        self._httpd.server_close()

    # ── HTTP ──
    def _handler_class(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "KairosSync/1"
            sys_version = ""

            def setup(self):
                self.request.settimeout(SOCKET_TIMEOUT_S)
                if server._ctx is not None:
                    # handshake on the request thread, never on the accept loop
                    self.request = server._ctx.wrap_socket(self.request, server_side=True)
                super().setup()

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_PUT(self):
                self._dispatch("PUT")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def do_HEAD(self):
                self._dispatch("HEAD")

            # ── plumbing ──
            def _dispatch(self, method):
                url = urlparse(self.path)
                self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                parts = [p for p in url.path.split("/") if p]
                is_ws = parts == ["v1", "events"]
                gate = server._sockets if is_ws else server._slots
                if not gate.acquire(blocking=False):
                    # i18n-ok: HTTP header name, not UI text
                    return self._error(_HttpError(503, "busy", {"Retry-After": "2"}))
                try:
                    server._route(self, method, parts)
                except _HttpError as e:
                    self._error(e)
                except (ConnectionError, TimeoutError, ssl.SSLError):
                    self.close_connection = True
                except Exception as e:                      # noqa: BLE001
                    print(f"[Sync] {method} {url.path}: {e!r}")
                    self._error(_HttpError(500, "internal"))
                finally:
                    gate.release()

            def body_json(self, cap=256 * 1024):
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > cap:
                    raise _HttpError(400, "bad_body")
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise _HttpError(400, "bad_json") from None

            def send_json(self, obj, status=200, headers=None):
                data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(data) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
                    import gzip                     # URLSession asks for it and inflates by itself
                    data = gzip.compress(data, 5)
                    headers = dict(headers or {}, **{"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
                self.send_bytes(data, "application/json; charset=utf-8", status, headers)

            def send_bytes(self, data, ctype, status=200, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _error(self, e):
                try:
                    self.send_json({"error": e.code}, e.status, e.headers)
                except OSError:
                    self.close_connection = True

            def device(self):
                auth = self.headers.get("Authorization", "")
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                dev = server.credentials.authenticate(token)
                if dev is None:
                    raise _HttpError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
                return dev

        return Handler

    def _route(self, h, method, parts):
        if parts == ["v1", "hello"] and method in ("GET", "HEAD"):
            state, bulk_ok = _power_state()
            return h.send_json({"app": "Kairos", "protocol": PROTOCOL, "name": self.name,
                                "library_id": self.library_id, "pairing": self.credentials.pairing_active(),
                                "power": state, "bulk_ok": bulk_ok})
        if parts == ["v1", "pair"] and method == "POST":
            body = h.body_json()
            try:
                token, dev_id = self.credentials.pair(body.get("pin"), body.get("device_name"), body.get("device_id"))
            except PermissionError as e:
                raise _HttpError(403, str(e)) from None
            return h.send_json({"token": token, "device_id": dev_id, "library_id": self.library_id,
                                "name": self.name, "protocol": PROTOCOL})

        dev = h.device()
        if parts == ["v1", "unpair"] and method == "POST":
            self.credentials.revoke(dev.get("id"))
            return h.send_json({"ok": True})
        if parts == ["v1", "changes"] and method == "GET":
            conn = self.connect_ro()
            try:
                page = changes(conn, _int(h.query.get("since", 0)), _int(h.query.get("limit", PAGE_DEFAULT)))
            finally:
                conn.close()
            return h.send_json(page)
        if parts == ["v1", "events"] and method == "GET":
            return self._websocket(h)
        if parts == ["v1", "micro"] and method == "GET":
            ids = _id_list(h.query.get("ids", ""), MICRO_BATCH_MAX)
            return h.send_bytes(pack_micro(self._micro(ids)), "application/octet-stream")
        if len(parts) == 4 and parts[:2] == ["v1", "media"] and parts[2].isdigit():
            return self._media_route(h, method, int(parts[2]), parts[3])
        if len(parts) == 4 and parts[:2] == ["v1", "face"] and parts[2].isdigit() and parts[3] == "crop":
            return self._face_crop(h, int(parts[2]))
        if parts == ["v1", "packs"] and method == "GET":
            return h.send_json({"packs": self.list_packs()})
        if len(parts) == 3 and parts[:2] == ["v1", "packs"] and method in ("GET", "HEAD"):
            return self._pack(h, parts[2])
        raise _HttpError(404, "not_found")

    # ── media ──
    def _media_row(self, media_id):
        conn = self.connect_ro()
        try:
            r = conn.execute(f"SELECT m.file_path, m.thumbnail_path, m.paired_video_path, m.fingerprint, "
                             f"m.file_mtime FROM media_items m WHERE m.id = ? AND {database.visible_media_sql('m')}",
                             (media_id,)).fetchone()
        finally:
            conn.close()
        if r is None:
            raise _HttpError(404, "not_found")
        return r

    def _media_route(self, h, method, media_id, what):
        if what in ("thumb", "original", "live") and method in ("GET", "HEAD"):
            file_path, thumb, live, fp, mtime = self._media_row(media_id)
            path = {"thumb": thumb, "original": file_path, "live": live}[what]
            if not path:
                raise _HttpError(404, "no_" + what)
            bulk = h.query.get("bulk") == "1"
            if what != "thumb":
                import cloud_files
                if not cloud_files.local_ok(path):        # never trigger a OneDrive download from the phone
                    raise _HttpError(409, "cloud_only")
            if bulk:
                self._require_bulk()
            etag = f'"{fp or ""}-{int(mtime or 0)}-{what}"'
            return self._send_file(h, path, etag=etag, bulk=bulk)
        if what == "edit" and method in ("PUT", "DELETE"):
            return self._put_edit(h, media_id, None if method == "DELETE" else h.body_json())
        if what == "favorite" and method == "PUT":
            value = bool(h.body_json().get("value"))
            self._media_row(media_id)
            conn = self.connect_rw()
            try:
                with conn:
                    conn.execute("UPDATE media_items SET is_favorite = ? WHERE id = ?", (int(value), media_id))
            finally:
                conn.close()
            self._notify_gui("favorite", media_id)
            return h.send_json({"id": media_id, "is_favorite": value})
        raise _HttpError(404, "not_found")

    def _put_edit(self, h, media_id, body):
        import quick_edit
        self._media_row(media_id)
        params = None if body is None else body.get("params", body)
        if params is not None and not isinstance(params, dict):
            raise _HttpError(400, "bad_params")
        adj = quick_edit.Adjustments.from_dict(params) if params is not None else None
        conn = self.connect_rw()
        try:
            with conn:
                stored = quick_edit.save(conn, media_id, adj)
                row = conn.execute("SELECT params, updated_at FROM media_edits WHERE media_id = ?",
                                   (media_id,)).fetchone()
        finally:
            conn.close()
        try:                                          # the grid's LQIP carries the edit: rebuild it
            import micro_previews
            st = micro_previews.peek()
            if st is not None:
                st.invalidate([media_id])
        except Exception:                             # noqa: BLE001
            pass
        self._notify_gui("edit", media_id)
        if not stored or row is None:
            return h.send_json({"media_id": media_id, "params": None, "updated_at": None})
        return h.send_json({"media_id": media_id, "params": json.loads(row[0]), "updated_at": row[1]})

    def _notify_gui(self, kind, media_id):
        cb = self.on_remote_change
        if cb is not None:
            try:
                cb(kind, media_id)
            except Exception as e:                    # noqa: BLE001
                print(f"[Sync] on_remote_change: {e}")

    def _face_crop(self, h, face_id):
        conn = self.connect_ro()
        try:
            r = conn.execute(f"SELECT fi.face_crop_path FROM face_instances fi JOIN media_items m ON m.id = fi.media_id "
                             f"WHERE fi.id = ? AND {database.visible_media_sql('m')}", (face_id,)).fetchone()
        finally:
            conn.close()
        path = paths.resolve_face_path(r[0]) if r else None
        if not path:
            raise _HttpError(404, "not_found")
        return self._send_file(h, path, etag=f'"face-{face_id}-{int(os.path.getmtime(path))}"')

    def _micro(self, ids):
        conn = self.connect_ro()
        try:
            visible = _visible_ids(conn, ids)
            ids = [i for i in ids if i in visible]
        finally:
            conn.close()
        if not ids:
            return []
        if self.micro_source is not None:
            return list(self.micro_source(ids))
        import micro_previews
        st = micro_previews.peek()
        if st is not None and st.loaded.is_set():
            return [(i, b) for i in ids if (b := st.get_bytes(i))]
        path = micro_previews._store_path()
        if not os.path.exists(path):
            return []
        conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True, timeout=10)
        try:
            out = []
            for part in _chunks(ids):
                out += conn.execute(f"SELECT id, data FROM micro WHERE id IN ({_qmarks(len(part))})", part).fetchall()
            return out
        finally:
            conn.close()

    # ── map packs ──
    def list_packs(self):
        out = []
        try:
            names = sorted(n for n in os.listdir(self.packs_dir) if n.lower().endswith(".mbtiles"))
        except OSError:
            return out
        for n in names:
            p = os.path.join(self.packs_dir, n)
            meta = {}
            try:
                c = sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro", uri=True, timeout=5)
                try:
                    meta = dict(c.execute("SELECT name, value FROM metadata").fetchall())
                finally:
                    c.close()
            except sqlite3.Error:
                continue
            st = os.stat(p)
            out.append({"id": os.path.splitext(n)[0], "file": n, "size": st.st_size, "mtime": int(st.st_mtime),
                        "title": meta.get("kairos:title") or meta.get("name") or n,
                        "format": (meta.get("format") or "").lower(),
                        "source": meta.get("kairos:source") or "", "bounds": meta.get("bounds"),
                        "minzoom": _int(meta.get("minzoom"), 0), "maxzoom": _int(meta.get("maxzoom"), 0)})
        return out

    def _pack(self, h, pack_id):
        hit = next((p for p in self.list_packs() if p["id"] == pack_id), None)   # whitelist: no traversal
        if hit is None:
            raise _HttpError(404, "not_found")
        self._require_bulk()
        return self._send_file(h, os.path.join(self.packs_dir, hit["file"]),
                               etag=f'"{hit["size"]}-{hit["mtime"]}"', bulk=True)

    @staticmethod
    def _require_bulk():
        state, ok = _power_state()
        if not ok:
            raise _HttpError(503, "power_saver", {"Retry-After": str(RETRY_AFTER_S)})

    # ── file streaming (Range, resumable) ──
    def _send_file(self, h, path, etag=None, bulk=False):
        try:
            fh = open(path, "rb")
        except OSError:
            raise _HttpError(404, "missing") from None
        with fh:
            size = os.fstat(fh.fileno()).st_size
            if etag and h.headers.get("If-None-Match") == etag:
                h.send_response(304)
                h.send_header("ETag", etag)
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            start, end, status = 0, size - 1, 200
            rng = h.headers.get("Range")
            if rng and (not h.headers.get("If-Range") or h.headers.get("If-Range") == etag):
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if not m or (not m.group(1) and not m.group(2)):
                    raise _HttpError(416, "bad_range", {"Content-Range": f"bytes */{size}"})
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:                                  # suffix: the last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    raise _HttpError(416, "bad_range", {"Content-Range": f"bytes */{size}"})
                status = 206
            length = end - start + 1 if size else 0
            h.send_response(status)
            h.send_header("Content-Type", _CTYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))
            h.send_header("Content-Length", str(length))
            h.send_header("Accept-Ranges", "bytes")
            h.send_header("Cache-Control", "private, max-age=31536000" if etag else "no-store")
            if etag:
                h.send_header("ETag", etag)
            if status == 206:
                h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            h.end_headers()
            if h.command == "HEAD":
                return
            if bulk:
                try:
                    import power
                    power.set_thread_eco(True)        # I/O-bound: efficiency cores are plenty
                except Exception:                     # noqa: BLE001
                    pass
            fh.seek(start)
            left = length
            while left > 0 and not self._stop.is_set():
                buf = fh.read(min(CHUNK, left))
                if not buf:
                    break
                h.wfile.write(buf)
                left -= len(buf)
                if bulk and _user_busy():
                    time.sleep(BULK_PACE_S)
            if left > 0:
                h.close_connection = True

    # ── WebSocket change feed (RFC 6455, server -> client text frames) ──
    def _websocket(self, h):
        key = h.headers.get("Sec-WebSocket-Key", "")
        if h.headers.get("Upgrade", "").lower() != "websocket" or not key:
            raise _HttpError(400, "websocket_required")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        h.send_response(101, "Switching Protocols")
        h.send_header("Upgrade", "websocket")
        h.send_header("Connection", "Upgrade")
        h.send_header("Sec-WebSocket-Accept", accept)
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        sock = h.connection
        # no `since`: the client is current; with one, a stale client is told at once
        seen = _int(h.query.get("since"), self._top_seq)
        last_ping = time.monotonic()
        _ws_send(sock, 0x1, json.dumps({"type": "hello", "cursor": self._top_seq}).encode("utf-8"))
        while not self._stop.is_set():
            top = self.wait_change(seen, 1.0)
            if top != seen:
                seen = top
                _ws_send(sock, 0x1, json.dumps({"type": "changes", "cursor": top}).encode("utf-8"))
            if time.monotonic() - last_ping >= WS_KEEPALIVE_S:
                _ws_send(sock, 0x9, b"")
                last_ping = time.monotonic()
            pending = sock.pending() if isinstance(sock, ssl.SSLSocket) else 0
            readable = pending or select.select([sock], [], [], 0)[0]
            if readable:
                op, payload = _ws_recv(sock)
                if op is None or op == 0x8:
                    try:
                        _ws_send(sock, 0x8, payload[:2] if payload else b"")
                    except OSError:
                        pass
                    return
                if op == 0x9:
                    _ws_send(sock, 0xA, payload)
        try:
            _ws_send(sock, 0x8, struct.pack(">H", 1001))
        except OSError:
            pass


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            return None
        buf += part
    return buf


def _ws_send(sock, opcode, payload):
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    sock.sendall(head + payload)


def _ws_recv(sock, cap=64 * 1024):
    """(opcode, payload) of one client frame; (None, b"") on EOF or a protocol violation."""
    head = _recv_exact(sock, 2)
    if head is None:
        return None, b""
    op, masked, n = head[0] & 0x0F, head[1] & 0x80, head[1] & 0x7F
    if n == 126:
        ext = _recv_exact(sock, 2)
        n = struct.unpack(">H", ext)[0] if ext else -1
    elif n == 127:
        ext = _recv_exact(sock, 8)
        n = struct.unpack(">Q", ext)[0] if ext else -1
    if n < 0 or n > cap or not masked:                # clients MUST mask (RFC 6455 5.1)
        return None, b""
    mask = _recv_exact(sock, 4)
    data = _recv_exact(sock, n) if n else b""
    if mask is None or data is None:
        return None, b""
    return op, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _id_list(text, cap):
    out = []
    for part in str(text).split(","):
        if part.strip().isdigit():
            out.append(int(part))
            if len(out) >= cap:
                break
    return out


# ── service (one per app run, like tile_store) ───────────────────────────────

_service = None
_service_lock = threading.Lock()
_remote_handler = None          # desktop GUI hook, survives stop/start (set_remote_change_handler)
last_error = None               # why the last start() failed, for the Settings status line


def set_remote_change_handler(fn):
    """fn(kind, media_id), called on a request thread after the phone wrote an edit or a
    favourite. Applies to the running service and to every later start()."""
    global _remote_handler
    _remote_handler = fn
    srv = _service
    if srv is not None:
        srv.on_remote_change = fn


def start(port=DEFAULT_PORT, host="0.0.0.0"):
    """Starts the bridge (idempotent). None when it cannot start (port taken, DB locked)."""
    global _service, last_error
    with _service_lock:
        if _service is None:
            try:
                _service = SyncServer(host=host, port=port)
            except Exception as e:                    # noqa: BLE001
                last_error = e
                print(f"[Sync] cannot start the LAN bridge: {e}")
                return None
            last_error = None
            _service.on_remote_change = _remote_handler
    return _service


def start_async(port=DEFAULT_PORT, host="0.0.0.0", on_ready=None):
    """GUI entry point: schema install, certificate and bind run on a worker thread."""
    def run():
        srv = start(port, host)
        if on_ready is not None:
            on_ready(srv)
    threading.Thread(target=run, name="sync-start", daemon=True).start()


def service():
    return _service


def stop():
    global _service
    with _service_lock:
        srv, _service = _service, None
    if srv is not None:
        srv.shutdown()


def _credentials(state_dir=None):
    srv = _service
    if srv is not None and state_dir is None:
        return srv.credentials                        # the live copy: no stale file read
    return Credentials(state_dir or os.path.join(paths.DATA_DIR, "sync"))


def paired_devices(state_dir=None):
    """[{id, name, paired_at}] for the Settings list. Token hashes never leave this module."""
    return [{"id": d.get("id", ""), "name": d.get("name", ""), "paired_at": d.get("paired_at", "")}
            for d in _credentials(state_dir).devices]


def forget_device(device_id, state_dir=None):
    """Revokes a phone's token. It must pair again to sync."""
    return _credentials(state_dir).revoke(device_id)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Kairos LAN bridge for the iOS companion")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args(argv)
    srv = SyncServer(host=args.host, port=args.port)
    offer = srv.begin_pairing()
    print(f"Kairos sync on {srv.host}:{srv.port}  library {srv.library_id}")
    print(f"certificate sha256 {srv.fingerprint}")
    print(f"PIN {offer['pin']} (valid {PIN_TTL_S} s)\nQR  {offer['uri']}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
