import AppKit
import SwiftUI
@preconcurrency import UserNotifications

/// Shared popover metrics. The menu-bar popover and the notification-tap window must
/// stay the same width, so both read these rather than repeating literals.
enum CompanionLayout {
    static let popoverWidth: CGFloat = 300
    /// The quota list scrolls; this keeps it tall enough to show several windows at once
    /// while still leaving room for the Today hero above it.
    static let quotaMinHeight: CGFloat = 150
    static let quotaMaxHeight: CGFloat = 260
}

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
                .frame(width: CompanionLayout.popoverWidth)
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
    // MenuBarExtra's popover cannot be opened programmatically (no public API), so a
    // notification tap presents the Low quota view in this dedicated floating window.
    @MainActor private static var alertWindow: NSWindow?

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if response.notification.request.content.userInfo["openQuota"] != nil {
            let s = store
            Task { @MainActor in
                guard let s else { return }
                s.quotaView = .low
                Self.openQuotaWindow(store: s)  // static -> no self capture across the @Sendable Task
            }
        }
        completionHandler()
    }

    @MainActor private static func openQuotaWindow(store: CompanionStore) {
        // Reuse the retained panel whether or not it is visible: it is
        // isReleasedWhenClosed = false, so makeKeyAndOrderFront reopens a closed one.
        // Keying off isVisible allocated a fresh NSPanel per notification-after-close
        // and leaked the previous one.
        if let w = alertWindow {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let hosting = NSHostingController(rootView:
            ContentView()
                .environmentObject(store)
                .frame(width: CompanionLayout.popoverWidth))
        let w = NSPanel(contentViewController: hosting)
        w.styleMask = [.titled, .closable, .fullSizeContentView]
        w.title = "Tokdash"
        w.titlebarAppearsTransparent = true
        w.isFloatingPanel = true
        w.level = .floating
        w.center()
        w.isReleasedWhenClosed = false
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        alertWindow = w
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}
