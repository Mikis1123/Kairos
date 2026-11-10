#if os(iOS)
import AVFoundation
import KairosCore
import UIKit

/// A page of the full-screen viewer (PhotoPager): a photo (PhotoPageController) or a video.
@MainActor
protocol ViewerPage: UIViewController {
    var id: MediaID { get }
    var onSingleTap: (() -> Void)? { get set }
    /// The edit changed on the desktop or in the editor.
    func reloadIfEditChanged()
    /// Paged away: photos return to Fit, videos rewind (as in Photos).
    func pageDidHide()
    /// The viewer's chrome was shown or hidden (single tap).
    func setChrome(_ visible: Bool, animated: Bool)
}

/// A UIView whose layer is an AVPlayerLayer.
final class PlayerView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }   // swiftlint:disable:this force_cast
    var player: AVPlayer? {
        get { playerLayer.player }
        set { playerLayer.player = newValue }
    }
}

/// Things that must be undone when the page is freed. Held in a plain class so its deinit
/// (nonisolated) can clean up without touching main-actor state of the controller.
final class PlayerTokens: @unchecked Sendable {
    var player: AVPlayer?
    var timeObserver: Any?
    var endObserver: NSObjectProtocol?
    var loader: MediaStreamLoader?

    func clear() {
        if let t = timeObserver { player?.removeTimeObserver(t) }
        timeObserver = nil
        if let e = endObserver { NotificationCenter.default.removeObserver(e) }
        endObserver = nil
        loader?.cancelAll()
        loader = nil
    }

    deinit { clear() }
}

/// Audio session per kind of playback: videos play with sound even when the ring switch is on
/// silent (Photos does the same), Live Photos follow the switch and mix with other audio.
enum PlaybackAudio {
    static func video() {
        let s = AVAudioSession.sharedInstance()
        try? s.setCategory(.playback, mode: .moviePlayback)
        try? s.setActive(true)
    }

    static func live() {
        try? AVAudioSession.sharedInstance().setCategory(.ambient, mode: .default)
    }

    static func done() {
        Task.detached(priority: .utility) {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }
}

/// A video in the viewer. Streams from the desktop (MediaStreamLoader) so playback starts after
/// the first megabyte, or plays the local copy when one was downloaded before. When streaming
/// fails it falls back to a full download once. Starts playing when paged to and pauses when
/// paged away, as in Photos. The controls bar follows the viewer's chrome (single tap).
@MainActor
final class VideoPageController: UIViewController, ViewerPage {
    let id: MediaID
    let model: AppModel
    var onSingleTap: (() -> Void)?

    private let playerView = PlayerView()
    private let poster = UIImageView()
    private let spinner = UIActivityIndicatorView(style: .large)
    private let bigPlay = UIButton(type: .system)
    private let bar = UIVisualEffectView(effect: UIBlurEffect(style: .systemUltraThinMaterialDark))
    private let playPause = UIButton(type: .system)
    private let slider = UISlider()
    private let elapsed = UILabel()
    private let remaining = UILabel()
    private let muteButton = UIButton(type: .system)
    private let message = UILabel()

    private let tokens = PlayerTokens()
    private var player: AVPlayer?
    private var observations: [NSKeyValueObservation] = []
    private var scrubbing = false
    private var resumeAfterScrub = false
    private var seeking = false
    private var pendingSeek: Double?
    private var triedDownload = false
    private var visible = false
    private var chromeVisible = true
    private var failed = false
    private var fallbackTask: Task<Void, Never>?
    private var posterTask: Task<Void, Never>?

    /// Mute is remembered for the session, like Photos.
    private static var muted = false

