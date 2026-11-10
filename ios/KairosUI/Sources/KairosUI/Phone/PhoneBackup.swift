#if os(iOS)
import Foundation
import KairosCore

/// Camera roll -> desktop backup (Parity sprint). Semi-automatic by default, as decided for
/// Phase 4: the Library banner offers "Back up", and only approved assets go. With automatic
/// backup on, every phone-only asset is approved as it appears and the background task sends
/// them while the phone charges on the home Wi-Fi.
///
/// One pass:
///   1. prove by content: unmatched assets with a known SHA-256 are offered to /v1/match, and
///      the ones the desktop already holds become `both` without uploading a byte
///   2. approved assets are exported from PhotoKit into the outbox (original + Live motion),
///      hashed, checked once more against /v1/match, and queued
///   3. the outbox drains (ImportQueue: resumable, idempotent, the desktop verifies SHA-256)
///   4. assets whose upload landed are `sent`; when the desktop's scanner has indexed them, the
///      next pass links them by hash (step 1) and they turn into `both`
/// `deep` (background processing, on power): also hash a few unmatched assets the matcher
/// could not place (renamed, re-dated) and ask the desktop whether it has those bytes.
actor PhoneBackup {
    struct Report: Sendable, Equatable {
        var linked = 0
        var queued = 0
        var skippedDuplicates = 0
        var failed = 0
    }

    let store: LibraryStore
    let phone: PhoneLibrary
    let imports: ImportQueue
    private var running = false

    static let staging = FileManager.default.temporaryDirectory.appendingPathComponent("kairos-backup", isDirectory: true)
    static let autoKey = "kairos.backup.automatic"

    static var automatic: Bool {
        get { UserDefaults.standard.bool(forKey: autoKey) }
        set { UserDefaults.standard.set(newValue, forKey: autoKey) }
    }

    init(store: LibraryStore, phone: PhoneLibrary, imports: ImportQueue) {
        self.store = store
        self.phone = phone
        self.imports = imports
    }

    @discardableResult
    func run(client: SyncClient, deep: Bool = false, budget: TimeInterval = 25) async -> Report {
        guard !running else { return Report() }
        running = true
        defer { running = false }
        let started = Date()
        var report = Report()
        if Self.automatic { _ = try? store.approveAllPhoneOnly() }

        // 1) proof by content for everything already hashed
        report.linked += await link(store.unmatchedWithSha(), client: client)

        // deep: hash a few unplaced assets and ask
        if deep {
            var offers: [MatchOffer] = []
            for a in store.unmatchedWithoutSha(limit: 40) {
                if Task.isCancelled || Date().timeIntervalSince(started) > budget / 2 { break }
                guard let sha = await phone.sha256(a.localId) else { continue }
                try? store.setPhoneSha(a.localId, sha)
                offers.append(MatchOffer(sha256: sha, size: a.fileSize ?? 0, name: a.fileName ?? ""))
            }
            if let m = try? await client.match(offers) { report.linked += (try? store.applyShaMatches(m)) ?? 0 }
        }

        // 2) export approved assets into the outbox
        for a in store.backupCandidates(limit: 100) {
            if Task.isCancelled || Date().timeIntervalSince(started) > budget { break }
            do {
                let files = try await phone.exportOriginals(a.localId, to: Self.staging)
                var queuedAny = false
                var primarySha: String?
                for f in files {
                    let sha = try ImportQueue.sha256(of: f.file)
                    if f.primary {
                        primarySha = sha
                        try? store.setPhoneSha(a.localId, sha)
                        let size = (try? FileManager.default.attributesOfItem(atPath: f.file.path)[.size] as? NSNumber)?
                            .int64Value ?? 0
                        if let m = try? await client.match([MatchOffer(sha256: sha, size: size, name: f.name)]),
                           !m.isEmpty, (try? store.applyShaMatches(m)) ?? 0 > 0 {
                            report.skippedDuplicates += 1                 // the PC has it: no upload
                            files.forEach { try? FileManager.default.removeItem(at: $0.file) }
                            queuedAny = false
                            break
                        }
                    }
                    if try await imports.enqueue(moving: f.file, name: f.name, sha256: sha) != nil { queuedAny = true }
                }
                if queuedAny {
                    try? store.setBackup([a.localId], .queued)
                    report.queued += 1
                } else if primarySha != nil, store.phoneAsset(localId: a.localId)?.mediaId == nil {
                    // every file was already in the outbox (an earlier pass): it is on its way
                    try? store.setBackup([a.localId], .queued)
                }
            } catch {
                report.failed += 1                                       // gone, or iCloud unreachable: next pass
            }
        }

        // 3) send
        await imports.drain(using: client)

        // 4) what left the outbox has landed on the desktop
        let stillQueued = Set(await imports.pending.map(\.sha256))
        let queued = (try? store.db.query("SELECT local_id, sha256 FROM phone_assets WHERE backup = 'queued'")) ?? []
        let landed = queued.compactMap { r -> String? in
            guard let id = r.string("local_id"), let sha = r.string("sha256") else { return nil }
            return stillQueued.contains(sha) ? nil : id
        }
        try? store.setBackup(landed, .sent)
        report.linked += await link(store.sentWithoutMatch(), client: client)
        return report
    }

    private func link(_ assets: [LibraryStore.PhoneAsset], client: SyncClient) async -> Int {
        let offers = assets.compactMap { a in
            a.sha256.map { MatchOffer(sha256: $0, size: a.fileSize ?? 0, name: a.fileName ?? "") }
        }
        guard !offers.isEmpty, let m = try? await client.match(offers) else { return 0 }
        return (try? store.applyShaMatches(m)) ?? 0
    }
}
#endif
