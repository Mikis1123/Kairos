import CoreGraphics
import Foundation
import ImageIO
import KairosCore
import SwiftUI
import UniformTypeIdentifiers

/// Pixels for the grid, the viewer and the editor.
///   micro:    the 48 px LQIP from the replica, decoded synchronously (~30 µs). The desktop bakes
///             the edit into it. After an edit made here, the loader re-bakes it from the edited
///             thumbnail, so the grid never flashes the old look.
///   bytes:    the desktop's thumbnail file (1120 x 840 class, unedited), fetched through a LIFO
///             gate with retries (FetchQueue.swift) and kept on disk in Application Support, so
///             the library stays sharp with the desktop switched off. 5 GB budget, LRU.
///   grid:     those bytes decoded at grid size (640 px long edge) with the edit applied.
///   thumb:    the same at full thumbnail resolution with the edit (the viewer's first pass).
///   raw:      full resolution WITHOUT the edit (the editor's fallback source).
///   original: streamed on demand into Caches/Kairos/originals (the newest 12 files are kept), then
///             decoded at the size the caller asks for: the viewer's screen pass and 1:1 detail
///             pass, or the editor's 2048 px working source.
/// Memory: one NSCache, costed in decoded bytes.
@MainActor
public final class ImageLoader {
    var store: LibraryStore?
    var client: SyncClient?
    private let cache = NSCache<NSString, CGImageBox>()
    private var pendingBytes: [String: PendingFetch] = [:]
    private var staleMicro: Set<MediaID> = []
    private var originalTasks: [MediaID: Task<URL?, Never>] = [:]
    /// The server allows 8 requests in flight; the sync engine, micro pages and originals need
    /// the rest.
    private let gate = FetchGate(limit: 4)
    let disk: DiskCache

    nonisolated static let microPixel = 48
    nonisolated static let gridPixel = 640
    nonisolated static let originalsKept = 12
    nonisolated static let budgetKey = "kairos.cache.budgetBytes"
    nonisolated static let defaultBudget: Int64 = 5 * 1024 * 1024 * 1024

    /// The offline cache budget on the phone (Settings will expose it; 5 GB by default).
    nonisolated static var cacheBudget: Int64 {
        let v = UserDefaults.standard.object(forKey: budgetKey) as? NSNumber
        return v.map { max($0.int64Value, 256 * 1024 * 1024) } ?? defaultBudget
    }

    final class CGImageBox {
        let image: CGImage
        init(_ i: CGImage) { image = i }
    }

    /// One disk/network fetch shared by every view asking for the same file. Cancelled when the
    /// last of them goes away (the cell scrolled off screen).
    final class PendingFetch: @unchecked Sendable {
        let task: Task<Data?, Never>
        var waiters = 0
        init(_ t: Task<Data?, Never>) { task = t }
    }

