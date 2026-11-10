#if canImport(SQLite3)
import Foundation

/// The camera roll in the replica (Parity sprint, schema v2). PhotoKit owns the pixels; this
/// table only knows what an asset is, whether the desktop holds it and where its backup stands.
extension LibraryStore {
    /// One camera-roll asset as PhoneLibrary reads it from PhotoKit.
    public struct PhoneAssetRecord: Sendable, Hashable {
        public var localId: String
        public var created: Date?
        public var modified: Date?
        public var isVideo: Bool
        public var live: Bool
        public var isFavorite: Bool
        public var width: Int
        public var height: Int
        public var duration: Double
        public var latitude: Double?
        public var longitude: Double?
        public var fileName: String?
        public var fileSize: Int64?

        public init(localId: String, created: Date?, modified: Date?, isVideo: Bool, live: Bool, isFavorite: Bool,
                    width: Int, height: Int, duration: Double, latitude: Double?, longitude: Double?,
                    fileName: String?, fileSize: Int64?) {
            self.localId = localId
            self.created = created
            self.modified = modified
            self.isVideo = isVideo
            self.live = live
            self.isFavorite = isFavorite
            self.width = width
            self.height = height
            self.duration = duration
            self.latitude = latitude
            self.longitude = longitude
            self.fileName = fileName
            self.fileSize = fileSize
        }
    }

    /// Backup of a phone-only asset to the desktop.
    public enum BackupState: String, Sendable, CaseIterable {
        case none          // not asked for
        case approved      // the user said "back up" (banner, or automatic backup is on)
        case queued        // exported into the outbox, uploading
        case sent          // the desktop verified the bytes; waiting for its scanner to index them
        case failed        // the desktop refused it for good (format, size)
    }

    public struct PhoneAsset: Sendable, Hashable {
        public let pid: Int64
        public let localId: String
        public let mediaId: MediaID?
        public let match: String?
        public let backup: BackupState
        public let sha256: String?
        public let fileName: String?
        public let fileSize: Int64?
        public let isVideo: Bool
        public let live: Bool
        public let inCloud: Bool

        /// The id this asset has in every list: its desktop item, or -pid while phone-only.
        public var listId: MediaID { mediaId ?? -pid }
    }

    static func phoneAsset(_ r: SQLiteDatabase.Row) -> PhoneAsset? {
        guard let pid = r.int("pid"), let local = r.string("local_id") else { return nil }
        return PhoneAsset(pid: pid, localId: local, mediaId: r.int("media_id"), match: r.string("match"),
                          backup: BackupState(rawValue: r.string("backup") ?? "") ?? BackupState.none,
                          sha256: r.string("sha256"), fileName: r.string("file_name"), fileSize: r.int("file_size"),
                          isVideo: r.string("media_type") == "video", live: r.bool("live"), inCloud: r.bool("in_cloud"))
    }

    // ── writes from PhoneLibrary ──

    /// Last modification time PhotoKit reported per asset: lets a refresh skip unchanged assets
    /// (reading their resources is the expensive part).
    public func phoneModified() -> [String: Double] {
        var out: [String: Double] = [:]
        for r in (try? db.query("SELECT local_id, modified FROM phone_assets")) ?? [] {
            if let id = r.string("local_id") { out[id] = r.double("modified") ?? 0 }
        }
        return out
    }

