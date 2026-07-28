import SwiftUI
import UserNotifications

@main
struct TokdashCompanionApp: App {
    @StateObject private var store: CompanionStore
    private let notificationDelegate: NotificationDelegate

    init() {
        let s = CompanionStore()
        _store = StateObject(wrappedValue: s)
        let del = NotificationDelegate()
        del.store = s
        notificationDelegate = del
        // Install the delegate early so notification taps + foreground delivery are handled.
        UNUserNotificationCenter.current().delegate = del
    }

    var body: some Scene {
        MenuBarExtra {
            ContentView()
                .environmentObject(store)
                .frame(width: 352)
                .onAppear { store.setOpen(true) }
                .onDisappear { store.setOpen(false) }
        } label: {
            Image(systemName: "chart.bar.fill")
                .accessibilityLabel(store.tooltipText)
                .help(store.tooltipText)
                .onAppear { store.startScheduler() }
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
                .environmentObject(store)
        }
    }
}

/// Handles low-quota notification taps (open the Low quota view) and foreground
/// presentation. Required by Apple for user responses and foreground delivery.
final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    weak var store: CompanionStore?

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if response.notification.request.content.userInfo["openQuota"] != nil {
            let s = store
            Task { @MainActor in
                s?.quotaView = .low
                NSApp.activate()
            }
        }
        completionHandler()
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}
