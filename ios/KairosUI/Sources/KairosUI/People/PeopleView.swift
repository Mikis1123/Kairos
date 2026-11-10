import CoreGraphics
import KairosCore
import SwiftUI

/// People: face portraits from the desktop's clustering (key face crop per person). Named and
/// favourite people come first, as on the desktop.
struct PeopleView: View {
    @Environment(AppModel.self) private var model
    @State private var people: [LibraryStore.PersonSummary] = []

    var body: some View {
        NavigationStack {
            Group {
                if people.isEmpty {
                    EmptyStateView(symbol: "person.2", title: "No people yet",
                                   message: "Faces recognised on your computer appear here.")
                } else {
                    ScrollView {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 96), spacing: Kairos.Space.lg)],
                                  spacing: Kairos.Space.lg) {
                            ForEach(people) { p in
                                NavigationLink(value: p) { PersonCell(person: p) }.buttonStyle(.plain)
                            }
                        }
                        .padding(Kairos.Space.lg)
                    }
                    .background(Kairos.Palette.background)
                }
            }
            .navigationTitle("People")
            .navigationDestination(for: LibraryStore.PersonSummary.self) { PersonDetailView(person: $0) }
            .kairosNavigationChrome()
        }
        .task(id: model.revision) { people = model.store?.people() ?? [] }
    }
}

struct PersonCell: View {
    let person: LibraryStore.PersonSummary
    @Environment(AppModel.self) private var model
    @State private var face: CGImage?

    var body: some View {
        VStack(spacing: Kairos.Space.xs) {
            ZStack {
                Circle().fill(Kairos.Palette.surface2)
                if let face {
                    Image(decorative: face, scale: 1).resizable().scaledToFill()
                } else {
                    Image(systemName: "person.fill").foregroundStyle(Kairos.Palette.textTertiary)
                }
            }
            .frame(width: 88, height: 88)
            .clipShape(Circle())
            .overlay(Circle().strokeBorder(person.isFavorite ? Kairos.Palette.accent : Kairos.Palette.hairline,
                                           lineWidth: person.isFavorite ? 2 : 1))
            Text(person.name?.isEmpty == false ? person.name! : "Unnamed")
                .font(Kairos.Typography.callout)
                .foregroundStyle(person.name?.isEmpty == false ? Kairos.Palette.text : Kairos.Palette.textTertiary)
                .lineLimit(1)
            Text("\(person.photoCount)").font(Kairos.Typography.caption).foregroundStyle(Kairos.Palette.textTertiary)
        }
        .task(id: "\(person.keyFaceId ?? 0)-\(model.revision)") {
            // Retried through the image loader's queue and kept on disk, so faces show offline.
            guard let fid = person.keyFaceId else { return }
            if let img = await model.images.faceCrop(fid) { face = img }
        }
    }
}

struct PersonDetailView: View {
    let person: LibraryStore.PersonSummary
    @Environment(AppModel.self) private var model
    @State private var items: [LibraryStore.TimelineEntry] = []
    @State private var viewer: ViewerRoute?

    var body: some View {
        ScrollView {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: 3), spacing: 2) {
                ForEach(items) { entry in
                    ThumbnailView(id: entry.id)
                        .aspectRatio(1, contentMode: .fit)
                        .onTapGesture { viewer = ViewerRoute(ids: items.map(\.id), start: entry.id) }
                }
            }
        }
        .background(Kairos.Palette.background)
        .navigationTitle(person.name ?? "Unnamed")
        .task(id: model.revision) { items = model.store?.media(withPerson: person.id) ?? [] }
        .fullScreenCoverCompat(item: $viewer) { ViewerView(ids: $0.ids, start: $0.start) }
    }
}
