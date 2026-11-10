import CoreGraphics
import KairosCore
import SwiftUI

/// Full-screen viewer (Phase 2): horizontal paging between the photos of the list that opened
/// it. Each page has native pinch zoom up to 400 % of 1:1, double-tap Fit <-> 1:1, and
/// rubber-banding with a spring settle (ZoomingImageView). Pixels arrive in passes: the micro
/// LQIP or cached thumbnail at once, then the edited 840 px thumbnail, then the original decoded
/// for the screen. When a zoom passes what that holds, a detail pass decodes up to 6000 px.
/// The slider button opens the touch editor (EditView). It writes the same `media_edits` JSON
/// as the desktop's Quick Edit.
/// Videos are pages of their own (VideoPageController: streamed, autoplay, scrubber). Live
/// Photos play their motion while pressed and held, or once from the LIVE button.
struct ViewerView: View {
    let ids: [MediaID]
    @State private var current: MediaID
    @State private var editing: EditRoute?
    @State private var chrome = true
    @State private var livePlay = 0
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    struct EditRoute: Identifiable {
        let id: MediaID
    }

    init(ids: [MediaID], start: MediaID) {
        self.ids = ids
        _current = State(initialValue: start)
    }

    var body: some View {
        ZStack(alignment: .top) {
            Color.black.ignoresSafeArea()
            #if os(iOS)
            PhotoPager(ids: ids, current: $current, revision: model.revision, model: model, chrome: chrome,
                       livePlay: livePlay,
                       onSingleTap: { withAnimation(Kairos.Motion.standard) { chrome.toggle() } })
                .ignoresSafeArea()
            #else
            TabView(selection: $current) {
                ForEach(ids, id: \.self) { id in FallbackPage(id: id).tag(id) }
            }
            #endif
            if chrome {
                toolbar
                    .transition(.opacity)
            }
        }
        .preferredColorScheme(.dark)
        .statusBarHiddenCompat(!chrome)
        .fullScreenCoverCompat(item: $editing) { route in
            EditView(id: route.id)
        }
    }

    private var toolbar: some View {
        HStack(spacing: 2) {
            Button { dismiss() } label: { Image(systemName: "xmark").padding(10) }
                .accessibilityLabel("Close")
            if model.store?.media(current)?.live == true {
                Button { livePlay += 1 } label: {
                    Label("LIVE", systemImage: "livephoto")
                        .font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 10)
                }
                .accessibilityLabel("Play Live Photo")
                .accessibilityHint("Or touch and hold the photo")
            }
            Spacer()
            if current < 0 {
                // three-state library: this one exists only in the camera roll so far
                Label("iPhone only", systemImage: "iphone")
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 8)
                    .accessibilityLabel("Only on this iPhone, not backed up to your computer yet")
            }
            let fav = model.store?.media(current)?.isFavorite == true
            Button { Task { await model.toggleFavorite(current) } } label: {
                Image(systemName: fav ? "heart.fill" : "heart").padding(10)
            }
            .accessibilityLabel(fav ? "Remove from favourites" : "Add to favourites")
            .disabled(current < 0)                       // favourites and edits live on the desktop
            Button { editing = EditRoute(id: current) } label: { Image(systemName: "slider.horizontal.3").padding(10) }
                .accessibilityLabel("Edit")
                .disabled(model.store?.media(current)?.isVideo == true || current < 0)
        }
        .font(.system(size: 17, weight: .semibold))
        .foregroundStyle(.white)
        .padding(.horizontal, 4)
        .background(.ultraThinMaterial, in: Capsule())
        .padding(.horizontal, Kairos.Space.lg)
    }
}

extension View {
    @ViewBuilder
    func statusBarHiddenCompat(_ hidden: Bool) -> some View {
        #if os(iOS)
        statusBarHidden(hidden)
        #else
        self
        #endif
    }
}

#if os(iOS)
import AVFoundation
import Photos
import PhotosUI
import UIKit

/// UIPageViewController pager: native paging physics, 20 pt between photos, and pages created
/// lazily (the neighbours preload), so a 20 000-photo list costs nothing up front.
struct PhotoPager: UIViewControllerRepresentable {
    let ids: [MediaID]
    @Binding var current: MediaID
    let revision: Int
    let model: AppModel
    let chrome: Bool
    let livePlay: Int
    var onSingleTap: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIViewController(context: Context) -> UIPageViewController {
        let pvc = UIPageViewController(transitionStyle: .scroll, navigationOrientation: .horizontal,
                                       options: [.interPageSpacing: 20])
        pvc.dataSource = context.coordinator
        pvc.delegate = context.coordinator
        pvc.view.backgroundColor = .clear
        pvc.setViewControllers([context.coordinator.page(for: current)], direction: .forward, animated: false)
        return pvc
    }