    nonisolated static var thumbsDirectory: URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true)) ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("Kairos/Thumbs", isDirectory: true)
    }

    init() {
        cache.totalCostLimit = 320 * 1024 * 1024
        disk = DiskCache(directory: Self.thumbsDirectory, budget: Self.cacheBudget)
    }

    private func put(_ img: CGImage, _ key: String) {
        cache.setObject(CGImageBox(img), forKey: key as NSString, cost: max(1, img.bytesPerRow * img.height))
    }

    private func hit(_ key: String) -> CGImage? { cache.object(forKey: key as NSString)?.image }

    // ── decoding ──

    nonisolated static func decode(_ data: Data, maxPixel: Int? = nil) -> CGImage? {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        return decode(src, maxPixel: maxPixel)
    }

    nonisolated static func decode(url: URL, maxPixel: Int?) -> CGImage? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return decode(src, maxPixel: maxPixel)
    }

    nonisolated static func decode(_ src: CGImageSource, maxPixel: Int?) -> CGImage? {
        // Always through the thumbnail path with the EXIF transform: pixels arrive upright, like
        // the desktop's (its edits are defined on the oriented image). Without a maxPixel the
        // file's own long edge is passed, so nothing is downscaled.
        var opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                     kCGImageSourceCreateThumbnailWithTransform: true,
                                     kCGImageSourceShouldCacheImmediately: true]
        if let maxPixel {
            opts[kCGImageSourceThumbnailMaxPixelSize] = maxPixel
        } else if let p = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
                  let w = p[kCGImagePropertyPixelWidth] as? Int, let h = p[kCGImagePropertyPixelHeight] as? Int {
            opts[kCGImageSourceThumbnailMaxPixelSize] = max(w, h)
        }
        return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
    }

    /// Upright pixel size of an image file (EXIF orientations 5-8 swap width and height).
    nonisolated static func pixelSize(url: URL) -> CGSize? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let p = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
              let w = p[kCGImagePropertyPixelWidth] as? Int, let h = p[kCGImagePropertyPixelHeight] as? Int
        else { return nil }
        let o = p[kCGImagePropertyOrientation] as? Int ?? 1
        return o >= 5 ? CGSize(width: h, height: w) : CGSize(width: w, height: h)
    }

    nonisolated static func jpeg(_ image: CGImage, quality: Double = 0.7) -> Data? {
        let out = NSMutableData()
        guard let dst = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dst, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        return CGImageDestinationFinalize(dst) ? out as Data : nil
    }

    nonisolated static func downscaled(_ image: CGImage, maxPixel: Int) -> CGImage? {
        let k = Double(maxPixel) / Double(max(image.width, image.height))
        guard k < 1 else { return image }
        let w = max(1, Int((Double(image.width) * k).rounded())), h = max(1, Int((Double(image.height) * k).rounded()))
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return ctx.makeImage()
    }

    nonisolated static func edited(_ img: CGImage, _ adj: Adjustments?) -> CGImage {
        guard let adj, !adj.isIdentity else { return img }
        #if canImport(Accelerate)
        return EditPipeline.render(img, adj) ?? img
        #else
        return img
        #endif
    }

    // ── micro ──

    public func micro(_ id: MediaID) -> CGImage? {
        let key = "m\(id)"
        if let img = hit(key) { return img }
        guard let data = store?.micro(id), let img = Self.decode(data) else { return nil }
        put(img, key)
        return img
    }

    /// Re-bakes the LQIP from an edited rendering (the desktop does the same after its own edits).
    public func rebakeMicro(_ id: MediaID, from edited: CGImage) {
        staleMicro.remove(id)
        let store = self.store
        Task.detached(priority: .utility) {
            guard let small = ImageLoader.downscaled(edited, maxPixel: ImageLoader.microPixel),
                  let data = ImageLoader.jpeg(small, quality: 0.6) else { return }
            try? store?.putMicro([(id, data)])
        }
        cache.removeObject(forKey: "m\(id)" as NSString)
        if let small = Self.downscaled(edited, maxPixel: Self.microPixel) { put(small, "m\(id)") }
    }

    // ── bytes: disk first, then the desktop ──

    /// Disk key of a thumbnail: id + fingerprint, so a file changed on the desktop is fetched again.
    private func thumbKey(_ id: MediaID) -> String {
        "t\(id)-\(store?.media(id)?.fingerprint ?? "x").jpg"
    }

    /// Shared, cancellable fetch. Every caller registers as a waiter; when the last one is
    /// cancelled, the underlying request is cancelled too (and leaves the gate's queue).
    private func bytes(key: String, fetch: (@Sendable (SyncClient) async throws -> Data)?) async -> Data? {
        let pending: PendingFetch
        if let p = pendingBytes[key] {
            pending = p
        } else {
            let disk = self.disk, gate = self.gate, client = self.client
            let task = Task<Data?, Never>.detached(priority: .userInitiated) {
                if let d = disk.read(key) { return d }
                guard let client, let fetch else { return nil }
                guard let d = await FetchRetry.run(gate: gate, { try await fetch(client) }), !d.isEmpty else { return nil }
                disk.write(d, key: key)
                return d
            }
            pending = PendingFetch(task)
            pendingBytes[key] = pending
        }
        pending.waiters += 1
        let data = await withTaskCancellationHandler {
            await pending.task.value
        } onCancel: {
            Task { @MainActor [weak self] in self?.dropWaiter(key, pending) }
        }
        if !Task.isCancelled {
            pending.waiters -= 1
            if pendingBytes[key] === pending { pendingBytes[key] = nil }
        }
        return data
    }

    private func dropWaiter(_ key: String, _ p: PendingFetch) {
        p.waiters -= 1
        guard p.waiters <= 0 else { return }
        p.task.cancel()
        if pendingBytes[key] === p { pendingBytes[key] = nil }
    }

    func thumbnailBytes(_ id: MediaID) async -> Data? {
        await bytes(key: thumbKey(id)) { try await $0.thumbnail(id) }
    }

    /// True when the thumbnail is on disk (shown offline).
    public func hasThumbnailOnDisk(_ id: MediaID) -> Bool { disk.contains(thumbKey(id)) }

    // ── thumbnails ──

    /// Best decoded thumbnail in memory (viewer resolution, else grid resolution).
    public func cachedThumbnail(_ id: MediaID) -> CGImage? { hit("t\(id)") ?? hit("g\(id)") }

    /// Grid-size decoded thumbnail in memory (the viewer's bigger one also serves).
    public func cachedGridThumbnail(_ id: MediaID) -> CGImage? { hit("g\(id)") ?? hit("t\(id)") }

    /// The grid's thumbnail: 640 px long edge, current edit applied.
    public func gridThumbnail(_ id: MediaID) async -> CGImage? {
        if let img = cachedGridThumbnail(id) { return img }
        return await decoded(id, key: "g\(id)", maxPixel: Self.gridPixel, edit: true)
    }

    /// The viewer's first pass: the desktop thumbnail at its full resolution, current edit applied.
    public func thumbnail(_ id: MediaID) async -> CGImage? {
        if let img = hit("t\(id)") { return img }
        return await decoded(id, key: "t\(id)", maxPixel: nil, edit: true)
    }

    /// The thumbnail as the desktop serves it (unedited, full resolution).
    public func rawThumbnail(_ id: MediaID) async -> CGImage? {
        if let img = hit("r\(id)") { return img }
        return await decoded(id, key: "r\(id)", maxPixel: nil, edit: false)
    }

    private func decoded(_ id: MediaID, key: String, maxPixel: Int?, edit: Bool) async -> CGImage? {
        guard let data = await thumbnailBytes(id), !Task.isCancelled else { return nil }
        if let img = hit(key) { return img }                 // decoded meanwhile by another caller
        let adj = edit ? store?.edit(for: id) : nil
        let img = await Task.detached(priority: .userInitiated) { () -> CGImage? in
            guard let raw = ImageLoader.decode(data, maxPixel: maxPixel) else { return nil }
            return ImageLoader.edited(raw, adj)
        }.value
        guard let img else { return nil }
        put(img, key)
        if edit, staleMicro.contains(id) { rebakeMicro(id, from: img) }
        return img
    }

    // ── faces ──

    /// A person's key-face crop, retried and cached on disk like the thumbnails.
    public func faceCrop(_ faceId: Int64, maxPixel: Int = 256) async -> CGImage? {
        let key = "f\(faceId)"
        if let img = hit(key) { return img }
        guard let data = await bytes(key: "f\(faceId).jpg", fetch: { try await $0.faceCrop(faceId) }),
              !Task.isCancelled else { return nil }
        let img = await Task.detached(priority: .userInitiated) { ImageLoader.decode(data, maxPixel: maxPixel) }.value
        if let img { put(img, key) }
        return img
    }

    // ── originals ──

    nonisolated static var originalsDirectory: URL {
        let base = (try? FileManager.default.url(for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil,
                                                 create: true)) ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("Kairos/originals", isDirectory: true)
    }

    /// Local file of the original (downloaded once, keyed by id + fingerprint so a changed file
    /// on the desktop is fetched again). Nil for videos, cloud-only originals and offline.
    public func originalFile(_ id: MediaID) async -> URL? {
        guard let item = store?.media(id), !item.isVideo else { return nil }
        let name = "\(id)-\(item.fingerprint ?? "x").\(item.fileExt?.trimmingCharacters(in: CharacterSet(charactersIn: ".")) ?? "img")"
        let dest = Self.originalsDirectory.appendingPathComponent(name)
        if FileManager.default.fileExists(atPath: dest.path) {
            try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: dest.path)
            return dest
        }
        if let running = originalTasks[id] { return await running.value }
        guard let client else { return nil }
        let task = Task<URL?, Never>.detached(priority: .userInitiated) {
            guard let tmp = try? await client.downloadOriginal(id) else { return nil }
            let fm = FileManager.default
            try? fm.createDirectory(at: ImageLoader.originalsDirectory, withIntermediateDirectories: true)
            try? fm.removeItem(at: dest)
            do { try fm.moveItem(at: tmp, to: dest) } catch { return nil }
            ImageLoader.pruneOriginals()
            return dest
        }
        originalTasks[id] = task
        let url = await task.value
        originalTasks[id] = nil
        return url
    }

    nonisolated static func pruneOriginals() {
        let fm = FileManager.default
        let keys: [URLResourceKey] = [.contentModificationDateKey]
        guard let files = try? fm.contentsOfDirectory(at: originalsDirectory, includingPropertiesForKeys: keys) else { return }
        let sorted = files.sorted {
            let a = (try? $0.resourceValues(forKeys: Set(keys)).contentModificationDate) ?? .distantPast
            let b = (try? $1.resourceValues(forKeys: Set(keys)).contentModificationDate) ?? .distantPast
            return a > b
        }
        for f in sorted.dropFirst(originalsKept) { try? fm.removeItem(at: f) }
    }

    /// Decoded original with the edit applied, plus the edited FULL-resolution pixel size (the
    /// viewer's content size, so 1:1 means one photo pixel per device pixel).
    public func original(_ id: MediaID, maxPixel: Int) async -> (image: CGImage, pixelSize: CGSize)? {
        guard let url = await originalFile(id) else { return nil }
        let adj = store?.edit(for: id)
        return await Task.detached(priority: .userInitiated) { () -> (image: CGImage, pixelSize: CGSize)? in
            guard let full = ImageLoader.pixelSize(url: url), let img = ImageLoader.decode(url: url, maxPixel: maxPixel)
            else { return nil }
            let a = adj?.normalized() ?? Adjustments()
            let (ow, oh) = EditMath.outputSize(width: Int(full.width), height: Int(full.height), a)
            return (ImageLoader.edited(img, adj), CGSize(width: ow, height: oh))
        }.value
    }

    /// The editor's working source: unedited, upright, at most `maxPixel` on the long edge. The
    /// original when it can be had, else the desktop thumbnail.
    public func editSource(_ id: MediaID, maxPixel: Int = 2048) async -> CGImage? {
        if let url = await originalFile(id) {
            let img = await Task.detached(priority: .userInitiated) { ImageLoader.decode(url: url, maxPixel: maxPixel) }.value
            if let img { return img }
        }
        return await rawThumbnail(id)
    }

    /// After an edit: drop the edited renderings and re-bake the LQIP from the next one.
    public func invalidate(_ ids: [MediaID]) {
        for id in ids {
            cache.removeObject(forKey: "t\(id)" as NSString)
            cache.removeObject(forKey: "g\(id)" as NSString)
            staleMicro.insert(id)
        }
    }
}

/// A grid cell: micro LQIP at once, the sharp thumbnail when it arrives. Scrolling the cell away
/// cancels its .task, which cancels the fetch if no other view wants the same photo.
public struct ThumbnailView: View {
    let id: MediaID
    @Environment(AppModel.self) private var model
    @State private var sharp: CGImage?

    public init(id: MediaID) { self.id = id }

    public var body: some View {
        GeometryReader { geo in
            ZStack {
                Kairos.Palette.placeholder
                let full = sharp ?? model.images.cachedGridThumbnail(id)
                if let img = full ?? model.images.micro(id) {
                    Image(decorative: img, scale: 1)
                        .resizable()
                        .interpolation(full == nil ? .low : .high)
                        .scaledToFill()
                        .frame(width: geo.size.width, height: geo.size.height)
                        .clipped()
                }
            }
        }
        .task(id: model.revision) {
            // No reset to nil first: every sync page bumps the revision, and the grid must not
            // flash back to the LQIP. An edited cell keeps its old pixels until the new ones land.
            // A failed fetch (desktop off, retries spent) is tried again on the next revision.
            if let img = await model.images.gridThumbnail(id) { sharp = img }
        }
    }
}
