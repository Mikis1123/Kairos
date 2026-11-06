#if canImport(SQLite3)
import Foundation

/// Keeps the replica current: pull the journal page by page, fetch LQIPs for new photos,
/// upload phone-side edits, then wait on the WebSocket feed and repeat. One actor, so pulls never
/// overlap. Each page is applied in one transaction together with its cursor, so a killed app
/// resumes exactly where it stopped.
public actor SyncEngine {
    public enum Status: Equatable, Sendable {
        case idle
        case syncing(done: Int)
        case live                       // caught up, listening for changes
        case offline(String)
        case needsPairing
    }

    public let store: LibraryStore
    public let client: SyncClient
    public private(set) var status: Status = .idle
    private var onStatus: (@Sendable (Status) -> Void)?
    private var onChange: (@Sendable (LibraryStore.Applied) -> Void)?
    private var runTask: Task<Void, Never>?

    public init(store: LibraryStore, client: SyncClient) {
        self.store = store
        self.client = client
    }

    /// Callbacks arrive off the main actor; the UI hops to it.
    public func observe(status: @escaping @Sendable (Status) -> Void,
                        changes: @escaping @Sendable (LibraryStore.Applied) -> Void) {
        onStatus = status
        onChange = changes
    }

    private func set(_ s: Status) {
        status = s
        onStatus?(s)
    }

    /// Pulls until caught up. Returns the number of changes applied.
    @discardableResult
    public func pull() async throws -> Int {
        var total = 0
        var since = store.cursor
        if let known = store.libraryId, known != client.endpoint.libraryId, !client.endpoint.libraryId.isEmpty {
            try store.reset()                        // paired with a different desktop library
            since = 0
        }
        while true {
            let page = try await client.changes(since: since)
            if page.reset {
                try store.apply(page)                // drops the replica, cursor 0
                since = 0
                continue
            }
            let applied = try store.apply(page)
            total += page.changes.count
            since = page.cursor
            set(.syncing(done: total))
            if !applied.mediaNeedingMicro.isEmpty { try await fetchMicro(applied.mediaNeedingMicro) }
            if applied.upserts + applied.deletes > 0 { onChange?(applied) }
            if !page.more { break }
        }
        return total
    }

    private func fetchMicro(_ ids: [MediaID]) async throws {
        var i = 0
        while i < ids.count {
            let part = Array(ids[i..<min(ids.count, i + WireProtocol.microBatch)])
            try store.putMicro(try await client.micro(part))
            i += WireProtocol.microBatch
        }
    }

    /// Uploads phone-side edits; the desktop's canonical answer replaces the local copy.
    public func pushEdits() async throws {
        for (id, adj) in store.dirtyEdits() {
            do {
                let canonical = try await client.putEdit(id, adj)
                try store.editUploaded(id, canonical: canonical)
            } catch SyncError.server(404, _) {
                try store.editUploaded(id, canonical: nil)   // the photo is gone or now hidden on the desktop
            }
        }
    }

    public func saveEdit(_ id: MediaID, _ adj: Adjustments) async {
        try? store.saveLocalEdit(id, adj)
        try? await pushEdits()                         // offline: stays dirty, retried next cycle
    }

    public func setFavorite(_ id: MediaID, _ value: Bool) async throws {
        try store.setFavoriteLocally(id, value)
        try await client.setFavorite(id, value)
    }

    /// The sync loop: pull, push, listen, repeat. Reconnects with capped exponential backoff.
    public func start() {
        guard runTask == nil else { return }
        runTask = Task { [weak self] in
            var backoff: UInt64 = 1
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    try await self.pull()
                    try await self.pushEdits()
                    await self.set(.live)
                    backoff = 1
                    for try await cursor in self.client.changeFeed() {
                        if cursor != self.store.cursor {
                            try await self.pull()
                            await self.set(.live)
                        }
                    }
                } catch SyncError.unauthorized {
                    await self.set(.needsPairing)
                    return
                } catch {
                    await self.set(.offline(String(describing: error)))
                }
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 60)
            }
        }
    }

    public func stop() {
        runTask?.cancel()
        runTask = nil
        set(.idle)
    }
}
#endif
