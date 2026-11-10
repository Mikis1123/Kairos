import Foundation
import KairosCore
import SwiftUI

/// Offline style over the installed packs. Vector packs (OpenMapTiles schema, the desktop's
/// "pbf" packs) get a graphite rendition of the desktop's dark map. Raster packs (topo, satellite)
/// are drawn as raster layers underneath. The style has no text layers, so it needs no glyph
/// server. Pin counts are drawn into the pin images, as on the desktop.
enum OfflineMapStyle {
    struct PackInfo {
        let format: String
        let bounds: (w: Double, s: Double, e: Double, n: Double)?
    }

    static func info(_ url: URL) -> PackInfo {
        guard let db = try? SQLiteDatabase(path: url.path, readOnly: true) else { return PackInfo(format: "pbf", bounds: nil) }
        var meta: [String: String] = [:]
        for row in (try? db.query("SELECT name, value FROM metadata")) ?? [] {
            if let k = row.string("name"), let v = row.string("value") { meta[k.lowercased()] = v }
        }
        var bounds: (Double, Double, Double, Double)?
        let parts = (meta["bounds"] ?? "").split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
        if parts.count == 4, parts[2] > parts[0], parts[3] > parts[1] { bounds = (parts[0], parts[1], parts[2], parts[3]) }
        return PackInfo(format: (meta["format"] ?? "pbf").lowercased(), bounds: bounds)
    }

    static func json(packs: [URL]) -> [String: Any] {
        var sources: [String: Any] = [:]
        var layers: [[String: Any]] = [["id": "background", "type": "background",
                                        "paint": ["background-color": "#06070C"]]]
        for (i, url) in packs.enumerated() {
            let id = "pack\(i)"
            let tileURL = "mbtiles://" + OfflinePacks.tileURLPath(url)
            let firstNew = layers.count
            defer {
                // the bundled world (z0-4) is too coarse to draw past z10: under a region pack its
                // overzoomed coastline would show through, and alone it would only be blocky
                if url == OfflinePacks.bundledWorld {
                    for j in firstNew..<layers.count { layers[j]["maxzoom"] = 10 }
                }
            }
            if info(url).format == "pbf" {
                sources[id] = ["type": "vector", "url": tileURL]
                layers += [
                    ["id": "\(id)-landcover", "type": "fill", "source": id, "source-layer": "landcover",
                     "paint": ["fill-color": "#0B0D12", "fill-opacity": 0.9]],
                    ["id": "\(id)-water", "type": "fill", "source": id, "source-layer": "water",
                     "paint": ["fill-color": "#0F1626"]],
                    ["id": "\(id)-building", "type": "fill", "source": id, "source-layer": "building", "minzoom": 13,
                     "paint": ["fill-color": "#141417"]],
                    ["id": "\(id)-roads", "type": "line", "source": id, "source-layer": "transportation",
                     "paint": ["line-color": "#2E2E35",
                               "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], 5, 0.4, 14, 3]]],
                    ["id": "\(id)-waterway", "type": "line", "source": id, "source-layer": "waterway",
                     "paint": ["line-color": "#14203A", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.3, 12, 1.6]]],
                    ["id": "\(id)-boundary", "type": "line", "source": id, "source-layer": "boundary",
                     "paint": ["line-color": "#3A3A42", "line-dasharray": [2, 2], "line-width": 0.8]],
                ]
            } else {
                sources[id] = ["type": "raster", "url": tileURL, "tileSize": 256]
                layers.insert(["id": "\(id)-raster", "type": "raster", "source": id,
                               "paint": ["raster-saturation": -0.35, "raster-brightness-max": 0.75]], at: 1)
            }
        }
        return ["version": 8, "name": "Kairos Offline", "sources": sources, "layers": layers]
    }

    /// Writes the style to Caches (not the user-visible Documents) and returns its file URL.
    static func write(packs: [URL]) -> URL? {
        let dir = OfflinePacks.cacheDirectory
        let url = dir.appendingPathComponent("kairos-offline-style.json")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: json(packs: packs))
            try data.write(to: url, options: .atomic)
            return url
        } catch {
            return nil
        }
    }

    static func unionBounds(_ packs: [URL]) -> (w: Double, s: Double, e: Double, n: Double)? {
        packs.compactMap { info($0).bounds }.reduce(nil) { acc, b in
            guard let a = acc else { return b }
            return (min(a.w, b.w), min(a.s, b.s), max(a.e, b.e), max(a.n, b.n))
        }
    }
}

