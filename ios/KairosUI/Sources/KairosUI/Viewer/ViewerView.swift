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
struct ViewerView: View {
    let ids: [MediaID]
    @State private var current: MediaID
    @State private var editing: EditRoute?
    @State private var chrome = true
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
            PhotoPager(ids: ids, current: $current, revision: model.revision, model: model,
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
            Spacer()
            let fav = model.store?.media(current)?.isFavorite == true
            Button { Task { await model.toggleFavorite(current) } } label: {
                Image(systemName: fav ? "heart.fill" : "heart").padding(10)
            }
            .accessibilityLabel(fav ? "Remove from favourites" : "Add to favourites")
            Button { editing = EditRoute(id: current) } label: { Image(systemName: "slider.horizontal.3").padding(10) }
                .accessibilityLabel("Edit")
                .disabled(model.store?.media(current)?.isVideo == true)
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
import UIKit

/// UIPageViewController pager: native paging physics, 20 pt between photos, and pages created
/// lazily (the neighbours preload), so a 20 000-photo list costs nothing up front.
struct PhotoPager: UIViewControllerRepresentable {
    let ids: [MediaID]
    @Binding var current: MediaID
    let revision: Int
    let model: AppModel
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
        if let shown = pvc.viewControllers?.first as? PhotoPageController, shown.id != current,
           let to = c.index[current] {
            let from = c.index[shown.id] ?? 0
            pvc.setViewControllers([c.page(for: current)], direction: to > from ? .forward : .reverse, animated: false)
        }
    }

    @MainActor
    final class Coordinator: NSObject, UIPageViewControllerDataSource, UIPageViewControllerDelegate {
        var parent: PhotoPager
        var revision: Int
        let index: [MediaID: Int]
        private let pages = NSMapTable<NSNumber, PhotoPageController>.strongToWeakObjects()

        init(_ parent: PhotoPager) {
            self.parent = parent
            revision = parent.revision
            var idx: [MediaID: Int] = [:]
            idx.reserveCapacity(parent.ids.count)
            for (i, id) in parent.ids.enumerated() where idx[id] == nil { idx[id] = i }
            index = idx
        }

        func page(for id: MediaID) -> PhotoPageController {
            if let live = pages.object(forKey: NSNumber(value: id)) { return live }
            let p = PhotoPageController(id: id, model: parent.model)
            p.onSingleTap = { [weak self] in self?.parent.onSingleTap() }
            pages.setObject(p, forKey: NSNumber(value: id))
            return p
        }

        func livePages() -> [PhotoPageController] {
            pages.objectEnumerator()?.allObjects.compactMap { $0 as? PhotoPageController } ?? []
        }

        private func neighbour(of vc: UIViewController, _ step: Int) -> UIViewController? {
            guard let p = vc as? PhotoPageController, let i = index[p.id] else { return nil }
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
            guard completed, let shown = pvc.viewControllers?.first as? PhotoPageController else { return }
            parent.current = shown.id
            for case let old as PhotoPageController in previousViewControllers where old !== shown {
                old.zoom.resetZoom()                     // coming back shows the photo at Fit, as in Photos
            }
        }
    }
}

/// One page: a ZoomingImageView fed by the loading passes.
@MainActor
final class PhotoPageController: UIViewController {
    let id: MediaID
    let model: AppModel
    let zoom = ZoomingImageView()
    var onSingleTap: (() -> Void)?
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

    override func loadView() { view = zoom }

    override func viewDidLoad() {
        super.viewDidLoad()
        zoom.onSingleTap = { [weak self] in self?.onSingleTap?() }
        zoom.onNeedsDetail = { [weak self] in self?.loadDetail() }
        zoom.accessibilityLabel = model.store?.media(id)?.fileName
        reload()
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
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
