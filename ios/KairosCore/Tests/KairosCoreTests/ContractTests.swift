import XCTest
@testable import KairosCore

/// The fixtures are written by the desktop's test_ios_sync.py (KAIROS_REGEN_FIXTURES=1): the same
/// bytes the Python server produces, so these tests break the moment either side drifts.
final class ContractTests: XCTestCase {
    func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    func testChangePageDecodesAndApplies() throws {
        let page = try JSONDecoder().decode(ChangePage.self, from: fixture("sync_changes_v1"))
        XCTAssertEqual(page.protocol, WireProtocol.version)
        XCTAssertFalse(page.more)
        XCTAssertEqual(page.changes.count, 10)

        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        let applied = try store.apply(page)
        XCTAssertEqual(store.cursor, page.cursor)
        XCTAssertEqual(store.libraryId, "fixture")
        XCTAssertEqual(applied.mediaNeedingMicro.sorted(), [1, 2])

        // vault photo (4) and the Live Photo's motion half (3) arrive as tombstones
        XCTAssertEqual(store.timeline().map(\.id), [2, 1])
        let photo = try XCTUnwrap(store.media(1))
        XCTAssertTrue(photo.isFavorite)
        XCTAssertTrue(photo.hasThumb)
        XCTAssertEqual(photo.latitude ?? 0, 48.97, accuracy: 1e-9)
        XCTAssertTrue(try XCTUnwrap(store.media(2)).live)

        XCTAssertEqual(store.events().first?.title, "Šumava")
        XCTAssertEqual(store.media(inEvent: 1).map(\.id), [1])
        XCTAssertEqual(store.people().map(\.name), ["Anna"])
        XCTAssertEqual(store.media(withPerson: 1).map(\.id), [1])
        XCTAssertEqual(store.search("anna", scope: .people).map(\.id), [1])
        XCTAssertEqual(store.search("Šumava", scope: .trips).map(\.id), [1])
        XCTAssertEqual(store.geoPoints().map(\.id), [1])

        let edit = try XCTUnwrap(store.edit(for: 1))
        XCTAssertEqual(edit.exposure, 25)
        XCTAssertEqual(edit.crop, [0.1, 0.1, 0.8, 0.8])
    }

    func testTombstoneCascadesAndDirtyEditWins() throws {
        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        try store.apply(try JSONDecoder().decode(ChangePage.self, from: fixture("sync_changes_v1")))
        var local = Adjustments()
        local.contrast = 40
        try store.saveLocalEdit(1, local)

        let later = """
        {"protocol":1,"library_id":"fixture","cursor":99,"more":false,"reset":false,"changes":[
          {"seq":98,"entity":"edit","id":1,"op":"upsert","data":{"media_id":1,"params":{"exposure":5},"updated_at":"x"}},
          {"seq":99,"entity":"media","id":2,"op":"delete"},
          {"seq":97,"entity":"hologram","id":7,"op":"upsert","data":{}}]}
        """
        try store.apply(try JSONDecoder().decode(ChangePage.self, from: Data(later.utf8)))
        XCTAssertEqual(store.edit(for: 1)?.contrast, 40)            // the unsent local edit survives
        XCTAssertNil(store.media(2))
        XCTAssertEqual(store.dirtyEdits().map(\.id), [1])
        XCTAssertEqual(store.cursor, 99)
    }

    func testResetDropsReplica() throws {
        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        try store.apply(try JSONDecoder().decode(ChangePage.self, from: fixture("sync_changes_v1")))
        let reset = #"{"protocol":1,"library_id":"fixture","cursor":0,"more":true,"reset":true,"changes":[]}"#
        try store.apply(try JSONDecoder().decode(ChangePage.self, from: Data(reset.utf8)))
        XCTAssertEqual(store.counts().media, 0)
        XCTAssertEqual(store.cursor, 0)
    }

