#if canImport(AVFoundation)
import AVFoundation
import Foundation
import KairosCore

/// Streams a video (or a Live Photo's motion half) from the desktop into AVPlayer.
///
/// AVPlayer cannot use our URLSession: the desktop's certificate is self-signed and pinned
/// (PinnedTrust), and every request needs the bearer token. So the asset gets a private URL
/// scheme, AVFoundation hands every byte request to this delegate, and the delegate answers it
/// with 1 MB `Range` requests through SyncClient. Playback starts after the first chunk instead
/// of after the whole file, and a seek costs one chunk.
///
/// Transient failures (the desktop's 503 "busy" when more than 8 requests are in flight, a
/// dropped Wi-Fi packet) are retried. A final one fails the loading request; the player item
/// then fails and the page falls back to a plain download (VideoPageController).
final class MediaStreamLoader: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    static let scheme = "kairos-stream"
    static let attempts = 5

    let client: SyncClient
    let id: MediaID
    let live: Bool
    let fileExt: String?
    let queue = DispatchQueue(label: "app.kairos.stream", qos: .userInitiated)

    // `tasks` is only touched on `queue` (where AVFoundation calls the delegate).
    private var tasks: [ObjectIdentifier: Task<Void, Never>] = [:]
    private let lock = NSLock()
    private var total: Int64?
    private var contentType: String?
    private var lastError: Error?

    init(client: SyncClient, id: MediaID, live: Bool, fileExt: String?) {
        self.client = client
        self.id = id
        self.live = live
        self.fileExt = fileExt
    }

    deinit {
        for t in tasks.values { t.cancel() }
    }

    /// The last final error, e.g. SyncError.server(409, "cloud_only").
    var failure: Error? {
        lock.lock(); defer { lock.unlock() }
        return lastError
    }

    func makeAsset() -> AVURLAsset {
        let url = URL(string: "\(Self.scheme)://media/\(id)/\(live ? "live" : "original")")!
        let asset = AVURLAsset(url: url)
        asset.resourceLoader.setDelegate(self, queue: queue)
        return asset
    }

    /// Cancels every running request (the page went away).
    func cancelAll() {
        queue.async { [self] in
            for t in tasks.values { t.cancel() }
            tasks.removeAll()
        }
    }

    // ── AVAssetResourceLoaderDelegate (called on `queue`) ──

    func resourceLoader(_ resourceLoader: AVAssetResourceLoader,
                        shouldWaitForLoadingOfRequestedResource loadingRequest: AVAssetResourceLoadingRequest) -> Bool {
        guard loadingRequest.request.url?.scheme == Self.scheme else { return false }
        let box = RequestBox(loadingRequest)
        let key = ObjectIdentifier(loadingRequest)
        tasks[key] = Task.detached(priority: .userInitiated) { [self] in
            await self.serve(box)
            self.queue.async { self.tasks[key] = nil }
        }
        return true
    }

    func resourceLoader(_ resourceLoader: AVAssetResourceLoader, didCancel loadingRequest: AVAssetResourceLoadingRequest) {
        tasks.removeValue(forKey: ObjectIdentifier(loadingRequest))?.cancel()
    }

    // ── serving ──

    final class RequestBox: @unchecked Sendable {
        let request: AVAssetResourceLoadingRequest
        init(_ r: AVAssetResourceLoadingRequest) { request = r }
    }

    private func known() -> (Int64?, String?) {
        lock.lock(); defer { lock.unlock() }
        return (total, contentType)
    }

    private func remember(_ chunk: MediaChunk) {
        lock.lock()
        if let t = chunk.total { total = t }
        if let c = chunk.contentType { contentType = c }
        lock.unlock()
    }

    /// Runs `body` on the delegate queue (AVFoundation's loading requests are not thread-safe).
    private func onQueue(_ body: @escaping @Sendable () -> Void) async {
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            queue.async {
                body()
                cont.resume()
            }
        }
    }

    private func serve(_ box: RequestBox) async {
        let req = box.request
        let dataRequest = req.dataRequest
        let wantsInfo = req.contentInformationRequest != nil
        let start = dataRequest?.requestedOffset ?? 0
        // A content-information-only request needs just the headers: 2 bytes are plenty.
        var end: Int64? = 2
        if let d = dataRequest {
            end = d.requestsAllDataToEndOfResource ? nil : start + Int64(d.requestedLength)
        }
        var position = start
        var infoSent = false
        do {
            while !Task.isCancelled {
                let (total, _) = known()
                let length = MediaRange.nextLength(position: position, end: end, total: total)
                if length == 0 { break }
                let chunk = try await fetch(offset: position, length: length)
                remember(chunk)
                if Task.isCancelled { return }
                let sendInfo = wantsInfo && !infoSent
                infoSent = true
                let (t, ctype) = known()
                let uti = MediaRange.uti(contentType: ctype, fileExt: live ? "mov" : fileExt)
                await onQueue {
                    if sendInfo, let info = req.contentInformationRequest {
                        info.contentType = uti
                        info.contentLength = t ?? 0
                        info.isByteRangeAccessSupported = true
                    }
                    if !chunk.data.isEmpty { dataRequest?.respond(with: chunk.data) }
                }
                if chunk.data.isEmpty || dataRequest == nil { break }
                position += Int64(chunk.data.count)
            }
            if Task.isCancelled { return }
            await onQueue { if !req.isFinished && !req.isCancelled { req.finishLoading() } }
        } catch {
            if Task.isCancelled { return }
            lock.lock(); lastError = error; lock.unlock()
            await onQueue { if !req.isFinished && !req.isCancelled { req.finishLoading(with: error) } }
        }
    }

    /// One chunk with retries on transient failures. 404 / 401 / 409 are final.
    private func fetch(offset: Int64, length: Int64) async throws -> MediaChunk {
        var attempt = 0
        while true {
            do {
                return try await client.mediaRange(id, live: live, offset: offset, length: length)
            } catch SyncError.deferred(let retryAfter) where attempt < Self.attempts {
                try await Task.sleep(nanoseconds: UInt64(min(Double(max(retryAfter, 1)), 2) * 1_000_000_000))
            } catch SyncError.server(let code, _) where (500...599).contains(code) && attempt < Self.attempts {
                try await Task.sleep(nanoseconds: UInt64(FetchRetry.backoff(attempt) * 1_000_000_000))
            } catch let e as URLError where e.code != .cancelled && attempt < Self.attempts {
                try await Task.sleep(nanoseconds: UInt64(FetchRetry.backoff(attempt) * 1_000_000_000))
            }
            attempt += 1
        }
    }
}
#endif
