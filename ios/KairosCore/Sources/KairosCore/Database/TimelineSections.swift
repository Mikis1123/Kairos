#if canImport(SQLite3)
import Foundation

/// Date-grouped timeline for the Library grid and its fast-scroll scrubber (Parity sprint).
///
/// Entries arrive newest first (LibraryStore.timeline). Sections are days at 3-4 columns,
/// months at 5-6 and years at 7, as in Photos. Each section knows where it starts as a fraction
/// of the whole scroll height (its rows plus a header row), so the scrubber maps a finger
/// position to a section, and a scroll offset back to the label it shows, without measuring views.
public struct TimelineSection: Identifiable, Sendable, Equatable {
    public let id: String                   // "2026-07-01", "2026-07", "2026" or "undated"
    public let year: Int?
    public let month: Int?
    public let day: Int?
    public let entries: [LibraryStore.TimelineEntry]
    public internal(set) var start: Double = 0      // fraction of the total height where it begins

    public static func == (a: TimelineSection, b: TimelineSection) -> Bool {
        a.id == b.id && a.entries.count == b.entries.count && a.entries.first?.id == b.entries.first?.id
    }
}

public enum TimelineGrouping: Sendable, Equatable {
    case day, month, year

    public static func forColumns(_ columns: Int) -> TimelineGrouping {
        columns <= 4 ? .day : (columns <= 6 ? .month : .year)
    }
}

public enum TimelineSections {
    /// Header height in grid rows, for the scroll-fraction weights.
    static let headerRows = 0.45

    /// (year, month, day) from a desktop date ("YYYY:MM:DD …" or ISO). Nil = undated.
    public static func ymd(_ date: String?) -> (Int, Int, Int)? {
        guard let d = date, d.count >= 10 else { return nil }
        let c = Array(d)
        guard let y = Int(String(c[0..<4])), let m = Int(String(c[5..<7])), let dd = Int(String(c[8..<10])),
              y > 1800, (1...12).contains(m), (1...31).contains(dd) else { return nil }
        return (y, m, dd)
    }

    public static func build(_ entries: [LibraryStore.TimelineEntry], grouping: TimelineGrouping,
                             columns: Int) -> [TimelineSection] {
        var out: [TimelineSection] = []
        var undated: [LibraryStore.TimelineEntry] = []
        var key = "", bucket: [LibraryStore.TimelineEntry] = []
        var parts: (Int?, Int?, Int?) = (nil, nil, nil)
        func flush() {
            if !bucket.isEmpty {
                out.append(TimelineSection(id: key, year: parts.0, month: parts.1, day: parts.2, entries: bucket))
            }
            bucket = []
        }
        for e in entries {
            guard let date = ymd(e.dateTaken) else {
                undated.append(e)
                continue
            }
            let (y, m, d) = date
            let k: String
            switch grouping {
            case .day: k = String(format: "%04d-%02d-%02d", y, m, d)
            case .month: k = String(format: "%04d-%02d", y, m)
            case .year: k = String(format: "%04d", y)
            }
            if k != key {
                flush()
                key = k
                parts = (y, grouping == .year ? nil : m, grouping == .day ? d : nil)
            }
            bucket.append(e)
        }
        flush()
        if !undated.isEmpty {
            out.append(TimelineSection(id: "undated", year: nil, month: nil, day: nil, entries: undated))
        }
        // weights: rows of the section plus its header
        let cols = Double(max(1, columns))
        let weights = out.map { (Double($0.entries.count) / cols).rounded(.up) + headerRows }
        let total = max(1e-9, weights.reduce(0, +))
        var acc = 0.0
        for i in out.indices {
            out[i].start = acc / total
            acc += weights[i]
        }
        return out
    }

    /// The section showing at a scroll fraction (0 = top, 1 = bottom).
    public static func index(at fraction: Double, in sections: [TimelineSection]) -> Int {
        guard !sections.isEmpty else { return 0 }
        let f = min(1, max(0, fraction))
        var lo = 0, hi = sections.count - 1
        while lo < hi {
            let mid = (lo + hi + 1) / 2
            if sections[mid].start <= f { lo = mid } else { hi = mid - 1 }
        }
        return lo
    }
}
#endif
