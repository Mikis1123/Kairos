import CoreGraphics
import KairosCore
import SwiftUI

/// The touch editor (Phase 2). It edits one `Adjustments` record, the same JSON the desktop's
/// Quick Edit writes (EDIT_VERSION 1), so an edit renders identically on both.
///
///   Adjust  the five desktop tones (Exposure, Highlights, Shadows, Warmth, Contrast), ±100.
///           One tool at a time under the photo (chips + a large slider). Values snap to 0 with a
///           detent. The preview follows the finger: only the tone LUT re-runs (EditRenderer).
///   Crop    CropStraightenView: the drag/pinch crop rectangle, the aspect presets and the
///           straighten dial.
///
/// Done writes the replica at once (a dirty row = queued for the desktop) and re-bakes the
/// grid's LQIP, so the library shows the edit immediately, online or offline. The upload runs
/// in the background, and the desktop's canonical answer comes back through sync.
/// Press and hold the photo to see the original.
struct EditView: View {
    let id: MediaID
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var adj = Adjustments()
    @State private var stored = Adjustments()
    @State private var mode: Mode = .adjust
    @State private var tool = "exposure"
    @State private var renderer = EditRenderer()
    @State private var showOriginal = false
    @State private var confirmDiscard = false
    @State private var saving = false

    enum Mode: String, CaseIterable, Identifiable {
        case adjust, crop
        var id: String { rawValue }
    }

    struct Tool {
        let key: String
        let label: String
        let symbol: String
    }

    static let tools = [
        Tool(key: "exposure", label: "Exposure", symbol: "plusminus.circle"),
        Tool(key: "highlights", label: "Highlights", symbol: "circle.lefthalf.filled"),
        Tool(key: "shadows", label: "Shadows", symbol: "circle.righthalf.filled"),
        Tool(key: "temperature", label: "Warmth", symbol: "thermometer.medium"),
        Tool(key: "contrast", label: "Contrast", symbol: "circle.lefthalf.striped.horizontal"),
    ]

    struct Aspect {
        let key: String
        let label: String
    }

    static let aspects = [
        Aspect(key: "free", label: "Free"), Aspect(key: "original", label: "Original"),
        Aspect(key: "1:1", label: "Square"), Aspect(key: "16:9", label: "16:9"), Aspect(key: "4:5", label: "4:5"),
    ]

    private var changed: Bool { adj.normalized() != stored.normalized() }

