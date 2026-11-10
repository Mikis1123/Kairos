import Foundation
#if canImport(CryptoKit)

/// Decides what became of the camera-roll assets that were `queued` for backup, from FACTS the
/// outbox recorded (what is still waiting, and how every finished file left it). It never infers
/// "sent" from "no longer in the outbox": that inference turned rejected and dropped files into
/// "backed up". An asset is `sent` only when every one of its files was acknowledged by the desktop.
public struct BackupSettlement: Equatable, Sendable {
    public struct Failure: Equatable, Sendable {
        public let localId: String
        public let reason: String
    }

    public var sent: [String] = []          // all files acknowledged
    public var failed: [Failure] = []       // at least one file refused for good
    public var requeue: [String] = []       // no trace in the outbox or the log: ask again, never assume
    public var consumed: [UUID] = []        // outcomes that have been settled and can be forgotten

    public init() {}

    public static func settle(queued: [(localId: String, sha256: String?)],
                              pending: [ImportQueue.Item],
                              outcomes: [ImportQueue.Outcome]) -> BackupSettlement {
        var out = BackupSettlement()
        let waitingGroups = Set(pending.compactMap(\.group))
        let waitingShas = Set(pending.map(\.sha256))
        for asset in queued {
            // still on its way (or paused): leave it queued
            if waitingGroups.contains(asset.localId) { continue }
            if let sha = asset.sha256, waitingShas.contains(sha) { continue }
            let mine = outcomes.filter { o in
                o.group == asset.localId || (asset.sha256 != nil && o.sha256 == asset.sha256)
            }
            out.consumed.append(contentsOf: mine.map(\.id))
            if let bad = mine.first(where: { !$0.ok }) {
                out.failed.append(Failure(localId: asset.localId, reason: bad.reason ?? "rejected"))
            } else if !mine.isEmpty {
                out.sent.append(asset.localId)
            } else {
                out.requeue.append(asset.localId)
            }
        }
        return out
    }
}

/// User-facing wording for the codes the desktop (and the queue) report.
public enum BackupReason {
    public static func text(_ code: String?) -> String {
        switch code {
        case "insufficient_storage": return "Your computer is out of disk space."
        case "unsupported_type": return "This format isn't supported by Kairos."
        case "too_large": return "The file is too large."
        case "sha_mismatch", "corrupted_in_transfer": return "The file was damaged in transfer."
        case "power_saver": return "Your computer is saving power."
        case "bad_sha256", "length_required", "bad_body": return "Your computer couldn't read the upload."
        case nil: return "Your computer didn't accept it."
        case let other?: return "Your computer reported \"\(other)\"."
        }
    }

    /// Why uploads are paused (the desktop's condition, never the file's).
    public static func pausedText(_ code: String?) -> String {
        switch code {
        case "insufficient_storage": return "Your computer is out of disk space. Free some space and Kairos will continue."
        case "power_saver": return "Your computer is saving power. Backup continues when it is plugged in."
        default: return "Your computer isn't ready (\(code ?? "unknown")). Kairos will try again."
        }
    }
}
#endif
