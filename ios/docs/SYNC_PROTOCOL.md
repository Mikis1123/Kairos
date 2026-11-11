# Kairos LAN bridge – protocol 1

Server: `sync_server.py` (desktop). Client: `KairosCore/Sync` (iOS).
Contract tests: `test_ios_sync.py` writes `KairosCore/Tests/KairosCoreTests/Fixtures/*.json`, and `ContractTests.swift` decodes them.

## Transport and trust

- HTTPS (TLS ≥ 1.2) on port **52790** across the whole LAN. The bridge is opt-in: it starts only when `sync_server_enabled` is set in config, or when you run `python sync_server.py`.
- Certificate: self-signed EC P-256, made on first start in `%APPDATA%/Kairos/sync/`. The client pins the **SHA-256 of the DER certificate**. It does no CA check and no hostname check.
- Pairing QR / link: `kairos://pair?h=<lan ip>&p=<port>&fp=<sha256 hex>&lib=<library id>&pin=<6 digits>&v=1`. The desktop shows it in Settings › Advanced › iPhone sync › Pair iPhone… (`phone_pairing.py`; QR from the dependency-free `qr_code.py`). Closing that sheet cancels the PIN.
- The PIN is single-use, valid for 120 s and allows 5 guesses. A correct PIN returns a 256-bit bearer token. The desktop stores only its SHA-256 (`sync/devices.json`). The phone keeps the token in the Keychain (`AfterFirstUnlockThisDeviceOnly`).
- Every endpoint except `hello` and `pair` requires `Authorization: Bearer <token>`.
- Capacity (Parity sprint): HTTP/1.1 keep-alive with TCP_NODELAY, up to 64 connections. Requests are admitted through two lanes: **interactive** (32 in flight: thumbnails, micro pages, video ranges, search, metadata) and **bulk** (2: packs, `?bulk=1` originals, uploads), so a backup never delays browsing. A request that finds its lane full **waits** up to 20 s for a slot; `503 busy` (`Retry-After: 1`) only answers a server that stayed stuck that long. Plus 4 WebSockets.
- JSON bodies over 1 KB are gzip-compressed when the client sends `Accept-Encoding: gzip`. URLSession does this automatically.

## Discovery (Fix Block C)

The QR address goes stale when the router hands the desktop a new IP. While the bridge runs, the desktop announces itself over Bonjour (`lan_discovery.py`, the `zeroconf` package, optional):

- Service `<name>._kairos._tcp.local.`, the bridge port, TXT `fp` = certificate SHA-256 (hex, the same value as the QR), `lib` = library id, `v` = protocol, `name`.
- The announced address is re-checked every 15 s and updated when the desktop's address changes.
- The phone (`DesktopDiscovery.swift`, `NWBrowser`) browses `_kairos._tcp` after a connection failure and on every network change. It accepts only the advert whose `fp` equals the fingerprint it paired with, resolves the address, relocates its `SyncClient` and saves the new endpoint. The token and the pin do not change. Needs `NSBonjourServices` in Info.plist and the Local Network permission.
- Nothing secret is announced (the fingerprint is public). A wrong or hostile advert still fails the TLS pin.

## Privacy rules (server-enforced)

