#if os(iOS)
import UIKit
#endif

/// Detents for the editor: a tick on whole steps, and a firmer bump at zero or on snapping to a
/// limit. No-ops where there is no Taptic Engine (macOS package builds).
@MainActor
enum Haptics {
    #if os(iOS)
    private static let selection = UISelectionFeedbackGenerator()
    private static let soft = UIImpactFeedbackGenerator(style: .soft)
    private static let rigid = UIImpactFeedbackGenerator(style: .rigid)
    #endif

    static func tick() {
        #if os(iOS)
        selection.selectionChanged()
        #endif
    }

    static func detent() {
        #if os(iOS)
        rigid.impactOccurred(intensity: 0.8)
        #endif
    }

    static func release() {
        #if os(iOS)
        soft.impactOccurred(intensity: 0.5)
        #endif
    }

    static func prepare() {
        #if os(iOS)
        selection.prepare()
        rigid.prepare()
        #endif
    }
}
