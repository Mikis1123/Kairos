import XCTest
@testable import KairosCore

/// Parity sprint (2026-11-10): the three-state library (AssetMatcher + phone_assets), Trip mode
/// journeys (wire format from sync_server.journey, pinned by test_ios_parity.py) and search.
final class AssetMatcherTests: XCTestCase {
    let prague = TimeZone(identifier: "Europe/Prague")!

    func date(_ s: String, _ zone: TimeZone) -> Date {
        AssetMatcher.absolute(s, offset: nil, zone: zone)!
    }

    func testStem() {
        XCTAssertEqual(AssetMatcher.stem("IMG_0042.HEIC"), "img_0042")
        XCTAssertEqual(AssetMatcher.stem("IMG_0042 (2).HEIC"), "img_0042")
        XCTAssertEqual(AssetMatcher.stem("Trip (Day 2).jpg"), "trip (day 2)")
        XCTAssertEqual(AssetMatcher.stem(".hidden"), ".hidden")
        XCTAssertNil(AssetMatcher.stem("  "))
    }

    func testAbsoluteHonoursOffsetAndFormats() {
        let a = AssetMatcher.absolute("2026:07:01 10:00:00", offset: "+02:00", zone: .gmt)!
        let b = AssetMatcher.absolute("2026-07-01T08:00:00", offset: "Z", zone: prague)!
        XCTAssertEqual(a, b)
        let c = AssetMatcher.absolute("2026:07:01 10:00:00", offset: nil, zone: prague)!   // CEST = +2
        XCTAssertEqual(a, c)
        XCTAssertEqual(AssetMatcher.absolute("2026:07:01 10:00:00", offset: "+0530", zone: .gmt)!
            .timeIntervalSince(AssetMatcher.absolute("2026:07:01 10:00:00", offset: "+00:00", zone: .gmt)!), -19800)
        XCTAssertNil(AssetMatcher.absolute("2026:07", offset: nil, zone: .gmt))
        XCTAssertEqual(AssetMatcher.wallString(a, zone: prague), "2026:07:01 10:00:00")
    }

    func testRules() {
        let t = date("2026:07:01 10:00:00", prague)
        let desktop: [AssetMatcher.DesktopKey] = [
            .init(id: 1, fileName: "IMG_0001.HEIC", dateTaken: "2026:07:01 10:00:00", timezoneOffset: nil, fileSize: 111),
            .init(id: 2, fileName: "IMG_0002.JPG", dateTaken: "2026:07:01 10:00:01", timezoneOffset: nil, fileSize: 500),
            .init(id: 3, fileName: "renamed.heic", dateTaken: "2026:07:01 11:00:00", timezoneOffset: nil, fileSize: 777),
            .init(id: 4, fileName: "IMG_0004.HEIC", dateTaken: "2025:01:01 00:00:00", timezoneOffset: nil, fileSize: 999),
        ]
        let phone: [AssetMatcher.PhoneKey] = [
            // name + size (time edited on the phone: still the same file)
            .init(localId: "a", fileName: "IMG_0001.HEIC", created: t.addingTimeInterval(86400), fileSize: 111),
            // name + time (exported as JPG on the PC, other size)
            .init(localId: "b", fileName: "IMG_0002.HEIC", created: t.addingTimeInterval(1.5), fileSize: 400),
            // time + size (renamed on the PC)
            .init(localId: "c", fileName: "IMG_0003.HEIC", created: t.addingTimeInterval(3600.4), fileSize: 777),
            // same name, a year apart, other size: a counter rollover, not the same photo
            .init(localId: "d", fileName: "IMG_0004.HEIC", created: t, fileSize: 1),
        ]
        let m = AssetMatcher.match(phone: phone, desktop: desktop, zone: prague)
        XCTAssertEqual(m["a"], .init(mediaId: 1, kind: .nameSize))
        XCTAssertEqual(m["b"], .init(mediaId: 2, kind: .nameTime))
        XCTAssertEqual(m["c"], .init(mediaId: 3, kind: .timeSize))
        XCTAssertNil(m["d"])
    }

