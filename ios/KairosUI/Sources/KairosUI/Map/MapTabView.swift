import KairosCore
import SwiftUI

/// Map: MapLibre Native over offline MBTiles packs. It is free and offline-first like the desktop
/// map, and calls no paid tile APIs. Packs come from two places, both read directly by MapLibre
/// (mbtiles://):
///   - Documents/Maps: region packs downloaded from the desktop (/v1/packs)
///   - Documents: any .mbtiles the user drops in through Finder (USB) or the Files app
///     (UIFileSharingEnabled + LSSupportsOpeningDocumentsInPlace in project.yml)
/// Photo pins are the desktop's teardrop pins with the same clustering (MapLibreView).
struct MapTabView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.scenePhase) private var scenePhase
    @State private var points: [LibraryStore.GeoPoint] = []
    @State private var showPacks = false
    @State private var installed: [URL] = []
    @State private var viewer: ViewerRoute?

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottom) {
                Kairos.Palette.mapBackground.ignoresSafeArea()
                MapLibreView(packs: installed, points: points, model: model) { viewer = $0 }
                    .ignoresSafeArea(edges: .top)
                if installed.isEmpty {
                    Text("Download a map region from your computer, or copy an .mbtiles file into Kairos with the Files app.")
                        .font(Kairos.Typography.callout)
                        .foregroundStyle(Kairos.Palette.textSecondary)
                        .multilineTextAlignment(.center)
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
        .task { await rescan() }
        .task(id: model.revision) { points = model.store?.geoPoints() ?? [] }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await rescan() } }   // a file dropped in via Finder / Files
        }
        .sheet(isPresented: $showPacks, onDismiss: { Task { await rescan() } }) {
            OfflinePacksView()
        }
        .fullScreenCoverCompat(item: $viewer) { route in
            ViewerView(ids: route.ids, start: route.start)
        }
    }

    private func rescan() async {
        let found = await Task.detached(priority: .utility) { OfflinePacks.installed() }.value
        if found != installed { installed = found }
    }
}

/// Offline region packs on the phone.
enum OfflinePacks {
    static var documents: URL {
        (try? FileManager.default.url(for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
    }

    /// Downloads from the desktop land here (Documents/Maps, visible in Files as "Kairos › Maps").
    static var directory: URL { documents.appendingPathComponent("Maps", isDirectory: true) }

    /// Generated files (style JSON, safe-name links): Caches, never shown to the user.
    static var cacheDirectory: URL {
        let base = (try? FileManager.default.url(for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil,
                                                 create: true)) ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("Kairos/map", isDirectory: true)
    }

    /// Phase 1 stored packs in Application Support. They move to Documents/Maps once.
    static var legacyDirectory: URL? {
        (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil,
                                      create: false))?.appendingPathComponent("Kairos/map_packs", isDirectory: true)
    }

    static func migrateLegacy() {
        let fm = FileManager.default
        guard let old = legacyDirectory, let files = try? fm.contentsOfDirectory(at: old, includingPropertiesForKeys: nil)
        else { return }
        try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
        for f in files where f.pathExtension.lowercased() == "mbtiles" {
            let dest = directory.appendingPathComponent(f.lastPathComponent)
            if !fm.fileExists(atPath: dest.path) { try? fm.moveItem(at: f, to: dest) }
        }
        try? fm.removeItem(at: old)
    }

    /// Valid packs, sorted by name. Unreadable files, iCloud placeholders and SQLite files
    /// that aren't MBTiles are skipped. Runs off the main thread (it opens each file).
    static func installed() -> [URL] {
        migrateLegacy()
        let fm = FileManager.default
        var out: [URL] = []
        var seen = Set<String>()
        for dir in [directory, documents] {
            let files = (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.fileSizeKey],
                                                     options: [.skipsHiddenFiles])) ?? []
            for f in files where f.pathExtension.lowercased() == "mbtiles" && !seen.contains(f.lastPathComponent) {
                if isValid(f) {
                    seen.insert(f.lastPathComponent)
                    out.append(f)
                }
            }
        }
        return out.sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }
    }

    static func isValid(_ url: URL) -> Bool {
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        guard size >= 4096, let db = try? SQLiteDatabase(path: url.path, readOnly: true) else { return false }
        // `tiles` is a table or a view (deduplicated packs); either way it must answer
        return (try? db.query("SELECT zoom_level FROM tiles LIMIT 1")) != nil
    }

    /// The path for the mbtiles:// URL. A file the user named with spaces or non-ASCII
    /// characters gets a symlink with a safe name in Caches, so the URL needs no escaping.
    static func tileURLPath(_ url: URL) -> String {
        let name = url.deletingPathExtension().lastPathComponent
        let safe = name.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) && $0.isASCII || "-_.".unicodeScalars.contains($0) }
        if safe && !url.path.contains(" ") { return url.path }
        let fm = FileManager.default
        let links = cacheDirectory.appendingPathComponent("links", isDirectory: true)
        try? fm.createDirectory(at: links, withIntermediateDirectories: true)
        let ascii = String(name.unicodeScalars.map { $0.isASCII && CharacterSet.alphanumerics.contains($0) ? Character($0) : "_" })
        // FNV-1a: stable across launches (hashValue is seeded per process)
        let h = url.path.utf8.reduce(UInt64(0xcbf29ce484222325)) { ($0 ^ UInt64($1)) &* 0x100000001b3 }
        let link = links.appendingPathComponent("\(ascii)-\(String(h % 1_000_000_007, radix: 36)).mbtiles")
        if (try? fm.destinationOfSymbolicLink(atPath: link.path)) != url.path {
            try? fm.removeItem(at: link)
            try? fm.createSymbolicLink(at: link, withDestinationURL: url)
        }
        return link.path
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
