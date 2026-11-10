#if canImport(CryptoKit) && canImport(SQLite3)
import XCTest
@testable import KairosCore

/// Fix Block A: the camera-roll backup never reports "sent" for something the desktop did not
/// acknowledge, and a full / failing desktop only pauses the queue.
final class BackupIntegrityTests: XCTestCase {
    /// A desktop that answers from a script, one answer per upload.
    final class ScriptedDesktop: ImportUploading, @unchecked Sendable {
        var present = false
        var script: [Error?] = []               // nil = accepted; an error = that answer
        private(set) var uploads = 0

        func importPresent(sha256: String) async throws -> Bool { present }

        func uploadImport(file: URL, name: String, sha256: String) async throws -> SyncClient.ImportOutcome {
            uploads += 1
            if !script.isEmpty, let err = script.removeFirst() { throw err }
            return .imported
        }
    }

    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("kairos-outbox-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func stage(_ bytes: String = "photo bytes") throws -> URL {
        let f = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".heic")
        try Data(bytes.utf8).write(to: f)
        return f
    }

    // ── the queue ──

    func testAcknowledgedUploadIsRecordedAsOkAndLeavesTheOutbox() async throws {
        let q = try ImportQueue(directory: dir)
        try await q.enqueue(moving: try stage(), name: "IMG_1.HEIC", group: "asset-1")
        await q.drain(using: ScriptedDesktop())
        let pending = await q.pending
        let outcomes = await q.outcomes
        XCTAssertTrue(pending.isEmpty)
        XCTAssertEqual(outcomes.map(\.ok), [true])
        XCTAssertEqual(outcomes.first?.group, "asset-1")
    }

    func testDiskFullKeepsTheFileQueuedAndOnlyPauses() async throws {
        let q = try ImportQueue(directory: dir)
        try await q.enqueue(moving: try stage(), name: "IMG_1.HEIC", group: "asset-1")
        let desktop = ScriptedDesktop()
        desktop.script = [SyncError.server(507, "insufficient_storage")]
        await q.drain(using: desktop)
        let pending = await q.pending
        let outcomes = await q.outcomes
        XCTAssertEqual(pending.count, 1, "507 must never drop the file")
        XCTAssertTrue(outcomes.isEmpty, "and must not be recorded as sent or failed")
        XCTAssertEqual(pending.first?.failures, 0)
        XCTAssertNotNil(pending.first?.pausedUntil)
        // while paused, a drain does not even try
        await q.drain(using: desktop)
        XCTAssertEqual(desktop.uploads, 1)
        // "Retry now" releases it, and the next answer is accepted
        await q.retryNow()
        await q.drain(using: desktop)
        let after = await q.outcomes
        XCTAssertEqual(after.map(\.ok), [true])
    }

    func testServerErrorsNeverDropAFileHoweverOftenTheyRepeat() async throws {
        let q = try ImportQueue(directory: dir)
        try await q.enqueue(moving: try stage(), name: "IMG_1.HEIC", group: "asset-1")
        let desktop = ScriptedDesktop()
        desktop.script = Array<Error?>(repeating: SyncError.server(500, "internal"), count: 8)
        for _ in 0..<8 {
            await q.retryNow()
            await q.drain(using: desktop)
        }
        let pending = await q.pending
        let outcomes = await q.outcomes
        XCTAssertEqual(pending.count, 1)
        XCTAssertTrue(outcomes.isEmpty)
    }

    func testOldDesktopWithoutImportEndpointPausesInsteadOfDropping() async throws {
        let q = try ImportQueue(directory: dir)
        try await q.enqueue(moving: try stage(), name: "IMG_1.HEIC", group: "asset-1")
        let desktop = ScriptedDesktop()
        desktop.script = [SyncError.server(404, "not_found")]
        await q.drain(using: desktop)
        let pending = await q.pending
        XCTAssertEqual(pending.count, 1)
    }

    func testRefusedFormatIsRecordedAsFailedWithItsReason() async throws {
        let q = try ImportQueue(directory: dir)
        try await q.enqueue(moving: try stage(), name: "IMG_1.HEIC", group: "asset-1")
        let desktop = ScriptedDesktop()
        desktop.script = [SyncError.server(415, "unsupported_type")]
        await q.drain(using: desktop)
        let pending = await q.pending
        let outcomes = await q.outcomes
        XCTAssertTrue(pending.isEmpty)
        XCTAssertEqual(outcomes.map(\.ok), [false])
        XCTAssertEqual(outcomes.first?.reason, "unsupported_type")
    }

    func testFiveCorruptedTransfersInARowAreRefusedNotSent() async throws {
        let q = try ImportQueue(directory: dir)
        try await q.enqueue(moving: try stage(), name: "IMG_1.HEIC", group: "asset-1")
        let desktop = ScriptedDesktop()
        desktop.script = Array<Error?>(repeating: SyncError.server(422, "sha_mismatch"), count: 5)
        await q.drain(using: desktop)
        let outcomes = await q.outcomes
        XCTAssertEqual(outcomes.map(\.ok), [false])
        XCTAssertEqual(outcomes.first?.reason, "corrupted_in_transfer")
        XCTAssertEqual(desktop.uploads, 5)
    }

    func testOutcomesAndPausesSurviveARestart() async throws {
        let q = try ImportQueue(directory: dir)
        try await q.enqueue(moving: try stage("a"), name: "A.HEIC", group: "asset-a")
        try await q.enqueue(moving: try stage("b"), name: "B.HEIC", group: "asset-b")
        let desktop = ScriptedDesktop()
        desktop.script = [nil, SyncError.server(507, "insufficient_storage")]
        await q.drain(using: desktop)
        let reopened = try ImportQueue(directory: dir)        // the app was killed and relaunched
        let outcomes = await reopened.outcomes
        let pending = await reopened.pending
        XCTAssertEqual(outcomes.map(\.group), ["asset-a"], "outcomes.json must not be swept as a stray file")
        XCTAssertEqual(pending.map(\.group), ["asset-b"])
        XCTAssertNotNil(pending.first?.pausedUntil)
    }

    // ── settling assets from those facts ──

    private func item(_ sha: String, group: String?) -> ImportQueue.Item {
        ImportQueue.Item(id: UUID(), name: "x.heic", sha256: sha, size: 1, added: Date(), failures: 0,
                         lastError: nil, group: group, deferrals: nil, pausedUntil: nil)
    }

    private func outcome(_ sha: String, group: String?, ok: Bool, reason: String? = nil) -> ImportQueue.Outcome {
        ImportQueue.Outcome(id: UUID(), sha256: sha, group: group, name: "x.heic", ok: ok, reason: reason, date: Date())
    }

    func testAnAssetIsSentOnlyWhenEveryFileWasAcknowledged() {
        // a Live Photo: the still was acknowledged, the motion half is still waiting (paused)
        let r = BackupSettlement.settle(queued: [(localId: "live", sha256: "still")],
                                        pending: [item("motion", group: "live")],
                                        outcomes: [outcome("still", group: "live", ok: true)])
        XCTAssertTrue(r.sent.isEmpty)
        XCTAssertTrue(r.failed.isEmpty)
        XCTAssertTrue(r.requeue.isEmpty)
        // both acknowledged: sent
        let done = BackupSettlement.settle(queued: [(localId: "live", sha256: "still")], pending: [],
                                           outcomes: [outcome("still", group: "live", ok: true),
                                                      outcome("motion", group: "live", ok: true)])
        XCTAssertEqual(done.sent, ["live"])
        XCTAssertEqual(done.consumed.count, 2)
    }

    func testARefusedFileMakesTheAssetFailedNeverSent() {
        let r = BackupSettlement.settle(queued: [(localId: "a", sha256: "s")], pending: [],
                                        outcomes: [outcome("s", group: "a", ok: false, reason: "unsupported_type")])
        XCTAssertTrue(r.sent.isEmpty)
        XCTAssertEqual(r.failed, [BackupSettlement.Failure(localId: "a", reason: "unsupported_type")])
    }

    func testNoTraceMeansAskAgainNotSent() {
        // before this fix a file that vanished from the outbox for ANY reason became "sent"
        let r = BackupSettlement.settle(queued: [(localId: "a", sha256: "s")], pending: [], outcomes: [])
        XCTAssertTrue(r.sent.isEmpty)
        XCTAssertEqual(r.requeue, ["a"])
    }

    func testStillQueuedAssetIsLeftAlone() {
        let r = BackupSettlement.settle(queued: [(localId: "a", sha256: "s")], pending: [item("s", group: nil)],
                                        outcomes: [])
        XCTAssertEqual(r, BackupSettlement())
    }

    // ── the store ──

    func testFailedAssetsKeepTheirReasonAndRetryMakesThemApprovedAgain() throws {
        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        let rec = LibraryStore.PhoneAssetRecord(localId: "a", created: Date(), modified: Date(), isVideo: false,
                                                live: false, isFavorite: false, width: 1, height: 1, duration: 0,
                                                latitude: nil, longitude: nil, fileName: "IMG_1.HEIC", fileSize: 1)
        try store.upsertPhoneAssets([rec])
        try store.setBackup(["a"], .queued)
        try store.setBackupFailed([(localId: "a", reason: "insufficient_storage")])
        var c = store.phoneCounts()
        XCTAssertEqual([c.failed, c.waiting, c.queued], [1, 0, 0])
        XCTAssertEqual(c.failedReason, "insufficient_storage")
        XCTAssertEqual(try store.retryFailedBackups(), 1)
        c = store.phoneCounts()
        XCTAssertEqual([c.failed, c.approved], [0, 1])
        XCTAssertNil(c.failedReason)
    }

    func testSchemaV3AddsTheBackupErrorColumn() throws {
        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        XCTAssertEqual(try store.db.scalarInt("PRAGMA user_version"), LibraryStore.schemaVersion)
        XCTAssertNoThrow(try store.db.query("SELECT backup_error FROM phone_assets"))
    }
}
#endif
