import KairosCore
import KairosUI
import SwiftUI

@main
struct KairosApp: App {
    @State private var model: AppModel

    init() {
        // BGTaskScheduler handlers must exist before launch finishes (background sync + backup)
        BackgroundSync.register()
        _model = State(initialValue: AppModel())
        BackgroundSync.schedule()
    }

    var body: some Scene {
        WindowGroup {
            RootTabView()
                .environment(model)
        }
    }
}
