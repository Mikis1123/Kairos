#if canImport(CoreGraphics) && canImport(Accelerate)
import Accelerate
import CoreGraphics
import Foundation

/// Applies an `Adjustments` record to a CGImage exactly as the desktop does it: geometry first, as
/// ONE affine draw (the same inverse map as cv2.warpAffine), then tone, as ONE per-channel table
/// lookup (vImage, the equivalent of cv2.LUT).
///
/// Colour: the desktop tones sRGB-encoded values (its display contract), so this renders into an
/// 8-bit sRGB context. Wide-gamut (P3) sources are converted to sRGB first. Phase 2 can move to
/// extended sRGB once the desktop does the same.
public enum EditPipeline {
    /// `scale` = output pixels per source pixel (thumbnails: < 1). Returns `image` unchanged for
    /// a neutral edit at scale 1.
    public static func render(_ image: CGImage, _ adj: Adjustments, scale: Double = 1) -> CGImage? {
        let a = adj.normalized()
        if a.isIdentity && abs(scale - 1) < 1e-9 { return image }
        let (ow, oh) = EditMath.outputSize(width: image.width, height: image.height, a, scale: scale)
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(data: nil, width: ow, height: oh, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        else { return nil }
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

        if !a.isToneIdentity, let data = ctx.data {
            applyTone(data: data, width: ow, height: oh, rowBytes: ctx.bytesPerRow, a)
        }
        return ctx.makeImage()
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
