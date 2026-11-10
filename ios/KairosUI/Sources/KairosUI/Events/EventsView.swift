import KairosCore
import SwiftUI

/// Events: trip cards (journeys, day trips, moments) from the desktop's trips.py, each tinted with
/// its cover colour (tint.py). A trip with photo positions opens in Trip mode (TripMapView): its
/// route, stops and day chapters on the offline map, with a play button that tours the stops.
struct EventsView: View {
    @Environment(AppModel.self) private var model
    @State private var events: [Event] = []

    var body: some View {
        NavigationStack {
            Group {
                if events.isEmpty {
                    EmptyStateView(symbol: "suitcase", title: "No trips yet",
                                   message: "Trips and journeys detected on your computer appear here.")
                } else {
                    ScrollView {
                        LazyVStack(spacing: Kairos.Space.md) {
                            ForEach(events) { event in
                                NavigationLink(value: event) { EventCard(event: event) }
                                    .buttonStyle(.plain)
                            }
                        }
                        .padding(Kairos.Space.lg)
                    }
                    .background(Kairos.Palette.background)
                }
            }
            .navigationTitle("Events")
            .navigationDestination(for: Event.self) { EventDetailView(event: $0) }
            .kairosNavigationChrome()
        }
        .task(id: model.revision) { events = model.store?.events() ?? [] }
    }
}

struct EventCard: View {
    let event: Event

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .bottomLeading) {
                Group {
                    if let cover = event.coverMediaId {
                        ThumbnailView(id: cover)
                    } else {
                        Kairos.Palette.surface2
                    }
                }
                .frame(height: 180)
                .clipped()
                LinearGradient(colors: [.clear, (Color(hexString: event.tint) ?? .black).opacity(0.85)],
                               startPoint: .center, endPoint: .bottom)
                VStack(alignment: .leading, spacing: 2) {
                    if event.isJourney {
                        Label("Journey", systemImage: "point.topleft.down.to.point.bottomright.curvepath")
                            .font(Kairos.Typography.caption)
                            .foregroundStyle(Kairos.Palette.accent)
                    }
                    Text(event.title ?? event.locationName ?? "Trip")
                        .font(Kairos.Typography.title)
                        .foregroundStyle(Kairos.Palette.text)
                    Text(subtitle).font(Kairos.Typography.callout).foregroundStyle(Kairos.Palette.textSecondary)
                }
                .padding(Kairos.Space.md)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: Kairos.Radius.lg, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Kairos.Radius.lg, style: .continuous)
            .strokeBorder(Kairos.Palette.hairline, lineWidth: 1))
        .accessibilityElement(children: .combine)
    }

    private var subtitle: String {
        let range = [event.startDate, event.endDate].compactMap { $0?.prefix(10).replacingOccurrences(of: ":", with: "-") }
        let dates = range.count == 2 && range[0] != range[1] ? "\(range[0]) – \(range[1])" : (range.first ?? "")
        let count = event.itemCount.map { " · \($0) items" } ?? ""
        return dates + count
    }
}

struct EventDetailView: View {
    let event: Event
    @Environment(AppModel.self) private var model
    @State private var items: [LibraryStore.TimelineEntry] = []
    @State private var chapters: [Event] = []
    @State private var viewer: ViewerRoute?
    @State private var trip: TripRoute?
    @State private var hasRoute = false

    struct TripRoute: Identifiable {
        let id = UUID()
        let autoplay: Bool
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Kairos.Space.lg) {
                if hasRoute {
                    HStack(spacing: Kairos.Space.sm) {
                        Button { trip = TripRoute(autoplay: true) } label: {
                            Label(event.isJourney ? "Play Journey" : "Play route", systemImage: "play.fill")
                                .font(Kairos.Typography.headline)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, Kairos.Space.md)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(Kairos.Palette.accentFill)
                        Button { trip = TripRoute(autoplay: false) } label: {
                            Label("Map", systemImage: "map")
                                .font(Kairos.Typography.headline)
                                .padding(.vertical, Kairos.Space.md)
                                .padding(.horizontal, Kairos.Space.sm)
                        }
                        .buttonStyle(.bordered)
                        .tint(Kairos.Palette.accent)
                    }
                }
                if event.isJourney {
                    if !chapters.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack {
                                ForEach(chapters) { ch in
                                    Text(ch.title ?? ch.startDate ?? "Day")
                                        .font(Kairos.Typography.callout)
                                        .padding(.horizontal, Kairos.Space.md).padding(.vertical, Kairos.Space.sm)
                                        .background(Kairos.Palette.pill, in: Capsule())
                                }
                            }
                        }
                    }
                }
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: 3), spacing: 2) {
                    ForEach(items) { entry in
                        ThumbnailView(id: entry.id)
                            .aspectRatio(1, contentMode: .fit)
                            .onTapGesture { viewer = ViewerRoute(ids: items.map(\.id), start: entry.id) }
                    }
                }
            }
            .padding(Kairos.Space.lg)
        }
        .background(Kairos.Palette.background)
        .navigationTitle(event.title ?? "Trip")
        .task(id: model.revision) {
            items = model.store?.media(inEvent: event.id) ?? []
            chapters = model.store?.chapters(of: event.id) ?? []
            hasRoute = !(model.store?.journeyPhotos(event: event.id).isEmpty ?? true)
        }
        .fullScreenCoverCompat(item: $viewer) { ViewerView(ids: $0.ids, start: $0.start) }
        .fullScreenCoverCompat(item: $trip) { route in
            TripMapView(event: event, autoplay: route.autoplay)
        }
    }
}