    func testBurstIsOneToOneAndShaIsKept() {
        let t = date("2026:07:01 10:00:00", prague)
        let desktop: [AssetMatcher.DesktopKey] = (0..<3).map {
            .init(id: Int64(10 + $0), fileName: "IMG_10\($0).HEIC", dateTaken: "2026:07:01 10:00:00",
                  timezoneOffset: nil, fileSize: 100)
        }
        let phone: [AssetMatcher.PhoneKey] = (0..<3).map {
            .init(localId: "p\($0)", fileName: "IMG_10\($0).HEIC", created: t, fileSize: 100)
        }
        let m = AssetMatcher.match(phone: phone, desktop: desktop, zone: prague, fixed: ["p2": 99])
        XCTAssertEqual(m["p0"]?.mediaId, 10)
        XCTAssertEqual(m["p1"]?.mediaId, 11)
        XCTAssertEqual(m["p2"], .init(mediaId: 99, kind: .sha256))
        XCTAssertEqual(Set(m.values.map(\.mediaId)).count, 3)
    }
}

final class PhoneStoreTests: XCTestCase {
    func makeStore() throws -> LibraryStore {
        let store = try LibraryStore(db: SQLiteDatabase(path: nil))
        let rows: [(Int64, String, String, Int64)] = [
            (1, "IMG_0001.HEIC", "2026:07:01 10:00:00", 111),
            (2, "IMG_0002.HEIC", "2026:07:02 10:00:00", 222),
        ]
        for (id, name, date, size) in rows {
            let payload = "{\"id\":\(id),\"file_name\":\"\(name)\",\"date_taken\":\"\(date)\",\"file_size\":\(size),"
                + "\"media_type\":\"photo\",\"is_favorite\":0,\"has_thumb\":1,\"live\":0}"
            try store.db.run("INSERT INTO media_items (id, date_taken, media_type, file_name, payload) VALUES (?, ?, 'photo', ?, ?)",
                             [.int(id), .text(date), .text(name), .text(payload)])
        }
        return store
    }

    func record(_ id: String, _ name: String, _ wall: String, size: Int64?) -> LibraryStore.PhoneAssetRecord {
        let zone = TimeZone(identifier: "Europe/Prague")!
        return .init(localId: id, created: AssetMatcher.absolute(wall, offset: nil, zone: zone), modified: nil,
                     isVideo: false, live: false, isFavorite: false, width: 4032, height: 3024, duration: 0,
                     latitude: 49.0, longitude: 13.5, fileName: name, fileSize: size)
    }

    func testThreeStatesInTheTimeline() throws {
        let store = try makeStore()
        let zone = TimeZone(identifier: "Europe/Prague")!
        try store.upsertPhoneAssets([record("L1", "IMG_0001.HEIC", "2026:07:01 10:00:00", size: 111),
                                     record("L9", "IMG_0009.HEIC", "2026:07:03 09:00:00", size: 900)], zone: zone)
        XCTAssertEqual(try store.rematchPhone(zone: zone), 1)
        let t = store.timeline()
        XCTAssertEqual(t.map(\.origin), [.phone, .pc, .both])          // newest first
        XCTAssertEqual(t.first?.dateTaken, "2026:07:03 09:00:00")
        let phoneId = try XCTUnwrap(t.first?.id)
        XCTAssertLessThan(phoneId, 0)
        XCTAssertEqual(store.media(phoneId)?.fileName, "IMG_0009.HEIC")
        XCTAssertEqual(store.media(phoneId)?.availability, "phone")
        XCTAssertEqual(store.localIdentifier(for: 1), "L1")
        XCTAssertEqual(store.localIdentifier(for: phoneId), "L9")
        XCTAssertNil(store.localIdentifier(for: 2))
        XCTAssertEqual(store.origin(of: 1), .both)
        XCTAssertEqual(store.geoPoints().filter { $0.id < 0 }.count, 1)
        let c = store.phoneCounts()
        XCTAssertEqual([c.total, c.both, c.phoneOnly, c.waiting], [2, 1, 1, 1])
        XCTAssertEqual(try store.rematchPhone(zone: zone), 0)            // stable: nothing changes
    }

