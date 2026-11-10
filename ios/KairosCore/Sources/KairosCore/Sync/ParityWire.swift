import Foundation

// Parity sprint (2026-11-10) additions to protocol 1. All of them are additive: an older desktop
// answers 404 and the phone degrades (offline search, local matching, a journey built from the
// replica). /v1/hello lists them in `features`.

/// /v1/search: the desktop's semantic index (CLIP / SigLIP + facets + OCR) ranks the library.
/// Embeddings never leave the PC; only ids come back, best first.
public struct SearchAnswer: Decodable, Sendable, Equatable {
    public let query: String
    public let ids: [MediaID]
    public let confident: Int          // the first `confident` ids are sure matches
    public let semantic: Bool          // the image model ranked them (not just facets)
    public let labels: [String]        // recognised facets ("Kasperske Hory", "videos")

    public init(query: String, ids: [MediaID], confident: Int, semantic: Bool, labels: [String]) {
        self.query = query
        self.ids = ids
        self.confident = confident
        self.semantic = semantic
        self.labels = labels
    }
}

/// One phone asset offered to /v1/match: proof by content (SHA-256 of the original resource).
public struct MatchOffer: Encodable, Sendable, Hashable {
    public let sha256: String
    public let size: Int64
    public let name: String

    public init(sha256: String, size: Int64, name: String) {
        self.sha256 = sha256
        self.size = size
        self.name = name
    }
}

/// Trip mode (/v1/journey/{event_id}): the desktop's journey as the Cesta film uses it, without
/// paths or URLs. Coordinates are WGS84 degrees, times are local wall-clock "YYYY-MM-DD HH:MM".
public struct Journey: Codable, Sendable, Equatable {
    public struct Point: Codable, Sendable, Equatable {
        public let lat: Double
        public let lon: Double

        public init(lat: Double, lon: Double) {
            self.lat = lat
            self.lon = lon
        }
    }

    public struct Stop: Codable, Sendable, Equatable {
        public let lat: Double
        public let lon: Double
        public let start: String?
        public let end: String?
        public let city: String
        public let count: Int
        public let entry: Point?           // first photo: where the route arrives
        public let exit: Point?            // last photo: where it leaves
        public let best: MediaID?          // the stop's hero shot
        public let photos: [MediaID]

        public init(lat: Double, lon: Double, start: String?, end: String?, city: String, count: Int,
                    entry: Point?, exit: Point?, best: MediaID?, photos: [MediaID]) {
            self.lat = lat
            self.lon = lon
            self.start = start
            self.end = end
            self.city = city
            self.count = count
            self.entry = entry
            self.exit = exit
            self.best = best
            self.photos = photos
        }
    }

    public struct Day: Codable, Sendable, Equatable {
        public let n: Int
        public let date: String?
        public let start: String?
        public let end: String?
        public let count: Int
        public let city: String
        public let first: Int              // index of the trip-wide stop where the day begins
        public let stops: [Stop]
        public let tracks: [String: [[Double]]]
    }

    public let id: Int64
    public let title: String
    public let location: String
    public let start: String?
    public let end: String?
    public let count: Int
    public let stops: [Stop]
    /// Leg index (as a string, "1" = stop 0 -> stop 1) -> [[lon, lat], ...] from imported GPX.
    public let tracks: [String: [[Double]]]
    public let days: [Day]

    public init(id: Int64, title: String, location: String, start: String?, end: String?, count: Int,
                stops: [Stop], tracks: [String: [[Double]]] = [:], days: [Day] = []) {
        self.id = id
        self.title = title
        self.location = location
        self.start = start
        self.end = end
        self.count = count
        self.stops = stops
        self.tracks = tracks
        self.days = days
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int64.self, forKey: .id)
        title = (try? c.decode(String.self, forKey: .title)) ?? ""
        location = (try? c.decode(String.self, forKey: .location)) ?? ""
        start = try? c.decode(String.self, forKey: .start)
        end = try? c.decode(String.self, forKey: .end)
        count = (try? c.decode(Int.self, forKey: .count)) ?? 0
        stops = (try? c.decode([Stop].self, forKey: .stops)) ?? []
        tracks = (try? c.decode([String: [[Double]]].self, forKey: .tracks)) ?? [:]
        days = (try? c.decode([Day].self, forKey: .days)) ?? []
    }

    /// The polyline of one leg (stop i-1 -> stop i): the GPX track when one was imported, else
    /// from where the previous stop was left to where the next one was reached.
    public static func leg(_ i: Int, stops: [Stop], tracks: [String: [[Double]]]) -> [Point] {
        guard i > 0, i < stops.count else { return [] }
        if let line = tracks[String(i)], line.count >= 2 {
            return line.compactMap { $0.count >= 2 ? Point(lat: $0[1], lon: $0[0]) : nil }
        }
        let a = stops[i - 1], b = stops[i]
        return [a.exit ?? Point(lat: a.lat, lon: a.lon), b.entry ?? Point(lat: b.lat, lon: b.lon)]
    }

    /// Every leg of a stop list, in order.
    public static func route(stops: [Stop], tracks: [String: [[Double]]]) -> [[Point]] {
        stops.indices.dropFirst().map { leg($0, stops: stops, tracks: tracks) }
    }
}
