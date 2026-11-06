import Foundation

/// Non-destructive Quick Edit: a 1:1 port of desktop `quick_edit.Adjustments` (EDIT_VERSION 1).
///
/// The master file is never touched on either platform. Both apps store the same JSON in
/// `media_edits.params`, so an edit made on the phone renders identically on the desktop and vice
/// versa. Validation (clamping) matches Python exactly, including its quirk that a non-numeric value
/// becomes 0.0 rather than the lower bound. Unknown keys from newer builds survive in `extra`.
/// The edit math lives in EditMath.swift. Fixtures/edit_contract_v1.json pins both sides.
public struct Adjustments: Hashable, Sendable {
    public static let editVersion = 1
    public static let angleLimit = 45.0
    public static let sliderLimit = 100.0
    public static let minCrop = 0.02
    public static let aspects: Set<String> = ["free", "original", "1:1", "16:9", "4:5"]
    public static let toneKeys = ["exposure", "highlights", "shadows", "temperature", "contrast"]

    public var crop: [Double] = [0, 0, 1, 1]       // x, y, w, h – fractions of the straightened frame
    public var aspect: String = "free"
    public var angle: Double = 0                     // degrees, + = clockwise on screen
    public var exposure: Double = 0
    public var highlights: Double = 0
    public var shadows: Double = 0
    public var temperature: Double = 0               // + = warmer
    public var contrast: Double = 0
    public var extra: [String: JSONValue] = [:]

    public init() {}

    /// Python `_clamp`: float(v), non-finite or unparsable -> 0.0 (NOT the lower bound).
    static func clamp(_ v: Double?, _ lo: Double, _ hi: Double) -> Double {
        guard let v, v.isFinite else { return 0 }
        return max(lo, min(hi, v))
    }

    public func tone(_ key: String) -> Double {
        switch key {
        case "exposure": return exposure
        case "highlights": return highlights
        case "shadows": return shadows
        case "temperature": return temperature
        case "contrast": return contrast
        default: return 0
        }
    }

    public mutating func setTone(_ key: String, _ value: Double) {
        switch key {
        case "exposure": exposure = value
        case "highlights": highlights = value
        case "shadows": shadows = value
        case "temperature": temperature = value
        case "contrast": contrast = value
        default: break
        }
    }

    public func normalized() -> Adjustments {
        var out = Adjustments()
        var x = 0.0, y = 0.0, w = 1.0, h = 1.0
        if crop.count == 4 { (x, y, w, h) = (crop[0], crop[1], crop[2], crop[3]) }
        w = Self.clamp(w, Self.minCrop, 1)
        h = Self.clamp(h, Self.minCrop, 1)
        x = Self.clamp(x, 0, 1 - w)
        y = Self.clamp(y, 0, 1 - h)
        out.crop = [x, y, w, h]
        out.aspect = Self.aspects.contains(aspect) ? aspect : "free"
        out.angle = Self.clamp(angle, -Self.angleLimit, Self.angleLimit)
        for k in Self.toneKeys { out.setTone(k, Self.clamp(tone(k), -Self.sliderLimit, Self.sliderLimit)) }
        out.extra = extra
        return out
    }

    public var isToneIdentity: Bool { Self.toneKeys.allSatisfy { abs(tone($0)) < 1e-6 } }

    public var isGeometryIdentity: Bool {
        guard crop.count == 4 else { return abs(angle) < 1e-6 }
        return abs(angle) < 1e-6 && crop[0] <= 1e-6 && crop[1] <= 1e-6 && crop[2] >= 1 - 1e-6 && crop[3] >= 1 - 1e-6
    }

    public var isIdentity: Bool { isToneIdentity && isGeometryIdentity }

    // ── persistence (same keys and rounding as Python to_dict / from_dict) ──

    public init(dict d: [String: JSONValue]) {
        let known: Set<String> = Set(["v", "crop", "aspect", "angle"] + Self.toneKeys)
        var a = Adjustments()
        if case .array(let arr)? = d["crop"], arr.count == 4 {
            a.crop = arr.map { $0.doubleValue ?? .nan }   // NaN -> clamp -> 0.0, like float() failing
        }
        a.aspect = d["aspect"]?.stringValue ?? "free"
        a.angle = d["angle"].map { $0.doubleValue ?? .nan } ?? 0
        for k in Self.toneKeys { a.setTone(k, d[k].map { $0.doubleValue ?? .nan } ?? 0) }
        a.extra = d.filter { !known.contains($0.key) }
        self = a.normalized()
    }

    public func toDict() -> [String: JSONValue] {
        func r(_ v: Double, _ places: Double) -> Double { (v * places).rounded() / places }
        var d = extra
        d["v"] = .number(Double(Self.editVersion))
        d["crop"] = .array(crop.map { .number(r($0, 1e6)) })
        d["aspect"] = .string(aspect)
        d["angle"] = .number(r(angle, 1e4))
        for k in Self.toneKeys { d[k] = .number(r(tone(k), 1e3)) }
        return d
    }

    public func jsonData() throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        return try enc.encode(toDict())
    }
}
