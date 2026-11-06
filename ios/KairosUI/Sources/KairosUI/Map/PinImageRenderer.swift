#if os(iOS)
import CoreGraphics
import KairosCore
import UIKit

/// The desktop's teardrop photo pin, drawn with CoreGraphics (port of drawPin in map_vault.html):
///   - an 80 × 79 pt image whose bottom-centre pixel is the tip, i.e. the coordinate
///     (symbol layer 'icon-anchor: bottom')
///   - a white teardrop (disc + two tangents meeting at the tip) with a soft drop shadow
///   - the photo clipped to the inner disc, centre-cropped (object-fit: cover), on #3A3A3C
///   - clusters: two translucent "stacked photo" discs behind it, and an accent count badge at the
///     top right with a 2 pt #1C1C1E rim and 12 pt semibold white digits
/// Geometry comes from PinClustering, the same constants the desktop page uses.
enum PinImageRenderer {
    static let backing = UIColor(red: 0x3A / 255, green: 0x3A / 255, blue: 0x3C / 255, alpha: 1)
    static let placeholder = UIColor(red: 0x8E / 255, green: 0x8E / 255, blue: 0x93 / 255, alpha: 1)
    static let badgeRim = UIColor(red: 0x1C / 255, green: 0x1C / 255, blue: 0x1E / 255, alpha: 1)

    static var size: CGSize { CGSize(width: PinClustering.pinBox, height: PinClustering.pinHeight) }

    static func teardrop(cx: CGFloat, cy: CGFloat, r: CGFloat, tipY: CGFloat) -> CGPath {
        let t = PinClustering.teardropTangents(centreY: Double(cy), radius: Double(r), tipY: Double(tipY))
        let p = CGMutablePath()
        p.move(to: CGPoint(x: cx, y: tipY))
        p.addLine(to: CGPoint(x: cx + r * cos(CGFloat(t.a1)), y: cy + r * sin(CGFloat(t.a1))))
        // y-down context: decreasing angle runs over the top, like canvas arc(..., anticlockwise: true)
        p.addArc(center: CGPoint(x: cx, y: cy), radius: r, startAngle: CGFloat(t.a1), endAngle: CGFloat(t.a2),
                 clockwise: true)
        p.closeSubpath()
        return p
    }

    static func draw(photo: CGImage?, count: Int, accent: UIColor, scale: CGFloat) -> UIImage {
        let fmt = UIGraphicsImageRendererFormat()
        fmt.scale = scale
        fmt.opaque = false
        fmt.preferredRange = .standard
        return UIGraphicsImageRenderer(size: size, format: fmt).image { rc in
            let g = rc.cgContext
            let box = CGFloat(PinClustering.pinBox)
            let r = CGFloat(PinClustering.pinDiameter / 2)
            let x0 = box / 2, c = CGFloat(PinClustering.pinPad) + r, tipY = CGFloat(PinClustering.pinHeight)

            func disc(_ x: CGFloat, _ y: CGFloat, _ rr: CGFloat) {
                g.fillEllipse(in: CGRect(x: x - rr, y: y - rr, width: 2 * rr, height: 2 * rr))
            }

            if count > 1 {                               // the stack of photos behind the pin
                g.setFillColor(UIColor(white: 1, alpha: 0.28).cgColor)
                disc(x0 + 7, c + 4, r - 2)
                g.setFillColor(UIColor(white: 1, alpha: 0.55).cgColor)
                disc(x0 + 3.5, c + 2, r - 1)
            }
            g.saveGState()
            g.setShadow(offset: CGSize(width: 0, height: 1.5), blur: 9, color: UIColor(white: 0, alpha: 0.55).cgColor)
            g.setFillColor(UIColor.white.cgColor)
            g.addPath(teardrop(cx: x0, cy: c, r: r, tipY: tipY))
            g.fillPath()
            g.restoreGState()

            let ir = r - 2.5
            let inner = CGRect(x: x0 - ir, y: c - ir, width: 2 * ir, height: 2 * ir)
            g.saveGState()
            g.addEllipse(in: inner)
            g.clip()
            g.setFillColor(backing.cgColor)
            g.fill(inner)
            if let photo {
                let iw = CGFloat(photo.width), ih = CGFloat(photo.height)
                let k = max(2 * ir / iw, 2 * ir / ih)
                // UIImage.draw honours the flipped UIKit context (CGContext.draw would mirror it)
                UIImage(cgImage: photo).draw(in: CGRect(x: x0 - iw * k / 2, y: c - ih * k / 2, width: iw * k, height: ih * k))
            } else {
                g.setFillColor(placeholder.cgColor)
                disc(x0, c, ir - 11.5)
            }
            g.restoreGState()

            if count > 1 {                               // count badge, top right
                let label = count > 9999 ? "9999+" : String(count)
                let font = UIFont.systemFont(ofSize: 12, weight: .semibold)
                let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.white]
                let tw = (label as NSString).size(withAttributes: attrs).width
                let w = max(22, ceil(tw) + 12), h: CGFloat = 22
                let rect = CGRect(x: x0 + r + 8 - w, y: c - r - 6, width: w, height: h)
                let pill = UIBezierPath(roundedRect: rect, cornerRadius: h / 2)
                accent.setFill()
                pill.fill()
                pill.lineWidth = 2
                badgeRim.setStroke()
                pill.stroke()
                let th = (label as NSString).size(withAttributes: attrs).height
                (label as NSString).draw(at: CGPoint(x: rect.midX - tw / 2, y: rect.midY - th / 2 + 0.5), withAttributes: attrs)
            }
        }
    }
}
#endif