    var body: some View {
        VStack(spacing: 0) {
            topBar
            canvas
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            Group {
                switch mode {
                case .adjust: adjustPanel
                case .crop: cropPanel
                }
            }
            .frame(height: 150)
            modePicker
        }
        .background(Color.black.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .task { await load() }
        .onChange(of: adj) { _, new in renderer.request(new, cropMode: mode == .crop) }
        .onChange(of: mode) { _, new in renderer.request(adj, cropMode: new == .crop) }
        .confirmationDialog("Discard your changes?", isPresented: $confirmDiscard, titleVisibility: .visible) {
            Button("Discard Changes", role: .destructive) { dismiss() }
            Button("Keep Editing", role: .cancel) {}
        }
    }

    // ── chrome ──

    private var topBar: some View {
        HStack {
            Button("Cancel") {
                if changed { confirmDiscard = true } else { dismiss() }
            }
            Spacer()
            if !adj.normalized().isIdentity || !stored.normalized().isIdentity {
                Button("Revert") {
                    withAnimation(Kairos.Motion.standard) { adj = Adjustments() }
                    Haptics.detent()
                }
                .foregroundStyle(Kairos.Palette.danger)
            }
            Spacer()
            Button {
                Task { await save() }
            } label: {
                Text("Done").fontWeight(.semibold)
            }
            .disabled(saving || renderer.source == nil)
            .foregroundStyle(Kairos.Palette.accent)
        }
        .font(.body)
        .foregroundStyle(Kairos.Palette.text)
        .padding(.horizontal, Kairos.Space.lg)
        .padding(.vertical, Kairos.Space.md)
    }

    @ViewBuilder
    private var canvas: some View {
        if let source = renderer.source {
            switch mode {
            case .adjust:
                let shown = showOriginal ? source : (renderer.preview ?? source)
                Image(decorative: shown, scale: 1)
                    .resizable()
                    .interpolation(.high)
                    .scaledToFit()
                    .padding(.horizontal, Kairos.Space.sm)
                    .onLongPressGesture(minimumDuration: 0.12, maximumDistance: 30, perform: {},
                                        onPressingChanged: { down in showOriginal = down })
                    .overlay(alignment: .top) {
                        if showOriginal {
                            Text("Original")
                                .font(Kairos.Typography.caption)
                                .padding(.horizontal, 10).padding(.vertical, 4)
                                .background(.ultraThinMaterial, in: Capsule())
                                .padding(.top, Kairos.Space.sm)
                        }
                    }
                    .accessibilityLabel("Photo preview")
                    .accessibilityHint("Touch and hold to see the original.")
            case .crop:
                CropStraightenView(image: renderer.toned ?? source, adj: $adj)
            }
        } else {
            ProgressView().tint(.white)
        }
    }

    private var modePicker: some View {
        HStack(spacing: Kairos.Space.xxl) {
            ForEach(Mode.allCases) { m in
                Button {
                    withAnimation(Kairos.Motion.standard) { mode = m }
                } label: {
                    VStack(spacing: 4) {
                        Image(systemName: m == .adjust ? "dial.medium" : "crop.rotate")
                            .font(.system(size: 20, weight: .medium))
                        Text(m == .adjust ? "Adjust" : "Crop").font(Kairos.Typography.caption)
                    }
                    .foregroundStyle(mode == m ? Kairos.Palette.accent : Kairos.Palette.textSecondary)
                    .frame(minWidth: 64, minHeight: 44)
                }
                .accessibilityAddTraits(mode == m ? .isSelected : [])
            }
        }
        .padding(.vertical, Kairos.Space.sm)
        .padding(.bottom, Kairos.Space.xs)
    }

    // ── adjust ──

    private var adjustPanel: some View {
        VStack(spacing: Kairos.Space.md) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: Kairos.Space.md) {
                    ForEach(Self.tools, id: \.key) { t in
                        ToolChip(symbol: t.symbol, value: adj.tone(t.key), selected: tool == t.key)
                            .onTapGesture(count: 2) {          // double-tap resets that tool (declared first: wins)
                                tool = t.key
                                adj.setTone(t.key, 0)
                                Haptics.detent()
                            }
                            .onTapGesture {
                                withAnimation(Kairos.Motion.micro) { tool = t.key }
                                Haptics.tick()
                            }
                            .accessibilityLabel(t.label)
                            .accessibilityValue("\(Int(adj.tone(t.key).rounded()))")
                            .accessibilityAddTraits(tool == t.key ? .isSelected : [])
                    }
                }
                .padding(.horizontal, Kairos.Space.lg)
            }
            let spec = Self.tools.first { $0.key == tool } ?? Self.tools[0]
            HStack {
                Text(spec.label.uppercased()).font(Kairos.Typography.caption)
                    .foregroundStyle(Kairos.Palette.textSecondary)
                Spacer()
                Text(signed(adj.tone(tool))).font(.callout.monospacedDigit().weight(.semibold))
                    .foregroundStyle(abs(adj.tone(tool)) < 0.5 ? Kairos.Palette.textSecondary : Kairos.Palette.text)
            }
            .padding(.horizontal, Kairos.Space.xl)
            ToneSlider(value: Binding(get: { adj.tone(tool) }, set: { adj.setTone(tool, $0) }))
                .padding(.horizontal, Kairos.Space.xl)
                .accessibilityLabel(spec.label)
        }
        .padding(.top, Kairos.Space.sm)
    }

    private func signed(_ v: Double) -> String {
        let i = Int(v.rounded())
        return i > 0 ? "+\(i)" : "\(i)"
    }

    // ── crop ──

    private var cropPanel: some View {
        VStack(spacing: Kairos.Space.md) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: Kairos.Space.sm) {
                    ForEach(Self.aspects, id: \.key) { a in
                        Button(a.label) {
                            guard let src = renderer.source else { return }
                            withAnimation(Kairos.Motion.spring) {
                                adj = CropGeometry.withAspect(adj, a.key, width: Double(src.width), height: Double(src.height))
                            }
                            Haptics.tick()
                        }
                        .font(Kairos.Typography.caption)
                        .padding(.horizontal, 12).padding(.vertical, 7)
                        .foregroundStyle(adj.aspect == a.key ? Kairos.Palette.onAccent : Kairos.Palette.text)
                        .background(adj.aspect == a.key ? Kairos.Palette.accentFill : Kairos.Palette.surface2,
                                    in: Capsule())
                    }
                    Button {
                        withAnimation(Kairos.Motion.spring) {
                            adj.crop = [0, 0, 1, 1]
                            adj.angle = 0
                            adj.aspect = "free"
                        }
                        Haptics.detent()
                    } label: {
                        Label("Reset", systemImage: "arrow.counterclockwise")
                    }
                    .font(Kairos.Typography.caption)
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .foregroundStyle(Kairos.Palette.text)
                    .background(Kairos.Palette.surface2, in: Capsule())
                    .disabled(adj.normalized().isGeometryIdentity && adj.aspect == "free")
                }
                .padding(.horizontal, Kairos.Space.lg)
            }
            StraightenDial(angle: $adj.angle)
                .padding(.horizontal, Kairos.Space.lg)
        }
        .padding(.top, Kairos.Space.sm)
    }

    // ── load / save ──

    private func load() async {
        let current = model.store?.edit(for: id) ?? Adjustments()
        stored = current
        adj = current
        // the thumbnail first (instant), then the sharper working source
        if let quick = await model.images.rawThumbnail(id) {
            renderer.setSource(quick)
            renderer.request(adj, cropMode: mode == .crop)
        }
        if let better = await model.images.editSource(id), better.width > (renderer.source?.width ?? 0) {
            renderer.setSource(better)
            renderer.request(adj, cropMode: mode == .crop)
        }
    }

    private func save() async {
        let a = adj.normalized()
        guard changed else { return dismiss() }
        saving = true
        var small: CGImage?
        if let src = renderer.source { small = await EditRenderer.finalSmall(src, a) }
        await model.saveEdit(id, a, rendered: small)
        Haptics.release()
        dismiss()
    }
}

