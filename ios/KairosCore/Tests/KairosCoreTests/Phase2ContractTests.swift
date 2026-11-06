import XCTest
@testable import KairosCore

/// Phase 2 contracts, written by the desktop's test_ios_phase2_contract.py:
///   crop_contract_v1.json  quick_edit.py crop helpers  -> CropGeometry
///   pin_clusters_v1.json   map_vault.html (run in node) -> PinClustering
final class CropContractTests: XCTestCase {
    struct DragCase: Decodable {
        let crop: [Double]
        let handle: String
        let dx: Double
        let dy: Double
        let ratio: Double?
        let out: [Double]
    }

    struct FitCase: Decodable {
        let crop: [Double]
        let ratio: Double?
        let out: [Double]
    }

    struct AspectCase: Decodable {
        let input: [String: JSONValue]
        let aspect: String
        let out: [String: JSONValue]
    }

    struct Doc: Decodable {
        let size: [Double]
        let min_crop: Double
        let ratios: [String: Double?]
        let drag: [DragCase]
        let fit: [FitCase]
        let with_aspect: [AspectCase]
    }

    func doc() throws -> Doc {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "crop_contract_v1", withExtension: "json",
                                                  subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Doc.self, from: Data(contentsOf: url))
    }

    func assertRect(_ a: CropGeometry.Rect, _ b: [Double], _ msg: String, file: StaticString = #filePath, line: UInt = #line) {
        let x = CropGeometry.array(a)
        for i in 0..<4 { XCTAssertEqual(x[i], b[i], accuracy: 1e-9, msg, file: file, line: line) }
    }

    func testRatiosAndMinCrop() throws {
        let d = try doc()
        XCTAssertEqual(d.min_crop, Adjustments.minCrop)
        for (k, v) in d.ratios {
            let mine = CropGeometry.aspectRatio(k, width: d.size[0], height: d.size[1])
            if let v { XCTAssertEqual(mine ?? .nan, v, accuracy: 1e-12, k) } else { XCTAssertNil(mine, k) }
        }
    }

    func testDragMatchesDesktop() throws {
        let d = try doc()
        for c in d.drag {
            let handle = try XCTUnwrap(CropGeometry.Handle(rawValue: c.handle))
            let out = CropGeometry.drag(CropGeometry.rect(c.crop), handle: handle, dx: c.dx, dy: c.dy, ratio: c.ratio,
                                        width: d.size[0], height: d.size[1])
            assertRect(out, c.out, "\(c.handle) \(c.crop) \(c.ratio ?? 0)")
        }
    }

    func testFitAspectMatchesDesktop() throws {
        let d = try doc()
        for c in d.fit {
            let out = CropGeometry.fitAspect(CropGeometry.rect(c.crop), ratio: c.ratio, width: d.size[0], height: d.size[1])
            assertRect(out, c.out, "\(c.crop) \(c.ratio ?? 0)")
        }
    }

    func testWithAspectMatchesDesktop() throws {
        let d = try doc()
        for c in d.with_aspect {
            let a = CropGeometry.withAspect(Adjustments(dict: c.input), c.aspect, width: d.size[0], height: d.size[1])
            let mine = a.toDict()
            XCTAssertEqual(mine["aspect"], c.out["aspect"])
            guard case .array(let want)? = c.out["crop"], case .array(let got)? = mine["crop"] else {
                return XCTFail("crop missing")
            }
            for (p, q) in zip(got, want) { XCTAssertEqual(p.doubleValue ?? .nan, q.doubleValue ?? .nan, accuracy: 1e-6) }
        }
    }

    func testPinchKeepsRatioAndFrame() {
        let start: CropGeometry.Rect = (0.2, 0.2, 0.4, 0.3)
        let bigger = CropGeometry.pinch(start, scale: 10, anchor: (0.4, 0.35))
        XCTAssertEqual(bigger.w / bigger.h, start.w / start.h, accuracy: 1e-12)
        XCTAssertLessThanOrEqual(bigger.x + bigger.w, 1 + 1e-12)
        XCTAssertLessThanOrEqual(bigger.y + bigger.h, 1 + 1e-12)
        XCTAssertEqual(max(bigger.w, bigger.h), 1, accuracy: 1e-12)
        let tiny = CropGeometry.pinch(start, scale: 0.0001, anchor: (0.4, 0.35))
        XCTAssertGreaterThanOrEqual(min(tiny.w, tiny.h), Adjustments.minCrop - 1e-12)
        XCTAssertEqual(CropGeometry.array(CropGeometry.pinch(start, scale: .nan, anchor: (0, 0))), CropGeometry.array(start))
    }

    func testHandleHitTesting() {
        let crop: CropGeometry.Rect = (0.2, 0.2, 0.6, 0.6)
        let slop = (x: 0.04, y: 0.04)
        XCTAssertEqual(CropGeometry.handle(at: (0.21, 0.19), in: crop, slop: slop), .tl)
        XCTAssertEqual(CropGeometry.handle(at: (0.79, 0.81), in: crop, slop: slop), .br)
        XCTAssertEqual(CropGeometry.handle(at: (0.5, 0.2), in: crop, slop: slop), .t)
        XCTAssertEqual(CropGeometry.handle(at: (0.5, 0.5), in: crop, slop: slop), .move)
        XCTAssertNil(CropGeometry.handle(at: (0.05, 0.5), in: crop, slop: slop))
    }
}

