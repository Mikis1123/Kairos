import KairosCore
import SwiftUI

/// The Hidden folder, behind Face ID / Touch ID with the passcode as fallback (VaultAuth). The
/// desktop never serves vault items over the LAN bridge (protocol 1), so once unlocked this shows
/// what the phone holds today: nothing, and why. Hidden-folder sync with its own key exchange is
/// planned for protocol 2. The session locks after 5 minutes idle or when the app leaves the
/// foreground (RootTabView).
struct HiddenFolderView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var unlocked = false
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Group {
                if unlocked {
                    EmptyStateView(symbol: "lock.open",
                                   title: "Hidden folder",
                                   message: "Hidden items stay on your computer for now. Syncing them to your iPhone needs its own encryption key and arrives in a later update.")
                } else {
                    locked
                }
            }
            .navigationTitle("Hidden")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                if unlocked {
                    ToolbarItem(placement: .primaryAction) {
                        Button("Lock", systemImage: "lock") {
                            model.vault.lock()
                            unlocked = false
                        }
                    }
                }
            }
            .kairosNavigationChrome()
            .background(Kairos.Palette.background)
        }
        .preferredColorScheme(.dark)
        .task {
            unlocked = model.vault.touch()
            if !unlocked { await unlock() }
        }
    }

    private var locked: some View {
        VStack(spacing: Kairos.Space.lg) {
            Image(systemName: "lock.fill")
                .font(.system(size: 52, weight: .light))
                .foregroundStyle(Kairos.Palette.brandGradient)
            Text("Hidden folder is locked").font(Kairos.Typography.title).foregroundStyle(Kairos.Palette.text)
            Button {
                Task { await unlock() }
            } label: {
                HStack {
                    if busy { ProgressView() }
                    Label(buttonTitle, systemImage: buttonSymbol)
                }
                .padding(.vertical, Kairos.Space.xs)
            }
            .buttonStyle(.borderedProminent)
            .tint(Kairos.Palette.accentFill)
            .disabled(busy || model.vault.availableMethod == .none)
            if let error {
                Text(error).font(Kairos.Typography.callout).foregroundStyle(Kairos.Palette.danger)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(Kairos.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var buttonTitle: String {
        switch model.vault.availableMethod {
        case .faceID: return "Unlock with Face ID"
        case .touchID: return "Unlock with Touch ID"
        case .opticID: return "Unlock with Optic ID"
        case .passcode: return "Unlock with Passcode"
        case .none: return "Set a passcode to use Hidden"
        }
    }

    private var buttonSymbol: String {
        switch model.vault.availableMethod {
        case .faceID: return "faceid"
        case .touchID: return "touchid"
        case .opticID: return "opticid"
        default: return "lock.open"
        }
    }

    private func unlock() async {
        busy = true
        defer { busy = false }
        do {
            try await model.vault.unlock()
            error = nil
            Haptics.success()
            withAnimation(Kairos.Motion.standard) { unlocked = true }
        } catch VaultAuth.Failure.cancelled {
            error = nil
        } catch VaultAuth.Failure.notAvailable {
            error = "Set a device passcode in Settings to protect the Hidden folder."
        } catch {
            self.error = "Couldn't unlock. Try again."
        }
    }
}
