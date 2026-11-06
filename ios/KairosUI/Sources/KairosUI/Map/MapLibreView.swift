import Foundation
import KairosCore
import SwiftUI

/// Offline style over the installed packs. Vector packs (OpenMapTiles schema, the desktop's
/// "pbf" packs) get a graphite rendition of the desktop's dark map. Raster packs (topo, satellite)
/// are drawn as raster layers underneath. Labels need offline glyphs (a font bundle) and come with
/// the Phase 2 style port.
enum OfflineMapStyle {
    static func packFormat(_ url: URL) -> String {
        guard let db = try? SQLiteDatabase(path: url.path, readOnly: true),
              let row = try? db.query("SELECT value FROM metadata WHERE name = 'format'").first else { return "pbf" }
        return (row.string("value") ?? "pbf").lowercased()
    }

    static func json(packs: [URL]) -> [String: Any] {
        var sources: [String: Any] = [:]
        var layers: [[String: Any]] = [["id": "background", "type": "background",
                                        "paint": ["background-color": "#06070C"]]]
        for (i, url) in packs.enumerated() {
            let id = "pack\(i)"
            let tileURL = "mbtiles://" + url.path
            if packFormat(url) == "pbf" {
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

    /// Writes the style next to the packs and returns its file URL (MapLibre loads styles by URL).
    static func write(packs: [URL]) -> URL? {
        let url = OfflinePacks.directory.appendingPathComponent("kairos-offline-style.json")
        do {
            try FileManager.default.createDirectory(at: OfflinePacks.directory, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: json(packs: packs))
            try data.write(to: url, options: .atomic)
            return url
        } catch {
            return nil
        }
    }
}

#if canImport(MapLibre) && os(iOS)
import CoreLocation
import MapLibre
import UIKit

struct MapLibreView: UIViewRepresentable {
    let packs: [URL]
    let points: [LibraryStore.GeoPoint]

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MLNMapView {
        let map = MLNMapView(frame: .zero, styleURL: OfflineMapStyle.write(packs: packs))
        map.backgroundColor = UIColor(red: 6 / 255, green: 7 / 255, blue: 12 / 255, alpha: 1)
        map.delegate = context.coordinator
        map.logoView.isHidden = true
        map.attributionButton.tintColor = .systemGray
        map.setCenter(CLLocationCoordinate2D(latitude: 49.8, longitude: 15.5), zoomLevel: 5, animated: false)
        context.coordinator.packs = packs
        context.coordinator.points = points
        return map
    }

    func updateUIView(_ map: MLNMapView, context: Context) {
        let c = context.coordinator
        if c.packs != packs {
            c.packs = packs
            map.styleURL = OfflineMapStyle.write(packs: packs)      // pins are re-added on load
        }
        if c.points != points {
            c.points = points
            c.updatePins(on: map)
        }
    }

    final class Coordinator: NSObject, MLNMapViewDelegate {
        var packs: [URL] = []
        var points: [LibraryStore.GeoPoint] = []
        private var didFit = false

        func mapView(_ mapView: MLNMapView, didFinishLoading style: MLNStyle) {
            let source = MLNShapeSource(identifier: "photos", features: [],
                                        options: [.clustered: true, .clusterRadius: 44])
            style.addSource(source)
            let dots = MLNCircleStyleLayer(identifier: "photo-dots", source: source)
            dots.circleColor = NSExpression(forConstantValue: UIColor(red: 0x8B / 255, green: 0x93 / 255,
                                                                      blue: 1, alpha: 1))
            dots.circleRadius = NSExpression(format: "TERNARY(cluster == YES, 11, 5)")
            dots.circleStrokeColor = NSExpression(forConstantValue: UIColor.white)
            dots.circleStrokeWidth = NSExpression(forConstantValue: 1.5)
            style.addLayer(dots)
            updatePins(on: mapView)
        }

        func updatePins(on map: MLNMapView) {
            guard let source = map.style?.source(withIdentifier: "photos") as? MLNShapeSource else { return }
            let features: [MLNPointFeature] = points.map { p in
                let f = MLNPointFeature()
                f.coordinate = CLLocationCoordinate2D(latitude: p.latitude, longitude: p.longitude)
                f.attributes = ["id": p.id]
                return f
            }
            source.shape = MLNShapeCollectionFeature(shapes: features)
            if !didFit, let first = points.first {
                didFit = true
                var sw = CLLocationCoordinate2D(latitude: first.latitude, longitude: first.longitude), ne = sw
                for p in points {
                    sw.latitude = min(sw.latitude, p.latitude); sw.longitude = min(sw.longitude, p.longitude)
                    ne.latitude = max(ne.latitude, p.latitude); ne.longitude = max(ne.longitude, p.longitude)
                }
                map.setVisibleCoordinateBounds(MLNCoordinateBounds(sw: sw, ne: ne),
                                               edgePadding: UIEdgeInsets(top: 60, left: 40, bottom: 60, right: 40),
                                               animated: false, completionHandler: nil)
            }
        }
    }
}
#else
/// Builds without MapLibre (macOS `swift build`): a placeholder so the shell still compiles.
struct MapLibreView: View {
    let packs: [URL]
    let points: [LibraryStore.GeoPoint]

    var body: some View {
        Kairos.Palette.mapBackground
            .overlay(Text("\(points.count) photos with location").foregroundStyle(Kairos.Palette.textSecondary))
    }
}
#endif
