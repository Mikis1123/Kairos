import KairosCore
import SwiftUI

/// Map: MapLibre Native over offline MBTiles region packs streamed from the desktop
/// (/v1/packs). It is free and offline-first, like the desktop map, and calls no paid tile APIs.
/// Photo pins are a GeoJSON circle layer in the Iris accent.
struct MapTabView: View {
    @Environment(AppModel.self) private var model
    @State private var points: [LibraryStore.GeoPoint] = []
    @State private var showPacks = false
    @State private var installed: [URL] = OfflinePacks.installed()

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottom) {
                Kairos.Palette.mapBackground.ignoresSafeArea()
                MapLibreView(packs: installed, points: points)
                    .ignoresSafeArea(edges: .top)
                if installed.isEmpty {
                    Text("Download a map region from your computer to see the map offline.")
                        .font(Kairos.Typography.callout)
                        .foregroundStyle(Kairos.Palette.textSecondary)
                        .padding(Kairos.Space.md)
                        .kairosCard()
                        .padding(Kairos.Space.lg)
                }
            }
            .navigationTitle("Map")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showPacks = true } label: { Image(systemName: "square.and.arrow.down.on.square") }
                        .accessibilityLabel("Offline maps")
                }
            }
            .kairosNavigationChrome()
        }
        .task(id: model.revision) { points = model.store?.geoPoints() ?? [] }
        .sheet(isPresented: $showPacks, onDismiss: { installed = OfflinePacks.installed() }) {
            OfflinePacksView()
        }
    }
}

/// Region packs on the phone: Application Support/Kairos/map_packs/<id>.mbtiles.
enum OfflinePacks {
    static var directory: URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true)) ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("Kairos/map_packs", isDirectory: true)
    }

    static func installed() -> [URL] {
        ((try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "mbtiles" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
}

struct OfflinePacksView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var packs: [MapPack] = []
    @State private var busy: String?
    @State private var message: String?

    var body: some View {
        NavigationStack {
            List {
                if let message {
                    Text(message).font(Kairos.Typography.callout).foregroundStyle(Kairos.Palette.warning)
                }
                ForEach(packs) { pack in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(pack.title).foregroundStyle(Kairos.Palette.text)
                            Text("\(ByteCountFormatter.string(fromByteCount: pack.size, countStyle: .file)) · z\(pack.minzoom)–\(pack.maxzoom)")
                                .font(Kairos.Typography.callout).foregroundStyle(Kairos.Palette.textSecondary)
                        }
                        Spacer()
                        if busy == pack.id {
                            ProgressView()
                        } else if isInstalled(pack) {
                            Image(systemName: "checkmark.circle.fill").foregroundStyle(Kairos.Palette.success)
                        } else {
                            Button("Download") { Task { await download(pack) } }
                                .buttonStyle(.borderedProminent)
                                .tint(Kairos.Palette.accentFill)
                                .disabled(busy != nil)
                        }
                    }
                    .listRowBackground(Kairos.Palette.surface)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Kairos.Palette.background)
            .navigationTitle("Offline maps")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        .task { await load() }
    }

    private func isInstalled(_ p: MapPack) -> Bool {
        FileManager.default.fileExists(atPath: OfflinePacks.directory.appendingPathComponent("\(p.id).mbtiles").path)
    }

    private func load() async {
        guard let client = model.client else {
            message = "Pair with your computer first."
            return
        }
        do { packs = try await client.packs() } catch { message = "Your computer isn't reachable." }
    }

    private func download(_ p: MapPack) async {
        guard let client = model.client else { return }
        busy = p.id
        defer { busy = nil }
        do {
            try await client.downloadPack(p, to: OfflinePacks.directory.appendingPathComponent("\(p.id).mbtiles"))
        } catch SyncError.deferred(let after) {
            message = "Your computer is saving battery. Try again in \(max(1, after / 60)) min."
        } catch {
            message = "Download failed: \(error.localizedDescription)"
        }
    }
}
