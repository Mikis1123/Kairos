import KairosCore
import SwiftUI

/// Search: one field with category chips. Phase 1 searches the replica offline (people, places,
/// trips, file names, dates). Semantic "describe it" search uses the desktop's CLIP/SigLIP index.
/// Protocol 2 forwards the query to /v1/search while the desktop is reachable.
struct SearchView: View {
    @Environment(AppModel.self) private var model
    @State private var text = ""
    @State private var scope: LibraryStore.SearchScope = .all
    @State private var results: [LibraryStore.TimelineEntry] = []
    @State private var viewer: ViewerRoute?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                chips
                if text.trimmingCharacters(in: .whitespaces).isEmpty {
                    EmptyStateView(symbol: "sparkle.magnifyingglass", title: "Search your library",
                                   message: "People, places, trips and dates. Describing a scene (\u{201C}dog on the beach\u{201D}) works when your computer is reachable.")
                } else if results.isEmpty {
                    EmptyStateView(symbol: "magnifyingglass", title: "No results", message: "Try another word or category.")
                } else {
                    ScrollView {
                        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: 4), spacing: 2) {
                            ForEach(results) { entry in
                                ThumbnailView(id: entry.id)
                                    .aspectRatio(1, contentMode: .fit)
                                    .onTapGesture { viewer = ViewerRoute(ids: results.map(\.id), start: entry.id) }
                            }
                        }
                    }
                }
            }
            .background(Kairos.Palette.background)
            .navigationTitle("Search")
            .searchable(text: $text, prompt: "Photos, people, places…")
            .kairosNavigationChrome()
        }
        .task(id: QueryKey(text: text, scope: scope, revision: model.revision)) {
            try? await Task.sleep(nanoseconds: 120_000_000)          // debounce typing
            guard !Task.isCancelled else { return }
            let q = text.trimmingCharacters(in: .whitespaces)
            results = q.isEmpty ? [] : (model.store?.search(q, scope: scope) ?? [])
        }
        .fullScreenCoverCompat(item: $viewer) { ViewerView(ids: $0.ids, start: $0.start) }
    }

    private struct QueryKey: Equatable {
        let text: String
        let scope: LibraryStore.SearchScope
        let revision: Int
    }

    private var chips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Kairos.Space.sm) {
                ForEach(LibraryStore.SearchScope.allCases, id: \.self) { s in
                    Button { withAnimation(Kairos.Motion.micro) { scope = s } } label: {
                        Label(Self.title(s), systemImage: Self.symbol(s))
                            .font(Kairos.Typography.callout)
                            .padding(.horizontal, Kairos.Space.md).padding(.vertical, Kairos.Space.sm)
                            .foregroundStyle(scope == s ? Kairos.Palette.onAccent : Kairos.Palette.text)
                            .background(scope == s ? Kairos.Palette.accentFill : Kairos.Palette.pill, in: Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, Kairos.Space.lg)
            .padding(.vertical, Kairos.Space.sm)
        }
    }

    static func title(_ s: LibraryStore.SearchScope) -> String {
        switch s {
        case .all: return "All"
        case .people: return "People"
        case .places: return "Places"
        case .trips: return "Trips"
        case .videos: return "Videos"
        case .favorites: return "Favourites"
        }
    }

    static func symbol(_ s: LibraryStore.SearchScope) -> String {
        switch s {
        case .all: return "square.grid.2x2"
        case .people: return "person"
        case .places: return "mappin.and.ellipse"
        case .trips: return "suitcase"
        case .videos: return "video"
        case .favorites: return "heart"
        }
    }
}
