#if os(iOS)
import BackgroundTasks
import Foundation

/// iOS background work (Parity sprint):
///   - app refresh (`refreshId`, ~every 15 min when iOS allows): pull the desktop's changes and
///     re-index the camera roll, so the app opens current
///   - processing (`processingId`, on external power + network, typically overnight while
///     charging on the home Wi-Fi): the backup pass, including the deep "does the PC already
///     have these bytes" hashing
/// iOS decides when they run; both are re-submitted on every launch and after every run.
/// Register before the app finishes launching (KairosApp.init).
public enum BackgroundSync {
    public static let refreshId = "app.kairos.ios.refresh"
    public static let processingId = "app.kairos.ios.backup"

    /// The live model. Set by AppModel.init; background launches create it too (the App struct
    /// is initialised even when no scene connects).
    @MainActor static weak var model: AppModel?

    public static func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshId, using: nil) { task in
            run(task, deep: false)
        }
        BGTaskScheduler.shared.register(forTaskWithIdentifier: processingId, using: nil) { task in
            run(task, deep: true)
        }
    }

    public static func schedule() {
        let refresh = BGAppRefreshTaskRequest(identifier: refreshId)
        refresh.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(refresh)
        let processing = BGProcessingTaskRequest(identifier: processingId)
        processing.requiresExternalPower = true
        processing.requiresNetworkConnectivity = true
        processing.earliestBeginDate = Date(timeIntervalSinceNow: 30 * 60)
        try? BGTaskScheduler.shared.submit(processing)
    }

    private static func run(_ task: BGTask, deep: Bool) {
        schedule()                                     // keep the chain going
        let work = Task { @MainActor in
            await model?.backgroundPass(deep: deep, budget: deep ? 240 : 25)
        }
        task.expirationHandler = { work.cancel() }
        Task {
            await work.value
            task.setTaskCompleted(success: !work.isCancelled)
        }
    }
}
#endif
