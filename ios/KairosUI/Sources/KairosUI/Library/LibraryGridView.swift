import KairosCore
import SwiftUI

/// Library: one newest-first timeline of the desktop library AND the camera roll (three-state:
/// PC only, on both, iPhone only), grouped under sticky date headers (days at 3-4 columns,
/// months at 5-6, years at 7) with a fast-scroll scrubber on the right edge. Pinch changes the
/// density (3…7 columns, Photos-style). The micro LQIP shows at once and the sharp thumbnail
/// when it arrives, so a fling never shows empty cells (the desktop's decode_queue contract).
/// Camera-roll pixels come from PhotoKit in place, desktop ones from the disk cache or the LAN.
struct LibraryGridView: View {
    @Environment(AppModel.self) private var model
    @State private var columns = 4
    @State private var pinchBase: Int?
    @State private var favoritesOnly = false
    @State private var items: [LibraryStore.TimelineEntry] = []
    @State private var sections: [TimelineSection] = []
    @State private var viewer: ViewerRoute?
    @State private var picking = false
    @State private var showHidden = false
    @State private var importNote: String?
    @State private var scrollFraction = 0.0
    @State private var scrolling = false

    private static let minColumns = 3, maxColumns = 7

    var body: some View {
        NavigationStack {
            Group {
                if items.isEmpty {
                    VStack(spacing: 0) {
                        banners.padding(Kairos.Space.md)
                        EmptyStateView(symbol: "photo.on.rectangle.angled", title: "No photos yet",
                                       message: model.isPaired ? "Your library is syncing from Kairos on your computer."
                                                               : "Pair with Kairos on your computer to see your library.")
                    }
                } else {
                    grid
                }
            }
            .navigationTitle("Library")
            .toolbar {
                ToolbarItem(placement: .principal) { SyncBadge() }
                #if os(iOS)
                ToolbarItem(placement: .topBarLeading) {
                    Button { picking = true } label: { Image(systemName: "plus") }
                        .disabled(!model.isPaired)
                        .accessibilityLabel("Send photos from iPhone to your computer")
                }
                #endif
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Toggle("Favourites only", isOn: $favoritesOnly)
                        #if os(iOS)
                        Toggle("Back up automatically", isOn: Binding(get: { model.automaticBackup },
                                                                      set: { model.automaticBackup = $0 }))
                        #endif
                        Button("Hidden folder", systemImage: "lock") { showHidden = true }
                        if model.isPaired {
                            Button("Unpair this iPhone", systemImage: "link.badge.plus", role: .destructive) {
                                Task { await model.unpair() }
                            }
                        }
                    } label: { Image(systemName: "ellipsis.circle") }
                }
            }
            .kairosNavigationChrome()
            .background(Kairos.Palette.background)
            .safeAreaInset(edge: .bottom) { importPill }
        }
        #if os(iOS)
        .sheet(isPresented: $picking) {
            PhotoPicker(onPicked: { picked, skipped in
                importNote = skipped > 0 ? "\(skipped) item(s) couldn't be read" : nil
                Task { await model.importPicked(picked.map { (file: $0.file, name: $0.name) }) }
            }, close: { picking = false })
            .ignoresSafeArea()
        }
        #endif
        .sheet(isPresented: $showHidden) { HiddenFolderView() }
        .task(id: TaskKey(revision: model.revision, favorites: favoritesOnly)) {
            items = model.store?.timeline(favoritesOnly: favoritesOnly) ?? []
            regroup()
        }
        .onChange(of: columns) { old, new in
            if TimelineGrouping.forColumns(old) != TimelineGrouping.forColumns(new) { regroup() }
        }
        .fullScreenCoverCompat(item: $viewer) { route in
            ViewerView(ids: route.ids, start: route.start)
        }
    }

    private func regroup() {
        sections = TimelineSections.build(items, grouping: .forColumns(columns), columns: columns)
    }

    // ── banners: camera-roll access, backup ──

    @ViewBuilder
    private var banners: some View {
        #if os(iOS)
        VStack(spacing: Kairos.Space.sm) {
            if model.phoneAccess == .notDetermined {
                BannerCard(symbol: "iphone", title: "Show your iPhone photos here",
                           message: "Kairos reads your camera roll in place — nothing is copied — and marks what isn't on your computer yet.",
                           action: "Allow") { Task { await model.enablePhoneLibrary() } }
            } else if model.phoneIndex.indexing && model.phoneIndex.total > 0 {
                BannerCard(symbol: "arrow.triangle.2.circlepath",
                           title: "Reading your camera roll… \(model.phoneIndex.done) of \(model.phoneIndex.total)",
                           message: nil, action: nil, onAction: nil)
            }
            let c = model.phoneCounts
            if c.approved + c.queued > 0 && model.isPaired {
                BannerCard(symbol: "icloud.and.arrow.up",
                           title: "Backing up \(c.approved + c.queued) to \(model.endpoint?.name ?? "your computer")…",
                           message: model.status == .live ? nil : "Continues when your computer is reachable.",
                           action: nil, onAction: nil)
            } else if c.waiting > 0 && model.isPaired {
                BannerCard(symbol: "iphone.and.arrow.forward",
                           title: "\(c.waiting) only on this iPhone",
                           message: "Ready to back up to \(model.endpoint?.name ?? "your computer").",
                           action: "Back up") { model.backUpAll() }
            }
            // the desktop refused some files for good: say why, and let the user ask again
            if c.failed > 0 && model.isPaired {
                BannerCard(symbol: "exclamationmark.triangle",
                           title: "\(c.failed) couldn't be backed up",
                           message: BackupReason.text(c.failedReason),
                           action: "Retry") { model.retryFailedBackups() }
            }
            // the desktop can't take uploads right now (disk full, busy, saving power): nothing is lost
            if let why = model.importProgress.paused, model.isPaired {
                BannerCard(symbol: "pause.circle",
                           title: "Backup paused",
                           message: BackupReason.pausedText(why),
                           action: "Retry now") { model.retryBackupNow() }
            }
        }
        #else
        EmptyView()
        #endif
    }

    /// "Sending 3 to DESKTOP…" while camera-roll uploads are queued; hidden otherwise.
    @ViewBuilder
    private var importPill: some View {
        let p = model.importProgress
        if p.pending > 0 || importNote != nil || !p.rejected.isEmpty {
            HStack(spacing: Kairos.Space.sm) {
                if p.pending > 0 {
                    if p.uploading != nil { ProgressView().controlSize(.small) }
                    else { Image(systemName: "icloud.and.arrow.up") }
                    Text(p.uploading != nil ? "Sending \(p.pending) to \(model.endpoint?.name ?? "your computer")…"
                                            : "\(p.pending) waiting for your computer")
                } else if !p.rejected.isEmpty {
                    Image(systemName: "exclamationmark.triangle")
                    Text("\(p.rejected.count) not sent. \(BackupReason.text(p.rejectedReason)) Tap to dismiss.")
                } else if let importNote {
                    Image(systemName: "exclamationmark.triangle")
                    Text(importNote)
                }
            }
            .font(Kairos.Typography.caption)
            .foregroundStyle(Kairos.Palette.text)
            .padding(.horizontal, Kairos.Space.md)
            .padding(.vertical, Kairos.Space.sm)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.bottom, Kairos.Space.sm)
            .transition(.move(edge: .bottom).combined(with: .opacity))
            .onTapGesture {
                // a finished notice can be dismissed; a live "Sending..." pill can't
                if p.pending == 0 {
                    model.dismissImportNotices()
                    importNote = nil
                }
            }
        }
    }

    private struct TaskKey: Equatable {
        let revision: Int
        let favorites: Bool
    }

    // ── grid ──

    private var grid: some View {
        ScrollViewReader { proxy in
            ScrollView {
                banners
                    .padding(.horizontal, Kairos.Space.md)
                    .padding(.vertical, Kairos.Space.sm)
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: columns), spacing: 2,
                          pinnedViews: [.sectionHeaders]) {
                    ForEach(sections) { section in
                        Section {
                            ForEach(section.entries) { entry in
                                ThumbnailView(id: entry.id)
                                    .aspectRatio(1, contentMode: .fit)
                                    .overlay(alignment: .bottomTrailing) { badges(entry) }
                                    .contentShape(Rectangle())
                                    .onTapGesture { viewer = ViewerRoute(ids: items.map(\.id), start: entry.id) }
                                    .accessibilityLabel(entry.dateTaken ?? "Photo")
                            }
                        } header: {
                            SectionHeader(section: section)
                        }
                    }
                }
                .animation(Kairos.Motion.spring, value: columns)
            }
            .scrollIndicators(.hidden)
            .onScrollGeometryChange(for: Double.self) { g in
                let range = g.contentSize.height - g.containerSize.height + g.contentInsets.top + g.contentInsets.bottom
                return range > 1 ? Double((g.contentOffset.y + g.contentInsets.top) / range) : 0
            } action: { _, f in
                scrollFraction = min(1, max(0, f))
            }
            .onScrollPhaseChange { _, phase in
                scrolling = phase != .idle
            }
            .overlay(alignment: .trailing) {
                if sections.count > 3 {
                    TimelineScrubber(sections: sections, fraction: scrollFraction, active: scrolling) { i in
                        guard sections.indices.contains(i), let first = sections[i].entries.first else { return }
                        proxy.scrollTo(first.id, anchor: .top)
                    }
                    .padding(.vertical, Kairos.Space.lg)
                }
            }
        }
        .simultaneousGesture(
            MagnifyGesture()
                .onChanged { value in
                    let base = pinchBase ?? columns
                    pinchBase = base
                    // spread = fewer, larger cells
                    let target = Int((Double(base) / value.magnification).rounded())
                    columns = min(Self.maxColumns, max(Self.minColumns, target))
                }
                .onEnded { _ in pinchBase = nil }
        )
    }

    @ViewBuilder
    private func badges(_ e: LibraryStore.TimelineEntry) -> some View {
        HStack(spacing: 3) {
            if e.origin == .phone { Image(systemName: "iphone") }        // not on the computer yet
            if e.isFavorite { Image(systemName: "heart.fill") }
            if e.live { Image(systemName: "livephoto") }
            if e.isVideo { Image(systemName: "play.fill") }
        }
        .font(Kairos.Typography.badge)
        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)         // a grid cell has a fixed size
        .foregroundStyle(.white)
        .shadow(radius: 2)
        .padding(4)
        .accessibilityLabel(e.origin == .phone ? "Only on this iPhone" : "")
    }
}

