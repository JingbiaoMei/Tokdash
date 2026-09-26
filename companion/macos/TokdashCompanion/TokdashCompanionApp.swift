import AppKit
import SwiftUI
@preconcurrency import UserNotifications

/// Shared popover metrics. The menu-bar popover and the notification-tap window must
/// stay the same width, so both read these rather than repeating literals.
enum CompanionLayout {
    static let popoverWidth: CGFloat = 300
    /// The quota list scrolls; this keeps it tall enough to show several windows at once
    /// while still leaving room for the Today hero above it. Raised to 340 per review so
    /// the All view shows noticeably more subscription rows before scrolling.
    static let quotaMinHeight: CGFloat = 150
    static let quotaMaxHeight: CGFloat = 340

    /// The explicit AppKit window sizing below handles restored frames; this also
    /// supplies a sensible ideal height during SwiftUI's initial layout.
    static var settingsIdealHeight: CGFloat {
        let visible = NSScreen.main?.visibleFrame.height ?? 900
        return min(1080, max(420, visible - 44))
    }
}

/// `MenuBarExtra` reads an AppKit image's intrinsic canvas when it creates the status
/// item and ignores SwiftUI offsets on the extracted label. Render the artwork into the
/// bottom of a fixed canvas instead: the transparent space above it provides a real
/// two-point downward optical adjustment.
@MainActor
enum CompanionMenuBarIcon {
    static let artworkSize = NSSize(width: 15, height: 16)
    static let canvasSize = NSSize(width: 15, height: 20)

    static let image: NSImage = {
        let source = NSImage(named: "MenuBarIcon")
            ?? NSImage(systemSymbolName: "chart.bar.fill", accessibilityDescription: "Tokdash")
            ?? NSImage(size: artworkSize)
        let canvas = NSImage(size: canvasSize, flipped: false) { _ in
            source.draw(
                in: NSRect(origin: .zero, size: artworkSize),
                from: NSRect(origin: .zero, size: source.size),
                operation: .sourceOver,
                fraction: 1
            )
            return true
        }
        canvas.isTemplate = true
        return canvas
    }()
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
        // One-shot: AppKit persists the Settings window frame, and the persisted 640 pt
        // height kept the form scrolling after the fit-content default landed. Discard the
        // saved frame ONCE so the new ideal height takes effect; later user resizes stick.
        if !UserDefaults.standard.bool(forKey: "tokdashSettingsFrameFitV1") {
            UserDefaults.standard.removeObject(forKey: "NSWindow Frame com_apple_SwiftUI_Settings_window")
            UserDefaults.standard.set(true, forKey: "tokdashSettingsFrameFitV1")
        }
    }

    var body: some Scene {
        MenuBarExtra {
            ContentView()
                .environmentObject(store)
                .frame(width: CompanionLayout.popoverWidth)
                .onAppear { store.setOpen(true) }
                .onDisappear { store.setOpen(false) }
        } label: {
            Image(nsImage: CompanionMenuBarIcon.image)
                .accessibilityLabel(store.tooltipText)
                .help(store.tooltipText)
                .onAppear { store.startScheduler() }
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
                .environmentObject(store)
        }
        // Without this the Settings window clamps to the content's fixed frame and the
        // user can't drag it larger than its opening size. .contentMinSize lets it grow
        // freely from the content's minimum (the grouped Form scrolls as it shrinks).
        .windowResizability(.contentMinSize)
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
        // Low-quota taps open the Low view; reset-credit taps open the All view,
        // because the credits row lives under the Codex group there (and only there).
        let info = response.notification.request.content.userInfo
        let opensAll = info["openQuotaAll"] != nil
        if opensAll || info["openQuota"] != nil {
            let s = store
            Task { @MainActor in
                guard let s else { return }
                s.quotaView = opensAll ? .all : .low
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

/// Form's ideal size does not override a restored AppKit Settings frame.
/// Apply the opening size to the actual window, then leave user resizing alone.
struct SettingsWindowSize: NSViewRepresentable {
    final class SizingView: NSView {
        private weak var sizedWindow: NSWindow?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window, sizedWindow !== window else { return }
            sizedWindow = window
            DispatchQueue.main.async { [weak window] in
                guard let window else { return }
                let screen = window.screen?.visibleFrame ?? NSScreen.main?.visibleFrame
                    ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
                var frame = window.frame
                frame.size.height = min(1080, screen.height - 24)
                frame.size.width = max(frame.width, 540)
                frame.origin.y = max(screen.minY + 12, min(frame.maxY, screen.maxY - 12) - frame.height)
                frame.origin.x = min(max(frame.minX, screen.minX), screen.maxX - frame.width)
                window.setFrame(frame, display: true)
            }
        }
    }
    func makeNSView(context: Context) -> SizingView { SizingView() }
    func updateNSView(_ nsView: SizingView, context: Context) {}
}
