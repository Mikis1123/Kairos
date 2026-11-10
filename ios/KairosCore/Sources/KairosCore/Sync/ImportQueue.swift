import Foundation
#if canImport(CryptoKit)
import CryptoKit

/// What the outbox needs from the desktop connection. `SyncClient` is the real one; tests script it.
public protocol ImportUploading: Sendable {
    func importPresent(sha256: String) async throws -> Bool
    func uploadImport(file: URL, name: String, sha256: String) async throws -> SyncClient.ImportOutcome
}

extension SyncClient: ImportUploading {}

/// Camera-roll photos on their way to the desktop (POST /v1/import). Picked files are copied into
/// an outbox in Application Support with a JSON manifest, so a batch survives the app being killed,
/// the phone leaving Wi-Fi or the desktop sleeping. One upload at a time (the bridge has 8 request
/// slots that browsing also needs).
///
/// An item leaves the outbox in exactly one of three ways, and each one is written to the durable
/// outcome log (`outcomes.json`) BEFORE the item is removed, so the backup state machine never has
/// to guess from "it is no longer in the outbox":
///   * acknowledged: the desktop verified the SHA-256 (or already had those bytes)      -> ok
///   * rejected:     the desktop refused the FILE for good (format, size, corrupted 5x)  -> failed(reason)
///   * unpair:       `removeAll()`; no outcome, the owner re-queues what it still wants
/// A problem on the DESKTOP's side (507 disk full, any 5xx, 503 power saver, an old desktop answering
/// 404) never costs the item anything: it stays queued and the queue pauses with a growing delay.
public actor ImportQueue {
    public struct Item: Codable, Identifiable, Sendable, Equatable {
        public let id: UUID
        public let name: String            // e.g. IMG_0042.HEIC, as the phone named it
        public let sha256: String
        public let size: Int64
        public let added: Date
        public var failures: Int           // transfers the desktop reported as corrupted (422)
        public var lastError: String?
        /// The camera-roll asset this file belongs to (a Live Photo has two files). nil = picked by hand.
        public var group: String?
        /// Times the desktop was in no state to take it (507, 5xx, 503 ...). Drives the pause length.
        public var deferrals: Int?
        public var pausedUntil: Date?

        var fileName: String {
            let ext = (name as NSString).pathExtension
            return ext.isEmpty ? id.uuidString : "\(id.uuidString).\(ext.lowercased())"
        }
    }

    /// How one file left the outbox. Kept until the backup state machine has settled its asset.
    public struct Outcome: Codable, Identifiable, Sendable, Equatable {
        public let id: UUID
        public let sha256: String
        public let group: String?
        public let name: String
        public let ok: Bool
        public let reason: String?
        public let date: Date
    }

    public struct Progress: Sendable, Equatable {
        public var pending: Int = 0
        public var bytesPending: Int64 = 0
        public var uploading: String?
        public var sent: Int = 0              // since launch
        public var rejected: [String] = []    // names the desktop refused for good
        public var rejectedReason: String?    // why the last one was refused (a BackupReason code)
        public var paused: String?            // the desktop can't take uploads right now: why
        public init() {}
    }

    /// Formats the desktop scanner indexes (sync_server.IMPORT_EXTS). Others are refused at pick time.
    public static let supportedExtensions: Set<String> = ["heic", "jpg", "jpeg", "png", "dng", "mov", "mp4"]
    /// 422 "sha_mismatch" is the only transfer error that is the file's problem; five in a row = give up.
    static let maxFailures = 5
    /// Answers that mean "this file will never be accepted". Everything else is the desktop's condition.
    static let permanentRejections: Set<Int> = [400, 411, 413, 415]
    static let maxOutcomes = 1000
    static let pauseBase: TimeInterval = 30
    static let pauseMax: TimeInterval = 1800

    private let directory: URL
    private var items: [Item] = []
    private var outcomeLog: [Outcome] = []
    private var progress = Progress()
    private var draining = false
    private var onProgress: (@Sendable (Progress) -> Void)?

    private var manifestURL: URL { directory.appendingPathComponent("manifest.json") }
    private var outcomesURL: URL { directory.appendingPathComponent("outcomes.json") }

    public init(directory: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        var loaded: [Item] = []
        if let data = try? Data(contentsOf: directory.appendingPathComponent("manifest.json")),
           let saved = try? JSONDecoder().decode([Item].self, from: data) {
            loaded = saved.filter { fm.fileExists(atPath: directory.appendingPathComponent($0.fileName).path) }
        }
        var loadedOutcomes: [Outcome] = []
        if let data = try? Data(contentsOf: directory.appendingPathComponent("outcomes.json")),
           let saved = try? JSONDecoder().decode([Outcome].self, from: data) {
            loadedOutcomes = saved
        }
        // files without a manifest entry: a crash between copy and save
        let known = Set(loaded.map(\.fileName)).union(["manifest.json", "outcomes.json"])
        for name in (try? fm.contentsOfDirectory(atPath: directory.path)) ?? [] where !known.contains(name) {
            try? fm.removeItem(at: directory.appendingPathComponent(name))
        }
        var start = Progress()
        start.pending = loaded.count
        start.bytesPending = loaded.reduce(0) { $0 + $1.size }
        // stored properties last: no actor-isolated access after self is fully initialized
        items = loaded
        outcomeLog = loadedOutcomes
        progress = start
        self.directory = directory
    }

    public func observe(_ callback: @escaping @Sendable (Progress) -> Void) {
        onProgress = callback
        callback(progress)
    }

    public var pending: [Item] { items }

    /// How every file that has left the outbox left it (see the type comment).
    public var outcomes: [Outcome] { outcomeLog }

    /// The owner has settled these outcomes into its own state; they are no longer needed.
    public func acknowledge(_ ids: [UUID]) {
        guard !ids.isEmpty else { return }
        let drop = Set(ids)
        outcomeLog.removeAll { drop.contains($0.id) }
        saveOutcomes()
    }

    /// "Retry now": clears every pause so the next drain tries immediately.
    public func retryNow() {
        for i in items.indices {
            items[i].pausedUntil = nil
            items[i].deferrals = nil
        }
        progress.paused = nil
        try? save()
        publish()
    }

    /// The user dismissed the "not sent" notice.
    public func clearRejected() {
        progress.rejected = []
        progress.rejectedReason = nil
        publish()
    }

    /// Takes ownership of `file` (moved, not copied: the caller staged it in tmp). Same bytes
    /// queued twice are kept once. `group` = the camera-roll asset the file belongs to.
    @discardableResult
    public func enqueue(moving file: URL, name: String, sha256 known: String? = nil,
                        group: String? = nil) throws -> Item? {
        let fm = FileManager.default
        let ext = (name as NSString).pathExtension.lowercased()
        guard Self.supportedExtensions.contains(ext) else {
            try? fm.removeItem(at: file)
            progress.rejected.append(name)
            progress.rejectedReason = "unsupported_type"
            publish()
            return nil
        }
        let digest = try known ?? Self.sha256(of: file)        // the camera-roll backup hashed it already
        if items.contains(where: { $0.sha256 == digest }) {
            try? fm.removeItem(at: file)
            return nil
        }
        let size = (try fm.attributesOfItem(atPath: file.path)[.size] as? NSNumber)?.int64Value ?? 0
        let item = Item(id: UUID(), name: name, sha256: digest, size: size, added: Date(), failures: 0,
                        lastError: nil, group: group, deferrals: nil, pausedUntil: nil)
        try fm.moveItem(at: file, to: directory.appendingPathComponent(item.fileName))
        items.append(item)
        try save()
        publish()
        return item
    }

    /// Uploads until the outbox is empty, the desktop is unreachable, or it asks us to wait. Safe to
    /// call often: a second call while draining is a no-op, and a paused queue returns at once until
    /// its delay has passed (or `retryNow()`).
    public func drain(using client: some ImportUploading) async {
        guard !draining else { return }
        draining = true
        defer {
            draining = false
            progress.uploading = nil
            publish()
        }
        while let item = items.first {
            if Task.isCancelled { return }
            if let until = item.pausedUntil, until > Date() {
                progress.paused = item.lastError ?? "paused"
                return
            }
            progress.uploading = item.name
            progress.paused = nil
            publish()
            let file = directory.appendingPathComponent(item.fileName)
            do {
                if try await client.importPresent(sha256: item.sha256) == false {
                    _ = try await client.uploadImport(file: file, name: item.name, sha256: item.sha256)
                }
                finish(item, ok: true, reason: nil)                  // the desktop verified (or already had) the bytes
            } catch SyncError.server(let status, let code) where Self.permanentRejections.contains(status) {
                finish(item, ok: false, reason: code.isEmpty ? "http \(status)" : code)   // will never succeed
            } catch SyncError.server(422, _) {
                // corrupted in flight: send again now, give up only after maxFailures in a row
                guard let i = items.firstIndex(where: { $0.id == item.id }) else { continue }
                items[i].failures += 1
                items[i].lastError = "sha_mismatch"
                if items[i].failures >= Self.maxFailures {
                    finish(items[i], ok: false, reason: "corrupted_in_transfer")
                } else {
                    try? save()
                }
            } catch SyncError.server(let status, let code) {
                // 507 disk full, 5xx, an old desktop's 404 ...: the desktop's condition, not the
                // file's. The item stays queued and the queue pauses; nothing is ever dropped here.
                pause(item, reason: code.isEmpty ? "http \(status)" : code, seconds: nil)
                return
            } catch SyncError.deferred(let retryAfter) {
                pause(item, reason: "power_saver", seconds: TimeInterval(max(retryAfter, 1)))
                return
            } catch {
                note(item, String(describing: error))            // offline / asleep: not the item's fault
                return                                           // wait for the next sync
            }
        }
    }

    /// Clears the outbox (unpair).
    public func removeAll() {
        for item in items { try? FileManager.default.removeItem(at: directory.appendingPathComponent(item.fileName)) }
        items.removeAll()
        outcomeLog.removeAll()
        try? save()
        saveOutcomes()
        progress = Progress()
        publish()
    }

    // ── internals ──

    /// The one exit from the outbox. The outcome is persisted first: if the app dies between the
    /// two writes, the item is still queued and simply asks the desktop again (idempotent by hash).
    private func finish(_ item: Item, ok: Bool, reason: String?) {
        outcomeLog.append(Outcome(id: UUID(), sha256: item.sha256, group: item.group, name: item.name,
                                  ok: ok, reason: reason, date: Date()))
        if outcomeLog.count > Self.maxOutcomes { outcomeLog.removeFirst(outcomeLog.count - Self.maxOutcomes) }
        saveOutcomes()
        try? FileManager.default.removeItem(at: directory.appendingPathComponent(item.fileName))
        items.removeAll { $0.id == item.id }
        try? save()
        if ok {
            progress.sent += 1
            progress.paused = nil
        } else {
            progress.rejected.append(item.name)
            progress.rejectedReason = reason
        }
        publish()
    }

    /// The desktop can't take uploads right now. The item keeps its place; the next attempt waits
    /// longer each time (30 s, 1 min, 2 min ... 30 min) unless the desktop gave a delay itself.
    private func pause(_ item: Item, reason: String, seconds: TimeInterval?) {
        guard let i = items.firstIndex(where: { $0.id == item.id }) else { return }
        let n = items[i].deferrals ?? 0
        let wait = seconds ?? min(Self.pauseBase * pow(2, Double(min(n, 6))), Self.pauseMax)
        items[i].deferrals = n + 1
        items[i].lastError = reason
        items[i].pausedUntil = Date().addingTimeInterval(wait)
        progress.paused = reason
        try? save()
    }

    /// Records a transport error without counting it: the item waits for the desktop.
    private func note(_ item: Item, _ reason: String) {
        guard let i = items.firstIndex(where: { $0.id == item.id }) else { return }
        items[i].lastError = reason
        try? save()
    }

    private func publish() {
        progress.pending = items.count
        progress.bytesPending = items.reduce(0) { $0 + $1.size }
        onProgress?(progress)
    }

    private func save() throws {
        try write(try JSONEncoder().encode(items), to: manifestURL)
    }

    private func saveOutcomes() {
        if let data = try? JSONEncoder().encode(outcomeLog) { try? write(data, to: outcomesURL) }
    }

    private func write(_ data: Data, to url: URL) throws {
        #if os(iOS)
        try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try data.write(to: url, options: .atomic)
        #endif
    }

    /// Streaming SHA-256 (videos can be gigabytes), lowercase hex as the desktop expects.
    public static func sha256(of file: URL) throws -> String {
        let fh = try FileHandle(forReadingFrom: file)
        defer { try? fh.close() }
        var hasher = SHA256()
        while let chunk = try fh.read(upToCount: 1 << 20), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
#endif
