import CoreGraphics
import KairosCore
import SwiftUI

/// Touch crop and straighten, writing exactly the desktop's geometry fields:
///   angle  degrees, + = clockwise, ±45. The frame keeps the source's size: the image rotates
///          about the centre and zooms by EditMath.straightenScale, so no empty corner shows.
///   crop   (x, y, w, h) as fractions of that straightened frame.
///   aspect "free" | "original" | "1:1" | "16:9" | "4:5". The drag math is CropGeometry, the
///          fixture-pinned port of quick_edit.drag_crop.
///
/// What is on screen IS that model: the frame has the source's aspect, the image inside it is
/// drawn rotated and zoomed with view transforms (GPU, display rate), and the crop rectangle is
/// drawn in frame fractions. Dragging the dial does not re-render any pixels.
///
/// Gestures: drag a corner or an edge to resize (ratio kept when locked), drag inside to move, and
/// pinch to scale the rectangle about its centre. The rule-of-thirds grid shows while a finger
/// is down.
struct CropStraightenView: View {
    let image: CGImage
    @Binding var adj: Adjustments
    @State private var dragStart: CropGeometry.Rect?
    @State private var dragHandle: CropGeometry.Handle?
    @State private var dragBase: CGSize = .zero         // translation already consumed (after a pinch)
    @State private var lastTranslation: CGSize = .zero
    @State private var pinchStart: CropGeometry.Rect?
    @State private var interacting = false

    static let padding: CGFloat = 28
    static let touchSlop: CGFloat = 22                  // 44 pt targets around corners and edges

    private var W: Double { Double(image.width) }
    private var H: Double { Double(image.height) }
    private var crop: CropGeometry.Rect { CropGeometry.rect(adj.normalized().crop) }
    private var ratio: Double? { CropGeometry.aspectRatio(adj.aspect, width: W, height: H) }

    var body: some View {
        GeometryReader { geo in
            let frame = Self.fittedFrame(in: geo.size, aspect: W / H)
            ZStack(alignment: .topLeading) {
                Image(decorative: image, scale: 1)
                    .resizable()
                    .interpolation(.high)
                    .frame(width: frame.width, height: frame.height)
                    .rotationEffect(.degrees(adj.angle))
                    .scaleEffect(EditMath.straightenScale(width: W, height: H, angle: adj.angle))
                    .frame(width: frame.width, height: frame.height)
                    .clipped()
                    .offset(x: frame.minX, y: frame.minY)
                CropOverlay(frame: frame, crop: crop, grid: interacting)
                    .allowsHitTesting(false)
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .topLeading)
            .contentShape(Rectangle())
            .gesture(dragGesture(frame))
            .simultaneousGesture(pinchGesture())
            .accessibilityElement()
            .accessibilityLabel("Crop")
            .accessibilityValue("\(Int((crop.w * 100).rounded())) by \(Int((crop.h * 100).rounded())) percent, "
                                + "straightened \(String(format: "%.1f", adj.angle)) degrees")
        }
        .animation(Kairos.Motion.micro, value: interacting)
    }

    static func fittedFrame(in size: CGSize, aspect: Double) -> CGRect {
        let aw = max(1, size.width - 2 * padding), ah = max(1, size.height - 2 * padding)
        var w = aw, h = aw / aspect
        if h > ah { h = ah; w = ah * aspect }
        return CGRect(x: (size.width - w) / 2, y: (size.height - h) / 2, width: w, height: h)
    }

    private func setCrop(_ r: CropGeometry.Rect) {
        adj.crop = CropGeometry.array(r)
    }

    private func dragGesture(_ frame: CGRect) -> some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .local)
            .onChanged { v in
                lastTranslation = v.translation
                guard pinchStart == nil else { return }         // two fingers: the pinch owns it
                if dragStart == nil {
                    let p = (x: Double((v.startLocation.x - frame.minX) / frame.width),
                             y: Double((v.startLocation.y - frame.minY) / frame.height))
                    let slop = (x: Double(Self.touchSlop / frame.width), y: Double(Self.touchSlop / frame.height))
                    dragHandle = CropGeometry.handle(at: p, in: crop, slop: slop)
                    dragStart = crop
                    dragBase = .zero
                    if dragHandle != nil { interacting = true; Haptics.prepare() }
                }
                guard let start = dragStart, let handle = dragHandle else { return }
                let dx = Double((v.translation.width - dragBase.width) / frame.width)
                let dy = Double((v.translation.height - dragBase.height) / frame.height)
                setCrop(CropGeometry.drag(start, handle: handle, dx: dx, dy: dy, ratio: ratio, width: W, height: H))
            }
            .onEnded { _ in
                if dragHandle != nil { Haptics.release() }
                dragStart = nil
                dragHandle = nil
                lastTranslation = .zero
                interacting = false
            }
    }

    private func pinchGesture() -> some Gesture {
        MagnifyGesture()
            .onChanged { v in
                if pinchStart == nil {
                    pinchStart = crop
                    interacting = true
                }
                guard let start = pinchStart else { return }
                let centre = (x: start.x + start.w / 2, y: start.y + start.h / 2)
                setCrop(CropGeometry.pinch(start, scale: Double(v.magnification), anchor: centre))
            }
            .onEnded { _ in
                pinchStart = nil
                // a finger may stay down: continue the drag from here, not from where it began
                dragStart = crop
                dragBase = lastTranslation
                interacting = dragHandle != nil
                Haptics.release()
            }
    }
}

