import KairosCore
import KairosUI
import SwiftUI

@main
struct KairosApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootTabView()
                .environment(model)
        }
    }
}
