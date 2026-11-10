import XCTest
@testable import KairosCore

/// Video / Live Photo streaming (iOS Phase 4, Batch 1). The header formats match what
/// sync_server._send_file answers (pinned on the desktop by test_ios_sync.py).
final class MediaRangeTests: XCTestCase {
    func testParseContentRange() {
        let r = MediaRange.parseContentRange("bytes 256-511/10240")
        XCTAssertEqual(r?.start, 256)
        XCTAssertEqual(r?.end, 511)
        XCTAssertEqual(r?.total, 10240)
        let open = MediaRange.parseContentRange("bytes 0-99/*")
        XCTAssertEqual(open?.end, 99)
        XCTAssertNil(open?.total ?? nil)
        XCTAssertNil(MediaRange.parseContentRange("bytes */10240"))      // a 416 answer
        XCTAssertNil(MediaRange.parseContentRange("bytes 10-5/100"))
        XCTAssertNil(MediaRange.parseContentRange("bytes 0-100/100"))    // end past the file
        XCTAssertNil(MediaRange.parseContentRange("items 0-1/2"))
    }

    func testRangeHeader() {
        XCTAssertEqual(MediaRange.header(offset: 0, length: 2), "bytes=0-1")
        XCTAssertEqual(MediaRange.header(offset: 1_048_576, length: 1_048_576), "bytes=1048576-2097151")
    }

    func testNextLengthClipsToRequestAndFile() {
        let c = MediaRange.chunk
        XCTAssertEqual(MediaRange.nextLength(position: 0, end: nil, total: nil), c)
        XCTAssertEqual(MediaRange.nextLength(position: 0, end: 2, total: nil), 2)          // info probe
        XCTAssertEqual(MediaRange.nextLength(position: 100, end: nil, total: 150), 50)
        XCTAssertEqual(MediaRange.nextLength(position: 150, end: nil, total: 150), 0)
        XCTAssertEqual(MediaRange.nextLength(position: 10, end: 5, total: nil), 0)
    }

    func testChunkPlanCoversRequestExactly() {
        // a player asking for 2.5 chunks from an odd offset gets every byte exactly once
        let start: Int64 = 12_345, end = start + MediaRange.chunk * 5 / 2
        var pos = start, sizes: [Int64] = []
        while true {
            let n = MediaRange.nextLength(position: pos, end: end, total: 10_000_000)
            if n == 0 { break }
            sizes.append(n)
            pos += n
        }
        XCTAssertEqual(pos, end)
        XCTAssertEqual(sizes, [MediaRange.chunk, MediaRange.chunk, MediaRange.chunk / 2])
    }

    func testUTI() {
        XCTAssertEqual(MediaRange.uti(contentType: "video/mp4", fileExt: ".MOV"), "public.mpeg-4")
        XCTAssertEqual(MediaRange.uti(contentType: "video/quicktime", fileExt: nil), "com.apple.quicktime-movie")
        XCTAssertEqual(MediaRange.uti(contentType: "application/octet-stream", fileExt: ".mp4"), "public.mpeg-4")
        XCTAssertEqual(MediaRange.uti(contentType: nil, fileExt: nil), "com.apple.quicktime-movie")
    }
}
