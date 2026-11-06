# Kairos LAN bridge – protocol 1

Server: `sync_server.py` (desktop). Client: `KairosCore/Sync` (iOS).
Contract tests: `test_ios_sync.py` writes `KairosCore/Tests/KairosCoreTests/Fixtures/*.json`, and `ContractTests.swift` decodes them.

## Transport and trust

- HTTPS (TLS ≥ 1.2) on port **52790** across the whole LAN. The bridge is opt-in: it starts only when `sync_server_enabled` is set in config, or when you run `python sync_server.py`.
- Certificate: self-signed EC P-256, made on first start in `%APPDATA%/Kairos/sync/`. The client pins the **SHA-256 of the DER certificate**. It does no CA check and no hostname check.
- Pairing QR / link: `kairos://pair?h=<lan ip>&p=<port>&fp=<sha256 hex>&lib=<library id>&pin=<6 digits>&v=1`
- The PIN is single-use, valid for 120 s and allows 5 guesses. A correct PIN returns a 256-bit bearer token. The desktop stores only its SHA-256 (`sync/devices.json`). The phone keeps the token in the Keychain (`AfterFirstUnlockThisDeviceOnly`).
- Every endpoint except `hello` and `pair` requires `Authorization: Bearer <token>`.
- Capacity: 8 HTTP requests in flight plus 4 WebSockets. Beyond that the server answers `503 busy` with `Retry-After: 2`.
- JSON bodies over 1 KB are gzip-compressed when the client sends `Accept-Encoding: gzip`. URLSession does this automatically.

## Privacy rules (server-enforced)

- Absolute paths never cross the wire.
- Hidden (vault) items, trashed items and the motion halves of Live Photos are never served. Their metadata reads as a tombstone, and their binaries return 404. This also applies to the faces, edits and album memberships of those items.
- Originals that are cloud-only placeholders (OneDrive/iCloud) return `409 cloud_only`, so the phone never triggers a download.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/hello` | `{app, protocol, name, library_id, pairing, power, bulk_ok}`, no auth |
| POST | `/v1/pair` | `{pin, device_name, device_id}` → `{token, device_id, library_id, name, protocol}`. Fails with 403 `bad_pin` or `no_pairing` |
| POST | `/v1/unpair` | revokes the calling device's token |
| GET | `/v1/changes?since=N&limit=L` | one page of the journal (see below), L ≤ 2000 |
| GET | `/v1/events` | WebSocket. Text frames `{"type":"hello"|"changes","cursor":N}`, ping every 20 s |
| GET | `/v1/micro?ids=1,2,…` | up to 500 ids. Body is `"KMP1"` + (u32 id, u32 len, JPEG)*, big-endian |
| GET | `/v1/media/{id}/thumb` | 840 px desktop thumbnail, **unedited**. ETag. |
| GET | `/v1/media/{id}/original` | original bytes with Range/206, ETag and If-Range. `?bulk=1` = background backfill |
| GET | `/v1/media/{id}/live` | the motion half of a Live Photo |
| PUT/DELETE | `/v1/media/{id}/edit` | body `{"params": {...}}`. The answer is the canonical (clamped) record, or `params: null` |
| PUT | `/v1/media/{id}/favorite` | `{"value": bool}` |
| GET | `/v1/face/{id}/crop` | face crop JPEG |
| GET | `/v1/packs` | offline MBTiles region packs `{id, file, size, mtime, title, format, source, bounds, minzoom, maxzoom}` |
| GET | `/v1/packs/{id}` | pack bytes with Range (resumable) |

## Incremental sync

The desktop's `sync_log(seq AUTOINCREMENT, entity, entity_id UNIQUE)` journal is filled by SQLite triggers (`ksync_*`). Each entity owns exactly one row. A change moves that row to a fresh `seq`. UPDATE triggers fire only when a synced column changes value.

Entities: `media`, `event`, `person`, `face`, `album` (the record carries the ordered `items`), and `edit` (keyed by media id).

```
GET /v1/changes?since=0
{"protocol":1,"library_id":"…","cursor":812,"more":true,"reset":false,
 "changes":[{"seq":1,"entity":"event","id":1,"op":"upsert","data":{…}},
            {"seq":4,"entity":"media","id":3,"op":"delete"}, …]}
```

Client loop:
1. `since` = stored cursor.
2. Apply each page in **one transaction, together with its cursor**.
3. Repeat while `more` is true.
4. Wait on `/v1/events`.
5. On `changes`, go back to step 2.

- When `reset: true` arrives (the desktop restored an older backup) or `library_id` changes, drop the replica and start again from 0.
- A tombstone for a media item cascades on the client to its faces, its edit, its album membership and its micro preview.
- Entities this build doesn't know are skipped. Their cursor still advances.

## Edits

`media_edits.params` is the JSON from `quick_edit.Adjustments.to_dict()`, EDIT_VERSION 1:

```json
{"v":1,"crop":[x,y,w,h],"aspect":"free","angle":0.0,
 "exposure":0.0,"highlights":0.0,"shadows":0.0,"temperature":0.0,"contrast":0.0}
```

- Unknown keys are preserved on both sides.
- Pixel math: `EditMath.swift` ports `quick_edit.py`. Fixtures pin the LUTs (±1 level) and the inverse warp.
- Conflicts: a local edit not yet uploaded (`dirty`) wins over an incoming one. After upload, the desktop's canonical answer replaces it. Otherwise the last writer wins (protocol 1).
- Thumbnails and originals are served unedited, and the client applies the edit. Micro previews are the exception: the desktop bakes the edit into them.

## Power and idle behaviour

- Everything runs on daemon threads. Nothing touches the Qt GUI thread. Writes call `on_remote_change(kind, media_id)` so the desktop can refresh.
- Bulk transfers (packs, `?bulk=1` originals) get `503 power_saver` with `Retry-After: 300` when `power.py` says PAUSE (battery saver, or a low battery under the throttle policy). Interactive requests are always served.
- While the desktop user is interacting (governor busy), bulk transfers pause 20 ms per 256 KB chunk, and the streaming thread runs under EcoQoS.

## Planned for protocol 2

`/v1/search` (CLIP/SigLIP on the desktop), Journey film manifests, Hidden-folder sync with its own key exchange, Bonjour discovery, phone → desktop import, and push through a relay when the PC is asleep.