    func updateUIViewController(_ pvc: UIPageViewController, context: Context) {
        let c = context.coordinator
        c.parent = self
        if c.revision != revision {
            c.revision = revision
            c.livePages().forEach { $0.reloadIfEditChanged() }
        }
        if c.chrome != chrome {
            c.chrome = chrome
            c.livePages().forEach { $0.setChrome(chrome, animated: true) }
        }
        if c.livePlay != livePlay {
            c.livePlay = livePlay
            (pvc.viewControllers?.first as? PhotoPageController)?.playLive(held: false)
        }
        if let shown = pvc.viewControllers?.first as? ViewerPage, shown.id != current,
           let to = c.index[current] {
            let from = c.index[shown.id] ?? 0
            pvc.setViewControllers([c.page(for: current)], direction: to > from ? .forward : .reverse, animated: false)
        }
    }

    @MainActor
    final class Coordinator: NSObject, UIPageViewControllerDataSource, UIPageViewControllerDelegate {
        var parent: PhotoPager
        var revision: Int
        var chrome: Bool
        var livePlay: Int
        let index: [MediaID: Int]
        private let pages = NSMapTable<NSNumber, UIViewController>.strongToWeakObjects()

        init(_ parent: PhotoPager) {
            self.parent = parent
            revision = parent.revision
            chrome = parent.chrome
            livePlay = parent.livePlay
            var idx: [MediaID: Int] = [:]
            idx.reserveCapacity(parent.ids.count)
            for (i, id) in parent.ids.enumerated() where idx[id] == nil { idx[id] = i }
            index = idx
        }

        func page(for id: MediaID) -> UIViewController {
            if let live = pages.object(forKey: NSNumber(value: id)) { return live }
            let p: ViewerPage = parent.model.store?.media(id)?.isVideo == true
                ? VideoPageController(id: id, model: parent.model)
                : PhotoPageController(id: id, model: parent.model)
            p.onSingleTap = { [weak self] in self?.parent.onSingleTap() }
            p.setChrome(chrome, animated: false)
            pages.setObject(p, forKey: NSNumber(value: id))
            return p
        }

        func livePages() -> [ViewerPage] {
            pages.objectEnumerator()?.allObjects.compactMap { $0 as? ViewerPage } ?? []
        }

        private func neighbour(of vc: UIViewController, _ step: Int) -> UIViewController? {
            guard let p = vc as? ViewerPage, let i = index[p.id] else { return nil }
            let j = i + step
            guard parent.ids.indices.contains(j) else { return nil }
            return page(for: parent.ids[j])
        }

        func pageViewController(_ pvc: UIPageViewController, viewControllerBefore vc: UIViewController) -> UIViewController? {
            neighbour(of: vc, -1)
        }

        func pageViewController(_ pvc: UIPageViewController, viewControllerAfter vc: UIViewController) -> UIViewController? {
            neighbour(of: vc, +1)
        }

        func pageViewController(_ pvc: UIPageViewController, didFinishAnimating finished: Bool,
                                previousViewControllers: [UIViewController], transitionCompleted completed: Bool) {
            guard completed, let shown = pvc.viewControllers?.first as? ViewerPage else { return }
            parent.current = shown.id
            for case let old as ViewerPage in previousViewControllers where old !== shown {
                old.pageDidHide()                        // photos return to Fit, videos rewind (as in Photos)
            }
        }
    }
}

/// One page: a ZoomingImageView fed by the loading passes. A Live Photo also gets a motion layer
/// above it, shown over the photo's frame while the motion plays.
@MainActor
final class PhotoPageController: UIViewController, ViewerPage, PHLivePhotoViewDelegate {
    let id: MediaID
    let model: AppModel
    let zoom = ZoomingImageView()
    var onSingleTap: (() -> Void)?
    /// Camera-roll Live Photos play natively (PHLivePhotoView: Apple's timing, sound and fade);
    /// desktop-only ones stream their motion half into `motionView`.
    private let liveView = PHLivePhotoView()
    private var localLive: PHLivePhoto?
    private var localLiveTask: Task<PHLivePhoto?, Never>?
    private let motionView = PlayerView()
    private let motionTokens = PlayerTokens()
    private var motionPlayer: AVPlayer?
    private var liveTask: Task<Void, Never>?
    private var livePlaying = false
    private var liveHeld = false
    private var loadTask: Task<Void, Never>?
    private var detailTask: Task<Void, Never>?
    private var hasOriginal = false
    private var hasDetail = false
    private var editKey: Data?

