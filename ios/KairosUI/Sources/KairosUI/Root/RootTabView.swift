import KairosCore
import SwiftUI

/// The five main tabs, matching the desktop's navigation: Library, Map, Events (Trips + Journey),
/// People, Search. The viewer and the edit sheet open from any of them. The Hidden folder lives
/// behind Library's menu, as on the desktop (Phase 2: needs its own key exchange).
public struct RootTabView: View {
    public enum Tab: Hashable { case library, map, events, people, search }

    @Environment(AppModel.self) private var model
    @State private var tab: Tab = .library

    public init() {}

    public var body: some View {
        TabView(selection: $tab) {
            LibraryGridView()
                .tabItem { Label("Library", systemImage: "photo.on.rectangle") }
                .tag(Tab.library)
            MapTabView()
                .tabItem { Label("Map", systemImage: "map") }
                .tag(Tab.map)
            EventsView()
                .tabItem { Label("Events", systemImage: "suitcase") }
                .tag(Tab.events)
            PeopleView()
                .tabItem { Label("People", systemImage: "person.2") }
                .tag(Tab.people)
            SearchView()
                .tabItem { Label("Search", systemImage: "magnifyingglass") }
                .tag(Tab.search)
        }
        .tint(Kairos.Palette.accent)
        .background(Kairos.Palette.background.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .sheet(isPresented: .constant(model.status == .needsPairing && model.store != nil)) {
            PairingView()
                .interactiveDismissDisabled()
        }
    }
}

/// Sync state for the navigation bars: a small dot plus a label.
struct SyncBadge: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: Kairos.Space.xs) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(label).font(Kairos.Typography.caption).foregroundStyle(Kairos.Palette.textSecondary)
        }
        .padding(.horizontal, Kairos.Space.sm)
        .padding(.vertical, Kairos.Space.xs)
        .background(Kairos.Palette.pill, in: Capsule())
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        switch model.status {
        case .live: return Kairos.Palette.success
        case .syncing: return Kairos.Palette.accent
        case .offline: return Kairos.Palette.warning
        case .needsPairing: return Kairos.Palette.danger
        case .idle: return Kairos.Palette.textTertiary
        }
    }

    private var label: String {
        switch model.status {
        case .live: return model.endpoint?.name ?? "Live"
        case .syncing(let n): return "Syncing \(n)"
        case .offline: return "Offline"
        case .needsPairing: return "Not paired"
        case .idle: return "Idle"
        }
    }
}

/// Shared empty state ("nothing synced yet", "no results").
struct EmptyStateView: View {
    let symbol: String
    let title: String
    let message: String

    var body: some View {
        VStack(spacing: Kairos.Space.md) {
            Image(systemName: symbol)
                .font(.system(size: 44, weight: .light))
                .foregroundStyle(Kairos.Palette.brandGradient)
            Text(title).font(Kairos.Typography.title).foregroundStyle(Kairos.Palette.text)
            Text(message).font(Kairos.Typography.callout).foregroundStyle(Kairos.Palette.textSecondary)
                .multilineTextAlignment(.center)
        }
        .padding(Kairos.Space.xxl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Kairos.Palette.background)
    }
}

extension View {
    /// Dark navigation chrome on every platform the package builds for.
    func kairosNavigationChrome() -> some View {
        #if os(iOS)
        return self
            .toolbarBackground(Kairos.Palette.sidebar, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .toolbarBackground(Kairos.Palette.sidebar, for: .tabBar)
            .toolbarBackground(.visible, for: .tabBar)
        #else
        return self
        #endif
    }
}
