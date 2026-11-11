import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// Kairos design tokens. These are the desktop values from tokens.py / theme.py ("Graphite Night"
/// dark mode, Iris accent), so both apps read as one product. Change them only together with the
/// desktop.
public enum Kairos {
    public enum Palette {
        public static let background = Color(hex: 0x05060B)      // app canvas (desktop splash / map edge)
        public static let mapBackground = Color(hex: 0x06070C)   // map_host.MAP_BACKGROUND: no grey flash
        public static let surface = Color(hex: 0x1C1C1E)         // cards, sheets (CONTRAST_BACKGROUNDS dark)
        public static let surface2 = Color(hex: 0x232328)
        public static let surface3 = Color(hex: 0x2E2E35)
        public static let sidebar = Color(hex: 0x121215)         // tab bar / chrome, one step darker
        public static let placeholder = Color(hex: 0x141417)
        public static let text = Color(hex: 0xF2F2F4)
        public static let textSecondary = Color(hex: 0xA1A1AA)
        // WCAG AA (>= 4.5:1) for small text on every surface it sits on: dark 0x9898A4 is 4.5+ on
        // surface3 (the lightest), light 0x5C5C66 is 5.2+ on the grey groups. The old 0x6B6B74 was
        // 2.5-3.8:1 in dark. test_block_c_mobile.py recomputes the ratios from these literals.
        public static let textTertiary = Color(dark: 0x9898A4, light: 0x5C5C66)
        public static let hairline = Color.white.opacity(20.0 / 255.0)
        public static let hairlineStrong = Color.white.opacity(36.0 / 255.0)
        public static let pill = Color.white.opacity(24.0 / 255.0)
        // Iris accent (tokens.ACCENTS["iris"]["dark"]): text/icons, button fill, gradient partner
        public static let accent = Color(hex: 0x8B93FF)
        public static let accentFill = Color(hex: 0x5E64E8)
        public static let accent2 = Color(hex: 0x4FD1C5)
        public static let onAccent = Color.white
        public static let danger = Color(hex: 0xFF453A)
        public static let warning = Color(hex: 0xFF9F0A)
        public static let success = Color(hex: 0x30D158)
        public static let brandGradient = LinearGradient(colors: [accent, accent2], startPoint: .topLeading,
                                                         endPoint: .bottomTrailing)
    }

    /// tokens.RADIUS
    public enum Radius {
        public static let xs: CGFloat = 6, sm: CGFloat = 8, md: CGFloat = 10, lg: CGFloat = 14, xl: CGFloat = 20
    }

    /// tokens.SPACE
    public enum Space {
        public static let xxs: CGFloat = 2, xs: CGFloat = 4, sm: CGFloat = 8, md: CGFloat = 12
        public static let lg: CGFloat = 16, xl: CGFloat = 24, xxl: CGFloat = 32
    }

    /// tokens.MOTION: the durations match. The curves are SwiftUI equivalents, and springs replace
    /// OutBack.
    public enum Motion {
        public static let micro = Animation.easeOut(duration: 0.12)
        public static let standard = Animation.easeInOut(duration: 0.22)
        public static let emphasized = Animation.easeOut(duration: 0.36)
        public static let spring = Animation.spring(response: 0.42, dampingFraction: 0.78)
        public static let scene = Animation.easeInOut(duration: 0.6)
    }

    /// tokens.TYPE_SCALE, scaled to iOS reading distance (desktop body 13 pt -> iOS 17 pt), kept
    /// on Dynamic Type through the system text styles.
    public enum Typography {
        public static let largeTitle = Font.largeTitle.weight(.semibold)
        public static let title = Font.title3.weight(.semibold)
        public static let headline = Font.headline
        public static let body = Font.body
        public static let callout = Font.callout
        public static let caption = Font.caption.weight(.semibold)
        /// Section headers and counters: system text styles, so they follow Dynamic Type (the
        /// rows that carry them wrap or grow instead of clipping).
        public static let sectionHeader = Font.headline
        public static let counter = Font.caption.weight(.semibold).monospacedDigit()
        /// Tiny glyphs on photos (badges): Dynamic Type up to the point a grid cell can hold them.
        public static let badge = Font.caption2.weight(.semibold)
    }
}

extension Color {
    /// A colour that follows the interface style: `dark` in dark mode, `light` otherwise.
    init(dark: UInt32, light: UInt32) {
        #if canImport(UIKit)
        func ui(_ v: UInt32) -> UIColor {
            UIColor(red: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255,
                    blue: CGFloat(v & 0xFF) / 255, alpha: 1)
        }
        self.init(UIColor { $0.userInterfaceStyle == .light ? ui(light) : ui(dark) })
        #else
        self.init(hex: dark)
        #endif
    }

    init(hex: UInt32, opacity: Double = 1) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255, opacity: opacity)
    }

    /// "#RRGGBB" (event tints from the desktop's tint.py); nil for "" / invalid.
    public init?(hexString: String?) {
        guard var s = hexString?.trimmingCharacters(in: .whitespaces), !s.isEmpty else { return nil }
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        self.init(hex: v)
    }
}

/// Card surface used across tabs: surface fill, hairline border, lg radius.
public struct KairosCard: ViewModifier {
    public func body(content: Content) -> some View {
        content
            .background(Kairos.Palette.surface, in: RoundedRectangle(cornerRadius: Kairos.Radius.lg, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Kairos.Radius.lg, style: .continuous)
                .strokeBorder(Kairos.Palette.hairline, lineWidth: 1))
    }
}

extension View {
    public func kairosCard() -> some View { modifier(KairosCard()) }
}