    func testBackupShaProofAndDesktopDelete() throws {
        let store = try makeStore()
        let zone = TimeZone(identifier: "Europe/Prague")!
        try store.upsertPhoneAssets([record("L5", "IMG_0005.HEIC", "2026:07:05 10:00:00", size: 555)], zone: zone)
        try store.rematchPhone(zone: zone)
        XCTAssertEqual(try store.approveAllPhoneOnly(), 1)
        XCTAssertEqual(store.backupCandidates().map(\.localId), ["L5"])
        try store.setPhoneSha("L5", String(repeating: "a", count: 64))
        try store.setBackup(["L5"], .sent)
        XCTAssertEqual(store.sentWithoutMatch().count, 1)
        // the desktop scanned the upload as item 2: proven by content
        XCTAssertEqual(try store.applyShaMatches([String(repeating: "a", count: 64): 2]), 1)
        XCTAssertEqual(store.phoneAsset(localId: "L5")?.mediaId, 2)
        XCTAssertEqual(store.phoneAsset(localId: "L5")?.backup, LibraryStore.BackupState.none)
        try store.rematchPhone(zone: zone)                               // the matcher keeps a SHA proof
        XCTAssertEqual(store.phoneAsset(localId: "L5")?.match, "sha256")
        // deleted on the desktop: phone-only again, never removed from the camera-roll index
        let tombstone = #"{"protocol":1,"library_id":"lib","cursor":9,"more":false,"reset":false,"#
            + #""changes":[{"seq":9,"entity":"media","id":2,"op":"delete"}]}"#
        try store.apply(try JSONDecoder().decode(ChangePage.self, from: Data(tombstone.utf8)))
        XCTAssertNil(store.phoneAsset(localId: "L5")?.mediaId)
        try store.removePhoneAssets(keeping: [])
        XCTAssertEqual(store.phoneCounts().total, 0)
    }

    func testRefreshKeepsListIdAndDropsShaWhenBytesChange() throws {
        let store = try makeStore()
        try store.upsertPhoneAssets([record("L7", "IMG_0007.HEIC", "2026:07:07 10:00:00", size: 700)])
        let pid = try XCTUnwrap(store.phoneAsset(localId: "L7")?.pid)
        try store.setPhoneSha("L7", String(repeating: "b", count: 64))
        try store.upsertPhoneAssets([record("L7", "IMG_0007.HEIC", "2026:07:07 10:00:00", size: 700)])
        XCTAssertNotNil(store.phoneAsset(localId: "L7")?.sha256)
        try store.upsertPhoneAssets([record("L7", "IMG_0007.HEIC", "2026:07:07 10:00:00", size: 701)])
        XCTAssertEqual(store.phoneAsset(localId: "L7")?.pid, pid)
        XCTAssertNil(store.phoneAsset(localId: "L7")?.sha256)
    }
}

final class JourneyWireTests: XCTestCase {
    func testDecodeServerJourneyAndLegs() throws {
        let json = """
        {"id": 1, "title": "Šumava", "location": "Kašperské Hory", "start": "2026-07-02 10:00",
         "end": "2026-07-02 17:00", "count": 3,
         "stops": [
           {"lat": 49.0, "lon": 13.5, "start": "2026-07-02 10:00", "end": "2026-07-02 10:00", "city": "",
            "count": 1, "entry": {"lat": 49.0, "lon": 13.5}, "exit": {"lat": 49.01, "lon": 13.51}, "best": 10, "photos": [10]},
           {"lat": 49.2, "lon": 13.9, "start": "2026-07-02 13:00", "end": "2026-07-02 13:00", "city": "Vimperk",
            "count": 1, "entry": {"lat": 49.19, "lon": 13.89}, "exit": {"lat": 49.2, "lon": 13.9}, "best": 11, "photos": [11]},
           {"lat": 49.5, "lon": 14.4, "start": "2026-07-02 17:00", "end": "2026-07-02 17:00", "city": "",
            "count": 1, "entry": null, "exit": null, "best": 12, "photos": [12]}],
         "tracks": {"2": [[13.9, 49.2], [14.1, 49.3], [14.4, 49.5]]}, "days": []}
        """
        let j = try JSONDecoder().decode(Journey.self, from: Data(json.utf8))
        XCTAssertEqual(j.stops.count, 3)
        let legs = Journey.route(stops: j.stops, tracks: j.tracks)
        XCTAssertEqual(legs.count, 2)
        XCTAssertEqual(legs[0], [.init(lat: 49.01, lon: 13.51), .init(lat: 49.19, lon: 13.89)])   // exit -> entry
        XCTAssertEqual(legs[1].count, 3)                                                        // the GPX line
        XCTAssertEqual(legs[1].last, .init(lat: 49.5, lon: 14.4))
    }

