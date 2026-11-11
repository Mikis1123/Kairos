#if canImport(SQLite3)
import Foundation

/// The phone's replica of the desktop library.
///
/// Table names match desktop photos.db (media_items, events, people, face_instances, albums,
/// album_items, media_edits). Only what the phone needs is stored. Embeddings, phash and
/// absolute paths stay on the desktop. Query columns are real columns. The full wire record of a
/// photo sits in `payload` (JSON), so new desktop fields need no migration.
///
/// No foreign keys: entities arrive in journal order, so a face can land before its photo.
/// Tombstones cascade by hand (`delete`).
public final class LibraryStore: @unchecked Sendable {
    public let db: SQLiteDatabase
    private let decoder = JSONDecoder()
    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys]
        return e
    }()

    public static let schemaVersion: Int64 = 4

    static let migrations: [String] = [
        """
        CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS media_items (
            id INTEGER PRIMARY KEY,
            date_taken TEXT, media_type TEXT, file_name TEXT,
            latitude REAL, longitude REAL, place_city TEXT, place_country TEXT,
            is_favorite INTEGER NOT NULL DEFAULT 0, event_id INTEGER, chapter_id INTEGER,
            live INTEGER NOT NULL DEFAULT 0, has_thumb INTEGER NOT NULL DEFAULT 0,
            fingerprint TEXT, payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_date ON media_items (date_taken DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_media_event ON media_items (event_id);
        CREATE INDEX IF NOT EXISTS idx_media_geo ON media_items (latitude, longitude) WHERE latitude IS NOT NULL;
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, title TEXT, location_name TEXT, start_date TEXT, end_date TEXT,
            item_count INTEGER, cover_media_id INTEGER, kind TEXT, parent_id INTEGER, tint TEXT
        );
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY, name TEXT, key_face_id INTEGER,
            is_favorite INTEGER NOT NULL DEFAULT 0, is_confirmed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS face_instances (
            id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL, person_id INTEGER,
            bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL, quality_score REAL
        );
        CREATE INDEX IF NOT EXISTS idx_face_person ON face_instances (person_id);
        CREATE INDEX IF NOT EXISTS idx_face_media ON face_instances (media_id);
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY, name TEXT, kind TEXT, parent_id INTEGER, position INTEGER,
            sort_mode TEXT, cover_media_id INTEGER, query TEXT, filter TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS album_items (
            album_id INTEGER NOT NULL, media_id INTEGER NOT NULL, position INTEGER,
            PRIMARY KEY (album_id, media_id)
        );
        CREATE INDEX IF NOT EXISTS idx_album_items_media ON album_items (media_id);
        CREATE TABLE IF NOT EXISTS media_edits (
            media_id INTEGER PRIMARY KEY, params TEXT NOT NULL, updated_at TEXT,
            dirty INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS micro_previews (media_id INTEGER PRIMARY KEY, data BLOB NOT NULL);
        """,
        // v2 (Parity sprint): the camera roll, indexed in place through PhotoKit. Only metadata:
        // the pixels stay in the Photos library and are read from there (never copied in).
        // A phone-only asset appears in every list as MediaID -pid (negative), a matched one
        // (media_id set) appears once, as its desktop item.
        """
        CREATE TABLE IF NOT EXISTS phone_assets (
            pid INTEGER PRIMARY KEY AUTOINCREMENT,
            local_id TEXT NOT NULL UNIQUE,
            date_taken TEXT, created REAL, modified REAL,
            media_type TEXT NOT NULL DEFAULT 'photo', live INTEGER NOT NULL DEFAULT 0,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            width INTEGER, height INTEGER, duration REAL,
            latitude REAL, longitude REAL,
            file_name TEXT, file_size INTEGER, sha256 TEXT,
            media_id INTEGER, match TEXT,
            backup TEXT NOT NULL DEFAULT 'none',
            in_cloud INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_phone_media ON phone_assets (media_id);
        CREATE INDEX IF NOT EXISTS idx_phone_date ON phone_assets (date_taken DESC);
        CREATE INDEX IF NOT EXISTS idx_phone_sha ON phone_assets (sha256) WHERE sha256 IS NOT NULL;
        """,
        // v3: why a backup failed (an ImportQueue / desktop reason code), shown with the Retry button.
        """
        ALTER TABLE phone_assets ADD COLUMN backup_error TEXT;
        """,
        // v4: a favourite toggled on the phone is queued in the replica until the desktop has it
        // (offline taps used to be lost, or reverted by the next sync). Same idea as media_edits.dirty.
        """
        ALTER TABLE media_items ADD COLUMN is_favorite_dirty INTEGER NOT NULL DEFAULT 0;
        """,
    ]

    public init(db: SQLiteDatabase) throws {
        self.db = db
        let current = try db.scalarInt("PRAGMA user_version") ?? 0
        for (i, sql) in Self.migrations.enumerated() where Int64(i + 1) > current {
            try db.transaction {
                try db.execute(sql)
                try db.execute("PRAGMA user_version = \(i + 1)")
            }
        }
    }

    public static func open(at url: URL) throws -> LibraryStore {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        return try LibraryStore(db: SQLiteDatabase(path: url.path))
    }

    // ── sync state ──

    public func state(_ key: String) -> String? {
        (try? db.query("SELECT value FROM sync_state WHERE key = ?", [.text(key)]))?.first?.string("value")
    }

    public func setState(_ key: String, _ value: String?) throws {
        if let value {
            try db.run("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)", [.text(key), .text(value)])
        } else {
            try db.run("DELETE FROM sync_state WHERE key = ?", [.text(key)])
        }
    }

    public var cursor: Int64 { state("cursor").flatMap { Int64($0) } ?? 0 }
    public var libraryId: String? { state("library_id") }

    /// Drops the replica (unpair, a different desktop library, or a desktop restored from backup).
    public func reset() throws {
        try db.transaction {
            for t in ["media_items", "events", "people", "face_instances", "albums", "album_items",
                      "media_edits", "micro_previews", "sync_state"] {
                try db.run("DELETE FROM \(t)")
            }
            // the camera roll stays indexed; only its links to the old library go
            try db.run("UPDATE phone_assets SET media_id = NULL, match = NULL")
        }
    }

    // ── applying a change page ──

    public struct Applied: Sendable {
        public var upserts = 0
        public var deletes = 0
        public var mediaNeedingMicro: [MediaID] = []
        public var editsChanged: [MediaID] = []
    }

    /// One transaction per page; the cursor moves inside it, so a crash never skips a change.
    @discardableResult
    public func apply(_ page: ChangePage) throws -> Applied {
        var out = Applied()
        try db.transaction {
            if page.reset {
                for t in ["media_items", "events", "people", "face_instances", "albums", "album_items",
                          "media_edits", "micro_previews"] {
                    try db.run("DELETE FROM \(t)")
                }
                try db.run("UPDATE phone_assets SET media_id = NULL, match = NULL")
                try setState("cursor", "0")
                return
            }
            for ch in page.changes {
                guard let entity = ch.entity else { continue }
                if let payload = ch.payload {
                    try upsert(payload, &out)
                    out.upserts += 1
                } else {
                    try delete(entity, ch.id)
                    out.deletes += 1
                }
            }
            try setState("library_id", page.libraryId)
            try setState("cursor", String(page.cursor))
        }
        return out
    }

    private func json<T: Encodable>(_ v: T) throws -> String {
        String(decoding: try encoder.encode(v), as: UTF8.self)
    }

    private func upsert(_ p: ChangePayload, _ out: inout Applied) throws {
        switch p {
        case .media(let m):
            let old = try db.query("SELECT payload, is_favorite, is_favorite_dirty FROM media_items WHERE id = ?",
                                   [.int(m.id)]).first
            let oldItem = old?.string("payload").flatMap { try? decoder.decode(MediaItem.self, from: Data($0.utf8)) }
            // A favourite toggled here and not yet delivered wins over what the desktop sends
            // (same rule as an unsent edit); its upload settles the desktop and clears the flag.
            var m = m
            let favoritePending = old?.bool("is_favorite_dirty") ?? false
            if favoritePending { m.isFavorite = old?.bool("is_favorite") ?? m.isFavorite }
            try db.run("""
                INSERT OR REPLACE INTO media_items (id, date_taken, media_type, file_name, latitude, longitude,
                    place_city, place_country, is_favorite, event_id, chapter_id, live, has_thumb, fingerprint, payload,
                    is_favorite_dirty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [.int(m.id), .init(m.dateTaken), .init(m.mediaType), .init(m.fileName), .init(m.latitude),
                      .init(m.longitude), .init(m.placeCity), .init(m.placeCountry), .init(m.isFavorite),
                      .init(m.eventId), .init(m.chapterId), .init(m.live), .init(m.hasThumb), .init(m.fingerprint),
                      .text(try json(m)), .init(favoritePending)])
            // new photo, or the file changed (new fingerprint): fetch its LQIP
            if oldItem == nil || oldItem?.fingerprint != m.fingerprint || oldItem?.fileMtime != m.fileMtime {
                if oldItem != nil { try db.run("DELETE FROM micro_previews WHERE media_id = ?", [.int(m.id)]) }
                out.mediaNeedingMicro.append(m.id)
            }
        case .event(let e):
            try db.run("""
                INSERT OR REPLACE INTO events (id, title, location_name, start_date, end_date, item_count,
                    cover_media_id, kind, parent_id, tint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [.int(e.id), .init(e.title), .init(e.locationName), .init(e.startDate), .init(e.endDate),
                      .init(e.itemCount), .init(e.coverMediaId), .init(e.kind), .init(e.parentId), .init(e.tint)])
        case .person(let p):
            try db.run("INSERT OR REPLACE INTO people (id, name, key_face_id, is_favorite, is_confirmed) VALUES (?, ?, ?, ?, ?)",
                       [.int(p.id), .init(p.name), .init(p.keyFaceId), .init(p.isFavorite), .init(p.isConfirmed)])
        case .face(let f):
            let b: [Double?] = f.bbox + [Double?](repeating: nil, count: max(0, 4 - f.bbox.count))
            try db.run("""
                INSERT OR REPLACE INTO face_instances (id, media_id, person_id, bbox_x, bbox_y, bbox_w, bbox_h, quality_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [.int(f.id), .int(f.mediaId), .init(f.personId), .init(b[0]), .init(b[1]), .init(b[2]),
                      .init(b[3]), .init(f.quality)])
        case .album(let a):
            try db.run("""
                INSERT OR REPLACE INTO albums (id, name, kind, parent_id, position, sort_mode, cover_media_id,
                    query, filter, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [.int(a.id), .init(a.name), .init(a.kind), .init(a.parentId), .init(a.position),
                      .init(a.sortMode), .init(a.coverMediaId), .init(a.query), .init(a.filter), .init(a.createdAt)])
            try db.run("DELETE FROM album_items WHERE album_id = ?", [.int(a.id)])
            for (i, mid) in a.items.enumerated() {
                try db.run("INSERT OR IGNORE INTO album_items (album_id, media_id, position) VALUES (?, ?, ?)",
                           [.int(a.id), .int(mid), .int(Int64(i))])
            }
        case .edit(let e):
            // A local edit not yet uploaded wins: its upload will overwrite the desktop's copy and
            // the canonical result comes back through the journal.
            let dirty = try db.query("SELECT dirty FROM media_edits WHERE media_id = ?", [.int(e.mediaId)]).first?.bool("dirty") ?? false
            if !dirty {
                try db.run("INSERT OR REPLACE INTO media_edits (media_id, params, updated_at, dirty) VALUES (?, ?, ?, 0)",
                           [.int(e.mediaId), .text(try json(e.params)), .init(e.updatedAt)])
                out.editsChanged.append(e.mediaId)
            }
        }
    }

    private func delete(_ entity: ChangeEntity, _ id: Int64) throws {
        let one: [SQLiteDatabase.Value] = [.int(id)]
        switch entity {
        case .media:
            try db.run("DELETE FROM media_items WHERE id = ?", one)
            try db.run("DELETE FROM face_instances WHERE media_id = ?", one)
            try db.run("DELETE FROM album_items WHERE media_id = ?", one)
            try db.run("DELETE FROM media_edits WHERE media_id = ?", one)
            try db.run("DELETE FROM micro_previews WHERE media_id = ?", one)
            // gone from the desktop: the camera-roll asset is phone-only again (never deleted here)
            try db.run("UPDATE phone_assets SET media_id = NULL, match = NULL WHERE media_id = ?", one)
        case .event: try db.run("DELETE FROM events WHERE id = ?", one)
        case .person:
            try db.run("DELETE FROM people WHERE id = ?", one)
            try db.run("UPDATE face_instances SET person_id = NULL WHERE person_id = ?", one)
        case .face: try db.run("DELETE FROM face_instances WHERE id = ?", one)
        case .album:
            try db.run("DELETE FROM albums WHERE id = ?", one)
            try db.run("DELETE FROM album_items WHERE album_id = ?", one)
        case .edit: try db.run("DELETE FROM media_edits WHERE media_id = ? AND dirty = 0", one)
        }
    }

    // ── micro previews ──

    public func putMicro(_ items: [(MediaID, Data)]) throws {
        try db.transaction {
            for (id, data) in items where !data.isEmpty {
                try db.run("INSERT OR REPLACE INTO micro_previews (media_id, data) "
                           + "SELECT ?, ? WHERE EXISTS (SELECT 1 FROM media_items WHERE id = ?)",
                           [.int(id), .blob(data), .int(id)])
            }
        }
    }

    public func micro(_ id: MediaID) -> Data? {
        (try? db.query("SELECT data FROM micro_previews WHERE media_id = ?", [.int(id)]))?.first?.data("data")
    }

    public func mediaMissingMicro(limit: Int = 5000) -> [MediaID] {
        ((try? db.query("SELECT m.id FROM media_items m LEFT JOIN micro_previews p ON p.media_id = m.id "
                        + "WHERE p.media_id IS NULL ORDER BY m.date_taken DESC LIMIT ?", [.int(Int64(limit))])) ?? [])
            .compactMap { $0.int("id") }
    }

    // ── edits ──

    public func edit(for id: MediaID) -> Adjustments? {
        guard let row = (try? db.query("SELECT params FROM media_edits WHERE media_id = ?", [.int(id)]))?.first,
              let text = row.string("params"),
              let dict = try? decoder.decode([String: JSONValue].self, from: Data(text.utf8)) else { return nil }
        return Adjustments(dict: dict)
    }

    /// A phone-side edit: stored at once (dirty) and uploaded by SyncEngine.pushEdits().
    /// A neutral edit is a revert, as on the desktop: an empty params object, uploaded as DELETE.
    public func saveLocalEdit(_ id: MediaID, _ adj: Adjustments) throws {
        let a = adj.normalized()
        let params = a.isIdentity ? "{}" : String(decoding: try a.jsonData(), as: UTF8.self)
        try db.run("INSERT OR REPLACE INTO media_edits (media_id, params, updated_at, dirty) VALUES (?, ?, NULL, 1)",
                   [.int(id), .text(params)])
    }

    /// One queued phone edit. `stored` is the exact params text in the replica: the upload
    /// answer may replace the row only while it still holds that text (see editUploaded).
    public struct PendingEdit: Sendable {
        public let id: MediaID
        public let adjustments: Adjustments
        public let stored: String
    }

    public func dirtyEdits() -> [PendingEdit] {
        ((try? db.query("SELECT media_id, params FROM media_edits WHERE dirty = 1")) ?? []).compactMap { r in
            guard let id = r.int("media_id"), let text = r.string("params"),
                  let dict = try? decoder.decode([String: JSONValue].self, from: Data(text.utf8)) else { return nil }
            return PendingEdit(id: id, adjustments: Adjustments(dict: dict), stored: text)
        }
    }

    public var pendingEditCount: Int {
        Int((try? db.scalarInt("SELECT COUNT(*) FROM media_edits WHERE dirty = 1")) ?? 0)
    }

    /// The desktop's canonical answer to an upload (nil = the edit was cleared). Compare-and-swap:
    /// it lands only if the row still holds what was uploaded. A newer local edit, saved while the
    /// request was in flight, stays dirty and goes up on the next push. Before this check, the
    /// late answer overwrote the newer edit and marked it clean, and the edit was lost.
    /// Returns false when a newer edit won.
    @discardableResult
    public func editUploaded(_ id: MediaID, canonical: EditRecord?, uploaded: String) throws -> Bool {
        var landed = false
        try db.transaction {
            let still = try db.query("SELECT 1 FROM media_edits WHERE media_id = ? AND dirty = 1 AND params = ?",
                                     [.int(id), .text(uploaded)]).first != nil
            guard still else { return }
            if let canonical {
                try db.run("INSERT OR REPLACE INTO media_edits (media_id, params, updated_at, dirty) VALUES (?, ?, ?, 0)",
                           [.int(id), .text(try json(canonical.params)), .init(canonical.updatedAt)])
            } else {
                try db.run("DELETE FROM media_edits WHERE media_id = ?", [.int(id)])
            }
            landed = true
        }
        return landed
    }

    // ── queries for the UI ──

    /// Where an item lives (three-state library).
    public enum Origin: Int, Sendable, Hashable {
        case pc = 0          // desktop only: streamed and cached on demand
        case both = 1        // on the desktop and in the camera roll: pixels read locally
        case phone = 2       // camera roll only: waiting for backup
    }

    public struct TimelineEntry: Hashable, Identifiable, Sendable {
        public let id: MediaID
        public let dateTaken: String?
        public let isVideo: Bool
        public let live: Bool
        public let isFavorite: Bool
        public var origin: Origin = .pc

        public init(id: MediaID, dateTaken: String?, isVideo: Bool, live: Bool, isFavorite: Bool, origin: Origin = .pc) {
            self.id = id
            self.dateTaken = dateTaken
            self.isVideo = isVideo
            self.live = live
            self.isFavorite = isFavorite
            self.origin = origin
        }
    }

    private func timeline(_ rows: [SQLiteDatabase.Row]) -> [TimelineEntry] {
        rows.compactMap { r in
            guard let id = r.int("id") else { return nil }
            return TimelineEntry(id: id, dateTaken: r.string("date_taken"), isVideo: r.string("media_type") == "video",
                                 live: r.bool("live"), isFavorite: r.bool("is_favorite"),
                                 origin: Origin(rawValue: Int(r.int("origin") ?? 0)) ?? .pc)
        }
    }

    private static let timelineCols = "id, date_taken, media_type, live, is_favorite"

    /// Newest first: desktop items (marked `both` when the camera roll holds them too) and the
    /// phone-only camera-roll assets in one list. 20 000 rows are about 1 MB of structs: the grid
    /// keeps the whole list.
    public func timeline(favoritesOnly: Bool = false) -> [TimelineEntry] {
        let fav = favoritesOnly ? " AND is_favorite = 1" : ""
        return timeline((try? db.query("""
            SELECT m.id, m.date_taken, m.media_type, m.live, m.is_favorite,
                   CASE WHEN p.media_id IS NULL THEN 0 ELSE 1 END AS origin
            FROM media_items m
            LEFT JOIN (SELECT DISTINCT media_id FROM phone_assets WHERE media_id IS NOT NULL) p ON p.media_id = m.id
            WHERE 1 = 1\(fav.replacingOccurrences(of: "is_favorite", with: "m.is_favorite"))
            UNION ALL
            SELECT -pid AS id, date_taken, media_type, live, is_favorite, 2 AS origin
            FROM phone_assets WHERE media_id IS NULL\(fav)
            ORDER BY 2 DESC, 1 DESC
            """)) ?? [])
    }

    public func media(_ id: MediaID) -> MediaItem? {
        if id < 0 { return phoneMedia(id) }
        guard let text = (try? db.query("SELECT payload FROM media_items WHERE id = ?", [.int(id)]))?.first?.string("payload")
        else { return nil }
        return try? decoder.decode(MediaItem.self, from: Data(text.utf8))
    }

    /// Top-level trips (journeys, day trips, moments), newest first. Chapters live inside journeys.
    public func events() -> [Event] {
        let rows = (try? db.query("SELECT * FROM events WHERE parent_id IS NULL AND COALESCE(kind, '') != 'chapter' "
                                  + "ORDER BY start_date DESC, id DESC")) ?? []
        return rows.compactMap(Self.event)
    }

    public func chapters(of journey: Int64) -> [Event] {
        ((try? db.query("SELECT * FROM events WHERE parent_id = ? ORDER BY start_date, id", [.int(journey)])) ?? [])
            .compactMap(Self.event)
    }

    static func event(_ r: SQLiteDatabase.Row) -> Event? {
        guard let id = r.int("id") else { return nil }
        var dict: [String: JSONValue] = ["id": .number(Double(id))]
        for k in ["title", "location_name", "start_date", "end_date", "kind", "tint"] {
            if let s = r.string(k) { dict[k] = .string(s) }
        }
        for k in ["item_count", "cover_media_id", "parent_id"] {
            if let v = r.int(k) { dict[k] = .number(Double(v)) }
        }
        guard let data = try? JSONEncoder().encode(dict) else { return nil }
        return try? JSONDecoder().decode(Event.self, from: data)
    }

    public func media(inEvent id: Int64) -> [TimelineEntry] {
        timeline((try? db.query("SELECT \(Self.timelineCols) FROM media_items WHERE event_id = ? OR chapter_id = ? "
                                + "ORDER BY date_taken, id", [.int(id), .int(id)])) ?? [])
    }

    public struct PersonSummary: Hashable, Identifiable, Sendable {
        public let id: Int64
        public let name: String?
        public let keyFaceId: Int64?
        public let photoCount: Int
        public let isFavorite: Bool
    }

    /// Named people first, then by how often they appear (the desktop's People order).
    public func people() -> [PersonSummary] {
        let rows = (try? db.query("""
            SELECT p.id, p.name, p.key_face_id, p.is_favorite, COUNT(DISTINCT f.media_id) AS n
            FROM people p JOIN face_instances f ON f.person_id = p.id
            JOIN media_items m ON m.id = f.media_id
            GROUP BY p.id
            ORDER BY p.is_favorite DESC, (p.name IS NULL OR p.name = '') ASC, n DESC
            """)) ?? []
        return rows.compactMap { r in
            guard let id = r.int("id") else { return nil }
            return PersonSummary(id: id, name: r.string("name"), keyFaceId: r.int("key_face_id"),
                                 photoCount: Int(r.int("n") ?? 0), isFavorite: r.bool("is_favorite"))
        }
    }

    public func media(withPerson id: Int64) -> [TimelineEntry] {
        timeline((try? db.query("SELECT DISTINCT m.id, m.date_taken, m.media_type, m.live, m.is_favorite "
                                + "FROM media_items m JOIN face_instances f ON f.media_id = m.id "
                                + "WHERE f.person_id = ? ORDER BY m.date_taken DESC", [.int(id)])) ?? [])
    }

    public struct GeoPoint: Hashable, Sendable {
        public let id: MediaID
        public let latitude: Double
        public let longitude: Double
    }

    public func geoPoints() -> [GeoPoint] {
        ((try? db.query("SELECT id, latitude, longitude FROM media_items WHERE latitude IS NOT NULL "
                        + "AND longitude IS NOT NULL UNION ALL SELECT -pid, latitude, longitude FROM phone_assets "
                        + "WHERE media_id IS NULL AND latitude IS NOT NULL AND longitude IS NOT NULL")) ?? []).compactMap { r in
            guard let id = r.int("id"), let la = r.double("latitude"), let lo = r.double("longitude") else { return nil }
            return GeoPoint(id: id, latitude: la, longitude: lo)
        }
    }

    public enum SearchScope: String, CaseIterable, Sendable {
        case all, people, places, trips, videos, favorites
    }

    /// Offline metadata search (names, places, trips, file names). Semantic CLIP search needs the
    /// desktop's index: protocol 2 adds /v1/search, see docs/SYNC_PROTOCOL.md.
    public func search(_ text: String, scope: SearchScope = .all, limit: Int = 2000) -> [TimelineEntry] {
        let q = "%" + text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "%", with: "") + "%"
        let cols = "m.id, m.date_taken, m.media_type, m.live, m.is_favorite"
        let tail = " ORDER BY m.date_taken DESC LIMIT \(limit)"
        let sql: String
        var args: [SQLiteDatabase.Value] = []
        switch scope {
        case .people:
            sql = "SELECT DISTINCT \(cols) FROM media_items m JOIN face_instances f ON f.media_id = m.id "
                + "JOIN people p ON p.id = f.person_id WHERE p.name LIKE ?" + tail
            args = [.text(q)]
        case .places:
            sql = "SELECT \(cols) FROM media_items m WHERE m.place_city LIKE ? OR m.place_country LIKE ?" + tail
            args = [.text(q), .text(q)]
        case .trips:
            sql = "SELECT \(cols) FROM media_items m JOIN events e ON e.id = m.event_id "
                + "WHERE e.title LIKE ? OR e.location_name LIKE ?" + tail
            args = [.text(q), .text(q)]
        case .videos:
            sql = "SELECT \(cols) FROM media_items m WHERE m.media_type = 'video' AND (m.file_name LIKE ? "
                + "OR m.place_city LIKE ?)" + tail
            args = [.text(q), .text(q)]
        case .favorites:
            sql = "SELECT \(cols) FROM media_items m WHERE m.is_favorite = 1 AND (m.file_name LIKE ? "
                + "OR m.place_city LIKE ? OR m.place_country LIKE ?)" + tail
            args = [.text(q), .text(q), .text(q)]
        case .all:
            sql = """
                SELECT \(cols) FROM media_items m WHERE m.file_name LIKE ?1 OR m.place_city LIKE ?1
                   OR m.place_country LIKE ?1 OR m.date_taken LIKE ?1
                   OR m.event_id IN (SELECT id FROM events WHERE title LIKE ?1 OR location_name LIKE ?1)
                   OR m.id IN (SELECT f.media_id FROM face_instances f JOIN people p ON p.id = f.person_id
                               WHERE p.name LIKE ?1)
                """ + tail
            args = [.text(q)]
        }
        return timeline((try? db.query(sql, args)) ?? [])
    }

    /// A favourite toggled on the phone: stored at once and flagged (is_favorite_dirty) until the
    /// desktop has it, so it survives being offline, a relaunch and the sync pages that arrive meanwhile.
    public func setFavoriteLocally(_ id: MediaID, _ value: Bool) throws {
        guard var m = media(id) else { return }
        m.isFavorite = value
        try db.run("UPDATE media_items SET is_favorite = ?, is_favorite_dirty = 1, payload = ? WHERE id = ?",
                   [.init(value), .text(try json(m)), .int(id)])
    }

    /// One queued favourite: the value to deliver.
    public struct PendingFavorite: Sendable, Equatable {
        public let id: MediaID
        public let value: Bool
    }

    public func dirtyFavorites() -> [PendingFavorite] {
        ((try? db.query("SELECT id, is_favorite FROM media_items WHERE is_favorite_dirty = 1")) ?? []).compactMap { r in
            r.int("id").map { PendingFavorite(id: $0, value: r.bool("is_favorite")) }
        }
    }

    public var pendingFavoriteCount: Int {
        Int((try? db.scalarInt("SELECT COUNT(*) FROM media_items WHERE is_favorite_dirty = 1")) ?? 0)
    }

    /// The desktop acknowledged `uploaded`. Compare-and-swap, like editUploaded: the flag clears only
    /// while the row still holds the value that was sent, so a toggle made while the request was in
    /// flight stays queued. `gone` (404: deleted or hidden on the desktop) drops the flag outright.
    /// Returns false when a newer toggle won.
    @discardableResult
    public func favoriteUploaded(_ id: MediaID, uploaded: Bool, gone: Bool = false) throws -> Bool {
        if gone {
            try db.run("UPDATE media_items SET is_favorite_dirty = 0 WHERE id = ?", [.int(id)])
            return true
        }
        var landed = false
        try db.transaction {
            let still = try db.query("SELECT 1 FROM media_items WHERE id = ? AND is_favorite_dirty = 1 AND is_favorite = ?",
                                     [.int(id), .init(uploaded)]).first != nil
            guard still else { return }
            try db.run("UPDATE media_items SET is_favorite_dirty = 0 WHERE id = ?", [.int(id)])
            landed = true
        }
        return landed
    }

    public func counts() -> (media: Int, events: Int, people: Int) {
        func n(_ t: String) -> Int { Int((try? db.scalarInt("SELECT COUNT(*) FROM \(t)")) ?? 0) }
        return (n("media_items"), n("events"), n("people"))
    }
}
#endif