    /// Inserts or refreshes assets. A changed asset keeps its pid (its list id), its match and
    /// its backup state; its SHA-256 is dropped when the bytes may have changed (size differs).
    public func upsertPhoneAssets(_ records: [PhoneAssetRecord], zone: TimeZone = .current) throws {
        guard !records.isEmpty else { return }
        try db.transaction {
            for a in records {
                let wall = a.created.map { AssetMatcher.wallString($0, zone: zone) }
                try db.run("""
                    INSERT INTO phone_assets (local_id, date_taken, created, modified, media_type, live, is_favorite,
                                              width, height, duration, latitude, longitude, file_name, file_size)
                    VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
                    ON CONFLICT(local_id) DO UPDATE SET
                        date_taken = ?2, created = ?3, modified = ?4, media_type = ?5, live = ?6, is_favorite = ?7,
                        width = ?8, height = ?9, duration = ?10, latitude = ?11, longitude = ?12,
                        file_name = COALESCE(?13, file_name),
                        sha256 = CASE WHEN ?14 IS NOT NULL AND file_size IS NOT ?14 THEN NULL ELSE sha256 END,
                        file_size = COALESCE(?14, file_size)
                    """, [.text(a.localId), .init(wall), .init(a.created?.timeIntervalSince1970),
                          .init(a.modified?.timeIntervalSince1970), .text(a.isVideo ? "video" : "photo"),
                          .init(a.live), .init(a.isFavorite), .int(Int64(a.width)), .int(Int64(a.height)),
                          .double(a.duration), .init(a.latitude), .init(a.longitude), .init(a.fileName),
                          .init(a.fileSize)])
            }
        }
    }

    /// Drops assets deleted from the camera roll (the desktop copy is never touched).
    public func removePhoneAssets(keeping present: Set<String>) throws {
        let known = ((try? db.query("SELECT local_id FROM phone_assets")) ?? []).compactMap { $0.string("local_id") }
        let gone = known.filter { !present.contains($0) }
        try removePhoneAssets(gone)
    }

    public func removePhoneAssets(_ localIds: [String]) throws {
        guard !localIds.isEmpty else { return }
        try db.transaction {
            for id in localIds { try db.run("DELETE FROM phone_assets WHERE local_id = ?", [.text(id)]) }
        }
    }

    public func setPhoneInCloud(_ localId: String, _ inCloud: Bool) throws {
        try db.run("UPDATE phone_assets SET in_cloud = ? WHERE local_id = ?", [.init(inCloud), .text(localId)])
    }

    public func setPhoneSha(_ localId: String, _ sha: String) throws {
        try db.run("UPDATE phone_assets SET sha256 = ? WHERE local_id = ?", [.text(sha), .text(localId)])
    }

    public func setBackup(_ localIds: [String], _ state: BackupState) throws {
        guard !localIds.isEmpty else { return }
        try db.transaction {
            for id in localIds {
                try db.run("UPDATE phone_assets SET backup = ? WHERE local_id = ?", [.text(state.rawValue), .text(id)])
            }
        }
    }

    /// Every phone-only asset not yet asked for becomes `approved` (the banner's "Back up all").
    @discardableResult
    public func approveAllPhoneOnly() throws -> Int {
        let n = phoneCounts().waiting
        try db.run("UPDATE phone_assets SET backup = 'approved' WHERE media_id IS NULL AND backup IN ('none', 'failed')")
        return n
    }

    // ── matching ──

    /// Inputs for AssetMatcher: every phone asset, and every desktop item.
    public func matchInputs() -> (phone: [AssetMatcher.PhoneKey], desktop: [AssetMatcher.DesktopKey],
                                  fixed: [String: MediaID]) {
        var phone: [AssetMatcher.PhoneKey] = []
        var fixed: [String: MediaID] = [:]
        for r in (try? db.query("SELECT local_id, file_name, created, file_size, media_id, match FROM phone_assets")) ?? [] {
            guard let id = r.string("local_id") else { continue }
            phone.append(.init(localId: id, fileName: r.string("file_name"),
                               created: r.double("created").map { Date(timeIntervalSince1970: $0) },
                               fileSize: r.int("file_size")))
            if r.string("match") == AssetMatcher.Kind.sha256.rawValue, let m = r.int("media_id") { fixed[id] = m }
        }
        let desktopRows = (try? db.query("""
            SELECT id, file_name, date_taken, json_extract(payload, '$.timezone_offset') AS tz,
                   json_extract(payload, '$.file_size') AS size
            FROM media_items
            """)) ?? []
        let desktop = desktopRows.compactMap { r -> AssetMatcher.DesktopKey? in
            guard let id = r.int("id") else { return nil }
            return .init(id: id, fileName: r.string("file_name"), dateTaken: r.string("date_taken"),
                         timezoneOffset: r.string("tz"), fileSize: r.int("size"))
        }
        // a SHA match whose desktop item is gone no longer holds
        let present = Set(desktop.map(\.id))
        fixed = fixed.filter { present.contains($0.value) }
        return (phone, desktop, fixed)
    }

