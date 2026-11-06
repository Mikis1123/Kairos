#if canImport(CoreGraphics) && canImport(Accelerate)
import Accelerate
import CoreGraphics
import Foundation

/// Applies an `Adjustments` record to a CGImage exactly as the desktop does it: geometry first, as
/// ONE affine draw (the same inverse map as cv2.warpAffine), then tone, as ONE per-channel table
/// lookup (vImage, the equivalent of cv2.LUT).
///
/// The two passes are also public on their own, for the live editor (Phase 2). A slider drag
/// changes only tone, so the editor keeps the geometry result and re-runs `applyTone`. That is one
/// vImage lookup, about 1 ms for a 2 MP preview. The geometry pass runs only when crop or angle
/// change.
///
/// Colour: the desktop tones sRGB-encoded values (its display contract), so this renders into an
/// 8-bit sRGB context. Wide-gamut (P3) sources are converted to sRGB first.
public enum EditPipeline {
    /// `scale` = output pixels per source pixel (thumbnails: < 1). Returns `image` unchanged for
    /// a neutral edit at scale 1.
    public static func render(_ image: CGImage, _ adj: Adjustments, scale: Double = 1) -> CGImage? {
        let a = adj.normalized()
        if a.isIdentity && abs(scale - 1) < 1e-9 { return image }
        guard let ctx = geometryContext(image, a, scale: scale) else { return nil }
        if !a.isToneIdentity, let data = ctx.data {
            applyTone(data: data, width: ctx.width, height: ctx.height, rowBytes: ctx.bytesPerRow, a)
        }
        return ctx.makeImage()
    }

    /// Geometry only (straighten + crop + scale), tone untouched.
    public static func renderGeometry(_ image: CGImage, _ adj: Adjustments, scale: Double = 1) -> CGImage? {
        let a = adj.normalized()
        if a.isGeometryIdentity && abs(scale - 1) < 1e-9 { return image }
        return geometryContext(image, a, scale: scale)?.makeImage()
    }

    /// Tone only: a copy of `image` (any format, redrawn as 8-bit sRGB) through the edit's LUT.
    public static func applyTone(_ image: CGImage, _ adj: Adjustments) -> CGImage? {
        let a = adj.normalized()
        if a.isToneIdentity { return image }
        guard let ctx = context(width: image.width, height: image.height) else { return nil }
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        guard let data = ctx.data else { return nil }
        applyTone(data: data, width: ctx.width, height: ctx.height, rowBytes: ctx.bytesPerRow, a)
        return ctx.makeImage()
    }

    static func context(width: Int, height: Int) -> CGContext? {
        guard let space = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
        return CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                         space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
    }

    static func geometryContext(_ image: CGImage, _ a: Adjustments, scale: Double) -> CGContext? {
        let (ow, oh) = EditMath.outputSize(width: image.width, height: image.height, a, scale: scale)
        guard let ctx = context(width: ow, height: oh) else { return nil }
        ctx.interpolationQuality = .high

        // Output -> source in continuous, y-down pixel coordinates (EditMath), inverted to the
        // source -> output map CoreGraphics draws with. CG's origin is bottom-left, so flip
        // the context to y-down first and flip the image back inside its own rectangle.
        let m = EditMath.continuousInverse(width: image.width, height: image.height, a, scale: scale)
        let outToSrc = CGAffineTransform(a: m[0], b: m[3], c: m[1], d: m[4], tx: m[2], ty: m[5])
        ctx.translateBy(x: 0, y: CGFloat(oh))
        ctx.scaleBy(x: 1, y: -1)
        ctx.concatenate(outToSrc.inverted())
        let w = CGFloat(image.width), h = CGFloat(image.height)
        ctx.translateBy(x: 0, y: h)
        ctx.scaleBy(x: 1, y: -1)
        // The straighten zoom keeps the frame covered (cv2 uses BORDER_REPLICATE for the last
        // sub-pixel), so nothing outside the image is ever visible.
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return ctx
    }

    /// In-place LUT on an RGBX8888 buffer (byte order R, G, B, X).
    static func applyTone(data: UnsafeMutableRawPointer, width: Int, height: Int, rowBytes: Int, _ a: Adjustments) {
        let lut = EditMath.buildLUT(a)
        let identity: [UInt8] = (0..<256).map { UInt8($0) }
        var src = vImage_Buffer(data: data, height: vImagePixelCount(height), width: vImagePixelCount(width),
                                rowBytes: rowBytes)
        var dst = src                                    // same memory: in place (no overlapping inout)
        // vImage names the channels A, R, G, B by byte position 0…3, so pass the tables in memory order.
        lut.r.withUnsafeBufferPointer { r in
            lut.g.withUnsafeBufferPointer { g in
                lut.b.withUnsafeBufferPointer { b in
                    identity.withUnsafeBufferPointer { x in
                        _ = vImageTableLookUp_ARGB8888(&src, &dst, r.baseAddress, g.baseAddress,
                                                       b.baseAddress, x.baseAddress, vImage_Flags(kvImageNoFlags))
                    }
                }
            }
        }
    }
}
#endif
