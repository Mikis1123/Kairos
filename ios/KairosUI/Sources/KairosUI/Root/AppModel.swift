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
    private var engine: SyncEngine?

    private static let endpointKey = "kairos.desktop.endpoint"
    private static var deviceId: String {
        if let id = UserDefaults.standard.string(forKey: "kairos.device.id") { return id }
        let id = UUID().uuidString
        UserDefaults.standard.set(id, forKey: "kairos.device.id")
        return id
    }

    public init() {
        do {
            let dir = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                  appropriateFor: nil, create: true)
            store = try LibraryStore.open(at: dir.appendingPathComponent("Kairos/library.sqlite"))
        } catch {
            storeError = String(describing: error)
        }
        images.store = store
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
                Task { @MainActor in self?.status = s }
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
        UserDefaults.standard.removeObject(forKey: Self.endpointKey)
        try? store?.reset()
        engine = nil
        client = nil
        endpoint = nil
        images.client = nil
        status = .needsPairing
        revision += 1
    }

    public func saveEdit(_ id: MediaID, _ adj: Adjustments) async {
        images.invalidate([id])
        if let engine {
            await engine.saveEdit(id, adj)
        } else {
            try? store?.saveLocalEdit(id, adj)
        }
        revision += 1
    }

    public func toggleFavorite(_ id: MediaID) async {
        guard let item = store?.media(id) else { return }
        if let engine { try? await engine.setFavorite(id, !item.isFavorite) }
        revision += 1
    }
}