    /// Writes a full match result: every asset not in `matches` becomes phone-only. Returns how
    /// many assets changed state (the UI reloads only when something did).
    @discardableResult
    public func applyMatches(_ matches: [String: AssetMatcher.Match]) throws -> Int {
        var changed = 0
        try db.transaction {
            let rows = try db.query("SELECT local_id, media_id, match FROM phone_assets")
            for r in rows {
                guard let id = r.string("local_id") else { continue }
                let want = matches[id]
                if r.int("media_id") == want?.mediaId && r.string("match") == want?.kind.rawValue { continue }
                changed += 1
                try db.run("UPDATE phone_assets SET media_id = ?, match = ?, "
                           + "backup = CASE WHEN ? IS NOT NULL THEN 'none' ELSE backup END WHERE local_id = ?",
                           [.init(want?.mediaId), .init(want?.kind.rawValue), .init(want?.mediaId), .text(id)])
            }
        }
        return changed
    }

    /// Re-runs the offline matcher over the whole camera roll (fast: hashed indexes, ~20 ms for
    /// 20 000 assets). Called after a PhotoKit refresh and after every sync page with media.
    @discardableResult
    public func rematchPhone(zone: TimeZone = .current) throws -> Int {
        let input = matchInputs()
        guard !input.phone.isEmpty else { return 0 }
        return try applyMatches(AssetMatcher.match(phone: input.phone, desktop: input.desktop, zone: zone,
                                                   fixed: input.fixed))
    }

    /// Records SHA-256 proofs from /v1/match (sha256 -> media id).
    @discardableResult
    public func applyShaMatches(_ matches: [String: MediaID]) throws -> Int {
        var n = 0
        try db.transaction {
            for (sha, media) in matches {
                guard try db.query("SELECT 1 FROM media_items WHERE id = ?", [.int(media)]).first != nil else { continue }
                try db.run("UPDATE phone_assets SET media_id = ?, match = 'sha256', backup = 'none' "
                           + "WHERE sha256 = ? AND (media_id IS NOT ? OR match IS NOT 'sha256')",
                           [.int(media), .text(sha), .int(media)])
                n += 1
            }
        }
        return n
    }

    // ── reads ──

    public func phoneAsset(localId: String) -> PhoneAsset? {
        (try? db.query("SELECT * FROM phone_assets WHERE local_id = ?", [.text(localId)]))?.first.flatMap(Self.phoneAsset)
    }

    /// The camera-roll asset behind a list id: -pid for phone-only, or the local copy of a
    /// desktop item (state `both`). Nil = desktop only.
    public func phoneAsset(for id: MediaID) -> PhoneAsset? {
        let rows = id < 0
            ? try? db.query("SELECT * FROM phone_assets WHERE pid = ?", [.int(-id)])
            : try? db.query("SELECT * FROM phone_assets WHERE media_id = ? ORDER BY in_cloud, pid LIMIT 1", [.int(id)])
        return rows?.first.flatMap(Self.phoneAsset)
    }

    public func localIdentifier(for id: MediaID) -> String? { phoneAsset(for: id)?.localId }

    public func origin(of id: MediaID) -> Origin {
        if id < 0 { return .phone }
        return (try? db.query("SELECT 1 FROM phone_assets WHERE media_id = ? LIMIT 1", [.int(id)]))?.first != nil ? .both : .pc
    }

    /// Phone-only assets the user approved for backup, oldest first (a long backup keeps the
    /// timeline's order on the desktop).
    public func backupCandidates(limit: Int = 200) -> [PhoneAsset] {
        ((try? db.query("SELECT * FROM phone_assets WHERE media_id IS NULL AND backup = 'approved' "
                        + "ORDER BY created LIMIT ?", [.int(Int64(limit))])) ?? []).compactMap(Self.phoneAsset)
    }

