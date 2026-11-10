import Foundation

/// Three-state library (Parity sprint): which camera-roll assets the desktop already holds.
///
/// A phone asset is `phone` (only on the iPhone), `both` (matched to a desktop item) or the
/// desktop item is `pc` (no local asset). Matching is offline and runs against the replica:
///   1. name + size     – same file name (case-insensitive) and the same byte count
///   2. name + time     – same name stem (extension ignored: HEIC exported as JPG still counts)
///                        and capture times within `nameTolerance`
///   3. time + size     – same byte count, capture times within `timeTolerance` (renamed files)
/// The strongest proof, SHA-256 of the original resource, comes from the desktop (/v1/match)
/// and is recorded as `.sha256`; the matcher never overrides it. Each desktop item is matched to
/// at most one phone asset (the closest in time wins), so burst shots never collapse.
public enum AssetMatcher {
    public enum Kind: String, Sendable, Codable, CaseIterable {
        case sha256, nameSize = "name+size", nameTime = "name+time", timeSize = "time+size"

        var rank: Int {
            switch self {
            case .sha256: return 0
            case .nameSize: return 1
            case .nameTime: return 2
            case .timeSize: return 3
            }
        }
    }

    public struct PhoneKey: Sendable, Hashable {
        public let localId: String
        public let fileName: String?
        public let created: Date?
        public let fileSize: Int64?

        public init(localId: String, fileName: String?, created: Date?, fileSize: Int64?) {
            self.localId = localId
            self.fileName = fileName
            self.created = created
            self.fileSize = fileSize
        }
    }

    public struct DesktopKey: Sendable, Hashable {
        public let id: MediaID
        public let fileName: String?
        public let dateTaken: String?
        public let timezoneOffset: String?
        public let fileSize: Int64?

        public init(id: MediaID, fileName: String?, dateTaken: String?, timezoneOffset: String?, fileSize: Int64?) {
            self.id = id
            self.fileName = fileName
            self.dateTaken = dateTaken
            self.timezoneOffset = timezoneOffset
            self.fileSize = fileSize
        }
    }

    public struct Match: Sendable, Hashable {
        public let mediaId: MediaID
        public let kind: Kind
    }

    public static let nameTolerance: TimeInterval = 2
    public static let timeTolerance: TimeInterval = 1

    // ── names ──

    /// "IMG_0042 (2).HEIC" -> "img_0042": lowercased, no extension, no " (n)" collision suffix
    /// (the desktop's inbox adds it when two files share a name).
    public static func stem(_ name: String?) -> String? {
        guard var s = name?.trimmingCharacters(in: .whitespaces), !s.isEmpty else { return nil }
        if let dot = s.lastIndex(of: "."), dot != s.startIndex { s = String(s[..<dot]) }
        if s.hasSuffix(")"), let open = s.lastIndex(of: "("), open > s.startIndex {
            let inner = s[s.index(after: open)..<s.index(before: s.endIndex)]
            if !inner.isEmpty, inner.allSatisfy(\.isNumber) {
                s = String(s[..<open]).trimmingCharacters(in: .whitespaces)
            }
        }
        return s.lowercased()
    }

    // ── dates ──

    /// Desktop `date_taken` ("YYYY:MM:DD HH:MM:SS", "YYYY-MM-DD HH:MM:SS" or ISO with "T") as an
    /// absolute time. With a `timezone_offset` ("+01:00", "+0100", "Z") it is exact; without one
    /// the wall time is read in `zone` (the phone's), as the desktop shows it.
    public static func absolute(_ dateTaken: String?, offset: String?, zone: TimeZone) -> Date? {
        guard let raw = dateTaken?.trimmingCharacters(in: .whitespaces), raw.count >= 19 else { return nil }
        let chars = Array(raw)
        func num(_ a: Int, _ n: Int) -> Int? { Int(String(chars[a..<(a + n)])) }
        guard let y = num(0, 4), let mo = num(5, 2), let d = num(8, 2), let h = num(11, 2), let mi = num(14, 2),
              let s = num(17, 2) else { return nil }
        var comps = DateComponents(year: y, month: mo, day: d, hour: h, minute: mi, second: s)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = parseOffset(offset).flatMap { TimeZone(secondsFromGMT: $0) } ?? zone
        comps.calendar = cal
        return cal.date(from: comps)
    }

