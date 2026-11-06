import KairosCore
import SwiftUI
#if os(iOS)
import PhotosUI
import UIKit
import UniformTypeIdentifiers

/// Apple Photos picker (PHPickerViewController) for sending camera-roll photos and videos to the
/// desktop. Runs out of process, so Kairos needs no Photos permission: it only ever sees what the
/// user picks. `.current` representation keeps the originals as shot (HEIC, ProRAW DNG, HEVC MOV)
/// instead of transcoding them to JPEG/H.264. Each pick is copied to tmp inside the provider's
/// callback (the system deletes its file when the callback returns) and then handed to
/// `onPicked`, which moves it into the ImportQueue outbox.
struct PhotoPicker: UIViewControllerRepresentable {
    struct Picked: Sendable {
        let file: URL           // staged copy in tmp, owned by the receiver
        let name: String        // IMG_0042.HEIC
    }

    /// Called once per batch, on the main actor, after every selected item was staged.
    /// `skipped` counts items that could not be read (e.g. iCloud download failed).
    let onPicked: ([Picked], _ skipped: Int) -> Void
    /// Closes the sheet (the presenter owns it); called as soon as the user taps Add or Cancel.
    let close: () -> Void

    func makeUIViewController(context: Context) -> PHPickerViewController {
        var config = PHPickerConfiguration()
        config.selectionLimit = 0                                    // unlimited
        config.filter = .any(of: [.images, .videos, .livePhotos])
        config.preferredAssetRepresentationMode = .current
        config.selection = .ordered
        let picker = PHPickerViewController(configuration: config)
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ controller: PHPickerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, PHPickerViewControllerDelegate {
        private let parent: PhotoPicker

        init(_ parent: PhotoPicker) { self.parent = parent }

        func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
            parent.close()
            guard !results.isEmpty else { return }
            let onPicked = parent.onPicked
            Task.detached(priority: .userInitiated) {
                var picked: [Picked] = []
                var skipped = 0
                for result in results {
                    if let p = await Self.stage(result.itemProvider) { picked.append(p) } else { skipped += 1 }
                }
                let batch = picked, missed = skipped
                await MainActor.run { onPicked(batch, missed) }
            }
        }

        /// The best original the provider offers: a movie for videos, otherwise the still image.
        /// For a Live Photo that is the still (HEIC); its motion half needs PhotoKit library access,
        /// which this permission-free picker deliberately avoids.
        static func preferredType(_ provider: NSItemProvider) -> UTType? {
            let types = provider.registeredTypeIdentifiers.compactMap(UTType.init)
            if let movie = types.first(where: { $0.conforms(to: .movie) }) { return movie }
            return types.first(where: { $0.conforms(to: .image) && !$0.conforms(to: .livePhoto) })
        }

        static func stage(_ provider: NSItemProvider) async -> Picked? {
            guard let type = preferredType(provider) else { return nil }
            return await withCheckedContinuation { (cont: CheckedContinuation<Picked?, Never>) in
                _ = provider.loadFileRepresentation(forTypeIdentifier: type.identifier) { url, _ in
                    guard let url else { return cont.resume(returning: nil) }
                    let ext = url.pathExtension.isEmpty ? (type.preferredFilenameExtension ?? "") : url.pathExtension
                    let base = provider.suggestedName.flatMap { ($0 as NSString).deletingPathExtension }
                        .flatMap { $0.isEmpty ? nil : $0 } ?? url.deletingPathExtension().lastPathComponent
                    let name = ext.isEmpty ? base : "\(base).\(ext.uppercased())"
                    let staged = FileManager.default.temporaryDirectory
                        .appendingPathComponent("import-\(UUID().uuidString)")
                        .appendingPathExtension(ext)
                    do {
                        try FileManager.default.copyItem(at: url, to: staged)   // url dies with this callback
                        cont.resume(returning: Picked(file: staged, name: name))
                    } catch {
                        cont.resume(returning: nil)
                    }
                }
            }
        }
    }
}
#endif
