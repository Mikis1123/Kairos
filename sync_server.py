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

Concurrency (Parity sprint, 2026-11-10)
  One thread per keep-alive connection (HTTP/1.1, TCP_NODELAY). The phone keeps a handful of
  connections open and reuses them, so a fast scroll costs no TCP or TLS handshakes. Requests are
  admitted through two lanes instead of a reject-when-full gate: the interactive lane (thumbnails,
  micro pages, video ranges, search) has INTERACTIVE_SLOTS and the bulk lane (map packs, bulk
  originals, uploads) has BULK_SLOTS, so a backup never starves browsing. A request that finds its
  lane full WAITS up to ADMIT_WAIT_S for a slot; 503 "busy" is only the answer to a truly stuck
  server, never to a fast scroll. Blocking SQLite and file reads are why this stays threaded rather
  than asyncio: every handler would run in an executor anyway, and with one phone on the LAN the
  thread count is bounded by MAX_CONNECTIONS.

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
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import shutil
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
INTERACTIVE_SLOTS = 32             # thumbnails, micro pages, video ranges, search: in flight at once
BULK_SLOTS = 2                     # map packs, bulk originals, camera-roll uploads
ADMIT_WAIT_S = 20.0                # a full lane queues the request this long before 503 "busy"
MAX_CONNECTIONS = 64               # keep-alive connections (threads); more are refused at accept
MAX_SOCKETS = 4                    # WebSocket change feeds
MATCH_MAX = 400                    # /v1/match: hashes per request
MATCH_HASH_BUDGET = 1024 ** 3      # /v1/match: bytes hashed per request when a file is not cached yet
SEARCH_LIMIT = 500
SEARCH_VECTOR_TTL_S = 120.0
FEATURES = ("search", "match", "journey", "lanes")
PAGE_DEFAULT, PAGE_MAX = 500, 2000
MICRO_BATCH_MAX = 500
WS_KEEPALIVE_S = 20.0
WS_PONG_TIMEOUT_S = 10.0           # a ping unanswered this long = a zombie socket (phone left Wi-Fi / slept)
WS_ADMIT_WAIT_S = 3.0              # a reconnecting phone waits this long for the slot its dead feed frees
BODY_DRAIN_MAX = 1024 * 1024       # unread request body up to this is discarded to keep the connection;
                                   # more than this and the connection is closed after the reply
NOTIFY_POLL_S = 1.0                # the change notifier polls MAX(seq), an indexed read
BULK_PACE_S = 0.02                 # per chunk while the desktop user interacts (~12 MB/s cap)
RETRY_AFTER_S = 300
SOCKET_TIMEOUT_S = 30.0
SCHEMA_VERSION = 1                 # sync_log + triggers; bump = triggers are recreated
MICRO_MAGIC = b"KMP1"
# Phone -> desktop import: the formats scanner.SUPPORTED_EXTS indexes (a test pins the subset).
IMPORT_EXTS = frozenset({".heic", ".jpg", ".jpeg", ".png", ".dng", ".mov", ".mp4"})
IMPORT_MAX_BYTES = 8 * 1024 ** 3   # one 4K ProRes clip fits; anything bigger is a client bug
IMPORT_FREE_RESERVE = 512 * 1024 ** 2   # an upload may not shrink the inbox drive below this (507)
INBOX_FOLDER_NAME = "Kairos iPhone"
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


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_import_name(name):
    """The phone's file name made safe for the inbox: base name only, no path or control
    characters, no Windows-reserved stem, a supported extension. "" = refuse."""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).lstrip(". ")
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in IMPORT_EXTS:
        return ""
    stem = stem.rstrip(". ")[:100] or "IMG"
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    return stem + ext


def default_inbox_dir():
    """Where phone imports land: Pictures/Kairos iPhone/<YYYY-MM>/. The desktop adds it to the
    library as a watched folder on the first import, so it behaves like any other folder."""
    return os.path.join(os.path.expanduser("~"), "Pictures", INBOX_FOLDER_NAME)


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