    func testPairingURI() {
        let fp = String(repeating: "ab", count: 32)
        let offer = PairingOffer(uri: "kairos://pair?h=192.168.1.5&p=52790&fp=\(fp)&lib=L&pin=123456&v=1")
        XCTAssertEqual(offer?.host, "192.168.1.5")
        XCTAssertEqual(offer?.port, 52790)
        XCTAssertEqual(offer?.pin, "123456")
        XCTAssertNil(PairingOffer(uri: "kairos://pair?h=x&p=1&fp=short&pin=123456"))
        XCTAssertNil(PairingOffer(uri: "kairos://pair?h=x&p=1&fp=\(fp)&pin=123456&v=99"))   // newer protocol
    }

    func testMicroFrame() throws {
        var blob = Data("KMP1".utf8)
        blob.append(contentsOf: [0, 0, 0, 7, 0, 0, 0, 2, 0xFF, 0xD8])
        let items = try MicroFrame.parse(blob)
        XCTAssertEqual(items.first?.0, 7)
        XCTAssertEqual(items.first?.1, Data([0xFF, 0xD8]))
        XCTAssertThrowsError(try MicroFrame.parse(Data("NOPE".utf8)))
    }
}

/// Fixtures/edit_contract_v1.json: LUTs and inverse warps from desktop quick_edit.py.
final class EditContractTests: XCTestCase {
    struct Case: Decodable {
        let input: [String: JSONValue]
        let normalized: [String: JSONValue]
        let size: [Int]
        let lut_r: [Int]
        let lut_g: [Int]
        let lut_b: [Int]
        let inverse_matrix: [Double]
        let output_size: [Int]
    }

    struct Doc: Decodable {
        let edit_version: Int
        let cases: [Case]
    }

    func testEditMathMatchesDesktop() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "edit_contract_v1", withExtension: "json",
                                                  subdirectory: "Fixtures"))
        let doc = try JSONDecoder().decode(Doc.self, from: Data(contentsOf: url))
        XCTAssertEqual(doc.edit_version, Adjustments.editVersion)
        for c in doc.cases {
            let a = Adjustments(dict: c.input)
            // normalisation + serialisation agree with Python to_dict()
            let mine = a.toDict()
            for (k, v) in c.normalized {
                switch (mine[k], v) {
                case (.array(let x)?, .array(let y)):
                    XCTAssertEqual(x.count, y.count, k)
                    for (p, q) in zip(x, y) { XCTAssertEqual(p.doubleValue ?? .nan, q.doubleValue ?? .nan, accuracy: 1e-9, k) }
                case (.string(let x)?, .string(let y)):
                    XCTAssertEqual(x, y, k)
                default:
                    XCTAssertEqual(mine[k]?.doubleValue ?? .nan, v.doubleValue ?? .nan, accuracy: 1e-9, k)
                }
            }
            let lut = EditMath.buildLUT(a)
            for i in 0..<256 {
                XCTAssertLessThanOrEqual(abs(Int(lut.r[i]) - c.lut_r[i]), 1, "R[\(i)] \(c.input)")
                XCTAssertLessThanOrEqual(abs(Int(lut.g[i]) - c.lut_g[i]), 1, "G[\(i)] \(c.input)")
                XCTAssertLessThanOrEqual(abs(Int(lut.b[i]) - c.lut_b[i]), 1, "B[\(i)] \(c.input)")
            }
            let m = EditMath.inverseMatrix(width: c.size[0], height: c.size[1], a)
            for (x, y) in zip(m, c.inverse_matrix) { XCTAssertEqual(x, y, accuracy: 1e-6) }
            let o = EditMath.outputSize(width: c.size[0], height: c.size[1], a)
            XCTAssertEqual([o.0, o.1], c.output_size)
        }
    }

    func testClampQuirksMatchPython() {
        let a = Adjustments(dict: ["exposure": .string("250"), "angle": .null, "aspect": .number(3),
                                   "crop": .array([.number(0.9), .number(0), .number(0.5), .number(1)]),
                                   "future_knob": .number(1)])
        XCTAssertEqual(a.exposure, 100)            // float("250") clamped
        XCTAssertEqual(a.angle, 0)
        XCTAssertEqual(a.aspect, "free")
        XCTAssertEqual(a.crop[0], 0.5, accuracy: 1e-12)     // x clamped to 1 - w
        XCTAssertEqual(a.toDict()["future_knob"], .number(1))
    }
}