- Absolute paths never cross the wire.
- Hidden (vault) items, trashed items and the motion halves of Live Photos are never served. Their metadata reads as a tombstone, and their binaries return 404. This also applies to the faces, edits and album memberships of those items.
- Originals that are cloud-only placeholders (OneDrive/iCloud) return `409 cloud_only`, so the phone never triggers a download.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/hello` | `{app, protocol, name, library_id, pairing, power, bulk_ok, features}`, no auth. `features` lists the additive endpoints below |
| POST | `/v1/pair` | `{pin, device_name, device_id}` → `{token, device_id, library_id, name, protocol}`. Fails with 403 `bad_pin` or `no_pairing` |
| POST | `/v1/unpair` | revokes the calling device's token |
| GET | `/v1/changes?since=N&limit=L` | one page of the journal (see below), L ≤ 2000 |
| GET | `/v1/events` | WebSocket. Text frames `{"type":"hello"|"changes","cursor":N}`, ping every 20 s |
| GET | `/v1/micro?ids=1,2,…` | up to 500 ids. Body is `"KMP1"` + (u32 id, u32 len, JPEG)*, big-endian |
| GET | `/v1/media/{id}/thumb` | 840 px desktop thumbnail, **unedited**. ETag. |
| GET | `/v1/media/{id}/original` | original bytes with Range/206, ETag and If-Range. `?bulk=1` = background backfill |
| GET | `/v1/media/{id}/live` | the motion half of a Live Photo, Range/206 like `original` |
| PUT/DELETE | `/v1/media/{id}/edit` | body `{"params": {...}}`. The answer is the canonical (clamped) record, or `params: null` |
| PUT | `/v1/media/{id}/favorite` | `{"value": bool}` |
| GET | `/v1/face/{id}/crop` | face crop JPEG |
| GET | `/v1/packs` | offline MBTiles region packs `{id, file, size, mtime, title, format, source, bounds, minzoom, maxzoom}` |
| GET | `/v1/packs/{id}` | pack bytes with Range (resumable) |
| GET | `/v1/import/{sha256}` | `{status: present|absent, name}`: pre-check before sending a camera-roll file |
| POST | `/v1/import?name=&sha256=` | raw file bytes (Content-Length required, ≤ 8 GB). See below |
| GET | `/v1/search?q=&limit=` | feature `search`. The desktop's semantic search (CLIP / SigLIP, facets, OCR) → `{query, ids, confident, semantic, labels}`. Visible ids only, best first; the first `confident` are sure matches |
| POST | `/v1/match` | feature `match`. `{"assets": [{sha256, size, name}]}` (≤ 400) → `{"matches": {sha256: media_id}}`. See "Three-state library" |
| GET | `/v1/journey/{event_id}` | feature `journey`. Trip mode: `{id, title, location, start, end, count, stops, tracks, days}`; 404 `no_journey` under two places |

## Three-state library (Parity sprint)

The phone indexes its camera roll through PhotoKit into `phone_assets` (metadata only: local identifier, capture time, original file name and size, position). Pixels stay in the Photos library and are read in place. Each asset is:

- **phone**: only on the iPhone. Listed as media id `-pid` (negative), badged, offered for backup.
- **both**: matched to a desktop item, listed once as that item, and drawn from the local original (no network). An original offloaded to iCloud is fetched from the PC instead.
- **pc**: a desktop item with no local asset: streamed and cached on demand (LIFO fetch queue).

Matching runs offline on the phone (`AssetMatcher`): same name and size, same name stem within 2 s, or same size within 1 s; one-to-one, closest in time wins. The proof by content is `/v1/match`: the desktop resolves a SHA-256 through the import journal (bytes the phone sent), then by hashing same-size library files (cached in `sync/hashes.sqlite` by path, size and mtime; ≤ 1 GB hashed per request; OneDrive placeholders are never downloaded). A Live Photo's motion half resolves to its still.

Backup: approved phone-only assets are exported from PhotoKit (original + Live motion under the same stem, so the desktop pairs them), hashed, checked against `/v1/match` (already there = linked, nothing uploaded), then sent through the import outbox. When the desktop has indexed the upload, the next pass links it by hash. Background: `BGAppRefreshTask` (pull + re-index) and `BGProcessingTask` (backup, on external power + network).

## Video and Live Photo playback (iOS)

- Videos stream into AVPlayer through a private URL scheme (`kairos-stream://`) and an `AVAssetResourceLoaderDelegate` (`MediaStreamLoader`), because AVFoundation cannot use the pinned URLSession. Every byte request becomes 1 MB `Range` requests on `/original`; the first is a 2-byte probe for the size and type. Playback starts after the first chunk.
- 503 `busy` and network errors are retried; 404, 401 and 409 `cloud_only` are final. If streaming fails otherwise, the phone downloads the whole file once and plays it locally.
- Live Photo motion halves (`/live`, a few MB) are downloaded whole while the photo is on screen, so touch-and-hold plays at once.
- Downloaded originals, videos and motion halves share `Caches/Kairos/originals`: newest first within 1.5 GB, the 3 newest always kept, cleared on unpair.

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
- Conflicts: a local edit not yet uploaded (`dirty`) wins over an incoming one. After upload, the desktop's canonical answer replaces it, but only if the row still holds exactly what was uploaded (compare-and-swap). A newer edit saved while the PUT was in flight stays dirty and goes up next. Otherwise the last writer wins (protocol 1).
- Favourites follow the same rule (`media_items.is_favorite_dirty`, schema v4): the heart flips in the replica at once and the flag stays until `PUT /v1/media/{id}/favorite` is acknowledged. The flag clears only while the row still holds the value that was sent (compare-and-swap); a toggle made meanwhile goes up next. While the flag is set, an incoming `media` upsert does not overwrite the local value. 404 drops the flag.
- Saving on the phone writes the replica row first (dirty), then uploads in the background. Offline edits stay queued until the next successful sync. The phone re-bakes its own micro preview from the edited rendering, so the grid matches at once.
- Phone writes reach the desktop GUI through `sync_server.set_remote_change_handler`. main.py routes them to the same grid refresh as a local Quick Edit or favourite.
- Thumbnails and originals are served unedited, and the client applies the edit. Micro previews are the exception: the desktop bakes the edit into them.

