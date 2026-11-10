import Foundation

/// Trip mode without the desktop: a journey rebuilt from the replica's photo positions, so the
/// route still draws offline before the desktop's /v1/journey answer was ever cached. Same idea
/// as the desktop's journey.build_stops: consecutive photos within `radiusKm` form one stop,
/// stops are in capture order, and a leg runs from where one stop was left to where the next
/// was reached. The radius grows until the trip has at most `maxStops` stops.
public enum JourneyLocal {
    public struct Photo: Sendable, Hashable {
        public let id: MediaID
        public let lat: Double
        public let lon: Double
        public let date: String          // desktop date_taken, any of its formats
        public let city: String?

        public init(id: MediaID, lat: Double, lon: Double, date: String, city: String?) {
            self.id = id
            self.lat = lat
            self.lon = lon
            self.date = date
            self.city = city
        }
    }

    public static let radiiKm: [Double] = [0.5, 1.5, 4, 10, 25, 60]
    public static let maxStops = 40
    static let photosPerStop = 8

    public static func haversineKm(_ a: (Double, Double), _ b: (Double, Double)) -> Double {
        let r = 6371.0088, d2r = Double.pi / 180
        let dLat = (b.0 - a.0) * d2r, dLon = (b.1 - a.1) * d2r
        let h = sin(dLat / 2) * sin(dLat / 2) + cos(a.0 * d2r) * cos(b.0 * d2r) * sin(dLon / 2) * sin(dLon / 2)
        return 2 * r * asin(min(1, sqrt(h)))
    }

    /// "2026:07:01 10:00:00" -> "2026-07-01 10:00" (the wire format of Journey times).
    public static func minute(_ date: String) -> String {
        var s = String(date.prefix(16))
        if s.count >= 10, Array(s)[4] == ":" {
            s = s.replacingOccurrences(of: ":", with: "-", options: [], range: s.startIndex..<s.index(s.startIndex, offsetBy: 10))
        }
        return s.replacingOccurrences(of: "T", with: " ")
    }

    static func group(_ photos: [Photo], radiusKm: Double) -> [[Photo]] {
        var out: [[Photo]] = []
        var cLat = 0.0, cLon = 0.0
        for p in photos {
            if var g = out.last, haversineKm((cLat, cLon), (p.lat, p.lon)) <= radiusKm {
                g.append(p)
                out[out.count - 1] = g
                let n = Double(g.count)
                cLat += (p.lat - cLat) / n
                cLon += (p.lon - cLon) / n
            } else {
                out.append([p])
                cLat = p.lat
                cLon = p.lon
            }
        }
        return out
    }

    public static func stops(_ photos: [Photo]) -> [Journey.Stop] {
        let sorted = photos.filter { abs($0.lat) <= 90 && abs($0.lon) <= 180 && !($0.lat == 0 && $0.lon == 0) }
            .sorted { (minute($0.date), $0.id) < (minute($1.date), $1.id) }
        guard !sorted.isEmpty else { return [] }
        var groups: [[Photo]] = []
        for r in radiiKm {
            groups = group(sorted, radiusKm: r)
            if groups.count <= maxStops { break }
        }
        return groups.map { g in
            let lat = g.map(\.lat).reduce(0, +) / Double(g.count)
            let lon = g.map(\.lon).reduce(0, +) / Double(g.count)
            var cities: [String: Int] = [:]
            for p in g { if let c = p.city, !c.isEmpty { cities[c, default: 0] += 1 } }
            let city = cities.max { ($0.value, $1.key) < ($1.value, $0.key) }?.key ?? ""
            let step = max(1, g.count / photosPerStop)
            let picks = stride(from: 0, to: g.count, by: step).prefix(photosPerStop).map { g[$0].id }
            return Journey.Stop(lat: lat, lon: lon, start: minute(g[0].date), end: minute(g[g.count - 1].date),
                                city: city, count: g.count,
                                entry: .init(lat: g[0].lat, lon: g[0].lon),
                                exit: .init(lat: g[g.count - 1].lat, lon: g[g.count - 1].lon),
                                best: g[g.count / 2].id, photos: picks)
        }
    }

    /// The whole journey, with day chapters when it spans two days or more.
    public static func journey(id: Int64, title: String, location: String, photos: [Photo]) -> Journey {
        let all = stops(photos)
        var days: [Journey.Day] = []
        let byDay = Dictionary(grouping: photos) { String(minute($0.date).prefix(10)) }
        if byDay.count >= 2 {
            for (n, key) in byDay.keys.sorted().enumerated() {
                let dayStops = stops(byDay[key] ?? [])
                let first = all.firstIndex { ($0.end ?? "") >= (dayStops.first?.start ?? "") } ?? 0
                days.append(Journey.Day(n: n + 1, date: key, start: dayStops.first?.start, end: dayStops.last?.end,
                                        count: byDay[key]?.count ?? 0, city: dayStops.first?.city ?? "",
                                        first: first, stops: dayStops, tracks: [:]))
            }
        }
        return Journey(id: id, title: title, location: location, start: all.first?.start, end: all.last?.end,
                       count: all.reduce(0) { $0 + $1.count }, stops: all, tracks: [:], days: days)
    }
}
