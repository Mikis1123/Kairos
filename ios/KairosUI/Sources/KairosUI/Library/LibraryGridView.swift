import KairosCore
import SwiftUI

/// Library: newest-first grid, pinch to change density (3…7 columns, Photos-style), micro LQIP
/// at once and the sharp thumbnail when it arrives, so a fling never shows empty cells (the
/// desktop's decode_queue contract).
struct LibraryGridView: View {
    @Environment(AppModel.self) private var model
    @State private var columns = 4
    @State private var pinchBase: Int?
    @State private var favoritesOnly = false
    @State private var items: [LibraryStore.TimelineEntry] = []
    @State private var viewer: ViewerRoute?
    @State private var picking = false
    @State private var showHidden = false
    @State private var importNote: String?

    private static let minColumns = 3, maxColumns = 7

    var body: some View {
        NavigationStack {
            Group {
                if items.isEmpty {
                    EmptyStateView(symbol: "photo.on.rectangle.angled", title: "No photos yet",
                                   message: model.isPaired ? "Your library is syncing from Kairos on your computer."
                                                           : "Pair with Kairos on your computer to see your library.")
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
        }
        .fullScreenCoverCompat(item: $viewer) { route in
            ViewerView(ids: route.ids, start: route.start)
        }
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
                    Text("\(p.rejected.count) not sent (unsupported format)")
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
        }
    }

    private struct TaskKey: Equatable {
        let revision: Int
        let favorites: Bool
    }

    private var grid: some View {
        ScrollView {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: columns), spacing: 2) {
                ForEach(items) { entry in
                    ThumbnailView(id: entry.id)
                        .aspectRatio(1, contentMode: .fit)
                        .overlay(alignment: .bottomTrailing) { badges(entry) }
                        .contentShape(Rectangle())
                        .onTapGesture { viewer = ViewerRoute(ids: items.map(\.id), start: entry.id) }
                        .accessibilityLabel(entry.dateTaken ?? "Photo")
                }
            }
            .animation(Kairos.Motion.spring, value: columns)
        }
        .scrollIndicators(.hidden)
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
            if e.isFavorite { Image(systemName: "heart.fill") }
            if e.live { Image(systemName: "livephoto") }
            if e.isVideo { Image(systemName: "play.fill") }
        }
        .font(.system(size: 10, weight: .semibold))
        .foregroundStyle(.white)
        .shadow(radius: 2)
        .padding(4)
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
