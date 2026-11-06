// swift-tools-version:5.10
// Kairos for iPhone – modular package. The app target itself lives in project.yml (XcodeGen),
// because SwiftPM cannot produce an iOS .app. `swift test` on a Mac runs the KairosCore tests
// (contract fixtures shared with the desktop's test_ios_sync.py).
import PackageDescription

let package = Package(
    name: "Kairos",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "KairosCore", targets: ["KairosCore"]),
        .library(name: "KairosUI", targets: ["KairosUI"]),
    ],
    dependencies: [
        // MapLibre Native (BSD-2): free, offline-capable, reads mbtiles:// sources directly.
        .package(url: "https://github.com/maplibre/maplibre-gl-native-distribution", from: "6.0.0"),
    ],
    targets: [
        .target(
            name: "KairosCore",
            path: "KairosCore/Sources/KairosCore"
        ),
        .target(
            name: "KairosUI",
            dependencies: [
                "KairosCore",
                .product(name: "MapLibre", package: "maplibre-gl-native-distribution",
                         condition: .when(platforms: [.iOS])),
            ],
            path: "KairosUI/Sources/KairosUI"
        ),
        .testTarget(
            name: "KairosCoreTests",
            dependencies: ["KairosCore"],
            path: "KairosCore/Tests/KairosCoreTests",
            resources: [.copy("Fixtures")]
        ),
    ]
)
