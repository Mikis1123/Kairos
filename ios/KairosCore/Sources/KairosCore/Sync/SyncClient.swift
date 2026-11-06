import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif
#if canImport(Security)
import Security
#endif

/// Accepts exactly one certificate: the desktop's self-signed one, identified by the SHA-256 of
/// its DER bytes from the pairing QR. No CA and no hostname check: the desktop has no domain,
/// and the pin is stronger than both.
public final class PinnedTrust: NSObject, URLSessionDelegate, @unchecked Sendable {
    public let fingerprint: String

    public init(fingerprint: String) { self.fingerprint = fingerprint.lowercased() }

    public func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge)
        async -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            return (.performDefaultHandling, nil)
        }
        #if canImport(CryptoKit) && canImport(Security)
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let leaf = chain.first else {
            return (.cancelAuthenticationChallenge, nil)
        }
        let der = SecCertificateCopyData(leaf) as Data
        let hex = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
        guard hex == fingerprint else { return (.cancelAuthenticationChallenge, nil) }
        return (.useCredential, URLCredential(trust: trust))
        #else
        return (.cancelAuthenticationChallenge, nil)
        #endif
    }
}

public enum SyncError: Error, Equatable, Sendable {
    case unauthorized               // token revoked on the desktop: pair again
    case pairing(String)            // "bad_pin" | "no_pairing"
    case deferred(retryAfter: Int)  // the desktop is on battery saver: bulk transfer later
    case server(Int, String)
    case badResponse
}

/// Typed client of the desktop LAN bridge (sync_server.py, protocol 1). Stateless apart from the
/// URLSession, so it is safe to share between the sync engine and the image loaders.
public final class SyncClient: @unchecked Sendable {
    public let endpoint: DesktopEndpoint
    private let token: String?
    private let session: URLSession
    private let decoder = JSONDecoder()

    public init(endpoint: DesktopEndpoint, token: String?) {
        self.endpoint = endpoint
        self.token = token
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 30
        cfg.httpMaximumConnectionsPerHost = 6           // the server allows 8 requests in flight
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.waitsForConnectivity = false
        session = URLSession(configuration: cfg, delegate: PinnedTrust(fingerprint: endpoint.fingerprint),
                             delegateQueue: nil)
    }

    deinit { session.finishTasksAndInvalidate() }

    // ── plumbing ──