## Phone → desktop import

- The phone picks photos/videos with `PHPickerViewController` (no Photos permission, originals as shot) and queues them in an on-disk outbox (`ImportQueue`). One upload at a time; offline items wait until sync is live again.
- `POST /v1/import` streams into `Pictures/Kairos iPhone/.partial/`, verifies the SHA-256 and only then renames the file into `Pictures/Kairos iPhone/<YYYY-MM>/<name>`. A name clash with different bytes gets ` (2)`.
- Idempotent by content (`sync/imports.sqlite`): the same bytes answer `{"status":"duplicate"}`. An entry whose file was moved or deleted no longer counts.
- Concurrent uploads of the same bytes (a phone that timed out and retried while the first request is still running) are safe: the "already here?" check and the rename happen under one lock, so exactly one request answers `imported` and the others `duplicate`.
- What the phone does with each answer (`ImportQueue`): `200` acknowledged -> the file is recorded as sent. `400 411 413 415` (and `422` five times in a row) -> refused for good, recorded as failed with the reason, shown with a Retry button. Everything else is the DESKTOP's condition, never the file's: `507` (disk full), any `5xx`, `503` (power saver), an old desktop's `404`. The file stays queued and the queue pauses (30 s, doubling, max 30 min; "Retry now" clears it). A camera-roll asset becomes `sent` only when every one of its files was acknowledged.
- Accepted: `.heic .jpg .jpeg .png .dng .mov .mp4` (a subset of `scanner.SUPPORTED_EXTS`, pinned by `test_ios_import.py`); others get 415. Bad hash → 422, the phone resends (5 tries).
- Each landed file calls `on_remote_change("import", 0)`. The desktop coalesces a batch (1.5 s), makes the inbox a watched library folder on first use and runs an incremental scan. The new media then reach the phone through the journal like any other.
- Live Photos arrive as the still only: the motion half needs PhotoKit library access, which the permission-free picker avoids.

## Power and idle behaviour

- Everything runs on daemon threads. Nothing touches the Qt GUI thread. Writes call `on_remote_change(kind, media_id)` so the desktop can refresh.
- Bulk transfers (packs, `?bulk=1` originals) get `503 power_saver` with `Retry-After: 300` when `power.py` says PAUSE (battery saver, or a low battery under the throttle policy). Interactive requests are always served.
- While the desktop user is interacting (governor busy), bulk transfers pause 20 ms per 256 KB chunk, and the streaming thread runs under EcoQoS.

## Planned for protocol 2

Journey film manifests (the animated film; Trip mode already draws the route), Hidden-folder sync with its own key exchange, and push through a relay when the PC is asleep.