    init(id: MediaID, model: AppModel) {
        self.id = id
        self.model = model
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    // ── layout ──

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .clear
        view.accessibilityLabel = model.store?.media(id)?.fileName

        playerView.playerLayer.videoGravity = .resizeAspect
        poster.contentMode = .scaleAspectFit
        poster.isUserInteractionEnabled = false
        for v in [playerView, poster] as [UIView] {
            v.frame = view.bounds
            v.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(v)
        }

        spinner.color = .white
        spinner.hidesWhenStopped = true

        let big = UIImage.SymbolConfiguration(pointSize: 64, weight: .regular)
        bigPlay.setImage(UIImage(systemName: "play.circle.fill", withConfiguration: big), for: .normal)
        bigPlay.tintColor = UIColor.white.withAlphaComponent(0.9)
        bigPlay.layer.shadowOpacity = 0.35
        bigPlay.layer.shadowRadius = 8
        bigPlay.layer.shadowOffset = .zero
        bigPlay.accessibilityLabel = "Play"
        bigPlay.addTarget(self, action: #selector(playTapped), for: .touchUpInside)
        bigPlay.isHidden = true

        message.textColor = .white
        message.font = .preferredFont(forTextStyle: .subheadline)
        message.numberOfLines = 0
        message.textAlignment = .center
        message.isHidden = true

        for v in [spinner, bigPlay, message] as [UIView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(v)
        }

        buildBar()

        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            bigPlay.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            bigPlay.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            message.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            message.centerYAnchor.constraint(equalTo: view.centerYAnchor, constant: 60),
            message.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 32),
            message.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -32),
        ])

        let tap = UITapGestureRecognizer(target: self, action: #selector(backgroundTapped))
        tap.cancelsTouchesInView = false
        tap.delegate = TapFilter.shared
        view.addGestureRecognizer(tap)

        loadPoster()
        preparePlayer()
        applyChrome(animated: false)
    }

    private func buildBar() {
        bar.layer.cornerRadius = 22
        bar.clipsToBounds = true
        bar.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(bar)

        let sym = UIImage.SymbolConfiguration(pointSize: 17, weight: .semibold)
        playPause.setImage(UIImage(systemName: "play.fill", withConfiguration: sym), for: .normal)
        playPause.tintColor = .white
        playPause.accessibilityLabel = "Play"
        playPause.addTarget(self, action: #selector(playPauseTapped), for: .touchUpInside)

        muteButton.tintColor = .white
        muteButton.addTarget(self, action: #selector(muteTapped), for: .touchUpInside)
        updateMuteIcon()

        for l in [elapsed, remaining] {
            l.textColor = .white
            l.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
            l.text = "0:00"
            l.setContentHuggingPriority(.required, for: .horizontal)
            l.setContentCompressionResistancePriority(.required, for: .horizontal)
        }
        remaining.text = "-0:00"

        slider.minimumTrackTintColor = .white
        slider.maximumTrackTintColor = UIColor.white.withAlphaComponent(0.3)
        let thumb = UIImage.SymbolConfiguration(pointSize: 12, weight: .bold)
        slider.setThumbImage(UIImage(systemName: "circle.fill", withConfiguration: thumb)?
            .withTintColor(.white, renderingMode: .alwaysOriginal), for: .normal)
        slider.accessibilityLabel = "Playback position"
        slider.addTarget(self, action: #selector(scrubBegan), for: .touchDown)
        slider.addTarget(self, action: #selector(scrubMoved), for: .valueChanged)
        slider.addTarget(self, action: #selector(scrubEnded), for: [.touchUpInside, .touchUpOutside, .touchCancel])

        let row = UIStackView(arrangedSubviews: [playPause, elapsed, slider, remaining, muteButton])
        row.axis = .horizontal
        row.alignment = .center
        row.spacing = 10
        row.translatesAutoresizingMaskIntoConstraints = false
        bar.contentView.addSubview(row)

        NSLayoutConstraint.activate([
            bar.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 16),
            bar.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -16),
            bar.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -8),
            bar.heightAnchor.constraint(equalToConstant: 44),
            row.leadingAnchor.constraint(equalTo: bar.contentView.leadingAnchor, constant: 12),
            row.trailingAnchor.constraint(equalTo: bar.contentView.trailingAnchor, constant: -12),
            row.centerYAnchor.constraint(equalTo: bar.contentView.centerYAnchor),
            playPause.widthAnchor.constraint(equalToConstant: 28),
            muteButton.widthAnchor.constraint(equalToConstant: 28),
        ])
    }

    // ── poster: the thumbnail until the first video frame is on screen ──

    private func loadPoster() {
        if let t = model.images.cachedThumbnail(id) ?? model.images.micro(id) {
            poster.image = UIImage(cgImage: t)
        }
        posterTask = Task { [weak self] in
            guard let self, let t = await self.model.images.thumbnail(self.id), !Task.isCancelled else { return }
            self.poster.image = UIImage(cgImage: t)
        }
    }

    // ── player ──

    private func preparePlayer() {
        if let local = model.images.cachedMediaFile(id, live: false) {
            attach(AVPlayerItem(url: local), streaming: false)
        } else if let client = model.images.client {
            let loader = MediaStreamLoader(client: client, id: id, live: false, fileExt: model.store?.media(id)?.fileExt)
            tokens.loader = loader
            let item = AVPlayerItem(asset: loader.makeAsset())
            item.preferredForwardBufferDuration = 6     // keeps the LAN share small while the grid loads
            attach(item, streaming: true)
        } else {
            fail(URLError(.notConnectedToInternet))
        }
    }

    private func attach(_ item: AVPlayerItem, streaming: Bool) {
        let p = player ?? AVPlayer()
        if let t = tokens.timeObserver { p.removeTimeObserver(t) }
        if let e = tokens.endObserver { NotificationCenter.default.removeObserver(e) }
        p.isMuted = Self.muted
        p.actionAtItemEnd = .pause
        p.replaceCurrentItem(with: item)
        player = p
        tokens.player = p
        playerView.player = p
        failed = false
        message.isHidden = true

        observations = [
            item.observe(\.status, options: [.new]) { [weak self] item, _ in
                Task { @MainActor in self?.statusChanged(item) }
            },
            playerView.playerLayer.observe(\.isReadyForDisplay, options: [.initial, .new]) { [weak self] layer, _ in
                let ready = layer.isReadyForDisplay
                Task { @MainActor in self?.poster.isHidden = ready }
            },
            p.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] _, _ in
                Task { @MainActor in self?.updateControls() }
            },
        ]
        tokens.timeObserver = p.addPeriodicTimeObserver(forInterval: CMTime(value: 1, timescale: 10), queue: .main) {
            [weak self] time in
            MainActor.assumeIsolated { self?.timeChanged(time) }
        }
        tokens.endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.reachedEnd() }
        }
        if visible { play() }
        updateControls()
    }

    private func statusChanged(_ item: AVPlayerItem) {
        guard item === player?.currentItem else { return }
        switch item.status {
        case .readyToPlay:
            spinner.stopAnimating()
            updateDuration()
        case .failed:
            let streamError = tokens.loader?.failure
            if tokens.loader != nil, !triedDownload, !Self.isFinal(streamError) {
                fallBackToDownload()
            } else {
                fail(streamError ?? item.error)
            }
        default:
            break
        }
    }

    /// Streaming failed (an old codec container AVFoundation can't read progressively, or the
    /// network dropped): fetch the whole file once and play it locally.
    private func fallBackToDownload() {
        triedDownload = true
        tokens.loader?.cancelAll()
        tokens.loader = nil
        spinner.startAnimating()
        bigPlay.isHidden = true
        fallbackTask = Task { [weak self] in
            guard let self else { return }
            switch await self.model.images.mediaFile(self.id, live: false) {
            case .success(let url):
                guard !Task.isCancelled else { return }
                self.attach(AVPlayerItem(url: url), streaming: false)
            case .failure(let error):
                self.fail(error)
            }
        }
    }

    /// Errors a retry cannot fix.
    private static func isFinal(_ error: Error?) -> Bool {
        guard let e = error as? SyncError else { return false }
        switch e {
        case .server(let code, _): return code == 404 || code == 409
        case .unauthorized: return true
        default: return false
        }
    }

    private func fail(_ error: Error?) {
        failed = true
        spinner.stopAnimating()
        bigPlay.isHidden = true
        poster.isHidden = false
        message.text = Self.explain(error)
        message.isHidden = false
        updateControls()
    }

    static func explain(_ error: Error?) -> String {
        if let e = error as? SyncError {
            switch e {
            case .server(409, _):
                return "This video is only in OneDrive on your computer. Make it available offline there to play it here."
            case .server(404, _):
                return "This video is no longer on your computer."
            case .unauthorized:
                return "This iPhone is no longer paired. Pair again to play videos."
            default:
                break
            }
        }
        if let u = error as? URLError, u.code != .cancelled {
            return "Connect to your computer to play this video."
        }
        return "This video can't be played."
    }

    // ── playback ──

    private func play() {
        guard let player, !failed else { return }
        PlaybackAudio.video()
        if let item = player.currentItem, item.duration.isNumeric,
           CMTimeCompare(player.currentTime(), item.duration) >= 0 {
            player.seek(to: .zero)
        }
        player.play()
    }

    private func pause() { player?.pause() }

    private func reachedEnd() {
        player?.pause()
        player?.seek(to: .zero, toleranceBefore: .zero, toleranceAfter: .zero)
        updateControls()
    }

    @objc private func playTapped() { play() }

    @objc private func playPauseTapped() {
        if player?.timeControlStatus == .paused { play() } else { pause() }
    }

    @objc private func muteTapped() {
        Self.muted.toggle()
        player?.isMuted = Self.muted
        updateMuteIcon()
    }

    @objc private func backgroundTapped() { onSingleTap?() }

    private func updateMuteIcon() {
        let sym = UIImage.SymbolConfiguration(pointSize: 15, weight: .semibold)
        muteButton.setImage(UIImage(systemName: Self.muted ? "speaker.slash.fill" : "speaker.wave.2.fill",
                                    withConfiguration: sym), for: .normal)
        muteButton.accessibilityLabel = Self.muted ? "Unmute" : "Mute"
    }

    private func updateControls() {
        let status = player?.timeControlStatus ?? .paused
        let paused = status == .paused && !scrubbing
        let sym = UIImage.SymbolConfiguration(pointSize: 17, weight: .semibold)
        playPause.setImage(UIImage(systemName: paused ? "play.fill" : "pause.fill", withConfiguration: sym), for: .normal)
        playPause.accessibilityLabel = paused ? "Play" : "Pause"
        if status == .waitingToPlayAtSpecifiedRate || (fallbackTask != nil && player?.currentItem?.status != .readyToPlay && !failed) {
            spinner.startAnimating()
        } else {
            spinner.stopAnimating()
        }
        bigPlay.isHidden = !paused || failed || player?.currentItem?.status != .readyToPlay
        bar.isUserInteractionEnabled = !failed
    }

    private func updateDuration() {
        guard let d = player?.currentItem?.duration, d.isNumeric, d.seconds > 0 else { return }
        slider.maximumValue = Float(d.seconds)
    }

    private func timeChanged(_ time: CMTime) {
        guard !scrubbing, time.isNumeric else { return }
        updateDuration()
        slider.value = Float(time.seconds)
        updateLabels(time.seconds)
    }

    private func updateLabels(_ seconds: Double) {
        let total = Double(slider.maximumValue)
        elapsed.text = Self.clock(seconds)
        remaining.text = "-" + Self.clock(max(0, total - seconds))
    }

    static func clock(_ s: Double) -> String {
        guard s.isFinite else { return "0:00" }
        let t = Int(s.rounded(.down))
        return t >= 3600 ? String(format: "%d:%02d:%02d", t / 3600, t / 60 % 60, t % 60)
                         : String(format: "%d:%02d", t / 60, t % 60)
    }

    // ── scrubbing: frame-accurate seeks, chained so the slider never queues a backlog ──

    @objc private func scrubBegan() {
        scrubbing = true
        resumeAfterScrub = player?.timeControlStatus != .paused
        player?.pause()
        pagerScrollView()?.isScrollEnabled = false        // a horizontal drag scrubs, not pages
    }

    @objc private func scrubMoved() {
        updateLabels(Double(slider.value))
        seek(to: Double(slider.value))
    }

    @objc private func scrubEnded() {
        scrubbing = false
        pagerScrollView()?.isScrollEnabled = true
        if resumeAfterScrub { play() }
        updateControls()
    }

    private func seek(to seconds: Double) {
        guard let player else { return }
        if seeking {
            pendingSeek = seconds
            return
        }
        seeking = true
        player.seek(to: CMTime(seconds: seconds, preferredTimescale: 600), toleranceBefore: .zero,
                    toleranceAfter: .zero) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.seeking = false
                if let next = self.pendingSeek {
                    self.pendingSeek = nil
                    self.seek(to: next)
                }
            }
        }
    }

    private func pagerScrollView() -> UIScrollView? {
        var v = view.superview
        while let cur = v {
            if let s = cur as? UIScrollView { return s }
            v = cur.superview
        }
        return nil
    }

    // ── page lifecycle ──

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        visible = true
        // The slider must get its touch at once, or the pager steals a horizontal drag first.
        pagerScrollView()?.delaysContentTouches = false
        play()
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        visible = false
        pause()
        PlaybackAudio.done()
    }

    func reloadIfEditChanged() {}                       // videos have no edits

    func pageDidHide() {
        pause()
        player?.seek(to: .zero)
    }

    func setChrome(_ visible: Bool, animated: Bool) {
        chromeVisible = visible
        if isViewLoaded { applyChrome(animated: animated) }
    }

    private func applyChrome(animated: Bool) {
        let apply = { self.bar.alpha = self.chromeVisible ? 1 : 0 }
        if animated { UIView.animate(withDuration: 0.2, animations: apply) } else { apply() }
        bar.isUserInteractionEnabled = chromeVisible && !failed
    }
}

/// Lets the page's background tap through only when it did not land on a control.
@MainActor
final class TapFilter: NSObject, UIGestureRecognizerDelegate {
    static let shared = TapFilter()

    func gestureRecognizer(_ g: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        var v = touch.view
        while let cur = v {
            if cur is UIControl || cur is UIVisualEffectView { return false }
            v = cur.superview
        }
        return true
    }
}
#endif
