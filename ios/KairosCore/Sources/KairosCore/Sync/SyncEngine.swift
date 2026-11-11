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
    private var pushing = false
    private var pushAgain = false
    private var pushingFavorites = false
    private var pushFavoritesAgain = false
    /// Finds the desktop's current address after a connection failure (Bonjour in the app).
    /// nil = no discovery; the QR address is all there is.
    private var locate: (@Sendable (DesktopEndpoint) async -> (host: String, port: Int)?)?
    private var onRelocated: (@Sendable (DesktopEndpoint) -> Void)?
    private var nap: Task<Void, Never>?

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

    /// Re-discovery (H10): after a connection failure the engine asks `locate` where the desktop
    /// is now; a different address is applied to the client and reported through `relocated`
    /// (the app saves it), and the loop retries at once instead of waiting out the backoff.
    public func observeLocation(locate: @escaping @Sendable (DesktopEndpoint) async -> (host: String, port: Int)?,
                                relocated: @escaping @Sendable (DesktopEndpoint) -> Void) {
        self.locate = locate
        onRelocated = relocated
    }

    /// The network changed (Wi-Fi joined, a new path): do not wait out the retry backoff.
    public func nudge() {
        nap?.cancel()
    }

    /// Looks for the desktop when the failure says "cannot reach it" (not a refusal from a
    /// reachable server). True when the address changed.
    private func relocateAfter(_ error: Error) async -> Bool {
        guard let locate, Self.isConnectivity(error) else { return false }
        let ep = client.endpoint
        guard let found = await locate(ep), !Task.isCancelled,
              let moved = DesktopDiscovery.moved(ep, to: found.host, port: found.port) else { return false }
        client.relocate(host: moved.host, port: moved.port)
        onRelocated?(client.endpoint)
        return true
    }

    /// URLSession / socket failures: the desktop is unreachable at the stored address (or another
    /// machine answers there and fails the certificate pin).
    nonisolated static func isConnectivity(_ error: Error) -> Bool {
        if error is URLError { return true }
        let ns = error as NSError
        return ns.domain == NSPOSIXErrorDomain || ns.domain == NSURLErrorDomain
    }

    private func sleep(_ seconds: UInt64) async {
        let t = Task { try? await Task.sleep(nanoseconds: seconds * 1_000_000_000) }
        nap = t
        await t.value
        nap = nil
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

    /// Uploads phone-side edits. The desktop's canonical answer replaces the local copy unless a
    /// newer local edit was saved meanwhile (LibraryStore.editUploaded compare-and-swap).
    public func pushEdits() async throws {
        guard !pushing else {
            pushAgain = true                           // a save during an upload: one more round
            return
        }
        pushing = true
        defer { pushing = false }
        repeat {
            pushAgain = false
            for e in store.dirtyEdits() {
                do {
                    let canonical = try await client.putEdit(e.id, e.adjustments)
                    try store.editUploaded(e.id, canonical: canonical, uploaded: e.stored)
                } catch SyncError.server(404, _) {
                    // the photo is gone or now hidden on the desktop: drop the queued edit
                    try store.editUploaded(e.id, canonical: nil, uploaded: e.stored)
                }
            }
        } while pushAgain
    }

    /// Saves a phone-side edit: the replica row is written (dirty) before this returns, so the
    /// grid and viewer show it at once, offline or not. The upload runs detached. Offline, the
    /// edit stays queued and the sync loop uploads it after the next pull.
    public func saveEdit(_ id: MediaID, _ adj: Adjustments) throws {
        try store.saveLocalEdit(id, adj)
        Task { try? await self.pushEdits() }
    }

    /// Saves a favourite toggle: the replica row is written (flagged dirty) before this returns,
    /// so the heart flips at once, online or not. The upload runs detached; offline, the flag
    /// stays and the sync loop delivers it after the next pull (compare-and-swap, like edits).
    public func setFavorite(_ id: MediaID, _ value: Bool) throws {
        try store.setFavoriteLocally(id, value)
        Task { try? await self.pushFavorites() }
    }

    /// Delivers queued favourites. The desktop's answer clears the flag only while the row still
    /// holds the value that was sent (LibraryStore.favoriteUploaded); a toggle made meanwhile goes
    /// up in the next round.
    public func pushFavorites() async throws {
        guard !pushingFavorites else {
            pushFavoritesAgain = true
            return
        }
        pushingFavorites = true
        defer { pushingFavorites = false }
        repeat {
            pushFavoritesAgain = false
            for f in store.dirtyFavorites() {
                do {
                    try await client.setFavorite(f.id, f.value)
                    try store.favoriteUploaded(f.id, uploaded: f.value)
                } catch SyncError.server(404, _) {
                    try store.favoriteUploaded(f.id, uploaded: f.value, gone: true)   // deleted or hidden on the desktop
                }
            }
        } while pushFavoritesAgain
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
                    try await self.pushFavorites()
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
                    if await self.relocateAfter(error) {
                        backoff = 1                    // the desktop moved: reconnect now
                        continue
                    }
                }
                await self.sleep(backoff)
                backoff = min(backoff * 2, 60)
            }
        }
    }

    public func stop() {
        runTask?.cancel()
        runTask = nil
        nap?.cancel()
        locate = nil
        onRelocated = nil
        status = .idle
        onStatus = nil                          // a replaced engine must not write a stale status
        onChange = nil
    }
}
#endif