    static func parseOffset(_ text: String?) -> Int? {
        guard let t = text?.trimmingCharacters(in: .whitespaces), !t.isEmpty else { return nil }
        if t == "Z" || t == "z" { return 0 }
        let sign: Int
        switch t.first {
        case "+": sign = 1
        case "-": sign = -1
        default: return nil
        }
        let digits = t.dropFirst().filter(\.isNumber)
        guard digits.count == 4 || digits.count == 2, let v = Int(digits) else { return nil }
        let (h, m) = digits.count == 4 ? (v / 100, v % 100) : (v, 0)
        guard h <= 14, m < 60 else { return nil }
        return sign * (h * 3600 + m * 60)
    }

    /// The phone's capture time as a desktop-style local wall string, so phone-only assets sort
    /// into the same `ORDER BY date_taken` as desktop items.
    public static func wallString(_ date: Date, zone: TimeZone) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = zone
        let c = cal.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
        return String(format: "%04d:%02d:%02d %02d:%02d:%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0,
                      c.hour ?? 0, c.minute ?? 0, c.second ?? 0)
    }

    // ── matching ──

    /// Phone local id -> match. `fixed` holds matches already proven by SHA-256: those desktop
    /// items are taken and those phone assets keep their match.
    public static func match(phone: [PhoneKey], desktop: [DesktopKey], zone: TimeZone = .current,
                             fixed: [String: MediaID] = [:]) -> [String: Match] {
        struct D { let key: DesktopKey; let time: Date?; let name: String?; let stem: String? }
        var byStem: [String: [Int]] = [:]
        var bySecond: [Int64: [Int]] = [:]
        var ds: [D] = []
        ds.reserveCapacity(desktop.count)
        for k in desktop {
            let d = D(key: k, time: absolute(k.dateTaken, offset: k.timezoneOffset, zone: zone),
                      name: k.fileName?.lowercased(), stem: stem(k.fileName))
            let i = ds.count
            ds.append(d)
            if let s = d.stem { byStem[s, default: []].append(i) }
            if let t = d.time { bySecond[Int64(t.timeIntervalSince1970.rounded(.down)), default: []].append(i) }
        }

        // every acceptable pairing, then greedy by (kind, time distance): deterministic, one-to-one
        struct Cand { let phone: Int; let desk: Int; let kind: Kind; let dt: TimeInterval }
        var cands: [Cand] = []
        let taken = Set(fixed.values)
        for (pi, p) in phone.enumerated() where fixed[p.localId] == nil {
            let pName = p.fileName?.lowercased()
            var seen = Set<Int>()
            func consider(_ di: Int) {
                guard !seen.contains(di) else { return }
                seen.insert(di)
                let d = ds[di]
                guard !taken.contains(d.key.id) else { return }
                let dt = (p.created != nil && d.time != nil) ? abs(p.created!.timeIntervalSince(d.time!)) : .infinity
                let sameSize = p.fileSize != nil && p.fileSize == d.key.fileSize && (p.fileSize ?? 0) > 0
                if sameSize, pName != nil, pName == d.name {
                    cands.append(Cand(phone: pi, desk: di, kind: .nameSize, dt: dt.isFinite ? dt : 1e9))
                } else if d.stem != nil, d.stem == stem(p.fileName), dt <= nameTolerance {
                    cands.append(Cand(phone: pi, desk: di, kind: .nameTime, dt: dt))
                } else if sameSize, dt <= timeTolerance {
                    cands.append(Cand(phone: pi, desk: di, kind: .timeSize, dt: dt))
                }
            }
            if let s = stem(p.fileName) { byStem[s]?.forEach(consider) }
            if let t = p.created {
                let sec = Int64(t.timeIntervalSince1970.rounded(.down))
                for b in (sec - 3)...(sec + 3) { bySecond[b]?.forEach(consider) }
            }
        }
        cands.sort { ($0.kind.rank, $0.dt, $0.phone, $0.desk) < ($1.kind.rank, $1.dt, $1.phone, $1.desk) }
        var out: [String: Match] = [:]
        for (id, media) in fixed { out[id] = Match(mediaId: media, kind: .sha256) }
        var usedPhone = Set<Int>(), usedDesk = Set<Int>()
        for c in cands where !usedPhone.contains(c.phone) && !usedDesk.contains(c.desk) {
            usedPhone.insert(c.phone)
            usedDesk.insert(c.desk)
            out[phone[c.phone].localId] = Match(mediaId: ds[c.desk].key.id, kind: c.kind)
        }
        return out
    }
}

