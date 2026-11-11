import Foundation
#if canImport(Network)
import Network
#endif

/// Finds the paired desktop again after its address changed (Bonjour / mDNS, `_kairos._tcp`).
///
/// The pairing QR carries an IP address. A DHCP renewal, a router reboot or another Wi-Fi changes
/// it, and before discovery the phone then showed "Offline" until the QR was scanned again. The
/// desktop announces `_kairos._tcp` (lan_discovery.py) with a TXT record carrying the SHA-256 of
/// its certificate (`fp`) and the library id (`lib`). The phone looks for the advert that matches
/// what it paired with, resolves its address and carries on. A match is only a hint: the TLS pin
/// still decides whether the machine at that address is the desktop.
public enum DesktopDiscovery {
    public static let serviceType = "_kairos._tcp"

    /// What the desktop announces.
    public struct Advert: Equatable, Sendable {
        public var name: String
        public var fingerprint: String
        public var libraryId: String

        public init(name: String, fingerprint: String, libraryId: String) {
            self.name = name
            self.fingerprint = fingerprint
            self.libraryId = libraryId
        }

        /// From the TXT record's keys (`fp`, `lib`, `name`). Nil without a fingerprint.
        public init?(txt: [String: String], serviceName: String) {
            guard let fp = txt["fp"], !fp.isEmpty else { return nil }
            self.init(name: txt["name"] ?? serviceName, fingerprint: fp.lowercased(), libraryId: txt["lib"] ?? "")
        }
    }

    /// True when the advert is the desktop this phone paired with: same certificate. Without a
    /// stored fingerprint (never the case after pairing) the library id has to agree instead.
    public static func isSameDesktop(_ advert: Advert, as endpoint: DesktopEndpoint) -> Bool {
        if !endpoint.fingerprint.isEmpty { return advert.fingerprint == endpoint.fingerprint.lowercased() }
        return !endpoint.libraryId.isEmpty && advert.libraryId == endpoint.libraryId
    }

    /// "192.168.1.20%en0" -> "192.168.1.20" (the scope id is not part of a URL host).
    public static func cleanHost(_ raw: String) -> String {
        String(raw.split(separator: "%", maxSplits: 1, omittingEmptySubsequences: false).first ?? Substring(raw))
    }

    /// The endpoint to use now: the stored one moved to `host:port`. Nil when nothing changed.
    public static func moved(_ endpoint: DesktopEndpoint, to host: String, port: Int) -> DesktopEndpoint? {
        let h = cleanHost(host)
        guard !h.isEmpty, port > 0, h != endpoint.host || port != endpoint.port else { return nil }
        var e = endpoint
        e.host = h
        e.port = port
        return e
    }

    #if canImport(Network)
    /// Browses for `timeout` seconds and returns the address of the advert that is `endpoint`'s
    /// desktop, or nil (not on this network, Local Network permission denied, ...).
    public static func find(matching endpoint: DesktopEndpoint, timeout: TimeInterval = 6) async -> (host: String, port: Int)? {
        await withCheckedContinuation { (cont: CheckedContinuation<(host: String, port: Int)?, Never>) in
            let session = Session(endpoint: endpoint) { cont.resume(returning: $0) }
            session.start(timeout: timeout)
        }
    }

    /// One browse. Finishes exactly once: with the first resolved match, or nil on the deadline.
    private final class Session: @unchecked Sendable {
        private let target: DesktopEndpoint
        private let done: ((host: String, port: Int)?) -> Void
        private let queue = DispatchQueue(label: "app.kairos.discovery")
        private var browser: NWBrowser?
        private var probes: [String: NWConnection] = [:]
        private var finished = false

        init(endpoint: DesktopEndpoint, done: @escaping ((host: String, port: Int)?) -> Void) {
            self.target = endpoint
            self.done = done
        }

        func start(timeout: TimeInterval) {
            queue.async { [self] in
                let b = NWBrowser(for: .bonjourWithTXTRecord(type: DesktopDiscovery.serviceType, domain: nil),
                                  using: .tcp)
                browser = b
                b.browseResultsChangedHandler = { [weak self] results, _ in self?.handle(results) }
                b.stateUpdateHandler = { [weak self] state in
                    if case .failed = state { self?.finish(nil) }
                }
                b.start(queue: queue)
            }
            // the strong capture keeps this session alive until the deadline: every other handler
            // holds it weakly, and nothing else owns it once find() returns into the continuation
            queue.asyncAfter(deadline: .now() + timeout) { [self] in finish(nil) }
        }

        private func handle(_ results: Set<NWBrowser.Result>) {
            guard !finished else { return }
            for r in results {
                guard case let .service(name, _, _, _) = r.endpoint,
                      case let .bonjour(record) = r.metadata,
                      let advert = Advert(txt: record.dictionary, serviceName: name),
                      DesktopDiscovery.isSameDesktop(advert, as: target),
                      probes[name] == nil else { continue }
                resolve(name: name, endpoint: r.endpoint)
            }
        }

        /// Bonjour hands out a service, not an address: open a plain TCP connection to it, read
        /// where the path went, and drop it (the TLS handshake never starts).
        private func resolve(name: String, endpoint: NWEndpoint) {
            let params = NWParameters.tcp
            if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options { ip.version = .v4 }
            let c = NWConnection(to: endpoint, using: params)
            probes[name] = c
            c.stateUpdateHandler = { [weak self, weak c] state in
                guard let self, let c else { return }
                switch state {
                case .ready:
                    if case let .hostPort(host, port)? = c.currentPath?.remoteEndpoint {
                        let h: String
                        switch host {
                        case .ipv4(let a): h = "\(a)"
                        case .ipv6(let a): h = "\(a)"
                        case .name(let n, _): h = n
                        @unknown default: h = ""
                        }
                        self.finish((DesktopDiscovery.cleanHost(h), Int(port.rawValue)))
                    } else {
                        self.finish(nil)
                    }
                case .failed, .cancelled:
                    self.probes[name] = nil
                default:
                    break
                }
            }
            c.start(queue: queue)
        }

        private func finish(_ result: (host: String, port: Int)?) {
            queue.async { [self] in
                guard !finished else { return }
                finished = true
                browser?.cancel()
                browser = nil
                probes.values.forEach { $0.cancel() }
                probes.removeAll()
                done(result)
            }
        }
    }
    #endif
}
