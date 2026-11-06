import KairosCore
import SwiftUI
#if os(iOS)
import AVFoundation
import UIKit

/// Full-screen camera that reads the pairing QR from Kairos on the computer. The first code that
/// parses as a `kairos://pair` offer (host, port, certificate fingerprint, library, PIN) ends the
/// scan: a haptic confirms it and `onOffer` receives the raw URI, which PairingView pairs with.
/// Other QR codes are ignored, so pointing at a Wi-Fi sticker does nothing.
struct QRScannerView: View {
    let onOffer: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var access: AVAuthorizationStatus = AVCaptureDevice.authorizationStatus(for: .video)

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            switch access {
            case .authorized:
                ScannerRepresentable { uri in
                    Haptics.success()
                    onOffer(uri)
                    dismiss()
                }
                .ignoresSafeArea()
                viewfinder
            case .notDetermined:
                ProgressView().tint(.white)
            default:
                denied
            }
            VStack {
                HStack {
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(.white)
                            .frame(width: 40, height: 40)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .accessibilityLabel("Close")
                }
                Spacer()
            }
            .padding(Kairos.Space.lg)
        }
        .preferredColorScheme(.dark)
        .task {
            if access == .notDetermined {
                _ = await AVCaptureDevice.requestAccess(for: .video)
                access = AVCaptureDevice.authorizationStatus(for: .video)
            }
        }
    }

    private var viewfinder: some View {
        VStack(spacing: Kairos.Space.lg) {
            Spacer()
            RoundedRectangle(cornerRadius: 28, style: .continuous)
                .strokeBorder(.white.opacity(0.9), lineWidth: 3)
                .frame(width: 250, height: 250)
                .shadow(color: .black.opacity(0.4), radius: 12)
            Text("Point at the code shown in Kairos on your computer")
                .font(Kairos.Typography.callout)
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
                .padding(.horizontal, Kairos.Space.xl)
            Spacer()
        }
        .allowsHitTesting(false)
    }

    private var denied: some View {
        VStack(spacing: Kairos.Space.md) {
            Image(systemName: "camera.fill").font(.system(size: 44, weight: .light)).foregroundStyle(.white)
            Text("Camera access is off").font(Kairos.Typography.title).foregroundStyle(.white)
            Text("Allow the camera in Settings to scan the pairing code, or paste the link instead.")
                .font(Kairos.Typography.callout).foregroundStyle(.white.opacity(0.7))
                .multilineTextAlignment(.center)
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            }
            .buttonStyle(.borderedProminent)
            .tint(Kairos.Palette.accentFill)
        }
        .padding(Kairos.Space.xl)
    }
}

private struct ScannerRepresentable: UIViewControllerRepresentable {
    let onOffer: (String) -> Void

    func makeUIViewController(context: Context) -> ScannerController {
        let c = ScannerController()
        c.onOffer = onOffer
        return c
    }

    func updateUIViewController(_ controller: ScannerController, context: Context) {
        controller.onOffer = onOffer
    }

    static func dismantleUIViewController(_ controller: ScannerController, coordinator: ()) {
        controller.stop()
    }
}

/// AVCaptureSession with a QR-only metadata output. The session starts and stops on its own serial
/// queue (startRunning blocks for ~100-300 ms and must stay off the main thread).
final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var onOffer: ((String) -> Void)?
    private let session = AVCaptureSession()
    private let sessionQueue = DispatchQueue(label: "app.kairos.qr-scanner")
    private var preview: AVCaptureVideoPreviewLayer?
    private var delivered = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        view.layer.addSublayer(layer)
        preview = layer
        sessionQueue.async { [weak self] in self?.configure() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.bounds
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        sessionQueue.async { [session] in if !session.isRunning { session.startRunning() } }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        stop()
    }

    func stop() {
        sessionQueue.async { [session] in if session.isRunning { session.stopRunning() } }
    }

    private func configure() {
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let input = try? AVCaptureDeviceInput(device: camera), session.canAddInput(input) else { return }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        if output.availableMetadataObjectTypes.contains(.qr) { output.metadataObjectTypes = [.qr] }
        // a pairing QR is held close: autofocus near, so dense codes resolve quickly
        if (try? camera.lockForConfiguration()) != nil {
            if camera.isAutoFocusRangeRestrictionSupported { camera.autoFocusRangeRestriction = .near }
            camera.unlockForConfiguration()
        }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard !delivered else { return }
        for case let code as AVMetadataMachineReadableCodeObject in objects where code.type == .qr {
            guard let text = code.stringValue, PairingOffer(uri: text) != nil else { continue }
            delivered = true
            stop()
            onOffer?(text)
            return
        }
    }
}
#endif
