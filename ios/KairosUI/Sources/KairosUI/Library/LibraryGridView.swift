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
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Toggle("Favourites only", isOn: $favoritesOnly)
                        Button("Hidden folder", systemImage: "lock") {}
                            .disabled(true)            // Phase 2: vault key exchange
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
        }
        .task(id: TaskKey(revision: model.revision, favorites: favoritesOnly)) {
            items = model.store?.timeline(favoritesOnly: favoritesOnly) ?? []
        }
        .fullScreenCoverCompat(item: $viewer) { route in
            ViewerView(ids: route.ids, start: route.start)
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