    /// Backed-up assets whose desktop item has not been linked yet (the PC is still scanning).
    public func sentWithoutMatch() -> [PhoneAsset] {
        ((try? db.query("SELECT * FROM phone_assets WHERE media_id IS NULL AND backup = 'sent' AND sha256 IS NOT NULL"))
            ?? []).compactMap(Self.phoneAsset)
    }

    /// Unmatched assets with a known hash: candidates for /v1/match.
    public func unmatchedWithSha(limit: Int = 400) -> [PhoneAsset] {
        ((try? db.query("SELECT * FROM phone_assets WHERE media_id IS NULL AND sha256 IS NOT NULL LIMIT ?",
                        [.int(Int64(limit))])) ?? []).compactMap(Self.phoneAsset)
    }

    /// Unmatched assets without a hash (hashing them asks the desktop "do you have these bytes?").
    public func unmatchedWithoutSha(limit: Int = 50) -> [PhoneAsset] {
        ((try? db.query("SELECT * FROM phone_assets WHERE media_id IS NULL AND sha256 IS NULL AND in_cloud = 0 "
                        + "ORDER BY created DESC LIMIT ?", [.int(Int64(limit))])) ?? []).compactMap(Self.phoneAsset)
    }

    public struct PhoneCounts: Sendable, Equatable {
        public var total = 0
        public var both = 0
        public var phoneOnly = 0
        public var waiting = 0          // phone-only, backup not asked for
        public var approved = 0
        public var queued = 0
        public var sent = 0
        public init() {}
    }

    public func phoneCounts() -> PhoneCounts {
        var c = PhoneCounts()
        let rows = (try? db.query("""
            SELECT COUNT(*) AS total,
                   SUM(media_id IS NOT NULL) AS both,
                   SUM(media_id IS NULL) AS phone,
                   SUM(media_id IS NULL AND backup IN ('none', 'failed')) AS waiting,
                   SUM(media_id IS NULL AND backup = 'approved') AS approved,
                   SUM(media_id IS NULL AND backup = 'queued') AS queued,
                   SUM(media_id IS NULL AND backup = 'sent') AS sent
            FROM phone_assets
            """)) ?? []
        if let r = rows.first {
            c.total = Int(r.int("total") ?? 0)
            c.both = Int(r.int("both") ?? 0)
            c.phoneOnly = Int(r.int("phone") ?? 0)
            c.waiting = Int(r.int("waiting") ?? 0)
            c.approved = Int(r.int("approved") ?? 0)
            c.queued = Int(r.int("queued") ?? 0)
            c.sent = Int(r.int("sent") ?? 0)
        }
        return c
    }

    /// Trip mode offline: the event's photos with a position, for JourneyLocal.
    public func journeyPhotos(event id: Int64) -> [JourneyLocal.Photo] {
        ((try? db.query("SELECT id, latitude, longitude, date_taken, place_city FROM media_items "
                        + "WHERE (event_id = ?1 OR chapter_id = ?1) AND latitude IS NOT NULL AND longitude IS NOT NULL "
                        + "AND date_taken IS NOT NULL", [.int(id)])) ?? []).compactMap { r in
            guard let mid = r.int("id"), let la = r.double("latitude"), let lo = r.double("longitude"),
                  let d = r.string("date_taken") else { return nil }
            return JourneyLocal.Photo(id: mid, lat: la, lon: lo, date: d, city: r.string("place_city"))
        }
    }

    /// A phone-only asset as a MediaItem, so the viewer, map and search need no second code path.
    func phoneMedia(_ id: MediaID) -> MediaItem? {
        guard let r = (try? db.query("SELECT * FROM phone_assets WHERE pid = ?", [.int(-id)]))?.first else { return nil }
        var m = MediaItem(id: id, fileName: r.string("file_name"), mediaType: r.string("media_type") ?? "photo",
                          dateTaken: r.string("date_taken"))
        m.fileSize = r.int("file_size")
        m.fileExt = r.string("file_name").map { ($0 as NSString).pathExtension.lowercased() }
        m.latitude = r.double("latitude")
        m.longitude = r.double("longitude")
        m.isFavorite = r.bool("is_favorite")
        m.live = r.bool("live")
        m.availability = "phone"
        return m
    }
}
#endif