    private func request(_ path: String, method: String = "GET", query: [String: String] = [:],
                         json: Any? = nil, headers: [String: String] = [:]) throws -> URLRequest {
        var c = URLComponents()
        c.scheme = "https"
        c.host = endpoint.host
        c.port = endpoint.port
        c.path = path
        if !query.isEmpty { c.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) } }
        guard let url = c.url else { throw SyncError.badResponse }
        var r = URLRequest(url: url)
        r.httpMethod = method
        if let token { r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        for (k, v) in headers { r.setValue(v, forHTTPHeaderField: k) }
        if let json {
            r.httpBody = try JSONSerialization.data(withJSONObject: json)
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return r
    }

    private func send(_ r: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, resp) = try await session.data(for: r)
        guard let http = resp as? HTTPURLResponse else { throw SyncError.badResponse }
        switch http.statusCode {
        case 200...299, 304: return (data, http)
        case 401: throw SyncError.unauthorized
        case 503:
            let retry = Int(http.value(forHTTPHeaderField: "Retry-After") ?? "") ?? 30
            throw SyncError.deferred(retryAfter: retry)
        default:
            let code = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String ?? ""
            if r.url?.path == "/v1/pair", http.statusCode == 403 { throw SyncError.pairing(code) }
            throw SyncError.server(http.statusCode, code)
        }
    }

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        let (data, _) = try await send(try request(path, query: query))
        return try decoder.decode(T.self, from: data)
    }

    // ── pairing ──

    public func hello() async throws -> Hello { try await get("/v1/hello") }

    /// Exchanges the PIN from the QR for a bearer token. Store it with `Keychain.save`.
    public func pair(pin: String, deviceName: String, deviceId: String) async throws -> PairResponse {
        let (data, _) = try await send(try request("/v1/pair", method: "POST",
                                                   json: ["pin": pin, "device_name": deviceName, "device_id": deviceId]))
        return try decoder.decode(PairResponse.self, from: data)
    }

    public func unpair() async throws {
        _ = try await send(try request("/v1/unpair", method: "POST"))
    }

    // ── metadata ──

    public func changes(since: Int64, limit: Int = WireProtocol.pageLimit) async throws -> ChangePage {
        try await get("/v1/changes", query: ["since": String(since), "limit": String(limit)])
    }

    public func micro(_ ids: [MediaID]) async throws -> [(MediaID, Data)] {
        guard !ids.isEmpty else { return [] }
        let (data, _) = try await send(try request("/v1/micro", query: ["ids": ids.map(String.init).joined(separator: ",")]))
        return try MicroFrame.parse(data)
    }

    // ── binaries ──

    public func thumbnail(_ id: MediaID) async throws -> Data {
        try await send(try request("/v1/media/\(id)/thumb")).0
    }

    public func faceCrop(_ faceId: Int64) async throws -> Data {
        try await send(try request("/v1/face/\(faceId)/crop")).0
    }

    /// Downloads the original (or the Live Photo's motion half) to a temporary file.
    /// `bulk` = a background backfill, which the desktop may defer on battery saver.
    public func downloadOriginal(_ id: MediaID, live: Bool = false, bulk: Bool = false) async throws -> URL {
        let r = try request("/v1/media/\(id)/\(live ? "live" : "original")", query: bulk ? ["bulk": "1"] : [:])
        return try await download(r)
    }

    public func packs() async throws -> [MapPack] {
        struct Packs: Decodable { let packs: [MapPack] }
        let p: Packs = try await get("/v1/packs")
        return p.packs
    }

    /// Offline map region pack (.mbtiles) into `destination`. Hundreds of MB: callers use a
    /// background URLSession in production (Phase 2); the server supports Range for resume.
    public func downloadPack(_ pack: MapPack, to destination: URL) async throws {
        let tmp = try await download(try request("/v1/packs/\(pack.id)"))
        let fm = FileManager.default
        try fm.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        if fm.fileExists(atPath: destination.path) { try fm.removeItem(at: destination) }
        try fm.moveItem(at: tmp, to: destination)
    }

    private func download(_ r: URLRequest) async throws -> URL {
        let (url, resp) = try await session.download(for: r)
        guard let http = resp as? HTTPURLResponse else { throw SyncError.badResponse }
        if http.statusCode == 401 { throw SyncError.unauthorized }
        if http.statusCode == 503 {
            throw SyncError.deferred(retryAfter: Int(http.value(forHTTPHeaderField: "Retry-After") ?? "") ?? 30)
        }
        guard (200...299).contains(http.statusCode) else { throw SyncError.server(http.statusCode, "") }
        // the system deletes `url` when this returns: move it somewhere we own
        let keep = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.moveItem(at: url, to: keep)
        return keep
    }

    // ── writes (phone -> desktop) ──

    /// Uploads an edit. The answer is the desktop's canonical (clamped) record; nil = cleared.
    public func putEdit(_ id: MediaID, _ adj: Adjustments) async throws -> EditRecord? {
        let r: URLRequest
        if adj.normalized().isIdentity {
            r = try request("/v1/media/\(id)/edit", method: "DELETE")
        } else {
            var req = try request("/v1/media/\(id)/edit", method: "PUT")
            req.httpBody = try JSONEncoder().encode(["params": adj.normalized().toDict()])
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r = req
        }
        let (data, _) = try await send(r)
        struct Answer: Decodable {
            let media_id: Int64
            let params: [String: JSONValue]?
            let updated_at: String?
        }
        let a = try decoder.decode(Answer.self, from: data)
        guard let params = a.params else { return nil }
        return EditRecord(mediaId: a.media_id, params: params, updatedAt: a.updated_at)
    }

    public func setFavorite(_ id: MediaID, _ value: Bool) async throws {
        _ = try await send(try request("/v1/media/\(id)/favorite", method: "PUT", json: ["value": value]))
    }

    // ── push: change notifications over WebSocket ──

    /// Yields the desktop's journal cursor whenever it moves. Ends when the socket drops; the
    /// engine reconnects with backoff.
    public func changeFeed() -> AsyncThrowingStream<Int64, Error> {
        AsyncThrowingStream { continuation in
            guard let r = try? self.request("/v1/events") else {
                continuation.finish(throwing: SyncError.badResponse)
                return
            }
            let task = self.session.webSocketTask(with: r)
            task.resume()
            func receive() {
                task.receive { result in
                    switch result {
                    case .failure(let e):
                        continuation.finish(throwing: e)
                    case .success(let msg):
                        var text: String?
                        switch msg {
                        case .string(let s): text = s
                        case .data(let d): text = String(data: d, encoding: .utf8)
                        @unknown default: text = nil
                        }
                        if let t = text, let obj = try? JSONSerialization.jsonObject(with: Data(t.utf8)) as? [String: Any],
                           let cursor = (obj["cursor"] as? NSNumber)?.int64Value {
                            continuation.yield(cursor)
                        }
                        receive()
                    }
                }
            }
            receive()
            continuation.onTermination = { _ in task.cancel(with: .goingAway, reason: nil) }
        }
    }
}

#if canImport(Security)
/// The bearer token lives in the Keychain (this device only, after first unlock).
public enum Keychain {
    static let service = "app.kairos.desktop-token"

    public static func save(_ token: String, account: String) throws {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                   kSecAttrService as String: service, kSecAttrAccount as String: account]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = Data(token.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let rc = SecItemAdd(add as CFDictionary, nil)
        guard rc == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(rc)) }
    }

    public static func load(account: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecAttrAccount as String: account, kSecReturnData as String: true,
                                kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    public static func delete(account: String) {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecAttrAccount as String: account]
        SecItemDelete(q as CFDictionary)
    }
}
#endif
