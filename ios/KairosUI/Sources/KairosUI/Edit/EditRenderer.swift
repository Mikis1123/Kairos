import CoreGraphics
import KairosCore
import Observation
import SwiftUI

/// Live preview for the editor. Off the main thread, latest wins, one render in flight.
///
///   Adjust mode: geometry (straighten + crop) is rendered once per crop/angle and cached; each
///                slider tick re-runs only the tone LUT on that cached bitmap (EditPipeline.applyTone,
///                a single vImage lookup, about 1 ms on a 2048 px source). Fast enough to follow a
///                finger at 120 Hz.
///   Crop mode:   tone only, no geometry. CropStraightenView shows the straightened frame with view
///                transforms (rotation + the straighten zoom), so the dial moves at display rate
///                and the bitmap changes only when tone changes.
/// While a render runs, new requests overwrite `pending`. The renderer never queues stale
/// frames.
@MainActor
@Observable
final class EditRenderer {
    private(set) var source: CGImage?
    private(set) var preview: CGImage?          // full edit (adjust mode)
    private(set) var toned: CGImage?            // tone only (crop mode)
    private var geometryKey: [Double]?
    private var geometryImage: CGImage?
    private var running = false
    private var pending: Request?

    struct Request {
        let adj: Adjustments
        let cropMode: Bool
    }

    func setSource(_ image: CGImage) {
        source = image
        geometryKey = nil
        geometryImage = nil
        preview = nil
        toned = nil
    }

    func request(_ adj: Adjustments, cropMode: Bool) {
        pending = Request(adj: adj.normalized(), cropMode: cropMode)
        if !running { pump() }
    }

    private static func key(_ a: Adjustments) -> [Double] { a.crop + [a.angle] }

    private func pump() {
        guard let req = pending, let source else { return }
        pending = nil
        running = true
        let key = Self.key(req.adj)
        let cachedGeometry = geometryKey == key ? geometryImage : nil
        Task {
            let (geometry, out) = await Task.detached(priority: .userInitiated) { () -> (CGImage?, CGImage?) in
                #if canImport(Accelerate)
                if req.cropMode { return (nil, EditPipeline.applyTone(source, req.adj)) }
                let g = cachedGeometry ?? EditPipeline.renderGeometry(source, req.adj) ?? source
                return (g, EditPipeline.applyTone(g, req.adj))
                #else
                return (source, source)
                #endif
            }.value
            if req.cropMode {
                toned = out
            } else {
                if let geometry, cachedGeometry == nil {
                    geometryImage = geometry
                    geometryKey = key
                }
                preview = out
            }
            running = false
            if pending != nil { pump() }
        }
    }

    /// A small final rendering for the grid's LQIP (re-baked on save).
    nonisolated static func finalSmall(_ source: CGImage, _ adj: Adjustments, maxPixel: Int = 256) async -> CGImage? {
        await Task.detached(priority: .userInitiated) { () -> CGImage? in
            let k = min(1, Double(maxPixel) / Double(max(source.width, source.height)))
            #if canImport(Accelerate)
            return EditPipeline.render(source, adj, scale: k)
            #else
            return source
            #endif
        }.value
    }
}
