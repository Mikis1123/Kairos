import Foundation

/// Photo pins on the map: a 1:1 port of the desktop map's clustering and pin geometry
/// (map_vault.html: computeClusters, mercPx, the teardrop pin, pinHitDistance).
///
/// Clustering is a screen-space grid of CELL_PX cells at the camera zoom. Clusters whose pins
/// would overlap (closer than PIN_PX) are then merged, largest first, at their pixel centroid. Both
/// apps place the same photos under the same pins at the same zoom. MapLibre Native and
/// MapLibre GL JS share the 512 px world at z0, so `zoom` means the same thing on both.
/// Fixtures/pin_clusters_v1.json (test_ios_phase2_contract.py runs the desktop's JS in node)
/// pins this against the original.
public enum PinClustering {
    // map_vault.html constants (CSS px = iOS points)
    public static let cellPx = 64.0
    public static let pinPx = 58.0             // pins closer than this would overlap (52 + margin)
    public static let tilePx = 512.0
    public static let pinDiameter = 52.0       // PIN_D
    public static let pinPad = 14.0            // PIN_PAD: room for the stack + count badge
    public static let pinTail = 13.0           // PIN_TAIL: disc bottom -> tip
    public static var pinBox: Double { pinDiameter + 2 * pinPad }            // image width
    public static var pinHeight: Double { pinPad + pinDiameter + pinTail }   // image height; tip = bottom row
    public static var pinLift: Double { pinDiameter / 2 + pinTail }          // tip -> disc centre

    public struct Point: Hashable, Sendable {
        public let id: MediaID
        public let lat: Double
        public let lon: Double

        public init(id: MediaID, lat: Double, lon: Double) {
            self.id = id
            self.lat = lat
            self.lon = lon
        }
    }

    public struct Cluster: Hashable, Sendable {
        public var items: [Point]
        public var lat: Double
        public var lon: Double
        public var px: (x: Double, y: Double) { PinClustering.mercPx(lat, lon, zoom) }
        let zoom: Double

        /// The photo shown in the pin: the first item, as on the desktop.
        public var cover: MediaID { items[0].id }
        public var count: Int { items.count }

        public static func == (a: Cluster, b: Cluster) -> Bool { a.items == b.items && a.lat == b.lat && a.lon == b.lon }
        public func hash(into h: inout Hasher) { h.combine(items) }
    }

    /// Web Mercator world pixels at zoom `z` (512 px world at z0), as mercPx in map_vault.html.
    public static func mercPx(_ lat: Double, _ lon: Double, _ z: Double) -> (x: Double, y: Double) {
        let size = tilePx * pow(2, z)
        let s = max(-0.9999, min(0.9999, sin(lat * .pi / 180)))
        return ((lon + 180) / 360 * size, (0.5 - log((1 + s) / (1 - s)) / (4 * .pi)) * size)
    }

    /// computeClusters(zoom, list): grid cells, then merge overlapping pins (largest first).
    /// Input order matters for ties, exactly as in JS (stable sort, insertion-ordered cells).
    public static func clusters(_ points: [Point], zoom: Double) -> [Cluster] {
        struct Cell { var items: [Point]; var lat: Double; var lon: Double; var px: (x: Double, y: Double) }
        var order: [String] = []
        var cells: [String: Cell] = [:]
        for m in points {
            let p = mercPx(m.lat, m.lon, zoom)
            let key = "\(Int(floor(p.x / cellPx))):\(Int(floor(p.y / cellPx)))"
            if cells[key] == nil {
                cells[key] = Cell(items: [], lat: 0, lon: 0, px: (0, 0))
                order.append(key)
            }
            cells[key]!.items.append(m)
            cells[key]!.lat += m.lat
            cells[key]!.lon += m.lon
        }
        // JS object keys keep insertion order for these (non-integer) keys.
        var list: [Cell] = order.map { k in
            var c = cells[k]!
            c.lat /= Double(c.items.count)
            c.lon /= Double(c.items.count)
            c.px = mercPx(c.lat, c.lon, zoom)
            return c
        }
        // Array.prototype.sort is stable (ES2019); so is this enumerated sort.
        list = list.enumerated().sorted { a, b in
            a.element.items.count != b.element.items.count
                ? a.element.items.count > b.element.items.count : a.offset < b.offset
        }.map(\.element)
        var merged: [Cell] = []
        for c in list {
            var host: Int?
            for (i, m) in merged.enumerated() where dist(m.px, c.px) < pinPx {
                host = i
                break
            }
            guard let h = host else {
                merged.append(c)
                continue
            }
            let n = Double(merged[h].items.count), k = Double(c.items.count)
            merged[h].lat = (merged[h].lat * n + c.lat * k) / (n + k)
            merged[h].lon = (merged[h].lon * n + c.lon * k) / (n + k)
            merged[h].items += c.items
            merged[h].px = mercPx(merged[h].lat, merged[h].lon, zoom)
        }
        return merged.map { Cluster(items: $0.items, lat: $0.lat, lon: $0.lon, zoom: zoom) }
    }

    /// viewFilter in map_vault.html: from z4, only the visible area plus half a screen on each
    /// side is clustered (the whole globe below z4 or when 300° or more are visible). Clustering
    /// then costs the same at z17 as at z5, and at most a few hundred pin images exist.
    public static func viewFilter(_ points: [Point], zoom: Double, west w: Double, east e0: Double,
                                  south s: Double, north n: Double) -> [Point] {
        let e = e0 < w ? e0 + 360 : e0                   // a view across the antimeridian
        if zoom < 4 || e - w >= 300 { return points }
        let dx = (e - w) * 0.5, dy = (n - s) * 0.5
        return points.filter { m in
            var lon = m.lon
            while lon < w - dx { lon += 360 }
            while lon > e + dx { lon -= 360 }
            return m.lat >= s - dy && m.lat <= n + dy && lon >= w - dx && lon <= e + dx
        }
    }

    static func dist(_ a: (x: Double, y: Double), _ b: (x: Double, y: Double)) -> Double {
        ((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y)).squareRoot()
    }

    /// Teardrop hit test for a pin whose tip is at screen point `tip`: the disc (pinLift above
    /// the tip, +3 pt slop), or the tapering tail below it. Returns the distance to the disc
    /// centre (smaller = closer), or nil for a miss. Port of map_vault.html pinHitDistance.
    public static func hitDistance(tip: (x: Double, y: Double), point pt: (x: Double, y: Double)) -> Double? {
        let cy = tip.y - pinLift
        let d = ((tip.x - pt.x) * (tip.x - pt.x) + (cy - pt.y) * (cy - pt.y)).squareRoot()
        if d <= pinDiameter / 2 + 3 { return d }
        let below = pt.y - cy                       // 0 at the disc centre, pinLift at the tip
        if below > 0 && below <= pinLift + 2 {
            let half = (pinDiameter / 2) * max(0, 1 - below / pinLift) + 3
            if abs(pt.x - tip.x) <= half { return d }
        }
        return nil
    }

    /// Outline of the teardrop in the pin image's coordinates (y down): the disc plus the two
    /// tangents that meet at the tip (bottom centre). Returns the tangent angles for the arc,
    /// as teardropPath in map_vault.html.
    public static func teardropTangents(centreY cy: Double, radius r: Double, tipY: Double) -> (a1: Double, a2: Double) {
        let d = tipY - cy
        let th = acos(min(1, r / d))
        return (.pi / 2 - th, .pi / 2 + th)
    }
}
