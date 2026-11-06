import Foundation

// Swift mirrors of the desktop tables as the LAN bridge serializes them (sync_server.py
// _media / _events / _people / _faces / _albums / _edits). Wire names are snake_case, the
// desktop column names. Absolute paths never cross the wire: `hasThumb` / `live` say whether
// /v1/media/{id}/thumb and /live exist.

public typealias MediaID = Int64

public struct MediaItem: Codable, Hashable, Identifiable, Sendable {
    public var id: MediaID
    public var fileName: String?
    public var fileSize: Int64?
    public var fileMtime: Double?
    public var fileExt: String?
    public var mediaType: String?          // "photo" | "video"
    public var dateTaken: String?          // EXIF "YYYY:MM:DD HH:MM:SS" or ISO, as on the desktop
    public var subsecTime: String?
    public var timezoneOffset: String?
    public var latitude: Double?
    public var longitude: Double?
    public var altitude: Double?
    public var compassHeading: Double?
    public var placeCity: String?
    public var placeCountry: String?
    public var deviceMake: String?
    public var deviceModel: String?
    public var lensModel: String?
    public var fNumber: Double?
    public var exposureTime: String?
    public var iso: Int64?
    public var focalLength: Double?
    public var focalLength35mm: Double?
    public var contentIdentifier: String?
    public var isFavorite: Bool
    public var eventId: Int64?
    public var chapterId: Int64?
    public var gpsSource: String?
    public var fingerprint: String?        // size + xxh3(first 64 kB): path-independent identity
    public var availability: String?
    public var hasThumb: Bool
    public var live: Bool                  // Live Photo: the motion half streams from /live

    enum CodingKeys: String, CodingKey {
        case id, latitude, longitude, altitude, iso, fingerprint, availability, live
        case fileName = "file_name", fileSize = "file_size", fileMtime = "file_mtime", fileExt = "file_ext"
        case mediaType = "media_type", dateTaken = "date_taken", subsecTime = "subsec_time"
        case timezoneOffset = "timezone_offset", compassHeading = "compass_heading"
        case placeCity = "place_city", placeCountry = "place_country"
        case deviceMake = "device_make", deviceModel = "device_model", lensModel = "lens_model"
        case fNumber = "f_number", exposureTime = "exposure_time", focalLength = "focal_length"
        case focalLength35mm = "focal_length_35mm", contentIdentifier = "content_identifier"
        case isFavorite = "is_favorite", eventId = "event_id", chapterId = "chapter_id"
        case gpsSource = "gps_source", hasThumb = "has_thumb"
    }

    public init(id: MediaID, fileName: String? = nil, mediaType: String? = "photo", dateTaken: String? = nil) {
        self.id = id
        self.fileName = fileName
        self.mediaType = mediaType
        self.dateTaken = dateTaken
        self.isFavorite = false
        self.hasThumb = false
        self.live = false
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.lossyInt(.id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "media without id")
        }
        self.id = id
        fileName = c.lossyString(.fileName); fileSize = c.lossyInt(.fileSize)
        fileMtime = c.lossyDouble(.fileMtime); fileExt = c.lossyString(.fileExt)
        mediaType = c.lossyString(.mediaType); dateTaken = c.lossyString(.dateTaken)
        subsecTime = c.lossyString(.subsecTime); timezoneOffset = c.lossyString(.timezoneOffset)
        latitude = c.lossyDouble(.latitude); longitude = c.lossyDouble(.longitude)
        altitude = c.lossyDouble(.altitude); compassHeading = c.lossyDouble(.compassHeading)
        placeCity = c.lossyString(.placeCity); placeCountry = c.lossyString(.placeCountry)
        deviceMake = c.lossyString(.deviceMake); deviceModel = c.lossyString(.deviceModel)
        lensModel = c.lossyString(.lensModel); fNumber = c.lossyDouble(.fNumber)
        exposureTime = c.lossyString(.exposureTime); iso = c.lossyInt(.iso)
        focalLength = c.lossyDouble(.focalLength); focalLength35mm = c.lossyDouble(.focalLength35mm)
        contentIdentifier = c.lossyString(.contentIdentifier); isFavorite = c.lossyBool(.isFavorite)
        eventId = c.lossyInt(.eventId); chapterId = c.lossyInt(.chapterId)
        gpsSource = c.lossyString(.gpsSource); fingerprint = c.lossyString(.fingerprint)
        availability = c.lossyString(.availability); hasThumb = c.lossyBool(.hasThumb); live = c.lossyBool(.live)
    }

    public var isVideo: Bool { mediaType == "video" }
    public var hasLocation: Bool { latitude != nil && longitude != nil }
}

