import Foundation

/// Pixel math of the Quick Edits: a 1:1 port of desktop quick_edit.py, kept free of any Apple
/// framework so it is unit-testable anywhere. EditPipeline.swift applies it with
/// CoreGraphics + vImage.
///
///   Tone: ONE 256-entry LUT per channel. Exposure and temperature act in linear light (sRGB EOTF),
///         then highlights, shadows and contrast shape a monotone Fritsch–Carlson curve in the
///         encoded domain. Monotone by construction, so no slider combination can solarise.
///   Geometry: ONE affine map. Straighten rotates about the centre and zooms just enough that no
///         empty corner enters the frame, then the normalised crop (in that straightened frame)
///         selects the output.
/// Fixtures/edit_contract_v1.json pins the LUT and the matrix against the Python originals.
public enum EditMath {
    public static let exposureEV = 2.0
    public static let tempGain = 0.18
    public static let shadowLift = 0.15
    public static let highlightLift = 0.15
    public static let midCoupling = 0.04
    public static let contrastSpan = 0.08
    static let luma = (0.2126, 0.7152, 0.0722)

    // ── tone ──

    static func srgbToLinear(_ v: Double) -> Double {
        v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
    }

    static func linearToSrgb(_ v: Double) -> Double {
        let c = min(max(v, 0), 1)
        return c <= 0.0031308 ? c * 12.92 : 1.055 * pow(c, 1 / 2.4) - 0.055
    }

    /// Fritsch–Carlson monotone cubic through strictly increasing (xs, ys), evaluated at t.
    static func monotoneCurve(_ xs: [Double], _ ys: [Double], _ t: Double) -> Double {
        let n = xs.count
        var h = [Double](repeating: 0, count: n - 1)
        var delta = [Double](repeating: 0, count: n - 1)
        for i in 0..<(n - 1) {
            h[i] = xs[i + 1] - xs[i]
            delta[i] = (ys[i + 1] - ys[i]) / h[i]
        }
        var m = [Double](repeating: 0, count: n)
        m[0] = delta[0]
        m[n - 1] = delta[n - 2]
        for i in 1..<(n - 1) { m[i] = (delta[i - 1] + delta[i]) / 2 }
        for i in 0..<(n - 1) {
            if delta[i] == 0 {
                m[i] = 0
                m[i + 1] = 0
                continue
            }
            let a = m[i] / delta[i], b = m[i + 1] / delta[i]
            let s = a * a + b * b
            if s > 9 {
                let k = 3 / s.squareRoot()
                m[i] = k * a * delta[i]
                m[i + 1] = k * b * delta[i]
            }
        }
        let tc = min(max(t, xs[0]), xs[n - 1])
        // numpy searchsorted(side="right") - 1, clipped to a valid interval
        var idx = xs.filter { $0 <= tc }.count - 1
        idx = min(max(idx, 0), n - 2)
        let u = (tc - xs[idx]) / h[idx]
        let h00 = (1 + 2 * u) * (1 - u) * (1 - u)
        let h10 = u * (1 - u) * (1 - u)
        let h01 = u * u * (3 - 2 * u)
        let h11 = u * u * (u - 1)
        return h00 * ys[idx] + h10 * h[idx] * m[idx] + h01 * ys[idx + 1] + h11 * h[idx] * m[idx + 1]
    }

    public static func curvePoints(_ a: Adjustments) -> (xs: [Double], ys: [Double]) {
        let s = a.shadows / 100, hl = a.highlights / 100, c = a.contrast / 100
        let xs = [0.0, 0.25, 0.5, 0.75, 1.0]
        let ys = [0.0,
                  0.25 + shadowLift * s - contrastSpan * c,
                  0.5 + midCoupling * (s + hl),
                  0.75 + highlightLift * hl + contrastSpan * c,
                  1.0]
        return (xs, ys)
    }

    /// Linear-light gains (R, G, B): exposure × white balance, luminance-neutral WB.
    public static func channelGains(_ a: Adjustments) -> (r: Double, g: Double, b: Double) {
        let t = a.temperature / 100
        var r = 1 + tempGain * t, g = 1.0, b = 1 - tempGain * t
        let l = r * luma.0 + g * luma.1 + b * luma.2       // warming must not brighten
        r /= l; g /= l; b /= l
        let e = pow(2.0, exposureEV * a.exposure / 100)
        return (r * e, g * e, b * e)
    }

    /// Per-channel uint8 LUTs (numpy rint = round half to even).
    public static func buildLUT(_ a: Adjustments) -> (r: [UInt8], g: [UInt8], b: [UInt8]) {
        let gains = channelGains(a)
        let (xs, ys) = curvePoints(a)
        func table(_ gain: Double) -> [UInt8] {
            (0..<256).map { i in
                let lin = srgbToLinear(Double(i) / 255)
                let enc = linearToSrgb(lin * gain)
                let v = (monotoneCurve(xs, ys, enc) * 255).rounded(.toNearestOrEven)
                return UInt8(min(max(v, 0), 255))
            }
        }
        return (table(gains.r), table(gains.g), table(gains.b))
    }

    // ── geometry ──

    public static func straightenScale(width w: Double, height h: Double, angle: Double) -> Double {
        let th = abs(angle) * .pi / 180
        let c = cos(th), s = sin(th)
        return max((w * c + h * s) / w, (w * s + h * c) / h)
    }

    public static func outputSize(width w: Int, height h: Int, _ a: Adjustments, scale: Double = 1) -> (Int, Int) {
        let cw = a.crop[2], ch = a.crop[3]
        func r(_ v: Double) -> Int { max(1, Int((v).rounded(.toNearestOrEven))) }   // Python round()
        return (r(cw * Double(w) * scale), r(ch * Double(h) * scale))
    }

    /// 2×3 affine [m00, m01, tx, m10, m11, ty] mapping OUTPUT pixel coords -> SOURCE pixel coords,
    /// OpenCV convention (integer coordinates are pixel centres), exactly as quick_edit.inverse_matrix.
    public static func inverseMatrix(width wi: Int, height hi: Int, _ a: Adjustments, scale: Double = 1) -> [Double] {
        let w = Double(wi), h = Double(hi)
        let x0 = a.crop[0], y0 = a.crop[1]
        let th = a.angle * .pi / 180
        let s = straightenScale(width: w, height: h, angle: a.angle)
        let cx = w / 2, cy = h / 2
        let c = cos(th), sn = sin(th)
        let ox = x0 * w - cx, oy = y0 * h - cy
        let k = 1 / (s * scale)
        let m00 = c * k, m01 = sn * k
        let m10 = -sn * k, m11 = c * k
        var tx = cx + (c * ox + sn * oy) / s
        var ty = cy + (-sn * ox + c * oy) / s
        tx += (m00 + m01) * 0.5 - 0.5
        ty += (m10 + m11) * 0.5 - 0.5
        return [m00, m01, tx, m10, m11, ty]
    }

    /// The same map in continuous coordinates (pixel centres at +0.5, the CoreGraphics model).
    /// It undoes the half-pixel shift OpenCV needs: src_c = A·out_c + t + 0.5 - A·(0.5, 0.5).
    public static func continuousInverse(width: Int, height: Int, _ a: Adjustments, scale: Double = 1) -> [Double] {
        var m = inverseMatrix(width: width, height: height, a, scale: scale)
        m[2] += 0.5 - (m[0] + m[1]) * 0.5
        m[5] += 0.5 - (m[3] + m[4]) * 0.5
        return m
    }
}
