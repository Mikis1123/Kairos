#if os(iOS)
import AVFoundation
import CoreGraphics
import CryptoKit
import Foundation
import ImageIO
import KairosCore
import Photos
import UIKit

/// The camera roll, read in place through PhotoKit (Parity sprint, three-state library).
///
/// Nothing is copied into Kairos: the replica's `phone_assets` table holds only metadata (local
/// identifier, capture time, file name and size, position), and every pixel is requested from
/// the Photos library when a view needs it. Assets the desktop already holds (AssetMatcher, or a
/// SHA-256 proof from /v1/match) are shown as their desktop item but drawn from the local
/// original, so a synchronised photo opens without any network. An asset whose original was
/// offloaded to iCloud ("Optimise iPhone Storage") reports `inCloud`; for a synchronised item
/// the viewer then fetches the full file from the PC instead of waking iCloud.
///
/// Indexing is incremental: an asset is re-read only when its modificationDate moved, so a
/// refresh of an unchanged 20 000-photo roll costs one fetch and no resource reads.
public final class PhoneLibrary: NSObject, PHPhotoLibraryChangeObserver, @unchecked Sendable {
    public enum Access: Sendable, Equatable { case notDetermined, denied, limited, full }

    public struct IndexProgress: Sendable, Equatable {
        public var indexing = false
        public var done = 0
        public var total = 0
    }

    private let store: LibraryStore
    private let manager = PHCachingImageManager()
    private let queue = DispatchQueue(label: "app.kairos.phone-index", qos: .utility)
    private let lock = NSLock()
    private var running = false
    private var again = false
    private var observing = false
    private var debounce: DispatchWorkItem?
    /// Called (off the main thread) after the index or the matches changed.
    var onChange: (@Sendable (IndexProgress) -> Void)?

    static let batch = 300

    init(store: LibraryStore) {
        self.store = store
        super.init()
        manager.allowsCachingHighQualityImages = false
    }

    deinit {
        if observing { PHPhotoLibrary.shared().unregisterChangeObserver(self) }
    }

    // ── permission ──

    public static var access: Access {
        switch PHPhotoLibrary.authorizationStatus(for: .readWrite) {
        case .authorized: return .full
        case .limited: return .limited
        case .notDetermined: return .notDetermined
        default: return .denied
        }
    }

    public static func requestAccess() async -> Access {
        _ = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        return access
    }

    var isAuthorized: Bool { [.full, .limited].contains(Self.access) }

    /// Starts observing the camera roll and indexes it. Safe to call again.
    func start() {
        guard isAuthorized else { return }
        lock.lock()
        let first = !observing
        observing = true
        lock.unlock()
        if first { PHPhotoLibrary.shared().register(self) }
        refresh()
    }

