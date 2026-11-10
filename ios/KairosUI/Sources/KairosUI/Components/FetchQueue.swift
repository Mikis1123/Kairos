import Foundation
import KairosCore

/// Concurrency gate for image fetches from the desktop. Waiters are served newest first (LIFO):
/// while the user scrolls, the cells now on screen asked last, so they load before the ones that
/// scrolled past. A waiter whose task is cancelled (its cell left the screen) leaves the queue
/// without ever taking a slot.
actor FetchGate {
    private let limit: Int
    private var running = 0
    private var waiters: [(id: UInt64, cont: CheckedContinuation<Void, Error>)] = []
    private var nextId: UInt64 = 0

    init(limit: Int) { self.limit = limit }

    func acquire() async throws {
        if Task.isCancelled { throw CancellationError() }
        if running < limit {
            running += 1
            return
        }
        let id = nextId
        nextId += 1
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                if Task.isCancelled {
                    cont.resume(throwing: CancellationError())
                } else {
                    waiters.append((id, cont))
                }
            }
        } onCancel: {
            Task { await self.cancelWaiter(id) }
        }
    }

    /// Hands the slot straight to the newest waiter, or frees it.
    func release() {
        if let w = waiters.popLast() {
            w.cont.resume()
        } else {
            running = max(0, running - 1)
        }
    }

    private func cancelWaiter(_ id: UInt64) {
        guard let i = waiters.firstIndex(where: { $0.id == id }) else { return }
        waiters.remove(at: i).cont.resume(throwing: CancellationError())
    }
}

/// One fetch with retries. Since the Parity sprint the desktop queues a burst instead of refusing
/// it, so 503 "busy" (Retry-After: 1) means a server stuck for 20 s, or an older desktop with the
/// 8-request cap; a dropped Wi-Fi packet surfaces as a timeout. Both are transient, so
/// they are retried with jittered exponential backoff. 404 and 401 are final.
enum FetchRetry {
    static let attempts = 6

    static func run(gate: FetchGate, _ op: @Sendable () async throws -> Data) async -> Data? {
        do { try await gate.acquire() } catch { return nil }
        // The slot is held through the backoff on purpose: the desktop is saturated, and handing
        // the slot to the next request would only earn it another 503.
        defer { Task { await gate.release() } }
        for attempt in 0..<attempts {
            if Task.isCancelled { return nil }
            let wait: Double
            do {
                return try await op()
            } catch SyncError.deferred(let retryAfter) {
                wait = min(Double(max(retryAfter, 1)), 10) * (1 + 0.5 * Double(attempt))
            } catch SyncError.server(let code, _) where (500...599).contains(code) {
                wait = backoff(attempt)
            } catch let e as URLError where e.code != .cancelled && e.code != .userAuthenticationRequired {
                wait = backoff(attempt)
            } catch {
                return nil                                  // 404, 401, cancelled, undecodable
            }
            let jittered = wait * Double.random(in: 0.75...1.25)
            do { try await Task.sleep(nanoseconds: UInt64(jittered * 1_000_000_000)) } catch { return nil }
        }
        return nil
    }

    static func backoff(_ attempt: Int) -> Double { min(0.5 * pow(2, Double(attempt)), 8) }
}

/// Persistent byte cache in Application Support (not Caches: iOS empties Caches under storage
/// pressure, and these files are what keeps the library visible with the desktop switched off).
/// Excluded from iCloud backup. Least recently used files go first once the budget is exceeded.
final class DiskCache: @unchecked Sendable {
    let directory: URL
    private let lock = NSLock()
    private var budget: Int64
    private var total: Int64 = -1                           // unknown until the first scan
    private var pruning = false
    private let queue = DispatchQueue(label: "app.kairos.diskcache", qos: .utility)

    init(directory: URL, budget: Int64) {
        self.directory = directory
        self.budget = budget
        let fm = FileManager.default
        try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
        var dir = directory
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? dir.setResourceValues(values)
    }

    func setBudget(_ bytes: Int64) {
        lock.lock(); budget = bytes; lock.unlock()
        schedulePrune()
    }

    private func url(_ key: String) -> URL {
        let safe = key.map { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" || $0 == "." ? $0 : "_" }
        return directory.appendingPathComponent(String(safe))
    }

    func contains(_ key: String) -> Bool { FileManager.default.fileExists(atPath: url(key).path) }

    func read(_ key: String) -> Data? {
        let u = url(key)
        guard let data = try? Data(contentsOf: u, options: .mappedIfSafe), !data.isEmpty else { return nil }
        queue.async { try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: u.path) }
        return data
    }

    func write(_ data: Data, key: String) {
        let u = url(key)
        do { try data.write(to: u, options: .atomic) } catch { return }
        lock.lock()
        if total >= 0 { total += Int64(data.count) }
        let over = total < 0 || total > budget
        lock.unlock()
        if over { schedulePrune() }
    }

    func removeAll() {
        let fm = FileManager.default
        try? fm.removeItem(at: directory)
        try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
        lock.lock(); total = 0; lock.unlock()
    }

    private func schedulePrune() {
        lock.lock()
        if pruning { lock.unlock(); return }
        pruning = true
        lock.unlock()
        queue.async { [self] in
            prune()
            lock.lock(); pruning = false; lock.unlock()
        }
    }

    /// Scans the folder once, then deletes the least recently read files down to 90 % of the budget.
    private func prune() {
        let fm = FileManager.default
        let keys: Set<URLResourceKey> = [.contentModificationDateKey, .fileAllocatedSizeKey, .fileSizeKey]
        guard let files = try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: Array(keys)) else { return }
        var entries: [(url: URL, date: Date, size: Int64)] = []
        entries.reserveCapacity(files.count)
        var sum: Int64 = 0
        for f in files {
            let v = try? f.resourceValues(forKeys: keys)
            let size = Int64(v?.fileAllocatedSize ?? v?.fileSize ?? 0)
            sum += size
            entries.append((f, v?.contentModificationDate ?? .distantPast, size))
        }
        lock.lock(); let limit = budget; lock.unlock()
        if sum > limit {
            let target = limit / 10 * 9
            for e in entries.sorted(by: { $0.date < $1.date }) where sum > target {
                if (try? fm.removeItem(at: e.url)) != nil { sum -= e.size }
            }
        }
        lock.lock(); total = sum; lock.unlock()
    }
}
