import Foundation

/// Crop tool math: a 1:1 port of the crop helpers in desktop quick_edit.py (aspect_ratio,
/// fit_aspect, with_aspect, drag_crop), plus the pinch-to-scale gesture the phone adds.
///
/// Every rectangle is (x, y, w, h) in fractions of the STRAIGHTENED frame. That frame has the
/// source's pixel size, because the straighten zoom keeps it covered. A crop the phone writes is
/// therefore exactly what the desktop's crop tool would write.
/// Fixtures/crop_contract_v1.json (test_ios_phase2_contract.py) pins this against Python.
public enum CropGeometry {
    public typealias Rect = (x: Double, y: Double, w: Double, h: Double)

    public enum Handle: String, CaseIterable, Sendable {
        case move, tl, tr, bl, br, l, r, t, b

        var left: Bool { self == .tl || self == .bl || self == .l }
        var right: Bool { self == .tr || self == .br || self == .r }
        var top: Bool { self == .tl || self == .tr || self == .t }
        var bottom: Bool { self == .bl || self == .br || self == .b }
    }

    /// quick_edit.ASPECTS: pixel width / height, nil = free. Same keys as Adjustments.aspects.
    public static func aspectRatio(_ aspect: String, width w: Double, height h: Double) -> Double? {
        switch aspect {
        case "original": return w / max(1, h)
        case "1:1": return 1
        case "16:9": return 16.0 / 9.0
        case "4:5": return 4.0 / 5.0
        default: return nil
        }
    }

    /// The largest rectangle with pixel ratio `ratio` inside `crop`, centred on it.
    public static func fitAspect(_ crop: Rect, ratio: Double?, width w: Double, height h: Double) -> Rect {
        guard let ratio, ratio > 0 else { return crop }
        let pw = crop.w * w, ph = crop.h * h
        if pw / ph > ratio {
            let nw = ph * ratio / w
            return (crop.x + (crop.w - nw) / 2, crop.y, nw, crop.h)
        }
        let nh = pw / ratio / h
        return (crop.x, crop.y + (crop.h - nh) / 2, crop.w, nh)
    }

    /// A new edit with an aspect preset applied to the current crop (largest fit, centred).
    public static func withAspect(_ adj: Adjustments, _ aspect: String, width w: Double, height h: Double) -> Adjustments {
        var a = adj
        a.aspect = aspect
        if let ratio = aspectRatio(aspect, width: w, height: h) {
            let base: Rect = adj.isGeometryIdentity ? (0, 0, 1, 1) : rect(adj.crop)
            a.crop = array(fitAspect(base, ratio: ratio, width: w, height: h))
        }
        return a.normalized()
    }

    /// Crop after dragging `handle` by (dx, dy) normalised units, keeping `ratio` (pixel w/h) when
    /// given. It stays inside the frame. When the ratio cannot be kept there, the crop is
    /// unchanged, like Python.
    public static func drag(_ crop: Rect, handle: Handle, dx: Double, dy: Double, ratio: Double?,
                            width w: Double, height h: Double) -> Rect {
        let minCrop = Adjustments.minCrop
        if handle == .move {
            return (min(max(0, crop.x + dx), 1 - crop.w), min(max(0, crop.y + dy), 1 - crop.h), crop.w, crop.h)
        }
        var l = crop.x, t = crop.y, r = crop.x + crop.w, b = crop.y + crop.h
        if handle.left { l = min(max(0, l + dx), r - minCrop) }
        if handle.right { r = max(min(1, r + dx), l + minCrop) }
        if handle.top { t = min(max(0, t + dy), b - minCrop) }
        if handle.bottom { b = max(min(1, b + dy), t + minCrop) }
        let nw = r - l, nh = b - t
        if let ratio, ratio > 0 {
            if (nw * w) / (nh * h) > ratio {
                let want = nh * h * ratio / w
                if handle.left { l = r - want } else { r = l + want }
            } else {
                let want = nw * w / ratio / h
                if handle.top { t = b - want } else { b = t + want }
            }
            if l < 0 || r > 1 || t < 0 || b > 1 { return crop }
        }
        return (l, t, r - l, b - t)
    }

    /// Pinch on the crop rectangle (phone only): scale about `anchor` (normalised) and keep the
    /// aspect. The result is clamped to the frame by shrinking, never by distorting, so a locked
    /// ratio survives.
    public static func pinch(_ crop: Rect, scale k: Double, anchor: (x: Double, y: Double)) -> Rect {
        guard k.isFinite, k > 0 else { return crop }
        let minCrop = Adjustments.minCrop
        // largest factor that keeps both sides within [minCrop, 1]
        var f = k
        f = min(f, 1 / crop.w, 1 / crop.h)
        f = max(f, minCrop / crop.w, minCrop / crop.h)
        let nw = crop.w * f, nh = crop.h * f
        var x = anchor.x - (anchor.x - crop.x) * f
        var y = anchor.y - (anchor.y - crop.y) * f
        x = min(max(0, x), 1 - nw)
        y = min(max(0, y), 1 - nh)
        return (x, y, nw, nh)
    }

    /// The handle under a point (normalised frame coordinates), with a touch slop given in
    /// normalised units per axis (44 pt / frame size). Corners win over edges, and edges win
    /// over move.
    public static func handle(at p: (x: Double, y: Double), in crop: Rect, slop: (x: Double, y: Double)) -> Handle? {
        let l = crop.x, t = crop.y, r = crop.x + crop.w, b = crop.y + crop.h
        let nearL = abs(p.x - l) <= slop.x, nearR = abs(p.x - r) <= slop.x
        let nearT = abs(p.y - t) <= slop.y, nearB = abs(p.y - b) <= slop.y
        let insideX = p.x >= l - slop.x && p.x <= r + slop.x
        let insideY = p.y >= t - slop.y && p.y <= b + slop.y
        guard insideX && insideY else { return nil }
        switch (nearL, nearR, nearT, nearB) {
        case (true, _, true, _): return .tl
        case (_, true, true, _): return .tr
        case (true, _, _, true): return .bl
        case (_, true, _, true): return .br
        case (true, _, _, _): return .l
        case (_, true, _, _): return .r
        case (_, _, true, _): return .t
        case (_, _, _, true): return .b
        default: return .move
        }
    }

    public static func rect(_ a: [Double]) -> Rect {
        a.count == 4 ? (a[0], a[1], a[2], a[3]) : (0, 0, 1, 1)
    }

    public static func array(_ r: Rect) -> [Double] { [r.x, r.y, r.w, r.h] }
}