#if canImport(MapLibre) && os(iOS)
import CoreLocation
import MapLibre
import UIKit

/// MapLibre Native over the offline packs, with the desktop's photo pins: the same clustering
/// (PinClustering: grid + overlap merge + view filter), the same teardrop images with the photo
/// and the count badge (PinImageRenderer), the same tip-anchored symbol layer, and the same
/// teardrop hit shape for taps. A single pin opens the viewer. A cluster flies to its photos, and
/// once they cannot spread further, it opens them in the viewer.
struct MapLibreView: UIViewRepresentable {
    let packs: [URL]
    let points: [LibraryStore.GeoPoint]
    let model: AppModel
    var onOpen: (ViewerRoute) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> MLNMapView {
        let map = MLNMapView(frame: .zero, styleURL: OfflineMapStyle.write(packs: packs))
        map.backgroundColor = UIColor(red: 6 / 255, green: 7 / 255, blue: 12 / 255, alpha: 1)
        map.delegate = context.coordinator
        map.logoView.isHidden = true
        map.attributionButton.tintColor = .systemGray
        map.setCenter(CLLocationCoordinate2D(latitude: 49.8, longitude: 15.5), zoomLevel: 5, animated: false)
        let c = context.coordinator
        c.map = map
        c.packs = packs
        c.setPoints(points)
        let tap = UITapGestureRecognizer(target: c, action: #selector(Coordinator.tapped(_:)))
        for case let r as UITapGestureRecognizer in map.gestureRecognizers ?? [] where r.numberOfTapsRequired == 2 {
            tap.require(toFail: r)                     // double-tap stays MapLibre's zoom
        }
        map.addGestureRecognizer(tap)
        return map
    }

    func updateUIView(_ map: MLNMapView, context: Context) {
        let c = context.coordinator
        c.parent = self
        if c.packs != packs {
            c.packs = packs
            map.styleURL = OfflineMapStyle.write(packs: packs)      // pins are re-added on load
        }
        if c.sourcePoints != points {
            c.setPoints(points)
            c.recluster(force: true)
        }
    }

    @MainActor
    final class Coordinator: NSObject, MLNMapViewDelegate {
        var parent: MapLibreView
        weak var map: MLNMapView?
        var packs: [URL] = []
        private(set) var sourcePoints: [LibraryStore.GeoPoint] = []
        private var points: [PinClustering.Point] = []
        private var clusters: [PinClustering.Cluster] = []
        private var shownKeys: [String] = []
        private var generation = 0
        private var didFit = false
        // pin images in the current style: key -> (quality, last use); 500 at most, like the desktop
        private var images: [String: (quality: Int, used: Int)] = [:]
        private var useClock = 0
        private var thumbRequests: Set<MediaID> = []
        static let imageBudget = 500
        static let sourceId = "pins", layerId = "pins"

        init(_ parent: MapLibreView) { self.parent = parent }

        func setPoints(_ p: [LibraryStore.GeoPoint]) {
            sourcePoints = p
            points = p.map { PinClustering.Point(id: $0.id, lat: $0.latitude, lon: $0.longitude) }
        }

        // ── style ──

        // MapLibre calls its delegate on the main thread, but MLNMapViewDelegate is not
        // main-actor annotated: hop explicitly (checked at runtime).
        nonisolated func mapView(_ mapView: MLNMapView, didFinishLoading style: MLNStyle) {
            MainActor.assumeIsolated { self.styleLoaded(mapView, style) }
        }

        nonisolated func mapView(_ mapView: MLNMapView, regionDidChangeAnimated animated: Bool) {
            MainActor.assumeIsolated { self.recluster() }
        }

        private func styleLoaded(_ mapView: MLNMapView, _ style: MLNStyle) {
            images.removeAll()                         // a new style starts without our images
            shownKeys = []
            let source = MLNShapeSource(identifier: Self.sourceId, features: [], options: nil)
            style.addSource(source)
            let pins = MLNSymbolStyleLayer(identifier: Self.layerId, source: source)
            pins.iconImageName = NSExpression(forKeyPath: "img")
            pins.iconAnchor = NSExpression(forConstantValue: "bottom")          // the tip is the coordinate
            pins.iconAllowsOverlap = NSExpression(forConstantValue: true)
            pins.iconIgnoresPlacement = NSExpression(forConstantValue: true)
            pins.iconPitchAlignment = NSExpression(forConstantValue: "viewport")
            pins.iconRotationAlignment = NSExpression(forConstantValue: "viewport")
            pins.symbolSortKey = NSExpression(forKeyPath: "count")             // big clusters on top
            style.addLayer(pins)
            fitIfNeeded(mapView)
            recluster(force: true)
        }

        private func fitIfNeeded(_ map: MLNMapView) {
            guard !didFit else { return }
            var b: (w: Double, s: Double, e: Double, n: Double)?
            if let first = points.first {
                var w = first.lon, e = first.lon, s = first.lat, n = first.lat
                for p in points {
                    w = min(w, p.lon); e = max(e, p.lon); s = min(s, p.lat); n = max(n, p.lat)
                }
                b = (w, s, e, n)
            } else {
                b = OfflineMapStyle.unionBounds(packs.filter { $0 != OfflinePacks.bundledWorld })
            }
            guard let b else { return }
            didFit = true
            map.setVisibleCoordinateBounds(MLNCoordinateBounds(sw: CLLocationCoordinate2D(latitude: b.s, longitude: b.w),
                                                               ne: CLLocationCoordinate2D(latitude: b.n, longitude: b.e)),
                                           edgePadding: UIEdgeInsets(top: 80, left: 40, bottom: 80, right: 40),
                                           animated: false, completionHandler: nil)
        }

        // ── clustering (at camera rest, off the main thread, applied only when it changed) ──

        func recluster(force: Bool = false) {
            guard let map, map.style?.source(withIdentifier: Self.sourceId) != nil else { return }
            if !didFit { fitIfNeeded(map) }
            generation += 1
            let gen = generation, z = map.zoomLevel, b = map.visibleCoordinateBounds, all = points
            Task {
                let next = await Task.detached(priority: .userInitiated) { () -> [PinClustering.Cluster] in
                    let list = PinClustering.viewFilter(all, zoom: z, west: b.sw.longitude, east: b.ne.longitude,
                                                        south: b.sw.latitude, north: b.ne.latitude)
                    return PinClustering.clusters(list, zoom: z)
                }.value
                guard gen == self.generation else { return }      // the camera moved on
                self.apply(next, force: force)
            }
        }

        private static func key(_ c: PinClustering.Cluster) -> String { "pin:\(c.cover)|\(c.count)" }

        private func apply(_ next: [PinClustering.Cluster], force: Bool) {
            guard let map, let style = map.style,
                  let source = style.source(withIdentifier: Self.sourceId) as? MLNShapeSource else { return }
            let keys = next.map { "\(Self.key($0))@\(($0.lat * 1e6).rounded()),\(($0.lon * 1e6).rounded())" }
            clusters = next
            for c in next { ensureImage(c, style) }
            if force || keys != shownKeys {             // same pins in the same places: no re-tiling
                shownKeys = keys
                source.shape = MLNShapeCollectionFeature(shapes: next.map { c in
                    let f = MLNPointFeature()
                    f.coordinate = CLLocationCoordinate2D(latitude: c.lat, longitude: c.lon)
                    f.attributes = ["img": Self.key(c), "count": c.count]
                    return f
                })
            }
            pruneImages(style, keep: Set(next.map(Self.key)))
        }

        // ── pin images ──

        private func ensureImage(_ c: PinClustering.Cluster, _ style: MLNStyle) {
            let key = Self.key(c)
            useClock += 1
            let thumb = parent.model.images.cachedThumbnail(c.cover)
            let photo = thumb ?? parent.model.images.micro(c.cover)
            let quality = thumb != nil ? 2 : (photo != nil ? 1 : 0)
            if let have = images[key], have.quality >= quality {
                images[key] = (have.quality, useClock)
            } else {
                let scale = min(3, map?.traitCollection.displayScale ?? 3)
                style.setImage(PinImageRenderer.draw(photo: photo, count: c.count, accent: UIColor(Kairos.Palette.accent),
                                                     scale: scale), forName: key)
                images[key] = (quality, useClock)
            }
            if quality < 2 && !thumbRequests.contains(c.cover) {
                thumbRequests.insert(c.cover)
                let cover = c.cover
                Task { [weak self] in
                    guard let self else { return }
                    _ = await self.parent.model.images.gridThumbnail(cover)   // the 48 px micro is soft at 52 pt
                    self.thumbRequests.remove(cover)
                    self.refreshImages(for: cover)
                }
            }
        }

        private func refreshImages(for cover: MediaID) {
            guard let style = map?.style else { return }
            for c in clusters where c.cover == cover { ensureImage(c, style) }
        }

        private func pruneImages(_ style: MLNStyle, keep: Set<String>) {
            guard images.count > Self.imageBudget else { return }
            let drop = images.filter { !keep.contains($0.key) }.sorted { $0.value.used < $1.value.used }
                .prefix(images.count - Self.imageBudget)
            for (k, _) in drop {
                style.removeImage(forName: k)
                images[k] = nil
            }
        }

        // ── taps: the teardrop hit shape ──

        @objc func tapped(_ g: UITapGestureRecognizer) {
            guard let map, g.state == .ended else { return }
            let pt = g.location(in: map)
            var best: (PinClustering.Cluster, Double)?
            for c in clusters {
                let tip = map.convert(CLLocationCoordinate2D(latitude: c.lat, longitude: c.lon), toPointTo: map)
                if let d = PinClustering.hitDistance(tip: (Double(tip.x), Double(tip.y)), point: (Double(pt.x), Double(pt.y))),
                   d < (best?.1 ?? .infinity) {
                    best = (c, d)
                }
            }
            guard let c = best?.0 else { return }
            Haptics.tick()
            let ids = c.items.map(\.id)
            var w = c.items[0].lon, e = w, s = c.items[0].lat, n = s
            for p in c.items { w = min(w, p.lon); e = max(e, p.lon); s = min(s, p.lat); n = max(n, p.lat) }
            let spread = max(e - w, n - s)
            if c.count == 1 || map.zoomLevel >= map.maximumZoomLevel - 0.5 || spread < 1e-5 {
                parent.onOpen(ViewerRoute(ids: ids, start: ids[0]))
                return
            }
            map.setVisibleCoordinateBounds(MLNCoordinateBounds(sw: CLLocationCoordinate2D(latitude: s, longitude: w),
                                                               ne: CLLocationCoordinate2D(latitude: n, longitude: e)),
                                           edgePadding: UIEdgeInsets(top: 120, left: 80, bottom: 120, right: 80),
                                           animated: true, completionHandler: nil)
        }
    }
}
#else
/// Builds without MapLibre (macOS `swift build`): a placeholder so the shell still compiles.
struct MapLibreView: View {
    let packs: [URL]
    let points: [LibraryStore.GeoPoint]
    let model: AppModel
    var onOpen: (ViewerRoute) -> Void

    var body: some View {
        Kairos.Palette.mapBackground
            .overlay(Text("\(points.count) photos with location").foregroundStyle(Kairos.Palette.textSecondary))
    }
}
#endif
