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
    /// Three-state library: camera-roll counts for the Library banner.
    public private(set) var phoneCounts = LibraryStore.PhoneCounts()
    #if os(iOS)
    /// The camera roll, read in place (nil without a replica).
    public private(set) var phone: PhoneLibrary?
    public private(set) var phoneAccess: PhoneLibrary.Access = PhoneLibrary.access
    public private(set) var phoneIndex = PhoneLibrary.IndexProgress()
    private var backup: PhoneBackup?
    private var backupTask: Task<Void, Never>?
    #endif

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
        #if os(iOS)
        BackgroundSync.model = self
        if let store {
            let lib = PhoneLibrary(store: store)
            lib.onChange = { [weak self] p in
                Task { @MainActor in self?.phoneChanged(p) }
            }
            phone = lib
            images.phone = lib
            if let imports { backup = PhoneBackup(store: store, phone: lib, imports: imports) }
            lib.start()                                   // no-op until the user grants access
            phoneCounts = store.phoneCounts()
        }
        #endif
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
                        #if os(iOS)
                        self.runBackup()                   // link proven copies, send approved ones
                        #endif
                        self.revision += 1                 // and retry the thumbnails that failed offline
                    }
                }
            }, changes: { [weak self] applied in
                Task { @MainActor in
                    guard let self else { return }
                    self.images.invalidate(applied.editsChanged)
                    if applied.upserts + applied.deletes > 0 { self.rematchPhone() }
                    self.revision += 1
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
        phoneCounts = store?.phoneCounts() ?? LibraryStore.PhoneCounts()
        try? FileManager.default.removeItem(at: Self.journeyCache)
        let disk = images.disk
        Task.detached(priority: .utility) {                     // the cached files belong to that library
            disk.removeAll()
            try? FileManager.default.removeItem(at: ImageLoader.originalsDirectory)
        }
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

    /// Dismisses the "not sent" notice above the grid.
    public func dismissImportNotices() {
        let q = imports
        Task { await q?.clearRejected() }
    }

    /// Starts draining the outbox if paired; a no-op while a drain is already running.
    public func sendImports() {
        guard let imports, let client else { return }
        Task.detached(priority: .utility) { await imports.drain(using: client) }
    }

    // ── three-state library (Parity sprint) ──

    /// New desktop items may be camera-roll assets the phone already shows as phone-only.
    func rematchPhone() {
        guard let store else { return }
        if (try? store.rematchPhone()) ?? 0 > 0 { revision += 1 }
        phoneCounts = store.phoneCounts()
    }

    #if os(iOS)
    private func phoneChanged(_ p: PhoneLibrary.IndexProgress) {
        phoneIndex = p
        phoneCounts = store?.phoneCounts() ?? LibraryStore.PhoneCounts()
        revision += 1
        if !p.indexing, PhoneBackup.automatic { runBackup() }
    }

    /// Asks for Photos access (the Library banner) and indexes the camera roll.
    public func enablePhoneLibrary() async {
        phoneAccess = await PhoneLibrary.requestAccess()
        phone?.start()
    }

    /// Back up automatically (background, on power) instead of waiting for the banner.
    public var automaticBackup: Bool {
        get { _ = revision; return PhoneBackup.automatic }
        set {
            PhoneBackup.automatic = newValue
            revision += 1
            if newValue { backUpAll() }
        }
    }

    /// The banner's "Back up": every phone-only asset is approved and the pass starts.
    public func backUpAll() {
        _ = try? store?.approveAllPhoneOnly()
        phoneCounts = store?.phoneCounts() ?? phoneCounts
        runBackup()
    }

    /// The banner's "Retry" after the desktop refused some uploads: they are asked for again, and a
    /// paused queue (desktop full / busy) is released at once.
    public func retryFailedBackups() {
        _ = try? store?.retryFailedBackups()
        phoneCounts = store?.phoneCounts() ?? phoneCounts
        retryBackupNow()
    }

    /// "Retry now" on a paused queue.
    public func retryBackupNow() {
        let q = imports
        Task {
            await q?.retryNow()
            runBackup()
        }
    }

    /// One backup pass while the desktop is reachable (a running pass absorbs the call).
    public func runBackup(deep: Bool = false) {
        guard backupTask == nil, let backup, let client else { return }
        backupTask = Task { [weak self] in
            await backup.run(client: client, deep: deep)
            guard let self else { return }
            self.backupTask = nil
            self.phoneCounts = self.store?.phoneCounts() ?? self.phoneCounts
            self.revision += 1
        }
    }

    /// BGAppRefreshTask / BGProcessingTask: index the roll, pull, and (deep) back up.
    func backgroundPass(deep: Bool, budget: TimeInterval) async {
        await phone?.refreshNow()
        rematchPhone()
        if let engine { _ = try? await engine.pull() }
        if let backup, let client, deep || PhoneBackup.automatic {
            await backup.run(client: client, deep: deep, budget: budget)
        }
        phoneCounts = store?.phoneCounts() ?? phoneCounts
        revision += 1
    }
    #endif

    // ── semantic search + Trip mode ──

    /// The desktop's semantic search; nil when unpaired, offline, or on an older desktop.
    public func semanticSearch(_ query: String) async -> SearchAnswer? {
        guard let client else { return nil }
        if case .offline = status { return nil }
        return try? await client.search(query)
    }

    nonisolated static var journeyCache: URL {
        let base = (try? FileManager.default.url(for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil,
                                                 create: true)) ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("Kairos/journeys", isDirectory: true)
    }

    /// Trip mode data: the desktop's journey (cached for offline), else the last cached copy,
    /// else one rebuilt from the replica's photo positions.
    public func journey(for event: Event) async -> Journey? {
        let file = Self.journeyCache.appendingPathComponent("\(event.id).json")
        if let client, let data = try? await client.journeyData(event.id),
           let j = try? JSONDecoder().decode(Journey.self, from: data) {
            try? FileManager.default.createDirectory(at: Self.journeyCache, withIntermediateDirectories: true)
            try? data.write(to: file, options: .atomic)
            return j
        }
        if let data = try? Data(contentsOf: file), let j = try? JSONDecoder().decode(Journey.self, from: data) {
            return j
        }
        guard let photos = store?.journeyPhotos(event: event.id), !photos.isEmpty else { return nil }
        return JourneyLocal.journey(id: event.id, title: event.title ?? "", location: event.locationName ?? "",
                                    photos: photos)
    }

    /// Phone edits not yet acknowledged by the desktop.
    public var pendingEdits: Int { _ = revision; return store?.pendingEditCount ?? 0 }

    public func toggleFavorite(_ id: MediaID) async {
        guard let item = store?.media(id) else { return }
        if let engine { try? await engine.setFavorite(id, !item.isFavorite) }
        revision += 1
    }
}
