import CoreGraphics
import KairosCore
import SwiftUI

/// Full-screen viewer: swipe between photos. The sharp image is the desktop thumbnail at once,
/// then the original, both with the non-destructive edit applied (EditPipeline). The edit sheet
/// writes the same `media_edits` JSON as the desktop's Quick Edit.
struct ViewerView: View {
    let ids: [MediaID]
    @State private var current: MediaID
    @State private var editing = false
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    init(ids: [MediaID], start: MediaID) {
        self.ids = ids
        _current = State(initialValue: start)
    }

    var body: some View {
        ZStack(alignment: .top) {
            Color.black.ignoresSafeArea()
            TabView(selection: $current) {
                ForEach(ids, id: \.self) { id in
                    ViewerPage(id: id).tag(id)
                }
            }
            #if os(iOS)
            .tabViewStyle(.page(indexDisplayMode: .never))
            #endif
            .ignoresSafeArea()
            HStack {
                Button { dismiss() } label: { Image(systemName: "xmark").padding(10) }
                Spacer()
                Button { Task { await model.toggleFavorite(current) } } label: {
                    Image(systemName: model.store?.media(current)?.isFavorite == true ? "heart.fill" : "heart").padding(10)
                }
                Button { editing = true } label: { Image(systemName: "slider.horizontal.3").padding(10) }
            }
            .font(.system(size: 17, weight: .semibold))
            .foregroundStyle(.white)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.horizontal, Kairos.Space.lg)
        }
        .preferredColorScheme(.dark)
        .sheet(isPresented: $editing) {
            QuickEditSheet(id: current)
                .presentationDetents([.medium])
        }
    }
}

struct ViewerPage: View {
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
        .task(id: model.revision) {
            image = await model.images.thumbnail(id)
            await loadOriginal()
        }
    }

    /// Full-resolution pass (downsampled to the screen) once the thumbnail shows.
    private func loadOriginal() async {
        guard let client = model.client, let item = model.store?.media(id), !item.isVideo,
              let url = try? await client.downloadOriginal(id) else { return }
        let adj = model.store?.edit(for: id)
        let full: CGImage? = await Task.detached(priority: .userInitiated) {
            defer { try? FileManager.default.removeItem(at: url) }
            guard let data = try? Data(contentsOf: url), let img = ImageLoader.decode(data, maxPixel: 3000) else { return nil }
            guard let adj, !adj.isIdentity else { return img }
            #if canImport(Accelerate)
            return EditPipeline.render(img, adj) ?? img
            #else
            return img
            #endif
        }.value
        if let full { image = full }
    }
}

/// Quick Edit (tone): the five desktop sliders, ±100, same keys and meaning. Crop and straighten
/// come with the gesture-driven crop tool in Phase 2. The record already round-trips both.
struct QuickEditSheet: View {
    let id: MediaID
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var adj = Adjustments()

    private struct SliderSpec {
        let key: String
        let label: String
        let symbol: String
    }

    private static let sliders = [
        SliderSpec(key: "exposure", label: "Exposure", symbol: "sun.max"),
        SliderSpec(key: "highlights", label: "Highlights", symbol: "circle.lefthalf.filled"),
        SliderSpec(key: "shadows", label: "Shadows", symbol: "circle.righthalf.filled"),
        SliderSpec(key: "temperature", label: "Warmth", symbol: "thermometer.medium"),
        SliderSpec(key: "contrast", label: "Contrast", symbol: "circle.lefthalf.striped.horizontal"),
    ]

    var body: some View {
        NavigationStack {
            List {
                ForEach(Self.sliders, id: \.key) { s in
                    VStack(alignment: .leading, spacing: Kairos.Space.xs) {
                        HStack {
                            Label(s.label, systemImage: s.symbol)
                            Spacer()
                            Text("\(Int(adj.tone(s.key).rounded()))").monospacedDigit()
                                .foregroundStyle(Kairos.Palette.textSecondary)
                        }
                        Slider(value: Binding(get: { adj.tone(s.key) }, set: { adj.setTone(s.key, $0) }),
                               in: -100...100)
                            .tint(Kairos.Palette.accent)
                    }
                    .listRowBackground(Kairos.Palette.surface)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Kairos.Palette.background)
            .navigationTitle("Adjust")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Revert") { adj = Adjustments(); save() }
                }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { save() } }
            }
        }
        .onAppear { adj = model.store?.edit(for: id) ?? Adjustments() }
    }

    private func save() {
        let a = adj
        Task {
            await model.saveEdit(id, a)
            dismiss()
        }
    }
}
