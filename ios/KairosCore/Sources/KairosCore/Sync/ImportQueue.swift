import Foundation
#if canImport(CryptoKit)
import CryptoKit

/// Camera-roll photos on their way to the desktop (POST /v1/import). Picked files are copied into
/// an outbox in Application Support with a JSON manifest, so a batch survives the app being killed,
/// the phone leaving Wi-Fi or the desktop sleeping. One upload at a time (the bridge has 8 request
/// slots that browsing also needs). An item leaves the outbox only after the desktop has verified
/// its SHA-256, or answered that it already has those bytes.
public actor ImportQueue {
    public struct Item: Codable, Identifiable, Sendable, Equatable {
        public let id: UUID
        public let name: String            // e.g. IMG_0042.HEIC, as the phone named it
        public let sha256: String
        public let size: Int64
        public let added: Date
        public var failures: Int
        public var lastError: String?

        var fileName: String {
            let ext = (name as NSString).pathExtension
            return ext.isEmpty ? id.uuidString : "\(id.uuidString).\(ext.lowercased())"
        }
    }

    public struct Progress: Sendable, Equatable {
        public var pending: Int = 0
        public var bytesPending: Int64 = 0
        public var uploading: String?
        public var sent: Int = 0              // since launch
        public var rejected: [String] = []    // names the desktop refused for good
        public init() {}
    }

    /// Formats the desktop scanner indexes (sync_server.IMPORT_EXTS). Others are refused at pick time.
    public static let supportedExtensions: Set<String> = ["heic", "jpg", "jpeg", "png", "dng", "mov", "mp4"]
    static let maxFailures = 5

    private let directory: URL
    private var items: [Item] = []
    private var progress = Progress()
    private var draining = false
    private var onProgress: (@Sendable (Progress) -> Void)?

    private var manifestURL: URL { directory.appendingPathComponent("manifest.json") }

    public init(directory: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        var loaded: [Item] = []
        if let data = try? Data(contentsOf: directory.appendingPathComponent("manifest.json")),
           let saved = try? JSONDecoder().decode([Item].self, from: data) {
            loaded = saved.filter { fm.fileExists(atPath: directory.appendingPathComponent($0.fileName).path) }
        }
        // files without a manifest entry: a crash between copy and save
        let known = Set(loaded.map(\.fileName)).union(["manifest.json"])
        for name in (try? fm.contentsOfDirectory(atPath: directory.path)) ?? [] where !known.contains(name) {
            try? fm.removeItem(at: directory.appendingPathComponent(name))
        }
        var start = Progress()
        start.pending = loaded.count
        start.bytesPending = loaded.reduce(0) { $0 + $1.size }
        // stored properties last: no actor-isolated access after self is fully initialized
        items = loaded
        progress = start
        self.directory = directory
    }

    public func observe(_ callback: @escaping @Sendable (Progress) -> Void) {
        onProgress = callback
        callback(progress)
    }

    public var pending: [Item] { items }

    /// Takes ownership of `file` (moved, not copied: the caller staged it in tmp). Same bytes
    /// queued twice are kept once.
    @discardableResult
    public func enqueue(moving file: URL, name: String) throws -> Item? {
        let fm = FileManager.default
        let ext = (name as NSString).pathExtension.lowercased()
        guard Self.supportedExtensions.contains(ext) else {
            try? fm.removeItem(at: file)
            progress.rejected.append(name)
            publish()
            return nil
        }
        let digest = try Self.sha256(of: file)
        if items.contains(where: { $0.sha256 == digest }) {
            try? fm.removeItem(at: file)
            return nil
        }
        let size = (try fm.attributesOfItem(atPath: file.path)[.size] as? NSNumber)?.int64Value ?? 0
        let item = Item(id: UUID(), name: name, sha256: digest, size: size, added: Date(), failures: 0, lastError: nil)
        try fm.moveItem(at: file, to: directory.appendingPathComponent(item.fileName))
        items.append(item)
        try save()
        publish()
        return item
    }

    /// Uploads until the outbox is empty or the desktop is unreachable. Safe to call often: a
    /// second call while draining is a no-op. Network errors leave the item queued for the next
    /// call (the app calls this whenever sync goes live).
    public func drain(using client: SyncClient) async {
        guard !draining else { return }
        draining = true
        defer {
            draining = false
            progress.uploading = nil
            publish()
        }
        while let item = items.first {
            if Task.isCancelled { return }
            progress.uploading = item.name
            publish()
            let file = directory.appendingPathComponent(item.fileName)
            do {
                if try await client.importPresent(sha256: item.sha256) == false {
                    _ = try await client.uploadImport(file: file, name: item.name, sha256: item.sha256)
                }
                finish(item, sent: true)
            } catch SyncError.server(let status, let code) where [400, 411, 413, 415].contains(status) {
                finish(item, sent: false, reason: code)          // will never succeed: drop it
            } catch SyncError.server(let status, let code) {
                retryLater(item, code.isEmpty ? "http \(status)" : code)
                if status != 422 { return }                      // 422 = corrupted in flight: send again now
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
        try? save()
        progress = Progress()
        publish()
    }

    // ── internals ──

    private func finish(_ item: Item, sent: Bool, reason: String? = nil) {
        try? FileManager.default.removeItem(at: directory.appendingPathComponent(item.fileName))
        items.removeAll { $0.id == item.id }
        try? save()
        if sent { progress.sent += 1 } else { progress.rejected.append(item.name) }
        publish()
    }

    /// Counts a failure the desktop reported. After maxFailures the item is dropped as rejected.
    private func retryLater(_ item: Item, _ reason: String) {
        guard let i = items.firstIndex(where: { $0.id == item.id }) else { return }
        items[i].failures += 1
        items[i].lastError = reason
        if items[i].failures >= Self.maxFailures {
            finish(items[i], sent: false, reason: reason)
        } else {
            try? save()
        }
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
        let data = try JSONEncoder().encode(items)
        #if os(iOS)
        try data.write(to: manifestURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try data.write(to: manifestURL, options: .atomic)
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
