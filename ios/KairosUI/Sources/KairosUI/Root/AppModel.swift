import CoreGraphics
import Foundation
import KairosCore
import Observation
import SwiftUI

/// App-wide state: the local replica, the paired desktop and the sync engine. The views read the
/// replica directly. `revision` bumps after every applied change page so lists reload.
@MainActor
@Observable
public final class AppModel {
    public private(set) var store: LibraryStore?
    public private(set) var endpoint: DesktopEndpoint?
    public private(set) var client: SyncClient?
    public private(set) var status: SyncEngine.Status = .idle
    public private(set) var revision = 0
    public private(set) var storeError: String?
    public let images = ImageLoader()
    /// Camera-roll uploads waiting for (or on their way to) the desktop.
    public private(set) var importProgress = ImportQueue.Progress()
    /// Face ID / passcode session for the Hidden folder.
    public let vault: VaultAuth
    private var imports: ImportQueue?
    private var engine: SyncEngine?

    private static let endpointKey = "kairos.desktop.endpoint"
    private static var deviceId: String {
        if let id = UserDefaults.standard.string(forKey: "kairos.device.id") { return id }
        let id = UUID().uuidString
        UserDefaults.standard.set(id, forKey: "kairos.device.id")
        return id
    }

    public init() {
        vault = VaultAuth()
        do {
            let dir = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                  appropriateFor: nil, create: true)
            store = try LibraryStore.open(at: dir.appendingPathComponent("Kairos/library.sqlite"))
        } catch {
            storeError = String(describing: error)
        }
        images.store = store
        if let dir = try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                  appropriateFor: nil, create: true),
           let q = try? ImportQueue(directory: dir.appendingPathComponent("Kairos/Outbox")) {
            imports = q
            Task {
                await q.observe { [weak self] p in
                    Task { @MainActor in self?.importProgress = p }
                }
            }
        }
        if let data = UserDefaults.standard.data(forKey: Self.endpointKey),
           let ep = try? JSONDecoder().decode(DesktopEndpoint.self, from: data),
           let token = Keychain.load(account: ep.deviceId) {
            connect(ep, token: token)
        } else {
            status = .needsPairing
        }
    }

    public var isPaired: Bool { endpoint != nil }

    private func connect(_ ep: DesktopEndpoint, token: String) {
        guard let store else { return }
        endpoint = ep
        let c = SyncClient(endpoint: ep, token: token)
        client = c
        images.client = c
        let e = SyncEngine(store: store, client: c)
        engine = e
        Task {
            await e.observe(status: { [weak self] s in
                Task { @MainActor in
                    guard let self else { return }
                    let wasLive = self.status == .live
                    self.status = s
                    if s == .live, !wasLive {
                        self.sendImports()                 // desktop reachable: flush the outbox
                        self.revision += 1                 // and retry the thumbnails that failed offline
                    }
                }
            }, changes: { [weak self] applied in
                Task { @MainActor in
                    self?.images.invalidate(applied.editsChanged)
                    self?.revision += 1
                }
            })
            await e.start()
        }
    }

    /// Pairs with the desktop from its QR payload (or the URI printed by `python sync_server.py`).
    public func pair(uri: String, deviceName: String) async throws {
        guard let offer = PairingOffer(uri: uri) else { throw SyncError.badResponse }
        var ep = DesktopEndpoint(host: offer.host, port: offer.port, fingerprint: offer.fingerprint,
                                 libraryId: offer.libraryId, name: offer.host, deviceId: Self.deviceId)
        let answer = try await SyncClient(endpoint: ep, token: nil)
            .pair(pin: offer.pin, deviceName: deviceName, deviceId: ep.deviceId)
        ep.name = answer.name
        ep.libraryId = answer.libraryId
        try Keychain.save(answer.token, account: ep.deviceId)
        UserDefaults.standard.set(try JSONEncoder().encode(ep), forKey: Self.endpointKey)
        connect(ep, token: answer.token)
    }

    public func unpair() async {
        if let engine { await engine.stop() }
        try? await client?.unpair()
        if let ep = endpoint { Keychain.delete(account: ep.deviceId) }
        await imports?.removeAll()
        vault.lock()
        UserDefaults.standard.removeObject(forKey: Self.endpointKey)
        try? store?.reset()
        engine = nil
        client = nil
        endpoint = nil
        images.client = nil
        let disk = images.disk
        Task.detached(priority: .utility) { disk.removeAll() }   // the cached thumbnails belong to that library
        status = .needsPairing
        revision += 1
    }

    /// Saves an edit: the replica row is written (dirty = queued for the desktop) before this
    /// returns, so every grid and the viewer show it at once. The upload runs detached in the
    /// engine, and offline it waits for the next sync. `rendered` (the editor's final preview)
    /// re-bakes the grid's LQIP immediately.
    public func saveEdit(_ id: MediaID, _ adj: Adjustments, rendered: CGImage? = nil) async {
        images.invalidate([id])
        if let engine {
            try? await engine.saveEdit(id, adj)
        } else {
            try? store?.saveLocalEdit(id, adj)
        }
        if let rendered { images.rebakeMicro(id, from: rendered) }
        revision += 1
    }

    /// Queues picked camera-roll files (already staged in tmp) and starts sending them. Offline,
    /// they wait in the outbox until the desktop is reachable again.
    public func importPicked(_ files: [(file: URL, name: String)]) async {
        guard let imports else { return }
        for f in files {
            do {
                try await imports.enqueue(moving: f.file, name: f.name)
            } catch {
                try? FileManager.default.removeItem(at: f.file)
            }
        }
        sendImports()
    }

    /// Starts draining the outbox if paired; a no-op while a drain is already running.
    public func sendImports() {
        guard let imports, let client else { return }
        Task.detached(priority: .utility) { await imports.drain(using: client) }
    }

    /// Phone edits not yet acknowledged by the desktop.
    public var pendingEdits: Int { _ = revision; return store?.pendingEditCount ?? 0 }

    public func toggleFavorite(_ id: MediaID) async {
        guard let item = store?.media(id) else { return }
        if let engine { try? await engine.setFavorite(id, !item.isFavorite) }
        revision += 1
    }
}
