import KairosCore
import SwiftUI

/// Search: one field with category chips. The replica answers offline at once (people, places,
/// trips, file names, dates). While the desktop is reachable, the same query also goes to its
/// semantic index (/v1/search: CLIP / SigLIP, facets, text in photos), which ranks the whole
/// library by what the photos show ("dog on the beach", "red car in snow"). The embeddings and
/// the model stay on the PC; the phone only gets ids. Sure semantic matches come first, then
/// the offline hits, then the possible semantic matches.
struct SearchView: View {
    @Environment(AppModel.self) private var model
    @State private var text = ""
    @State private var scope: LibraryStore.SearchScope = .all
    @State private var local: [LibraryStore.TimelineEntry] = []
    @State private var semantic: SearchAnswer?
    @State private var searchingPC = false
    @State private var viewer: ViewerRoute?

    private var results: [LibraryStore.TimelineEntry] {
        Self.merge(local: local, semantic: semantic, scope: scope, store: model.store)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                chips
                if text.trimmingCharacters(in: .whitespaces).isEmpty {
                    EmptyStateView(symbol: "sparkle.magnifyingglass", title: "Search your library",
                                   message: "People, places, trips and dates. Describing a scene (\u{201C}dog on the beach\u{201D}) works when your computer is reachable.")
                } else if results.isEmpty && !searchingPC {
                    EmptyStateView(symbol: "magnifyingglass", title: "No results", message: "Try another word or category.")
                } else {
                    semanticNote
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
            local = q.isEmpty ? [] : (model.store?.search(q, scope: scope) ?? [])
        }
        .task(id: SemanticKey(text: text, scope: scope)) {
            let q = text.trimmingCharacters(in: .whitespaces)
            guard q.count >= 2, Self.semanticScopes.contains(scope), model.isPaired else {
                semantic = nil
                return
            }
            try? await Task.sleep(nanoseconds: 350_000_000)          // the PC is asked once typing pauses
            guard !Task.isCancelled else { return }
            searchingPC = true
            let answer = await model.semanticSearch(q)
            guard !Task.isCancelled else { return }
            searchingPC = false
            semantic = answer
        }
        .fullScreenCoverCompat(item: $viewer) { ViewerView(ids: $0.ids, start: $0.start) }
    }

    private struct QueryKey: Equatable {
        let text: String
        let scope: LibraryStore.SearchScope
        let revision: Int
    }

    private struct SemanticKey: Equatable {
        let text: String
        let scope: LibraryStore.SearchScope
    }

    /// Scopes where "describe the scene" makes sense (people / places / trips are exact facets).
    static let semanticScopes: Set<LibraryStore.SearchScope> = [.all, .videos, .favorites]

    @ViewBuilder
    private var semanticNote: some View {
        if searchingPC || semantic?.semantic == true {
            HStack(spacing: Kairos.Space.xs) {
                if searchingPC { ProgressView().controlSize(.mini) } else { Image(systemName: "sparkles") }
                Text(searchingPC ? "Asking \(model.endpoint?.name ?? "your computer")…"
                                 : "Ranked by what the photos show (on \(model.endpoint?.name ?? "your computer"))")
                Spacer()
            }
            .font(Kairos.Typography.caption)
            .foregroundStyle(Kairos.Palette.textSecondary)
            .padding(.horizontal, Kairos.Space.lg)
            .padding(.bottom, Kairos.Space.xs)
        }
    }

    /// Sure semantic matches, then the offline hits, then the possible semantic matches; each id
    /// once, only items the replica has (the PC may know newer ones), filtered by the chip.
    static func merge(local: [LibraryStore.TimelineEntry], semantic: SearchAnswer?, scope: LibraryStore.SearchScope,
                      store: LibraryStore?) -> [LibraryStore.TimelineEntry] {
        guard let semantic, !semantic.ids.isEmpty, let store else { return local }
        func entry(_ id: MediaID) -> LibraryStore.TimelineEntry? {
            guard let m = store.media(id) else { return nil }
            if scope == .videos, !m.isVideo { return nil }
            if scope == .favorites, !m.isFavorite { return nil }
            return .init(id: id, dateTaken: m.dateTaken, isVideo: m.isVideo, live: m.live, isFavorite: m.isFavorite)
        }
        let sure = semantic.ids.prefix(semantic.confident).compactMap(entry)
        let maybe = semantic.ids.dropFirst(semantic.confident).compactMap(entry)
        var seen = Set<MediaID>()
        return (sure + local + maybe).filter { seen.insert($0.id).inserted }
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