    public func photoLibraryDidChange(_ changeInstance: PHChange) {
        // bursts (an import of 300 photos) arrive as many changes: index once they settle
        lock.lock()
        debounce?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.refresh() }
        debounce = work
        lock.unlock()
        queue.asyncAfter(deadline: .now() + 1.0, execute: work)
    }

    /// Indexes on the background queue; a call while indexing schedules one more pass.
    func refresh() {
        lock.lock()
        if running {
            again = true
            lock.unlock()
            return
        }
        running = true
        lock.unlock()
        queue.async { [weak self] in self?.indexLoop() }
    }

    /// Awaitable refresh (background tasks).
    func refreshNow() async {
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            queue.async { [weak self] in
                self?.index()
                cont.resume()
            }
        }
    }

    private func indexLoop() {
        while true {
            index()
            lock.lock()
            if !again {
                running = false
                lock.unlock()
                return
            }
            again = false
            lock.unlock()
        }
    }

    private func index() {
        guard isAuthorized else { return }
        let opts = PHFetchOptions()
        opts.includeAssetSourceTypes = [.typeUserLibrary]          // not shared albums / iTunes syncs
        opts.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        opts.predicate = NSPredicate(format: "mediaType == %d OR mediaType == %d",
                                     PHAssetMediaType.image.rawValue, PHAssetMediaType.video.rawValue)
        let result = PHAsset.fetchAssets(with: opts)
        let known = store.phoneModified()
        var present = Set<String>()
        present.reserveCapacity(result.count)
        var pending: [LibraryStore.PhoneAssetRecord] = []
        var progress = IndexProgress(indexing: true, done: 0, total: result.count)
        let notify = onChange
        result.enumerateObjects { asset, _, _ in
            let id = asset.localIdentifier
            present.insert(id)
            progress.done += 1
            let mod = asset.modificationDate?.timeIntervalSince1970 ?? 0
            if let k = known[id], abs(k - mod) < 0.001 { return }
            pending.append(Self.record(asset))
            if pending.count >= Self.batch {
                try? self.store.upsertPhoneAssets(pending)
                pending.removeAll(keepingCapacity: true)
                try? self.store.rematchPhone()
                notify?(progress)                                   // the grid fills in as it goes
            }
        }
        try? store.upsertPhoneAssets(pending)
        try? store.removePhoneAssets(keeping: present)
        try? store.rematchPhone()
        progress.indexing = false
        notify?(progress)
    }

    /// Metadata of one asset. The original resource names the file (IMG_0042.HEIC) and its size.
    static func record(_ asset: PHAsset) -> LibraryStore.PhoneAssetRecord {
        let resources = PHAssetResource.assetResources(for: asset)
        let wanted: [PHAssetResourceType] = asset.mediaType == .video ? [.video, .fullSizeVideo] : [.photo, .fullSizePhoto]
        let primary = wanted.lazy.compactMap { t in resources.first { $0.type == t } }.first ?? resources.first
        return LibraryStore.PhoneAssetRecord(
            localId: asset.localIdentifier, created: asset.creationDate, modified: asset.modificationDate,
            isVideo: asset.mediaType == .video, live: asset.mediaSubtypes.contains(.photoLive),
            isFavorite: asset.isFavorite, width: asset.pixelWidth, height: asset.pixelHeight,
            duration: asset.duration, latitude: asset.location?.coordinate.latitude,
            longitude: asset.location?.coordinate.longitude, fileName: primary?.originalFilename,
            fileSize: primary.flatMap(fileSize))
    }

    /// The resource's byte count. PhotoKit has no public API for it; the `fileSize` key is read
    /// only when the object answers it, so a future iOS without it degrades to "unknown".
    static func fileSize(_ r: PHAssetResource) -> Int64? {
        guard r.responds(to: NSSelectorFromString("fileSize")) else { return nil }
        return (r.value(forKey: "fileSize") as? NSNumber)?.int64Value
    }

    static func asset(_ localId: String) -> PHAsset? {
        PHAsset.fetchAssets(withLocalIdentifiers: [localId], options: nil).firstObject
    }

    // ── pixels, in place ──

    /// Which rendition: `.current` is what Photos shows (with Apple edits) for phone-only assets;
    /// `.original` matches the desktop's file, the base Kairos edits apply to.
    enum Version { case current, original }

    /// A decoded image no larger than `maxPixel` on the long edge. Nil when the asset is gone, or
    /// only in iCloud and `allowNetwork` is false (then `inCloud` is recorded for the caller).
    func image(_ localId: String, maxPixel: Int, version: Version, allowNetwork: Bool) async -> CGImage? {
        guard let asset = Self.asset(localId) else { return nil }
        let opts = PHImageRequestOptions()
        opts.deliveryMode = .highQualityFormat                      // exactly one callback
        opts.resizeMode = .fast
        opts.isNetworkAccessAllowed = allowNetwork
        opts.version = version == .original ? .original : .current
        let k = Double(maxPixel) / Double(max(1, max(asset.pixelWidth, asset.pixelHeight)))
        let size = k < 1 ? CGSize(width: Double(asset.pixelWidth) * k, height: Double(asset.pixelHeight) * k)
                         : CGSize(width: asset.pixelWidth, height: asset.pixelHeight)
        let box = RequestBox()
        let store = self.store
        return await withTaskCancellationHandler {
            await withCheckedContinuation { (cont: CheckedContinuation<CGImage?, Never>) in
                box.id = manager.requestImage(for: asset, targetSize: size, contentMode: .aspectFit, options: opts) { img, info in
                    if img == nil, (info?[PHImageResultIsInCloudKey] as? Bool) == true {
                        try? store.setPhoneInCloud(localId, true)
                    }
                    cont.resume(returning: img?.cgImage)
                }
            }
        } onCancel: { [manager] in
            if let id = box.id { manager.cancelImageRequest(id) }
        }
    }

    /// The encoded original (HEIC / JPEG / DNG bytes) for full-resolution decoding in memory.
    func imageData(_ localId: String, version: Version, allowNetwork: Bool) async -> Data? {
        guard let asset = Self.asset(localId) else { return nil }
        let opts = PHImageRequestOptions()
        opts.deliveryMode = .highQualityFormat
        opts.isNetworkAccessAllowed = allowNetwork
        opts.version = version == .original ? .original : .current
        let store = self.store
        return await withCheckedContinuation { (cont: CheckedContinuation<Data?, Never>) in
            manager.requestImageDataAndOrientation(for: asset, options: opts) { data, _, _, info in
                if data == nil, (info?[PHImageResultIsInCloudKey] as? Bool) == true {
                    try? store.setPhoneInCloud(localId, true)
                } else if data != nil {
                    try? store.setPhoneInCloud(localId, false)
                }
                cont.resume(returning: data)
            }
        }
    }

    /// A player item for a local video (no copy, no network unless allowed).
    func playerItem(_ localId: String, allowNetwork: Bool) async -> AVPlayerItem? {
        guard let asset = Self.asset(localId) else { return nil }
        let opts = PHVideoRequestOptions()
        opts.isNetworkAccessAllowed = allowNetwork
        opts.deliveryMode = .highQualityFormat
        opts.version = .original
        let store = self.store
        return await withCheckedContinuation { (cont: CheckedContinuation<AVPlayerItem?, Never>) in
            manager.requestPlayerItem(forVideo: asset, options: opts) { item, info in
                if item == nil, (info?[PHImageResultIsInCloudKey] as? Bool) == true {
                    try? store.setPhoneInCloud(localId, true)
                }
                cont.resume(returning: item)
            }
        }
    }

    /// The Live Photo for PHLivePhotoView (native hold-to-play, its own haptics and timing).
    func livePhoto(_ localId: String, targetSize: CGSize, allowNetwork: Bool) async -> PHLivePhoto? {
        guard let asset = Self.asset(localId), asset.mediaSubtypes.contains(.photoLive) else { return nil }
        let opts = PHLivePhotoRequestOptions()
        opts.deliveryMode = .highQualityFormat
        opts.isNetworkAccessAllowed = allowNetwork
        opts.version = .original
        let store = self.store
        let once = OnceFlag()
        return await withCheckedContinuation { (cont: CheckedContinuation<PHLivePhoto?, Never>) in
            manager.requestLivePhoto(for: asset, targetSize: targetSize, contentMode: .aspectFit, options: opts) { lp, info in
                // a degraded first delivery can precede the final one: answer with the final
                if (info?[PHImageResultIsDegradedKey] as? Bool) == true, lp != nil { return }
                if lp == nil, (info?[PHImageResultIsInCloudKey] as? Bool) == true {
                    try? store.setPhoneInCloud(localId, true)
                }
                if once.claim() { cont.resume(returning: lp) }
            }
        }
    }

    // ── backup export (the only time bytes leave PhotoKit, and only into the outbox) ──

    struct Exported: Sendable {
        let file: URL
        let name: String
        let primary: Bool
    }

    /// The original resources of an asset written into `directory` for upload: the photo or
    /// video, plus a Live Photo's motion half under the same stem, so the desktop pairs them.
    /// The outbox deletes each file as soon as the desktop has verified it.
    func exportOriginals(_ localId: String, to directory: URL) async throws -> [Exported] {
        guard let asset = Self.asset(localId) else { throw CocoaError(.fileNoSuchFile) }
        let resources = PHAssetResource.assetResources(for: asset)
        let wanted: [PHAssetResourceType] = asset.mediaType == .video ? [.video, .fullSizeVideo] : [.photo, .fullSizePhoto]
        guard let primary = wanted.lazy.compactMap({ t in resources.first { $0.type == t } }).first ?? resources.first
        else { throw CocoaError(.fileNoSuchFile) }
        var picks: [(PHAssetResource, Bool)] = [(primary, true)]
        if asset.mediaSubtypes.contains(.photoLive), let motion = resources.first(where: { $0.type == .pairedVideo }) {
            picks.append((motion, false))
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var out: [Exported] = []
        let stem = (primary.originalFilename as NSString).deletingPathExtension
        for (res, isPrimary) in picks {
            var name = res.originalFilename
            if !isPrimary {                                          // IMG_0042.HEIC + IMG_0042.MOV
                let ext = (name as NSString).pathExtension
                name = stem + "." + (ext.isEmpty ? "MOV" : ext)
            }
            let dest = directory.appendingPathComponent("\(UUID().uuidString)-\(name)")
            let opts = PHAssetResourceRequestOptions()
            opts.isNetworkAccessAllowed = true                       // a backup may wake iCloud
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                PHAssetResourceManager.default().writeData(for: res, toFile: dest, options: opts) { error in
                    if let error { cont.resume(throwing: error) } else { cont.resume() }
                }
            }
            out.append(Exported(file: dest, name: name, primary: isPrimary))
        }
        return out
    }

    /// SHA-256 of the original resource, streamed (no file written): "does the PC have these
    /// exact bytes already?" for assets the offline matcher could not place.
    func sha256(_ localId: String) async -> String? {
        guard let asset = Self.asset(localId) else { return nil }
        let resources = PHAssetResource.assetResources(for: asset)
        let wanted: [PHAssetResourceType] = asset.mediaType == .video ? [.video, .fullSizeVideo] : [.photo, .fullSizePhoto]
        guard let res = wanted.lazy.compactMap({ t in resources.first { $0.type == t } }).first else { return nil }
        let hasher = StreamingSHA256()
        let opts = PHAssetResourceRequestOptions()
        opts.isNetworkAccessAllowed = false                          // never pull iCloud just to compare
        return await withCheckedContinuation { (cont: CheckedContinuation<String?, Never>) in
            PHAssetResourceManager.default().requestData(for: res, options: opts, dataReceivedHandler: { chunk in
                hasher.update(chunk)
            }, completionHandler: { error in
                cont.resume(returning: error == nil ? hasher.hex() : nil)
            })
        }
    }
}

/// PHImageManager request id, set after the request starts and read by the cancel handler.
final class RequestBox: @unchecked Sendable {
    var id: PHImageRequestID?
}

final class OnceFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false

    func claim() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if done { return false }
        done = true
        return true
    }
}

final class StreamingSHA256: @unchecked Sendable {
    private var hasher = SHA256()
    private let lock = NSLock()

    func update(_ data: Data) {
        lock.lock(); defer { lock.unlock() }
        hasher.update(data: data)
    }

    func hex() -> String {
        lock.lock(); defer { lock.unlock() }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
#endif