    func testLocalJourneyGroupsAndSplitsDays() {
        let photos: [JourneyLocal.Photo] = [
            .init(id: 1, lat: 49.0, lon: 13.5, date: "2026:07:01 10:00:00", city: "A"),
            .init(id: 2, lat: 49.0001, lon: 13.5001, date: "2026:07:01 10:05:00", city: "A"),
            .init(id: 3, lat: 49.3, lon: 13.9, date: "2026:07:01 15:00:00", city: "B"),
            .init(id: 4, lat: 49.6, lon: 14.3, date: "2026:07:02 09:00:00", city: "C"),
        ]
        let j = JourneyLocal.journey(id: 5, title: "T", location: "", photos: photos.reversed())
        XCTAssertEqual(j.stops.map(\.count), [2, 1, 1])
        XCTAssertEqual(j.stops.first?.start, "2026-07-01 10:00")
        XCTAssertEqual(j.days.map(\.n), [1, 2])
        XCTAssertEqual(j.days.last?.first, 2)
        XCTAssertEqual(JourneyLocal.minute("2026-07-01T10:00:00"), "2026-07-01 10:00")
    }

    func testSearchAnswerDecodes() throws {
        let a = try JSONDecoder().decode(SearchAnswer.self, from: Data(
            #"{"query":"pes","ids":[2,1],"confident":1,"semantic":true,"labels":["lidé"]}"#.utf8))
        XCTAssertEqual(a, SearchAnswer(query: "pes", ids: [2, 1], confident: 1, semantic: true, labels: ["lidé"]))
    }
}

final class TimelineSectionTests: XCTestCase {
    func entry(_ id: MediaID, _ date: String?) -> LibraryStore.TimelineEntry {
        .init(id: id, dateTaken: date, isVideo: false, live: false, isFavorite: false)
    }

    func testGroupingAndScrubMapping() {
        let entries = [entry(1, "2026:07:02 10:00:00"), entry(2, "2026:07:02 09:00:00"), entry(3, "2026:07:01 23:00:00"),
                       entry(4, "2026-06-30T12:00:00"), entry(5, "2025:12:31 10:00:00"), entry(6, nil)]
        let days = TimelineSections.build(entries, grouping: .day, columns: 4)
        XCTAssertEqual(days.map(\.id), ["2026-07-02", "2026-07-01", "2026-06-30", "2025-12-31", "undated"])
        XCTAssertEqual(days[0].entries.map(\.id), [1, 2])
        XCTAssertEqual(days[0].start, 0)
        XCTAssertTrue(zip(days, days.dropFirst()).allSatisfy { $0.start < $1.start })
        let months = TimelineSections.build(entries, grouping: .month, columns: 6)
        XCTAssertEqual(months.map(\.id), ["2026-07", "2026-06", "2025-12", "undated"])
        XCTAssertNil(months[0].day)
        XCTAssertEqual(TimelineSections.build(entries, grouping: .year, columns: 7).map(\.id), ["2026", "2025", "undated"])
        XCTAssertEqual(TimelineSections.index(at: 0, in: days), 0)
        XCTAssertEqual(TimelineSections.index(at: 1, in: days), days.count - 1)
        XCTAssertEqual(TimelineSections.index(at: days[2].start + 1e-6, in: days), 2)
        XCTAssertEqual(TimelineGrouping.forColumns(3), .day)
        XCTAssertEqual(TimelineGrouping.forColumns(5), .month)
        XCTAssertEqual(TimelineGrouping.forColumns(7), .year)
    }
}