/// Dimmed outside, white border, L-shaped corner handles, thirds grid while interacting.
struct CropOverlay: View {
    let frame: CGRect
    let crop: CropGeometry.Rect
    let grid: Bool

    var body: some View {
        Canvas { ctx, size in
            let r = CGRect(x: frame.minX + crop.x * frame.width, y: frame.minY + crop.y * frame.height,
                           width: crop.w * frame.width, height: crop.h * frame.height)
            var dim = Path(CGRect(origin: .zero, size: size))
            dim.addRect(r)
            ctx.fill(dim, with: .color(.black.opacity(0.58)), style: FillStyle(eoFill: true))
            if grid {
                var g = Path()
                for i in 1...2 {
                    let x = r.minX + r.width * CGFloat(i) / 3, y = r.minY + r.height * CGFloat(i) / 3
                    g.move(to: CGPoint(x: x, y: r.minY)); g.addLine(to: CGPoint(x: x, y: r.maxY))
                    g.move(to: CGPoint(x: r.minX, y: y)); g.addLine(to: CGPoint(x: r.maxX, y: y))
                }
                ctx.stroke(g, with: .color(.white.opacity(0.4)), lineWidth: 0.75)
            }
            ctx.stroke(Path(r), with: .color(.white.opacity(0.9)), lineWidth: 1)
            let len: CGFloat = min(22, r.width / 3, r.height / 3), t: CGFloat = 3
            var corners = Path()
            for (cx, cy, sx, sy) in [(r.minX, r.minY, 1.0, 1.0), (r.maxX, r.minY, -1.0, 1.0),
                                     (r.minX, r.maxY, 1.0, -1.0), (r.maxX, r.maxY, -1.0, -1.0)] {
                let ox = cx - CGFloat(sx) * t / 2, oy = cy - CGFloat(sy) * t / 2
                corners.move(to: CGPoint(x: ox + CGFloat(sx) * len, y: oy))
                corners.addLine(to: CGPoint(x: ox, y: oy))
                corners.addLine(to: CGPoint(x: ox, y: oy + CGFloat(sy) * len))
            }
            ctx.stroke(corners, with: .color(.white), style: StrokeStyle(lineWidth: t, lineCap: .square))
        }
    }
}

/// Straighten dial: a ruler of degree ticks under a fixed centre needle. Drag it sideways, as
/// in Photos. It snaps to 0° within ±0.4°, ticks every 5°, gives a firm detent at 0° and at the
/// ±45° limits, and resets on double-tap.
struct StraightenDial: View {
    @Binding var angle: Double
    @State private var dragStart: Double?
    @State private var lastDetent: Int?

    static let pointsPerDegree: CGFloat = 9
    static let snap = 0.4

    var body: some View {
        VStack(spacing: 6) {
            Text(String(format: "%.1f°", angle))
                .font(.caption.monospacedDigit().weight(.semibold))
                .foregroundStyle(abs(angle) < 0.05 ? Kairos.Palette.textSecondary : Kairos.Palette.accent)
            Canvas { ctx, size in
                let mid = size.width / 2, ppd = Self.pointsPerDegree
                for d in -45...45 {
                    let x = mid + (CGFloat(d) - CGFloat(angle)) * ppd
                    guard x > -2, x < size.width + 2 else { continue }
                    let major = d % 5 == 0
                    let fade = max(0.15, 1 - abs(x - mid) / (size.width / 2))
                    let h: CGFloat = d == 0 ? 18 : (major ? 13 : 7)
                    var p = Path()
                    p.move(to: CGPoint(x: x, y: size.height / 2 - h / 2))
                    p.addLine(to: CGPoint(x: x, y: size.height / 2 + h / 2))
                    ctx.stroke(p, with: .color(.white.opacity((major ? 0.85 : 0.4) * fade)), lineWidth: major ? 1.5 : 1)
                }
                var needle = Path()
                needle.move(to: CGPoint(x: mid, y: 2))
                needle.addLine(to: CGPoint(x: mid, y: size.height - 2))
                ctx.stroke(needle, with: .color(Kairos.Palette.accent), style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
            }
            .frame(height: 30)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { v in
                        if dragStart == nil {
                            dragStart = angle
                            lastDetent = Int((angle / 5).rounded(.towardZero))
                            Haptics.prepare()
                        }
                        var a = (dragStart ?? 0) - Double(v.translation.width / Self.pointsPerDegree)
                        let limit = Adjustments.angleLimit
                        if a <= -limit || a >= limit {
                            a = min(max(a, -limit), limit)
                            if abs(angle) < limit { Haptics.detent() }
                        }
                        if abs(a) < Self.snap { a = 0 }
                        a = (a * 10).rounded() / 10
                        let detent = Int((a / 5).rounded(.towardZero))
                        if a == 0 && angle != 0 {
                            Haptics.detent()
                        } else if detent != lastDetent {
                            Haptics.tick()
                        }
                        lastDetent = detent
                        angle = a
                    }
                    .onEnded { _ in dragStart = nil }
            )
            // simultaneous: the zero-distance drag above would otherwise claim every touch
            .simultaneousGesture(TapGesture(count: 2).onEnded {
                angle = 0
                Haptics.detent()
            })
            .accessibilityElement()
            .accessibilityLabel("Straighten")
            .accessibilityValue(String(format: "%.1f degrees", angle))
            .accessibilityAdjustableAction { dir in
                switch dir {
                case .increment: angle = min(Adjustments.angleLimit, (angle + 1).rounded())
                case .decrement: angle = max(-Adjustments.angleLimit, (angle - 1).rounded())
                @unknown default: break
                }
            }
        }
    }
}
