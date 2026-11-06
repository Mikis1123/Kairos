import Foundation
#if canImport(LocalAuthentication)
import LocalAuthentication

/// Gate for the Hidden folder, the phone's counterpart of the desktop's Windows Hello unlock
/// (locked_folder.py). Face ID / Touch ID first; the device passcode when biometrics are not set up,
/// locked out after failed attempts, or the user taps "Use Passcode". The unlock is a session: it
/// ends after `idleTimeout` without use, or as soon as the app leaves the foreground (the UI calls
/// `lock()`), matching the desktop's auto-lock.
@MainActor
public final class VaultAuth {
    public enum Method: Equatable, Sendable { case faceID, touchID, opticID, passcode, none }

    public enum Failure: Error, Equatable, Sendable {
        case cancelled                 // the user dismissed the prompt: say nothing
        case notAvailable              // no passcode on this device: the vault cannot be protected
        case failed(String)
    }

    public private(set) var isUnlocked = false
    public var idleTimeout: TimeInterval
    private var unlockedAt: Date?
    private var lastUse: Date?
    private let makeContext: () -> LAContext

    public init(idleTimeout: TimeInterval = 5 * 60, makeContext: @escaping () -> LAContext = { LAContext() }) {
        self.idleTimeout = idleTimeout
        self.makeContext = makeContext
    }

    /// What the unlock button should say ("Unlock with Face ID").
    public var availableMethod: Method {
        let ctx = makeContext()
        var error: NSError?
        if ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) {
            switch ctx.biometryType {
            case .faceID: return .faceID
            case .touchID: return .touchID
            case .opticID: return .opticID
            default: break
            }
        }
        return ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) ? .passcode : .none
    }

    /// True while the session is valid. Each check counts as use and extends the idle window.
    public func touch() -> Bool {
        guard isUnlocked, let last = lastUse else { return false }
        if Date().timeIntervalSince(last) > idleTimeout {
            lock()
            return false
        }
        lastUse = Date()
        return true
    }

    public func lock() {
        isUnlocked = false
        unlockedAt = nil
        lastUse = nil
    }

    /// Prompts for Face ID / Touch ID, falling back to the passcode. Throws `.cancelled` when
    /// the user backs out, so callers can stay silent.
    public func unlock(reason: String = "Unlock your Hidden folder") async throws {
        if touch() { return }
        let ctx = makeContext()
        ctx.localizedFallbackTitle = "Use Passcode"
        ctx.localizedCancelTitle = "Cancel"
        do {
            if ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil) {
                do {
                    try await evaluate(ctx, .deviceOwnerAuthenticationWithBiometrics, reason)
                } catch let e as LAError where Self.wantsPasscode(e.code) {
                    // fresh context: a context that just failed biometrics may refuse to re-prompt
                    let pass = makeContext()
                    try await evaluate(pass, .deviceOwnerAuthentication, reason)
                }
            } else {
                // no biometrics enrolled (or locked out): straight to the passcode
                try await evaluate(ctx, .deviceOwnerAuthentication, reason)
            }
        } catch let e as LAError {
            switch e.code {
            case .userCancel, .appCancel, .systemCancel: throw Failure.cancelled
            case .passcodeNotSet: throw Failure.notAvailable
            default: throw Failure.failed(e.localizedDescription)
            }
        }
        let now = Date()
        isUnlocked = true
        unlockedAt = now
        lastUse = now
    }

    static func wantsPasscode(_ code: LAError.Code) -> Bool {
        switch code {
        case .userFallback, .biometryLockout, .biometryNotAvailable, .biometryNotEnrolled: return true
        default: return false
        }
    }

    private func evaluate(_ ctx: LAContext, _ policy: LAPolicy, _ reason: String) async throws {
        let ok = try await ctx.evaluatePolicy(policy, localizedReason: reason)
        if !ok { throw LAError(.authenticationFailed) }
    }
}
#endif