/// A sticky date header: "Wed 1 July 2026", "July 2026" or "2026".
struct SectionHeader: View {
    let section: TimelineSection

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Kairos.Space.sm) {
            Text(SectionHeader.title(section))
                .font(Kairos.Typography.sectionHeader)
                .foregroundStyle(Kairos.Palette.text)
                .lineLimit(2)
            Text("\(section.entries.count)")
                .font(Kairos.Typography.counter)
                .foregroundStyle(Kairos.Palette.textTertiary)
                .fixedSize()
            Spacer()
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
        .padding(.horizontal, Kairos.Space.md)
        .padding(.vertical, Kairos.Space.sm)
        .background(.ultraThinMaterial)
    }

    static func date(_ s: TimelineSection) -> Date? {
        guard let y = s.year else { return nil }
        return Calendar(identifier: .gregorian).date(from: DateComponents(year: y, month: s.month ?? 1, day: s.day ?? 1))
    }

    static func title(_ s: TimelineSection) -> String {
        guard let d = date(s) else { return "Undated" }
        if s.day != nil { return d.formatted(.dateTime.weekday(.abbreviated).day().month(.wide).year()) }
        if s.month != nil { return d.formatted(.dateTime.month(.wide).year()) }
        return d.formatted(.dateTime.year())
    }

    /// The scrubber's bubble: shorter.
    static func short(_ s: TimelineSection) -> String {
        guard let d = date(s) else { return "Undated" }
        if s.month != nil { return d.formatted(.dateTime.month(.abbreviated).year()) }
        return d.formatted(.dateTime.year())
    }
}

