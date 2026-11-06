import Foundation

/// Any JSON value, kept verbatim. Edit parameters carry keys from newer builds ("extra" on the
/// desktop) and must survive a round trip through the phone untouched.
public enum JSONValue: Codable, Hashable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    /// Python's float(): numbers, numeric strings and booleans; nil otherwise.
    public var doubleValue: Double? {
        switch self {
        case .number(let n): return n
        case .bool(let b): return b ? 1 : 0
        case .string(let s): return Double(s.trimmingCharacters(in: .whitespaces))
        default: return nil
        }
    }

    public var stringValue: String? {
        switch self {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() && abs(n) < 1e15 ? String(Int64(n)) : String(n)
        case .bool(let b): return b ? "True" : "False"
        default: return nil
        }
    }
}

/// SQLite is dynamically typed: a desktop column declared INTEGER can hold text written by an
/// old build. One odd value must not fail the whole change page, so wire fields decode leniently.
extension KeyedDecodingContainer {
    func lossyInt(_ key: Key) -> Int64? {
        guard let v = try? decodeIfPresent(JSONValue.self, forKey: key), let d = v.doubleValue,
              d.isFinite, abs(d) < 9e18 else { return nil }
        return Int64(d)
    }

    func lossyDouble(_ key: Key) -> Double? {
        guard let v = try? decodeIfPresent(JSONValue.self, forKey: key), let d = v.doubleValue,
              d.isFinite else { return nil }
        return d
    }

    func lossyString(_ key: Key) -> String? {
        (try? decodeIfPresent(JSONValue.self, forKey: key))?.stringValue
    }

    func lossyBool(_ key: Key) -> Bool {
        guard let v = try? decodeIfPresent(JSONValue.self, forKey: key), let d = v.doubleValue else { return false }
        return d != 0
    }
}
