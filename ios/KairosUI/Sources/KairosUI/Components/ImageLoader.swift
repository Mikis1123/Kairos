import CoreGraphics
import Foundation
import ImageIO
import KairosCore
import SwiftUI

/// Pixels for the grid and the viewer.
///   micro: the 48 px LQIP from the replica, decoded synchronously (~30 µs). The desktop already
///          baked the edit into it.
///   thumb: the desktop's 840 px thumbnail (unedited source) plus the edit applied here
///          (EditPipeline), cached in memory per (id, edit).
/// Originals stream on demand in the viewer.
@MainActor
public final class ImageLoader {
    var store: LibraryStore?
    var client: SyncClient?
    private let cache = NSCache<NSString, CGImageBox>()
    private var inflight: [MediaID: Task<CGImage?, Never>] = [:]

    final class CGImageBox {
        let image: CGImage
        init(_ i: CGImage) { image = i }
    }

    init() { cache.countLimit = 600 }

    nonisolated static func decode(_ data: Data, maxPixel: Int? = nil) -> CGImage? {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        if let maxPixel {
            let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                         kCGImageSourceCreateThumbnailWithTransform: true,
                                         kCGImageSourceThumbnailMaxPixelSize: maxPixel]
            return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
        }
        let opts: [CFString: Any] = [kCGImageSourceShouldCacheImmediately: true]
        return CGImageSourceCreateImageAtIndex(src, 0, opts as CFDictionary)
    }

    public func micro(_ id: MediaID) -> CGImage? {
        let key = "m\(id)" as NSString
        if let hit = cache.object(forKey: key) { return hit.image }
        guard let data = store?.micro(id), let img = Self.decode(data) else { return nil }
        cache.setObject(CGImageBox(img), forKey: key)
        return img
    }

    public func cachedThumbnail(_ id: MediaID) -> CGImage? {
        cache.object(forKey: "t\(id)" as NSString)?.image
    }

    public func thumbnail(_ id: MediaID) async -> CGImage? {
        if let hit = cachedThumbnail(id) { return hit }
        if let running = inflight[id] { return await running.value }
        guard let client else { return nil }
        let adj = store?.edit(for: id)
        let task = Task<CGImage?, Never>.detached(priority: .userInitiated) {
            guard let data = try? await client.thumbnail(id), let img = ImageLoader.decode(data, maxPixel: 840)
            else { return nil }
            guard let adj, !adj.isIdentity else { return img }
            #if canImport(Accelerate)
            return EditPipeline.render(img, adj) ?? img
            #else
            return img
            #endif
        }
        inflight[id] = task
        let img = await task.value
        inflight[id] = nil
        if let img { cache.setObject(CGImageBox(img), forKey: "t\(id)" as NSString) }
        return img
    }

    public func invalidate(_ ids: [MediaID]) {
        for id in ids {
            cache.removeObject(forKey: "t\(id)" as NSString)
            cache.removeObject(forKey: "m\(id)" as NSString)
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
        .task(id: model.revision) { sharp = await model.images.thumbnail(id) }
    }
}