/// Fast-scroll scrubber: a handle on the right edge that follows the scroll position and appears
/// while scrolling. Dragging it jumps through the timeline, with a bubble naming the month under
/// the finger and a haptic tick on every month (a firmer one on every year).
struct TimelineScrubber: View {
    let sections: [TimelineSection]
    let fraction: Double
    let active: Bool
    var onScrub: (Int) -> Void

    @State private var dragging = false
    @State private var dragFraction = 0.0
    @State private var lastIndex = -1
    @State private var lingering = false

    private static let handle = CGSize(width: 34, height: 44)

    private func drag(_ h: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { v in
                if !dragging {
                    dragging = true
                    Haptics.prepare()
                }
                dragFraction = min(1, max(0, Double((v.location.y - Self.handle.height / 2) / h)))
                let i = TimelineSections.index(at: dragFraction, in: sections)
                if i != lastIndex {
                    let major = lastIndex >= 0 && sections.indices.contains(lastIndex)
                        && sections[lastIndex].year != sections[i].year
                    Haptics.scrubStep(major: major)
                    lastIndex = i
                    onScrub(i)
                }
            }
            .onEnded { _ in
                dragging = false
                lastIndex = -1
                lingering = true
            }
    }

    var body: some View {
        GeometryReader { geo in
            let h = max(1, geo.size.height - Self.handle.height)
            let f = dragging ? dragFraction : fraction
            let index = TimelineSections.index(at: f, in: sections)
            ZStack(alignment: .topTrailing) {
                HStack(spacing: Kairos.Space.sm) {
                    if dragging, sections.indices.contains(index) {
                        Text(SectionHeader.short(sections[index]))
                            .font(Kairos.Typography.headline)
                            .foregroundStyle(Kairos.Palette.text)
                            .padding(.horizontal, Kairos.Space.md)
                            .padding(.vertical, Kairos.Space.sm)
                            .background(.regularMaterial, in: Capsule())
                            .transition(.opacity.combined(with: .move(edge: .trailing)))
                    }
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(Kairos.Palette.text)
                        .frame(width: Self.handle.width, height: Self.handle.height)
                        .background(.regularMaterial, in: UnevenRoundedRectangle(topLeadingRadius: 22, bottomLeadingRadius: 22))
                        .shadow(color: .black.opacity(0.3), radius: 4, y: 1)
                }
                .offset(y: CGFloat(f) * h)
                .allowsHitTesting(false)
                // the grab strip: only the right edge, and only while the scrubber shows, so a
                // normal scroll that starts near the edge is never stolen
                Color.clear
                    .frame(width: 44)
                    .contentShape(Rectangle())
                    .gesture(drag(h))
                    .allowsHitTesting(dragging || active || lingering)
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .topTrailing)
            .opacity(dragging || active || lingering ? 1 : 0)
            .animation(Kairos.Motion.micro, value: dragging)
            .animation(.easeOut(duration: 0.35), value: active || lingering)
            .task(id: active || dragging) {
                // stay a moment after the scroll (or a drag) stops, so the thumb can be grabbed
                if !(active || dragging) {
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    if !Task.isCancelled { lingering = false }
                } else {
                    lingering = true
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .frame(maxWidth: 220)
        .accessibilityElement()
        .accessibilityLabel("Timeline")
        .accessibilityValue(sections.indices.contains(TimelineSections.index(at: fraction, in: sections))
                            ? SectionHeader.title(sections[TimelineSections.index(at: fraction, in: sections)]) : "")
        .accessibilityAdjustableAction { dir in
            let i = TimelineSections.index(at: fraction, in: sections)
            let j = dir == .increment ? min(sections.count - 1, i + 1) : max(0, i - 1)
            onScrub(j)
        }
    }
}

/// A one-line card above the grid: Photos access, backup offers and progress.
struct BannerCard: View {
    let symbol: String
    let title: String
    let message: String?
    let action: String?
    var onAction: (() -> Void)?

    init(symbol: String, title: String, message: String?, action: String?, onAction: (() -> Void)?) {
        self.symbol = symbol
        self.title = title
        self.message = message
        self.action = action
        self.onAction = onAction
    }

    var body: some View {
        HStack(spacing: Kairos.Space.md) {
            Image(systemName: symbol)
                .font(.system(size: 20, weight: .medium))
                .foregroundStyle(Kairos.Palette.brandGradient)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(Kairos.Typography.headline).foregroundStyle(Kairos.Palette.text)
                if let message {
                    Text(message).font(Kairos.Typography.caption).foregroundStyle(Kairos.Palette.textSecondary)
                }
            }
            Spacer(minLength: 0)
            if let action, let onAction {
                Button(action, action: onAction)
                    .buttonStyle(.borderedProminent)
                    .tint(Kairos.Palette.accentFill)
            }
        }
        .padding(Kairos.Space.md)
        .kairosCard()
    }
}

struct ViewerRoute: Identifiable, Hashable {
    let ids: [MediaID]
    let start: MediaID
    var id: MediaID { start }
}

extension View {
    /// Full-screen cover on iOS, a sheet where fullScreenCover doesn't exist (macOS package builds).
    @ViewBuilder
    func fullScreenCoverCompat<Item: Identifiable, Content: View>(item: Binding<Item?>,
                                                                  @ViewBuilder content: @escaping (Item) -> Content) -> some View {
        #if os(iOS)
        fullScreenCover(item: item, content: content)
        #else
        sheet(item: item, content: content)
        #endif
    }
}