/// A tool button: the symbol inside a ring that fills with the value (iris for +, teal for -).
struct ToolChip: View {
    let symbol: String
    let value: Double
    let selected: Bool

    var body: some View {
        ZStack {
            Circle().stroke(Kairos.Palette.hairlineStrong, lineWidth: 2)
            Circle()
                .trim(from: 0, to: CGFloat(min(1, abs(value) / 100)))
                .stroke(value >= 0 ? Kairos.Palette.accent : Kairos.Palette.accent2,
                        style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .scaleEffect(x: value >= 0 ? 1 : -1, y: 1)
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(selected ? Kairos.Palette.text : Kairos.Palette.textSecondary)
        }
        .frame(width: 48, height: 48)
        .background(selected ? Kairos.Palette.surface2 : .clear, in: Circle())
        .contentShape(Circle())
    }
}

/// ±100 slider with a centre mark, a detent at 0 (it snaps within ±2), and a tick every 10.
struct ToneSlider: View {
    @Binding var value: Double
    @State private var lastStep: Int?

    var body: some View {
        Slider(value: Binding(get: { value }, set: { v in
            var x = v
            if abs(x) < 2 { x = 0 }
            if x == 0 && value != 0 { Haptics.detent() }
            let step = Int((x / 10).rounded(.towardZero))
            if let lastStep, step != lastStep, x != 0 { Haptics.tick() }
            lastStep = step
            value = x
        }), in: -100...100, onEditingChanged: { editing in
            if editing { Haptics.prepare() } else { lastStep = nil }
        })
        .tint(Kairos.Palette.accent)
        .background(alignment: .center) {
            Rectangle().fill(Kairos.Palette.textTertiary).frame(width: 1.5, height: 14)
        }
        .accessibilityValue("\(Int(value.rounded()))")
    }
}
