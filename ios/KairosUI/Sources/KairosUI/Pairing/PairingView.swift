import KairosCore
import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// Pairing: scan the QR code that Kairos shows on the computer, or paste its kairos://pair link.
/// For development, `python sync_server.py` prints that link. The PIN is single-use and expires
/// after 2 minutes. The certificate fingerprint in the link is pinned for good.
struct PairingView: View {
    @Environment(AppModel.self) private var model
    @State private var link = ""
    @State private var busy = false
    @State private var error: String?
    @State private var scanning = false

    var body: some View {
        VStack(spacing: Kairos.Space.xl) {
            Spacer()
            Image(systemName: "laptopcomputer.and.iphone")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(Kairos.Palette.brandGradient)
            VStack(spacing: Kairos.Space.sm) {
                Text("Connect to Kairos").font(Kairos.Typography.largeTitle).foregroundStyle(Kairos.Palette.text)
                Text("Start pairing in Kairos on your computer, then scan its code or paste its link. Both devices must be on the same Wi-Fi.")
                    .font(Kairos.Typography.callout)
                    .foregroundStyle(Kairos.Palette.textSecondary)
                    .multilineTextAlignment(.center)
            }
            Button { scanning = true } label: {
                Label("Scan pairing code", systemImage: "qrcode.viewfinder").frame(maxWidth: .infinity)
                    .padding(.vertical, Kairos.Space.sm)
            }
            .buttonStyle(.borderedProminent)
            .tint(Kairos.Palette.accentFill)
            .disabled(busy || !Self.canScan)
            VStack(alignment: .leading, spacing: Kairos.Space.sm) {
                TextField("kairos://pair?…", text: $link, axis: .vertical)
                    .font(.system(.footnote, design: .monospaced))
                    .autocorrectionDisabled()
                    .padding(Kairos.Space.md)
                    .kairosCard()
                Button {
                    Task { await pair() }
                } label: {
                    HStack {
                        if busy { ProgressView() }
                        Text("Pair").frame(maxWidth: .infinity)
                    }
                    .padding(.vertical, Kairos.Space.sm)
                }
                .buttonStyle(.bordered)
                .disabled(busy || PairingOffer(uri: link) == nil)
                if let error {
                    Text(error).font(Kairos.Typography.callout).foregroundStyle(Kairos.Palette.danger)
                }
            }
            Spacer()
        }
        .padding(Kairos.Space.xl)
        .background(Kairos.Palette.background.ignoresSafeArea())
        .preferredColorScheme(.dark)
        #if os(iOS)
        .fullScreenCover(isPresented: $scanning) {
            QRScannerView { uri in
                // the scanner only hands over codes that parse as a kairos://pair offer
                link = uri
                Task { await pair() }
            }
        }
        #endif
    }

    /// The simulator and camera-less devices fall back to pasting the link.
    private static var canScan: Bool {
        #if os(iOS) && !targetEnvironment(simulator)
        return true
        #else
        return false
        #endif
    }

    private func pair() async {
        busy = true
        defer { busy = false }
        do {
            try await model.pair(uri: link, deviceName: Self.deviceName)
            error = nil
        } catch SyncError.pairing("bad_pin") {
            error = "That code didn't match. Show a new code on your computer."
        } catch SyncError.pairing(_) {
            error = "The code expired. Show a new code on your computer."
        } catch {
            self.error = "Couldn't reach your computer. Check that both are on the same Wi-Fi."
        }
    }

    static var deviceName: String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return Host.current().localizedName ?? "Mac"
        #endif
    }
}