    static let screenPassPixels = 3000
    static let detailPassPixels = 6000              // ~108 MB RGBA at 4:3, one page at a time

    init(id: MediaID, model: AppModel) {
        self.id = id
        self.model = model
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    private var isLive: Bool { model.store?.media(id)?.live == true }

    override func loadView() {
        let root = UIView()
        root.backgroundColor = .clear
        zoom.frame = root.bounds
        zoom.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        root.addSubview(zoom)
        motionView.isUserInteractionEnabled = false
        motionView.playerLayer.videoGravity = .resizeAspectFill
        motionView.clipsToBounds = true
        motionView.alpha = 0
        root.addSubview(motionView)
        liveView.isUserInteractionEnabled = false
        liveView.contentMode = .scaleAspectFill
        liveView.clipsToBounds = true
        liveView.isMuted = false
        liveView.alpha = 0
        liveView.delegate = self
        root.addSubview(liveView)
        view = root
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        zoom.onSingleTap = { [weak self] in self?.onSingleTap?() }
        zoom.onNeedsDetail = { [weak self] in self?.loadDetail() }
        zoom.onZoomedChange = { [weak self] zoomed in if zoomed { self?.stopLive(animated: true) } }
        zoom.accessibilityLabel = model.store?.media(id)?.fileName
        if isLive {
            let hold = UILongPressGestureRecognizer(target: self, action: #selector(held(_:)))
            hold.minimumPressDuration = 0.25
            zoom.addGestureRecognizer(hold)
            zoom.accessibilityHint = "Live Photo. Touch and hold to play."
        }
        reload()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        // The motion half is a few MB: fetch it while the user looks, so a hold plays at once.
        // A camera-roll Live Photo is prepared from PhotoKit instead (no network).
        if isLive, model.images.localAsset(id) != nil {
            _ = loadLocalLive()
        } else if isLive, id > 0, model.images.cachedMediaFile(id, live: true) == nil {
            let images = model.images, id = self.id
            Task { _ = await images.mediaFile(id, live: true) }
        }
    }

    /// The PHLivePhoto of a camera-roll asset, requested once and shared by prefetch and play.
    @discardableResult
    private func loadLocalLive() -> Task<PHLivePhoto?, Never>? {
        if let localLiveTask { return localLiveTask }
        guard let phone = model.phone, let asset = model.images.localAsset(id) else { return nil }
        let scale = view.window?.screen.scale ?? 3
        let size = CGSize(width: view.bounds.width * scale, height: view.bounds.height * scale)
        let allowNetwork = id < 0
        let t = Task { [weak self] () -> PHLivePhoto? in
            let lp = await phone.livePhoto(asset.localId, targetSize: size, allowNetwork: allowNetwork)
            self?.localLive = lp
            return lp
        }
        localLiveTask = t
        return t
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        stopLive(animated: false)
        detailTask?.cancel()
        if hasDetail {                               // give the big bitmap back
            hasDetail = false
            reload()
        }
    }

    private func currentEditKey() -> Data? {
        try? model.store?.edit(for: id)?.jsonData()
    }

    func reloadIfEditChanged() {
        if currentEditKey() != editKey { reload() }
    }

    func pageDidHide() { zoom.resetZoom() }

    func setChrome(_ visible: Bool, animated: Bool) {}

    // ── Live Photo ──

    @objc private func held(_ g: UILongPressGestureRecognizer) {
        switch g.state {
        case .began: playLive(held: true)
        case .ended, .cancelled, .failed: if liveHeld { stopLive(animated: true) }
        default: break
        }
    }

    /// Plays the motion half over the photo, at Fit only (zoomed in, the frames would not line
    /// up). Held: until the finger lifts or the clip ends. From the LIVE button: once through.
    func playLive(held: Bool) {
        guard isLive, !zoom.isZoomedIn, !livePlaying else { return }
        livePlaying = true
        liveHeld = held
        Haptics.live()
        liveTask?.cancel()
        liveTask = Task { [weak self] in
            guard let self else { return }
            if let lp = await self.loadLocalLive()?.value, !Task.isCancelled, self.livePlaying {
                self.startLocal(lp)
                return
            }
            guard self.id > 0 else {                    // phone-only and unreadable: nothing to play
                self.livePlaying = false
                return
            }
            guard case .success(let url) = await self.model.images.mediaFile(self.id, live: true),
                  !Task.isCancelled, self.livePlaying else {
                self.livePlaying = false
                return
            }
            self.startMotion(url)
        }
    }

    private func startMotion(_ url: URL) {
        let player: AVPlayer
        if let p = motionPlayer, (p.currentItem?.asset as? AVURLAsset)?.url == url {
            player = p
        } else {
            motionTokens.clear()
            player = AVPlayer(url: url)
            player.actionAtItemEnd = .pause
            motionPlayer = player
            motionTokens.player = player
            motionView.player = player
            motionTokens.endObserver = NotificationCenter.default.addObserver(
                forName: .AVPlayerItemDidPlayToEndTime, object: player.currentItem, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.stopLive(animated: true) }
            }
        }
        // the photo's on-screen rect at Fit
        motionView.frame = zoom.imageView.convert(zoom.imageView.bounds, to: view)
        PlaybackAudio.live()
        player.seek(to: .zero, toleranceBefore: .zero, toleranceAfter: .zero)
        player.play()
        UIView.animate(withDuration: 0.18) { self.motionView.alpha = 1 }
    }

    private func startLocal(_ lp: PHLivePhoto) {
        liveView.livePhoto = lp
        liveView.frame = zoom.imageView.convert(zoom.imageView.bounds, to: view)
        PlaybackAudio.live()
        liveView.alpha = 1
        liveView.startPlayback(with: .full)
    }

    nonisolated func livePhotoView(_ livePhotoView: PHLivePhotoView, didEndPlaybackWith playbackStyle: PHLivePhotoViewPlaybackStyle) {
        MainActor.assumeIsolated { self.stopLive(animated: true) }
    }

    func stopLive(animated: Bool) {
        guard livePlaying else { return }
        livePlaying = false
        liveHeld = false
        liveTask?.cancel()
        liveView.stopPlayback()
        let player = motionPlayer
        let hide = {
            self.motionView.alpha = 0
            self.liveView.alpha = 0
        }
        if animated {
            UIView.animate(withDuration: 0.25, animations: hide) { _ in player?.pause() }
        } else {
            hide()
            player?.pause()
        }
    }

    func reload() {
        loadTask?.cancel()
        detailTask?.cancel()
        hasOriginal = false
        hasDetail = false
        editKey = currentEditKey()
        if let t = model.images.cachedThumbnail(id) {
            show(t, nil)
        } else if let m = model.images.micro(id) {
            show(m, nil)
        }
        loadTask = Task { [weak self] in
            guard let self else { return }
            if let t = await self.model.images.thumbnail(self.id), !Task.isCancelled { self.show(t, nil) }
            if let o = await self.model.images.original(self.id, maxPixel: Self.screenPassPixels), !Task.isCancelled {
                self.show(o.image, o.pixelSize)
                self.hasOriginal = true
            }
        }
    }

    private func loadDetail() {
        guard hasOriginal, !hasDetail, detailTask == nil || detailTask?.isCancelled == true else { return }
        detailTask = Task { [weak self] in
            guard let self else { return }
            if let o = await self.model.images.original(self.id, maxPixel: Self.detailPassPixels), !Task.isCancelled {
                self.show(o.image, o.pixelSize)
                self.hasDetail = true
            }
            self.detailTask = nil
        }
    }

    /// Before the original's real size is known, a bitmap stands in at its own size. Once the size
    /// is known, every later bitmap of the same aspect is stretched over it, so the zoom never jumps.
    private func show(_ image: CGImage, _ pixelSize: CGSize?) {
        var size = pixelSize ?? CGSize(width: image.width, height: image.height)
        if pixelSize == nil, zoom.pixelSize.width > 0 {
            let a = zoom.pixelSize.width / zoom.pixelSize.height, b = CGFloat(image.width) / CGFloat(image.height)
            if abs(a - b) / a < 0.02 { size = zoom.pixelSize }
        }
        zoom.display(image, pixelSize: size)
    }
}
#else
/// macOS package builds (no UIKit): a plain page so the shell compiles and previews.
struct FallbackPage: View {
    let id: MediaID
    @Environment(AppModel.self) private var model
    @State private var image: CGImage?

    var body: some View {
        ZStack {
            if let img = image ?? model.images.cachedThumbnail(id) ?? model.images.micro(id) {
                Image(decorative: img, scale: 1).resizable().scaledToFit()
            } else {
                ProgressView()
            }
        }
        .task(id: model.revision) { image = await model.images.thumbnail(id) }
    }
}
#endif