/// Trips: kind = moment | day_trip | journey | chapter (chapters point to their journey).
public struct Event: Codable, Hashable, Identifiable, Sendable {
    public var id: Int64
    public var title: String?
    public var locationName: String?
    public var startDate: String?
    public var endDate: String?
    public var itemCount: Int64?
    public var coverMediaId: MediaID?
    public var kind: String?
    public var parentId: Int64?
    public var tint: String?               // "#RRGGBB" cover tint, "" = grey, nil = not computed

    enum CodingKeys: String, CodingKey {
        case id, title, kind, tint
        case locationName = "location_name", startDate = "start_date", endDate = "end_date"
        case itemCount = "item_count", coverMediaId = "cover_media_id", parentId = "parent_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.lossyInt(.id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "event without id")
        }
        self.id = id
        title = c.lossyString(.title); locationName = c.lossyString(.locationName)
        startDate = c.lossyString(.startDate); endDate = c.lossyString(.endDate)
        itemCount = c.lossyInt(.itemCount); coverMediaId = c.lossyInt(.coverMediaId)
        kind = c.lossyString(.kind); parentId = c.lossyInt(.parentId); tint = c.lossyString(.tint)
    }

    public var isJourney: Bool { kind == "journey" }
}

public struct Person: Codable, Hashable, Identifiable, Sendable {
    public var id: Int64
    public var name: String?
    public var keyFaceId: Int64?
    public var isFavorite: Bool
    public var isConfirmed: Bool

    enum CodingKeys: String, CodingKey {
        case id, name
        case keyFaceId = "key_face_id", isFavorite = "is_favorite", isConfirmed = "is_confirmed"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.lossyInt(.id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "person without id")
        }
        self.id = id
        name = c.lossyString(.name); keyFaceId = c.lossyInt(.keyFaceId)
        isFavorite = c.lossyBool(.isFavorite); isConfirmed = c.lossyBool(.isConfirmed)
    }
}

public struct Face: Codable, Hashable, Identifiable, Sendable {
    public var id: Int64
    public var mediaId: MediaID
    public var personId: Int64?
    public var bbox: [Double?]             // x, y, w, h – normalised to the photo
    public var quality: Double?

    enum CodingKeys: String, CodingKey {
        case id, bbox, quality
        case mediaId = "media_id", personId = "person_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.lossyInt(.id), let media = c.lossyInt(.mediaId) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "face without ids")
        }
        self.id = id
        mediaId = media
        personId = c.lossyInt(.personId)
        let raw = (try? c.decodeIfPresent([JSONValue].self, forKey: .bbox)) ?? nil
        bbox = (raw ?? []).map { $0.doubleValue }
        quality = c.lossyDouble(.quality)
    }
}

public struct Album: Codable, Hashable, Identifiable, Sendable {
    public var id: Int64
    public var name: String?
    public var kind: String?               // "manual" | "smart"
    public var parentId: Int64?
    public var position: Int64?
    public var sortMode: String?
    public var coverMediaId: MediaID?
    public var query: String?
    public var filter: String?
    public var createdAt: String?
    public var items: [MediaID]            // ordered, visible members only

    enum CodingKeys: String, CodingKey {
        case id, name, kind, position, query, filter, items
        case parentId = "parent_id", sortMode = "sort_mode", coverMediaId = "cover_media_id"
        case createdAt = "created_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.lossyInt(.id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "album without id")
        }
        self.id = id
        name = c.lossyString(.name); kind = c.lossyString(.kind); parentId = c.lossyInt(.parentId)
        position = c.lossyInt(.position); sortMode = c.lossyString(.sortMode)
        coverMediaId = c.lossyInt(.coverMediaId); query = c.lossyString(.query)
        filter = c.lossyString(.filter); createdAt = c.lossyString(.createdAt)
        let raw = (try? c.decodeIfPresent([JSONValue].self, forKey: .items)) ?? nil
        items = (raw ?? []).compactMap { $0.doubleValue.map { MediaID($0) } }
    }
}

/// One row of desktop `media_edits`: the non-destructive edit of one item.
public struct EditRecord: Codable, Hashable, Sendable {
    public var mediaId: MediaID
    public var params: [String: JSONValue]
    public var updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case params
        case mediaId = "media_id", updatedAt = "updated_at"
    }

    public init(mediaId: MediaID, params: [String: JSONValue], updatedAt: String?) {
        self.mediaId = mediaId
        self.params = params
        self.updatedAt = updatedAt
    }

    public var adjustments: Adjustments { Adjustments(dict: params) }
}
