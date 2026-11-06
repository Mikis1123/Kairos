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
///   thumb:    the desktop's 840 px thumbnail (unedited source) plus the edit applied here
///             (EditPipeline), cached in memory per id.
///   raw:      the same thumbnail WITHOUT the edit (the editor's fallback source).
///   original: streamed on demand into Caches/Kairos/originals (the newest 12 files are kept), then
///             decoded at the size the caller asks for: the viewer's screen pass and 1:1 detail
///             pass, or the editor's 2048 px working source.
@MainActor
public final class ImageLoader {
    var store: LibraryStore?
    var client: SyncClient?
    private let cache = NSCache<NSString, CGImageBox>()
    private var inflight: [MediaID: Task<CGImage?, Never>] = [:]
    private var staleMicro: Set<MediaID> = []
    private var originalTasks: [MediaID: Task<URL?, Never>] = [:]

    nonisolated static let microPixel = 48
    nonisolated static let originalsKept = 12

    final class CGImageBox {
        let image: CGImage
        init(_ i: CGImage) { image = i }
    }

    init() { cache.countLimit = 600 }

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
        // the desktop's (its edits are defined on the oriented image).
        var opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                     kCGImageSourceCreateThumbnailWithTransform: true,
                                     kCGImageSourceShouldCacheImmediately: true]
        if let maxPixel { opts[kCGImageSourceThumbnailMaxPixelSize] = maxPixel }
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
        let key = "m\(id)" as NSString
        if let hit = cache.object(forKey: key) { return hit.image }
        guard let data = store?.micro(id), let img = Self.decode(data) else { return nil }
        cache.setObject(CGImageBox(img), forKey: key)
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
        if let small = Self.downscaled(edited, maxPixel: Self.microPixel) {
            cache.setObject(CGImageBox(small), forKey: "m\(id)" as NSString)
        }
    }

    // ── thumbnails ──

    public func cachedThumbnail(_ id: MediaID) -> CGImage? {
        cache.object(forKey: "t\(id)" as NSString)?.image
    }

    /// The 840 px desktop thumbnail with the current edit applied.
    public func thumbnail(_ id: MediaID) async -> CGImage? {
        if let hit = cachedThumbnail(id) { return hit }
        if let running = inflight[id] { return await running.value }
        guard client != nil || cache.object(forKey: "r\(id)" as NSString) != nil else { return nil }
        let adj = store?.edit(for: id)
        let task = Task<CGImage?, Never> { [weak self] in
            guard let raw = await self?.rawThumbnail(id) else { return nil }
            return await Task.detached(priority: .userInitiated) { ImageLoader.edited(raw, adj) }.value
        }
        inflight[id] = task
        let img = await task.value
        inflight[id] = nil
        if let img {
            cache.setObject(CGImageBox(img), forKey: "t\(id)" as NSString)
            if staleMicro.contains(id) { rebakeMicro(id, from: img) }
        }
        return img
    }

    /// The thumbnail as the desktop serves it (unedited).
    public func rawThumbnail(_ id: MediaID) async -> CGImage? {
        let key = "r\(id)" as NSString
        if let hit = cache.object(forKey: key) { return hit.image }
        guard let client else { return nil }
        let img = await Task.detached(priority: .userInitiated) { () -> CGImage? in
            guard let data = try? await client.thumbnail(id) else { return nil }
            return ImageLoader.decode(data, maxPixel: 840)
        }.value
        if let img { cache.setObject(CGImageBox(img), forKey: key) }
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
            staleMicro.insert(id)
        }
    }
}

/// A grid cell: micro LQIP at once, the sharp thumbnail when it arrives.
public struct ThumbnailView: View {
    let id: MediaID
    @Environment(AppModel.self) private var model
    @State private var sharp: CGImage?

    public init(id: MediaID) { self.id = id }

    public var body: some View {
        GeometryReader { geo in
            ZStack {
                Kairos.Palette.placeholder
                if let img = sharp ?? model.images.cachedThumbnail(id) ?? model.images.micro(id) {
                    Image(decorative: img, scale: 1)
                        .resizable()
                        .interpolation(sharp == nil ? .low : .high)
                        .scaledToFill()
                        .frame(width: geo.size.width, height: geo.size.height)
                        .clipped()
                }
            }
        }
        .task(id: model.revision) {
            // No reset to nil first: every sync page bumps the revision, and the grid must not
            // flash back to the LQIP. An edited cell keeps its old pixels until the new ones land.
            if let img = await model.images.thumbnail(id) { sharp = img }
        }
    }
}
