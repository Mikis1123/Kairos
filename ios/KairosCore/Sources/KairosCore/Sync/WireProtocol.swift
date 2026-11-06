import Foundation

/// Kairos LAN bridge, protocol 1. Server: sync_server.py on the desktop. Full spec: ios/docs/SYNC_PROTOCOL.md.
public enum WireProtocol {
    public static let version = 1
    public static let pageLimit = 1000
    public static let microBatch = 500
    static let microMagic = Data("KMP1".utf8)
}

public struct Hello: Decodable, Sendable {
    public let app: String
    public let `protocol`: Int
    public let name: String
    public let libraryId: String
    public let pairing: Bool
    public let power: String        // "ac" | "battery" | "saver" | "unknown"
    public let bulkOk: Bool         // false: the desktop asks to defer packs / bulk originals

    enum CodingKeys: String, CodingKey {
        case app, `protocol`, name, pairing, power
        case libraryId = "library_id", bulkOk = "bulk_ok"
    }
}

public struct PairResponse: Decodable, Sendable {
    public let token: String
    public let deviceId: String
    public let libraryId: String
    public let name: String

    enum CodingKeys: String, CodingKey {
        case token, name
        case deviceId = "device_id", libraryId = "library_id"
    }
}

/// What the desktop's pairing QR code holds: kairos://pair?h=…&p=…&fp=…&lib=…&pin=…&v=1
public struct PairingOffer: Hashable, Sendable {
    public let host: String
    public let port: Int
    public let fingerprint: String          // SHA-256 of the server certificate (DER), hex
    public let libraryId: String
    public let pin: String

    public init?(uri: String) {
        guard let c = URLComponents(string: uri.trimmingCharacters(in: .whitespacesAndNewlines)),
              c.scheme == "kairos", c.host == "pair" else { return nil }
        var q: [String: String] = [:]
        for item in c.queryItems ?? [] { q[item.name] = item.value ?? "" }
        guard let h = q["h"], !h.isEmpty, let p = q["p"].flatMap(Int.init), let fp = q["fp"], fp.count == 64,
              let pin = q["pin"], pin.count == 6 else { return nil }
        if let v = q["v"].flatMap(Int.init), v > WireProtocol.version { return nil }
        host = h; port = p; fingerprint = fp.lowercased(); libraryId = q["lib"] ?? ""; self.pin = pin
    }
}

/// A paired desktop, persisted (token in the Keychain, the rest in UserDefaults).
public struct DesktopEndpoint: Codable, Hashable, Sendable {
    public var host: String
    public var port: Int
    public var fingerprint: String
    public var libraryId: String
    public var name: String
    public var deviceId: String

    public init(host: String, port: Int, fingerprint: String, libraryId: String, name: String, deviceId: String) {
        self.host = host; self.port = port; self.fingerprint = fingerprint
        self.libraryId = libraryId; self.name = name; self.deviceId = deviceId
    }

    public var baseURL: URL { URL(string: "https://\(host):\(port)")! }
}

public enum ChangeEntity: String, Codable, Sendable {
    case media, event, person, face, album, edit
}

public enum ChangePayload: Sendable {
    case media(MediaItem)
    case event(Event)
    case person(Person)
    case face(Face)
    case album(Album)
    case edit(EditRecord)
}

public struct Change: Decodable, Sendable {
    public let seq: Int64
    public let entity: ChangeEntity?        // nil = an entity this build doesn't know (skipped)
    public let id: Int64
    public let payload: ChangePayload?      // nil = tombstone (deleted, hidden, trashed, Live motion half)

    enum CodingKeys: String, CodingKey { case seq, entity, id, op, data }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = try c.decode(Int64.self, forKey: .seq)
        id = try c.decode(Int64.self, forKey: .id)
        let kind = ChangeEntity(rawValue: try c.decode(String.self, forKey: .entity))
        entity = kind
        let op = try c.decode(String.self, forKey: .op)
        guard op == "upsert", let kind else {
            payload = nil
            return
        }
        switch kind {
        case .media: payload = .media(try c.decode(MediaItem.self, forKey: .data))
        case .event: payload = .event(try c.decode(Event.self, forKey: .data))
        case .person: payload = .person(try c.decode(Person.self, forKey: .data))
        case .face: payload = .face(try c.decode(Face.self, forKey: .data))
        case .album: payload = .album(try c.decode(Album.self, forKey: .data))
        case .edit: payload = .edit(try c.decode(EditRecord.self, forKey: .data))
        }
    }
}

public struct ChangePage: Decodable, Sendable {
    public let `protocol`: Int
    public let libraryId: String
    public let cursor: Int64
    public let more: Bool
    public let reset: Bool
    public let changes: [Change]

    enum CodingKeys: String, CodingKey {
        case `protocol`, cursor, more, reset, changes
        case libraryId = "library_id"
    }
}

public struct MapPack: Decodable, Hashable, Identifiable, Sendable {
    public let id: String
    public let file: String
    public let size: Int64
    public let mtime: Int64
    public let title: String
    public let format: String               // "pbf" (OpenMapTiles vector) | "png" | "jpg" | "webp"
    public let source: String
    public let bounds: String?
    public let minzoom: Int
    public let maxzoom: Int
}

/// /v1/micro response: "KMP1" + (u32 id, u32 length, JPEG)*, big-endian.
public enum MicroFrame {
    public static func parse(_ data: Data) throws -> [(MediaID, Data)] {
        let bytes = [UInt8](data)
        guard bytes.count >= 4, Data(bytes[0..<4]) == WireProtocol.microMagic else {
            throw URLError(.cannotParseResponse)
        }
        func u32(_ at: Int) -> UInt32 {
            (UInt32(bytes[at]) << 24) | (UInt32(bytes[at + 1]) << 16) | (UInt32(bytes[at + 2]) << 8) | UInt32(bytes[at + 3])
        }
        var out: [(MediaID, Data)] = []
        var pos = 4
        while pos + 8 <= bytes.count {
            let id = MediaID(u32(pos)), n = Int(u32(pos + 4))
            pos += 8
            guard pos + n <= bytes.count else { throw URLError(.cannotParseResponse) }
            out.append((id, Data(bytes[pos..<(pos + n)])))
            pos += n
        }
        return out
    }
}
