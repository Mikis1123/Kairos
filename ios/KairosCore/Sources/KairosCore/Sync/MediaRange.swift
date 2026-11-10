import Foundation

/// Byte-range arithmetic for streaming videos and Live Photo motion from the desktop
/// (GET /v1/media/{id}/original|live with `Range`, answered 206 + `Content-Range`).
/// Pure functions, so the contract is unit-tested without AVFoundation or a network.
public enum MediaRange {
    /// Size of one ranged request. 1 MB is ~10 ms on home Wi-Fi: small enough that a seek
    /// abandons little, large enough that TLS and HTTP overhead stay negligible.
    public static let chunk: Int64 = 1 << 20

    /// `bytes 256-511/10240` -> (256, 511, 10240). `bytes */10240` (a 416) -> nil.
    /// An unknown total (`bytes 0-99/*`) gives total = nil.
    public static func parseContentRange(_ header: String) -> (start: Int64, end: Int64, total: Int64?)? {
        let h = header.trimmingCharacters(in: .whitespaces)
        guard h.lowercased().hasPrefix("bytes ") else { return nil }
        let spec = h.dropFirst(6)
        let parts = spec.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2 else { return nil }
        let bounds = parts[0].split(separator: "-", maxSplits: 1, omittingEmptySubsequences: false)
        guard bounds.count == 2, let start = Int64(bounds[0]), let end = Int64(bounds[1]), start >= 0, end >= start
        else { return nil }
        let total: Int64?
        if parts[1] == "*" {
            total = nil
        } else {
            guard let t = Int64(parts[1]), t > end else { return nil }
            total = t
        }
        return (start, end, total)
    }

    /// `Range` header for `length` bytes from `offset` (length >= 1).
    public static func header(offset: Int64, length: Int64) -> String {
        "bytes=\(offset)-\(offset + max(1, length) - 1)"
    }

    /// Length of the next request: one chunk, clipped to what the player asked for (`end`,
    /// exclusive, nil = to the end of the file) and to the file size once known.
    /// 0 = nothing left to fetch.
    public static func nextLength(position: Int64, end: Int64?, total: Int64?, chunk: Int64 = MediaRange.chunk) -> Int64 {
        var n = chunk
        if let end { n = min(n, end - position) }
        if let total { n = min(n, total - position) }
        return max(0, n)
    }

    /// Uniform type identifier AVFoundation needs for the content-information request.
    /// From the server's Content-Type first, else the file extension; QuickTime as the last
    /// resort (iPhone videos and Live Photo motion halves are .MOV).
    public static func uti(contentType: String?, fileExt: String?) -> String {
        switch contentType?.lowercased().split(separator: ";").first.map(String.init) {
        case "video/mp4": return "public.mpeg-4"
        case "video/x-m4v": return "com.apple.m4v-video"
        case "video/quicktime": return "com.apple.quicktime-movie"
        default: break
        }
        switch fileExt?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")) {
        case "mp4": return "public.mpeg-4"
        case "m4v": return "com.apple.m4v-video"
        case "3gp": return "public.3gpp"
        default: return "com.apple.quicktime-movie"
        }
    }
}

/// A chunk of a ranged download: the bytes plus what the server said about the whole file.
public struct MediaChunk: Sendable {
    public let data: Data
    public let total: Int64?
    public let contentType: String?

    public init(data: Data, total: Int64?, contentType: String?) {
        self.data = data
        self.total = total
        self.contentType = contentType
    }
}