class _CountingReader:
    """rfile wrapper that counts the bytes handed out, so the handler knows how much of a request
    body a handler left unread (an early 401 / 415 / 413 reply before the body was consumed)."""

    def __init__(self, raw):
        self._raw = raw
        self.consumed = 0

    def read(self, n=-1):
        data = self._raw.read(n)
        self.consumed += len(data)
        return data

    def readline(self, limit=-1):
        data = self._raw.readline(limit)
        self.consumed += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64
    block_on_close = False

    def __init__(self, *a, **kw):
        self._conns = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        # at the cap, accepting pauses: further connects wait in the kernel backlog, and only a
        # client that keeps leaking connections for ADMIT_WAIT_S is refused
        if not self._conns.acquire(timeout=ADMIT_WAIT_S):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conns.release()

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError, OSError)):
            return                          # dropped Wi-Fi, an unpinned client hanging up: normal
        super().handle_error(request, client_address)


class SyncServer:
    def __init__(self, host="0.0.0.0", port=DEFAULT_PORT, db_path=None, state_dir=None,
                 packs_dir=None, micro_source=None, use_tls=True, name=None, inbox_dir=None):
        self.db_path = db_path or database.DB_PATH
        self.state_dir = state_dir or os.path.join(paths.DATA_DIR, "sync")
        self.packs_dir = packs_dir or paths.MAP_PACKS_DIR
        self.inbox_dir = inbox_dir or default_inbox_dir()
        self._import_lock = threading.Lock()
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
        self._lanes = {"interactive": threading.BoundedSemaphore(INTERACTIVE_SLOTS),
                       "bulk": threading.BoundedSemaphore(BULK_SLOTS)}
        self._sockets = threading.BoundedSemaphore(MAX_SOCKETS)
        self._ws_lock = threading.Lock()
        self._ws_open = {}                        # feed id -> {"dev": device id, "kick": Event}
        self.search_fn = None                     # callable(query, limit) -> dict; None = _search_default
        self._vectors = (0.0, None)               # (loaded at, {id: vector}) for the default search
        self._vectors_lock = threading.Lock()
        self.stats = {"admitted": 0, "waited": 0, "busy": 0, "ws_reaped": 0, "ws_replaced": 0}
        self._seq_cond = threading.Condition()
        self._top_seq = self._read_top()
        self._stop = threading.Event()
        self.advertiser = None                    # lan_discovery.Advertiser while announced (advertise())
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

    def advertise(self):
        """Announces the bridge as `_kairos._tcp` (Bonjour) so a phone whose paired IP went stale
        (DHCP, a new router, another Wi-Fi) finds the desktop again. Best effort; True when live.
        Off by default so tests that build servers never multicast."""
        if self.advertiser is None:
            import lan_discovery
            adv = lan_discovery.Advertiser(self.port, self.fingerprint, self.library_id, self.name, PROTOCOL)
            self.advertiser = adv
        return bool(self.advertiser.start())

    def shutdown(self):
        self._stop.set()
        adv, self.advertiser = self.advertiser, None
        if adv is not None:
            adv.stop()
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
                try:                                # headers and body are separate writes: without
                    self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # this, Nagle +
                except OSError:                     # delayed ACK stall every keep-alive reply ~40 ms
                    pass
                if server._ctx is not None:
                    # handshake on the request thread, never on the accept loop
                    self.request = server._ctx.wrap_socket(self.request, server_side=True)
                super().setup()
                self.rfile = _CountingReader(self.rfile)

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
            def _pending_body(self):
                """Request-body bytes nobody read yet; None when the length is unknowable
                (chunked / garbage Content-Length)."""
                if self.headers.get("Transfer-Encoding"):
                    return None
                try:
                    declared = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                return max(0, declared - self.rfile.consumed)

            def _settle_body(self):
                """Whatever a handler left unread would be parsed as the NEXT request line on this
                keep-alive connection (the phone then got "501 Unsupported method"). Discard a small
                remainder; for a big or unknowable one the reply already said "Connection: close"."""
                pending = self._pending_body()
                if pending is None:
                    self.close_connection = True
                    return
                if pending <= 0:
                    return
                if pending > BODY_DRAIN_MAX:
                    self.close_connection = True       # drain a bit anyway: closing on unread data RSTs the reply
                try:
                    left = min(pending, BODY_DRAIN_MAX)
                    if self.close_connection:
                        self.request.settimeout(2.0)
                    while left > 0:
                        buf = self.rfile.read(min(65536, left))
                        if not buf:
                            self.close_connection = True
                            return
                        left -= len(buf)
                except (OSError, ValueError):
                    self.close_connection = True

            def _dispatch(self, method):
                self.rfile.consumed = 0                # headers are parsed: from here on it counts body bytes
                try:
                    self._handle(method)
                finally:
                    self._settle_body()

            def _handle(self, method):
                url = urlparse(self.path)
                self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                parts = [p for p in url.path.split("/") if p]
                is_ws = parts == ["v1", "events"]
                gate = server._sockets if is_ws else server._lanes[server.lane(method, parts, self.query)]
                wait_s = ADMIT_WAIT_S
                if is_ws:
                    # a reconnecting phone: authenticate FIRST, retire the dead feed that same device
                    # left behind (its slot is what we are about to need), then wait briefly for it
                    try:
                        server._ws_replace(self.device().get("id"))
                    except _HttpError as e:
                        return self._error(e)
                    wait_s = WS_ADMIT_WAIT_S
                if not gate.acquire(blocking=False):
                    server.stats["waited"] += 1
                    # a full lane queues the request (a fast scroll); only a stuck server says busy
                    if not gate.acquire(timeout=wait_s):
                        server.stats["busy"] += 1
                        # i18n-ok: HTTP header name, not UI text
                        return self._error(_HttpError(503, "busy", {"Retry-After": "1"}))
                server.stats["admitted"] += 1
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
                pending = self._pending_body()
                if pending is None or pending > BODY_DRAIN_MAX:
                    self.send_header("Connection", "close")      # we will not read the rest of that body
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

    @staticmethod
    def lane(method, parts, query):
        """'bulk' for transfers that may take minutes (packs, bulk originals, uploads), else
        'interactive'. Separate lanes: a camera-roll backup never delays a thumbnail."""
        if len(parts) == 3 and parts[:2] == ["v1", "packs"]:
            return "bulk"
        if parts == ["v1", "import"] and method == "POST":
            return "bulk"
        if query.get("bulk") == "1":
            return "bulk"
        return "interactive"

    def _route(self, h, method, parts):
        if parts == ["v1", "hello"] and method in ("GET", "HEAD"):
            state, bulk_ok = _power_state()
            return h.send_json({"app": "Kairos", "protocol": PROTOCOL, "name": self.name,
                                "library_id": self.library_id, "pairing": self.credentials.pairing_active(),
                                "power": state, "bulk_ok": bulk_ok, "features": list(FEATURES)})
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
            return self._websocket(h, dev)
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
        if parts == ["v1", "import"] and method == "POST":
            return h.send_json(self._import(h, dev))
        if len(parts) == 3 and parts[:2] == ["v1", "import"] and method == "GET":
            rel = self._imported(_sha_param(parts[2]))
            # free_bytes lets the phone stop BEFORE it exports and streams a big video (507 otherwise)
            return h.send_json({"status": "present" if rel else "absent", "name": rel,
                                "free_bytes": self._inbox_free_bytes(), "reserve_bytes": IMPORT_FREE_RESERVE})
        if parts == ["v1", "search"] and method == "GET":
            return h.send_json(self.search(h.query.get("q", ""), _int(h.query.get("limit"), SEARCH_LIMIT)))
        if parts == ["v1", "match"] and method == "POST":
            return h.send_json(self.match(h.body_json(cap=1024 * 1024)))
        if len(parts) == 3 and parts[:2] == ["v1", "journey"] and parts[2].isdigit() and method == "GET":
            return h.send_json(self.journey(int(parts[2])))
        raise _HttpError(404, "not_found")

    # ── semantic search (the desktop's CLIP / SigLIP index; embeddings never leave the PC) ──
    def search(self, query, limit=SEARCH_LIMIT):
        """{"ids": [...best first], "confident": n, "semantic": bool, "labels": [...]}. Only
        visible items. The desktop app installs its own warm search (set_search_provider), so a
        query from the phone costs the same ~20 ms as one typed on the PC."""
        query = str(query or "").strip()[:200]
        limit = max(1, min(int(limit or SEARCH_LIMIT), 2000))
        if not query:
            return {"query": "", "ids": [], "confident": 0, "semantic": False, "labels": []}
        fn = self.search_fn or _search_provider or self._search_default
        try:
            res = fn(query, limit)
            if res is None:                           # the desktop's index isn't warm yet
                res = self._search_default(query, limit)
        except Exception as e:                        # noqa: BLE001 – a model error = no results
            print(f"[Sync] search: {e!r}")
            res = {"ids": [], "confident": 0, "semantic": False, "labels": []}
        conn = self.connect_ro()
        try:
            visible = _visible_ids(conn, [int(i) for i in res.get("ids", [])])
        finally:
            conn.close()
        ids = [int(i) for i in res.get("ids", []) if int(i) in visible][:limit]
        return {"query": query, "ids": ids, "confident": min(int(res.get("confident", 0)), len(ids)),
                "semantic": bool(res.get("semantic")), "labels": list(res.get("labels", []))}

    def _search_default(self, query, limit):
        import search_engine
        conn = self.connect_ro()
        try:
            universe = [r[0] for r in conn.execute(
                f"SELECT m.id FROM media_items m WHERE {database.visible_media_sql('m')} "
                "ORDER BY m.date_taken DESC, m.id DESC")]
        finally:
            conn.close()
        with self._vectors_lock:
            at, vectors = self._vectors
            if vectors is None or time.monotonic() - at > SEARCH_VECTOR_TTL_S:
                vectors = database.get_search_vectors()
                self._vectors = (time.monotonic(), vectors)
        r = search_engine.run_search(query, universe, vectors)
        return {"ids": r.ids[:limit], "confident": len(r.confident), "semantic": r.semantic, "labels": r.labels}

    # ── three-state sync: which phone assets does the desktop already hold? ──
    def _hashes_db(self):
        conn = sqlite3.connect(os.path.join(self.state_dir, "hashes.sqlite"), timeout=30)
        conn.execute("CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, "
                     "sha256 TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hashes_sha ON hashes (sha256)")
        return conn

    @staticmethod
    def _norm(p):
        return os.path.normcase(os.path.normpath(p or ""))

    def _file_sha(self, path, budget):
        """SHA-256 of a desktop file, cached by (path, size, mtime). (hex or None, bytes hashed)."""
        try:
            st = os.stat(path)
        except OSError:
            return None, 0
        hconn = self._hashes_db()
        try:
            row = hconn.execute("SELECT size, mtime, sha256 FROM hashes WHERE path = ?", (self._norm(path),)).fetchone()
            if row and row[0] == st.st_size and abs((row[1] or 0) - st.st_mtime) < 1e-3:
                return row[2], 0
            if st.st_size > budget:
                return None, 0
            import cloud_files
            if not cloud_files.local_ok(path):            # never pull a OneDrive file down to hash it
                return None, 0
            hasher = hashlib.sha256()
            with open(path, "rb") as fh:
                while buf := fh.read(1 << 20):
                    hasher.update(buf)
            digest = hasher.hexdigest()
            with hconn:
                hconn.execute("INSERT OR REPLACE INTO hashes VALUES (?, ?, ?, ?)",
                              (self._norm(path), st.st_size, st.st_mtime, digest))
            return digest, st.st_size
        finally:
            hconn.close()

    def match(self, body):
        """POST /v1/match {"assets": [{"sha256": hex, "size": n, "name": "IMG_0042.HEIC"}, ...]}
        -> {"matches": {sha256: media_id}}. Proof by content, not by name: a hash is resolved
        through the camera-roll import journal first (bytes the phone sent), then by hashing the
        desktop files of the same size (cached in sync/hashes.sqlite, budgeted per request). A Live
        Photo's motion half resolves to its still's media id."""
        assets = body.get("assets") if isinstance(body, dict) else None
        if not isinstance(assets, list):
            raise _HttpError(400, "bad_body")
        wanted = {}
        for a in assets[:MATCH_MAX]:
            if not isinstance(a, dict):
                continue
            try:
                digest = _sha_param(str(a.get("sha256", "")))
            except _HttpError:
                continue
            wanted[digest] = (_int(a.get("size"), 0), str(a.get("name") or ""))
        out = {}
        if not wanted:
            return {"matches": out}
        conn = self.connect_ro()
        try:
            rows = conn.execute(f"SELECT m.id, m.file_path, m.paired_video_path, m.file_size, m.file_name "
                                f"FROM media_items m WHERE {database.visible_media_sql('m')}").fetchall()
        finally:
            conn.close()
        by_path = {}
        for mid, fp, live, _size, _name in rows:
            if fp:
                by_path[self._norm(fp)] = mid
            if live:
                by_path.setdefault(self._norm(live), mid)
        # 1) the import journal: bytes the phone itself sent
        with self._import_lock:
            iconn = self._imports_db()
            try:
                for part in _chunks(list(wanted)):
                    for digest, rel in iconn.execute(
                            f"SELECT sha256, rel_path FROM imports WHERE sha256 IN ({_qmarks(len(part))})", part):
                        mid = by_path.get(self._norm(os.path.join(self.inbox_dir, rel)))
                        if mid is not None:
                            out[digest] = mid
            finally:
                iconn.close()
        # 2) cached hashes of library files
        left = [d for d in wanted if d not in out]
        if left:
            hconn = self._hashes_db()
            try:
                for part in _chunks(left):
                    for path, size, mtime, digest in hconn.execute(
                            f"SELECT path, size, mtime, sha256 FROM hashes WHERE sha256 IN ({_qmarks(len(part))})",
                            part):
                        mid = by_path.get(path)
                        if mid is None or digest in out:
                            continue
                        try:
                            st = os.stat(path)
                        except OSError:
                            continue
                        if st.st_size == size and abs((mtime or 0) - st.st_mtime) < 1e-3:   # still those bytes
                            out[digest] = mid
            finally:
                hconn.close()
        # 3) hash same-size candidates (same name first), within the per-request budget
        left = [d for d in wanted if d not in out]
        if left:
            files = []                                  # (path, media id, size, name)
            for mid, fp, live, size, name in rows:
                if fp:
                    files.append((fp, mid, size, (name or os.path.basename(fp)).lower()))
                if live:
                    files.append((live, mid, None, os.path.basename(live).lower()))
            budget = MATCH_HASH_BUDGET
            for digest in left:
                size, name = wanted[digest]
                if size <= 0:
                    continue
                cands = []
                for path, mid, fsize, fname in files:
                    if fsize is None:
                        try:
                            fsize = os.path.getsize(path)
                        except OSError:
                            continue
                    if fsize == size:
                        cands.append((fname != name.lower(), path, mid))
                for _, path, mid in sorted(cands)[:4]:
                    got, cost = self._file_sha(path, budget)
                    budget -= cost
                    if got == digest:
                        out[digest] = mid
                        break
                if budget <= 0:
                    break
        return {"matches": out}

    # ── Trip mode: the desktop's journey (stops, day chapters, GPX legs) without any paths ──
    def journey(self, event_id):
        import journey as jr
        conn = self.connect_ro()
        try:
            data = jr.build_journey(conn, event_id)
        finally:
            conn.close()
        if data is None:
            raise _HttpError(404, "no_journey")

        def stop(s):
            return {"lat": s["lat"], "lon": s["lon"], "start": s.get("start"), "end": s.get("end"),
                    "city": s.get("city") or "", "count": s.get("count", 0),
                    "entry": s.get("entry"), "exit": s.get("exit"),
                    "best": (s.get("best") or {}).get("id"),
                    "photos": [p["id"] for p in s.get("photos", [])]}

        out = {"id": data["id"], "title": data.get("title") or "", "location": data.get("location") or "",
               "start": data.get("start"), "end": data.get("end"), "count": data.get("count", 0),
               "stops": [stop(s) for s in data["stops"]], "tracks": data.get("tracks") or {}, "days": []}
        for d in data.get("days") or []:
            out["days"].append({"n": d["n"], "date": d.get("date"), "start": d.get("start"), "end": d.get("end"),
                                "count": d.get("count", 0), "city": d.get("city") or "",
                                "first": d.get("first", 0), "stops": [stop(s) for s in d.get("stops") or []],
                                "tracks": d.get("tracks") or {}})
        return out

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

    # ── phone -> desktop import ──
    def _imports_db(self):
        conn = sqlite3.connect(os.path.join(self.state_dir, "imports.sqlite"), timeout=30)
        conn.execute("CREATE TABLE IF NOT EXISTS imports (sha256 TEXT PRIMARY KEY, rel_path TEXT NOT NULL, "
                     "device_id TEXT, imported_at TEXT)")
        return conn

    def _imported(self, digest):
        """Inbox-relative path of an earlier import with these bytes, or None. An entry whose
        file the user has since moved or deleted no longer counts: the phone may send it again."""
        with self._import_lock:
            return self._imported_locked(digest)

    def _imported_locked(self, digest):
        """_imported for a caller that already holds `_import_lock` (it is not re-entrant)."""
        conn = self._imports_db()
        try:
            row = conn.execute("SELECT rel_path FROM imports WHERE sha256 = ?", (digest,)).fetchone()
        finally:
            conn.close()
        if row and os.path.isfile(os.path.join(self.inbox_dir, row[0])):
            return row[0].replace(os.sep, "/")
        return None

    def _inbox_free_bytes(self):
        """Free space on the drive the inbox lives on (the nearest existing parent while the inbox
        folder has not been created yet); None when it cannot be read."""
        path = os.path.abspath(self.inbox_dir)
        while path and not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        try:
            return int(shutil.disk_usage(path).free)
        except OSError:
            return None

    def _import(self, h, dev):
        """POST /v1/import?name=<file name>&sha256=<hex>, raw bytes as the body. Streams into
        the inbox through a .partial file, verifies the SHA-256 and only then renames it into
        place, so the desktop's scanner never sees half a file. Idempotent by content: a retry
        after a dropped connection, or the same photo picked twice, answers "duplicate"."""
        name = safe_import_name(h.query.get("name", ""))
        if not name:
            raise _HttpError(415, "unsupported_type")
        digest = _sha_param(h.query.get("sha256", ""))
        try:
            size = int(h.headers.get("Content-Length") or -1)
        except ValueError:
            size = -1
        if size <= 0:
            raise _HttpError(411, "length_required")
        if size > IMPORT_MAX_BYTES:
            h.close_connection = True
            raise _HttpError(413, "too_large")
        free = self._inbox_free_bytes()
        if free is not None and free < size + IMPORT_FREE_RESERVE:
            # 507, not 500: the phone treats it as "pause and keep the file", never as a rejection
            h.close_connection = True
            raise _HttpError(507, "insufficient_storage", {"Retry-After": str(RETRY_AFTER_S)})
        partial = os.path.join(self.inbox_dir, ".partial")
        os.makedirs(partial, exist_ok=True)
        tmp = os.path.join(partial, uuid.uuid4().hex + ".part")
        hasher = hashlib.sha256()
        rel = None
        try:
            try:
                with open(tmp, "wb") as fh:
                    left = size
                    while left > 0:
                        buf = h.rfile.read(min(CHUNK, left))
                        if not buf:                      # the phone dropped off Wi-Fi mid-upload
                            h.close_connection = True
                            raise _HttpError(400, "short_body")
                        hasher.update(buf)
                        fh.write(buf)
                        left -= len(buf)
            except OSError as e:
                if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                    h.close_connection = True            # the disk filled up mid-upload
                    raise _HttpError(507, "insufficient_storage", {"Retry-After": str(RETRY_AFTER_S)}) from None
                raise
            if hasher.hexdigest() != digest:
                raise _HttpError(422, "sha_mismatch")
            # The bytes are streamed and verified outside the lock (a 4 GB video must not block the
            # other lane), but "is it already here?" and "put it there" are ONE critical section.
            # Checking before taking the lock let two uploads of the same bytes (a phone that timed
            # out and retried while the first handler was still running) both pass the check and
            # land as IMG_1.heic and IMG_1 (2).heic.
            with self._import_lock:
                known = self._imported_locked(digest)
                if known:
                    return {"status": "duplicate", "name": known}
                month = _dt.datetime.now().strftime("%Y-%m")
                folder = os.path.join(self.inbox_dir, month)
                os.makedirs(folder, exist_ok=True)
                stem, ext = os.path.splitext(name)
                final, n = os.path.join(folder, name), 2
                while os.path.exists(final):
                    final = os.path.join(folder, f"{stem} ({n}){ext}")
                    n += 1
                os.replace(tmp, final)
                rel = os.path.relpath(final, self.inbox_dir)
                conn = self._imports_db()
                try:
                    with conn:
                        conn.execute("INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?)",
                                     (digest, rel, dev.get("id", ""),
                                      _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")))
                finally:
                    conn.close()
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._notify_gui("import", 0)
        return {"status": "imported", "name": rel.replace(os.sep, "/")}

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
    def _ws_replace(self, device_id):
        """Retire the open feeds of this device: a phone that reconnects (Wi-Fi roam, wake from
        sleep) is by definition not listening on the old one, but a half-open socket would hold
        one of the MAX_SOCKETS slots until TCP gave up (minutes)."""
        with self._ws_lock:
            old = [f for f in self._ws_open.values() if f["dev"] == device_id]
        for feed in old:
            feed["kick"].set()
            self.stats["ws_replaced"] += 1

    def _websocket(self, h, dev=None):
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
        feed = {"dev": (dev or {}).get("id"), "kick": threading.Event()}
        with self._ws_lock:
            self._ws_open[id(feed)] = feed
        try:
            self._ws_loop(h, sock, feed)
        finally:
            with self._ws_lock:
                self._ws_open.pop(id(feed), None)

    def _ws_loop(self, h, sock, feed):
        # no `since`: the client is current; with one, a stale client is told at once
        seen = _int(h.query.get("since"), self._top_seq)
        ping_sent = None                      # monotonic time of an unanswered ping
        last_ping = time.monotonic()
        _ws_send(sock, 0x1, json.dumps({"type": "hello", "cursor": self._top_seq}).encode("utf-8"))
        while not self._stop.is_set() and not feed["kick"].is_set():
            top = self.wait_change(seen, 1.0)
            if top != seen:
                seen = top
                _ws_send(sock, 0x1, json.dumps({"type": "changes", "cursor": top}).encode("utf-8"))
            # read what the client sent BEFORE judging it: a pong that arrived during the wait above
            # must count, or a healthy phone is reaped by the very timer meant to catch dead ones
            while True:
                pending = sock.pending() if isinstance(sock, ssl.SSLSocket) else 0
                if not (pending or select.select([sock], [], [], 0)[0]):
                    break
                op, payload = _ws_recv(sock)
                ping_sent = None              # any frame from the client proves it is alive
                if op is None or op == 0x8:
                    try:
                        _ws_send(sock, 0x8, payload[:2] if payload else b"")
                    except OSError:
                        pass
                    return
                if op == 0x9:
                    _ws_send(sock, 0xA, payload)
            now = time.monotonic()
            if ping_sent is not None and now - ping_sent >= WS_PONG_TIMEOUT_S:
                # TCP still says "open" (the phone vanished without a FIN) but nothing answers:
                # free the slot, or a reconnecting phone meets 503 "busy" for minutes
                self.stats["ws_reaped"] += 1
                return
            if ping_sent is None and now - last_ping >= WS_KEEPALIVE_S:
                _ws_send(sock, 0x9, b"")
                ping_sent = last_ping = now
        try:
            _ws_send(sock, 0x8, struct.pack(">H", 1001 if self._stop.is_set() else 1000))
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


def _sha_param(text):
    text = (text or "").strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise _HttpError(400, "bad_sha256")
    return text


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


_search_provider = None


def set_search_provider(fn):
    """fn(query, limit) -> {"ids", "confident", "semantic", "labels"}: the desktop app's warm
    search (its embeddings cache and loaded text model), used for /v1/search. None = the bridge
    loads the vectors itself (standalone runs)."""
    global _search_provider
    _search_provider = fn


def set_remote_change_handler(fn):
    """fn(kind, media_id), called on a request thread after the phone wrote an edit or a
    favourite, or ("import", 0) after a camera-roll upload landed in the inbox.
    Applies to the running service and to every later start()."""
    global _remote_handler
    _remote_handler = fn
    srv = _service
    if srv is not None:
        srv.on_remote_change = fn


def start(port=DEFAULT_PORT, host="0.0.0.0", advertise=False):
    """Starts the bridge (idempotent). None when it cannot start (port taken, DB locked).
    advertise = also announce it over Bonjour (the desktop app does, tests do not)."""
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
        srv = _service
    if advertise and srv is not None:
        srv.advertise()                           # outside the lock: registering takes ~1 s
    return srv


def start_async(port=DEFAULT_PORT, host="0.0.0.0", on_ready=None):
    """GUI entry point: schema install, certificate and bind run on a worker thread."""
    def run():
        srv = start(port, host, advertise=True)
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
    srv.advertise()
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