final class PinContractTests: XCTestCase {
    struct P: Decodable {
        let id: Int64
        let lat: Double
        let lon: Double
    }

    struct C: Decodable {
        let ids: [Int64]
        let lat: Double
        let lon: Double
    }

    struct Hit: Decodable {
        let point: [Double]
        let distance: Double?
    }

    struct Doc: Decodable {
        let constants: [String: Double]
        let points: [P]
        let zooms: [Double]
        let clusters: [String: [C]]
        let hit_tip: [Double]
        let hits: [Hit]
    }

    func doc() throws -> Doc {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "pin_clusters_v1", withExtension: "json",
                                                  subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Doc.self, from: Data(contentsOf: url))
    }

    func testConstantsMatchTheDesktopPage() throws {
        let c = try doc().constants
        XCTAssertEqual(c["cell_px"], PinClustering.cellPx)
        XCTAssertEqual(c["pin_px"], PinClustering.pinPx)
        XCTAssertEqual(c["tile_px"], PinClustering.tilePx)
        XCTAssertEqual(c["pin_d"], PinClustering.pinDiameter)
        XCTAssertEqual(c["pin_pad"], PinClustering.pinPad)
        XCTAssertEqual(c["pin_tail"], PinClustering.pinTail)
    }

    func testClustersMatchTheDesktopMap() throws {
        let d = try doc()
        let points = d.points.map { PinClustering.Point(id: $0.id, lat: $0.lat, lon: $0.lon) }
        for z in d.zooms {
            let key = z == z.rounded() ? String(Int(z)) : String(z)       // JS String(3) == "3"
            let want = try XCTUnwrap(d.clusters[key], key)
            let got = PinClustering.clusters(points, zoom: z)
            XCTAssertEqual(got.count, want.count, "zoom \(z)")
            for (g, w) in zip(got, want) {
                XCTAssertEqual(g.items.map(\.id), w.ids, "zoom \(z)")
                XCTAssertEqual(g.lat, w.lat, accuracy: 1e-9)
                XCTAssertEqual(g.lon, w.lon, accuracy: 1e-9)
            }
        }
    }

    func testTeardropHitShapeMatchesTheDesktop() throws {
        let d = try doc()
        let tip = (x: d.hit_tip[0], y: d.hit_tip[1])
        for h in d.hits {
            let got = PinClustering.hitDistance(tip: tip, point: (h.point[0], h.point[1]))
            if let want = h.distance {
                XCTAssertEqual(got ?? .nan, want, accuracy: 1e-9, "\(h.point)")
            } else {
                XCTAssertNil(got, "\(h.point)")
            }
        }
    }
}

/// The edit queue: an upload answer must never clobber a newer local edit.
final class EditQueueTests: XCTestCase {
    func testLateUploadAnswerDoesNotOverwriteANewerEdit() throws {
        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        var first = Adjustments()
        first.exposure = 10
        try store.saveLocalEdit(5, first)
        let inFlight = try XCTUnwrap(store.dirtyEdits().first)

        var second = Adjustments()
        second.exposure = 60                                   // saved while the PUT is in flight
        try store.saveLocalEdit(5, second)

        let answer = EditRecord(mediaId: 5, params: first.toDict(), updatedAt: "t1")
        XCTAssertFalse(try store.editUploaded(5, canonical: answer, uploaded: inFlight.stored))
        XCTAssertEqual(store.edit(for: 5)?.exposure, 60)
        XCTAssertEqual(store.pendingEditCount, 1, "the newer edit is still queued")

        let next = try XCTUnwrap(store.dirtyEdits().first)
        XCTAssertTrue(try store.editUploaded(5, canonical: EditRecord(mediaId: 5, params: second.toDict(), updatedAt: "t2"),
                                             uploaded: next.stored))
        XCTAssertEqual(store.pendingEditCount, 0)
        XCTAssertEqual(store.edit(for: 5)?.exposure, 60)
    }

    func testRevertUploadsAsClear() throws {
        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        try store.saveLocalEdit(9, Adjustments())                 // neutral = revert
        let e = try XCTUnwrap(store.dirtyEdits().first)
        XCTAssertTrue(e.adjustments.isIdentity)
        XCTAssertTrue(try store.editUploaded(9, canonical: nil, uploaded: e.stored))
        XCTAssertNil(store.edit(for: 9))
    }
}
