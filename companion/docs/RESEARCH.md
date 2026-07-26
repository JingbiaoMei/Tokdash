# Platform and environment research

As of 2026-07-25.

## Findings

### macOS

- SwiftUI's `MenuBarExtra` is the direct fit for a persistent menu-bar control. Its
  `.window` style is intended for richer popover-like content, and `LSUIElement` hides a
  menu-bar-only utility from the Dock and app switcher.
- Apple's current Liquid Glass guidance says standard SwiftUI/AppKit popovers, controls,
  bars, and sheets adopt the current material automatically when built with current SDKs.
  Apple recommends reducing custom backgrounds, using custom glass sparingly, and testing
  reduced transparency and motion.
- Therefore, build a normal SwiftUI `MenuBarExtra` first. Do not hand-recreate Liquid Glass
  or make explicit glass effects the foundation of the UI.
- Xcode 26 requires macOS Sequoia 15.6 or later. macOS compilation, signing, notarization,
  and real menu-bar testing require a Mac.

Recommendation: develop and compile the macOS client on the MacBook. Linux remains useful
for docs, API-contract work, and review, but not as the macOS build authority.

### Windows

- Microsoft recommends WinUI 3 with the Windows App SDK for new native Windows apps.
- Acrylic is the intended material for transient, light-dismiss surfaces such as flyouts.
  Mica is intended for long-lived base surfaces such as a settings window.
- Windows still exposes notification-area icons through the Win32
  `Shell_NotifyIcon` API. The Windows App SDK's requested modern tray abstraction remains a
  backlog item, so a small Win32 interop layer is required.
- Microsoft guidance treats the notification area as status and access for background
  features with no desktop presence. It recommends one stable icon, concise tooltips,
  windows positioned near the icon, and avoiding rapid icon changes.
- WinUI requires MSBuild and a Windows development environment. WSL/Linux can host source
  work but cannot validate XAML compilation, notification-area behavior, Acrylic, or MSIX.

Recommendation: C#/WinUI 3 for content, direct `Shell_NotifyIconW` interop for the icon,
and an Acrylic WinUI flyout. Prove activation, positioning, light dismiss, and accessibility
in a small spike before building the product UI.

## Checked machines

### MacBook (`ssh macbook`)

```text
OS: macOS 26.5.2
Architecture: arm64
Xcode: 26.6 (build 17F113)
Swift: 6.3.3
Git: 2.50.1
Free space on /: about 306 GiB
```

The Mac toolchain is ready.

### Current Windows host

```text
OS: Windows 11 Education 10.0.26200, build 26200
Visual Studio: 2022 Build Tools present
.NET SDK: none found
```

The Windows host is suitable, but its WinUI toolchain is not ready. Install a supported
.NET SDK and the current Windows App SDK/WinUI development components before implementation.

## Framework comparison

| Option | Resident weight | Platform fidelity | Tray/menu integration | Recommendation |
|---|---:|---:|---:|---|
| SwiftUI + WinUI 3 | low/native | highest | direct, with Win32 interop on Windows | choose |
| Electron | high | low without extensive custom work | mature | reject for a lightweight companion |
| Tauri/web UI | lower than Electron | medium | plugin/native work still required | reject for MVP |
| Flutter | medium | medium | plugin/native work required | reject |
| Avalonia | medium | better on Windows than macOS menu-bar fidelity | native interop required | reject |

The two clients share little UI code, but the UI is small. Sharing the API contract and
fixtures gives most of the maintenance benefit without sacrificing the platform-native
surface.

## Risks to retire early

1. Windows notification-area flyout focus, light dismissal, multi-monitor placement, and
   DPI behavior.
2. macOS popover layout under Liquid Glass with Reduce Transparency and Increase Contrast.
3. Tokdash cold-cache latency and `503` backpressure when today/month requests start
   together.
4. Base URLs containing the Tailscale `/tokdash` path.
5. Packaging and launch-at-login behavior under signed macOS and MSIX builds.

## Sources

- [Apple: MenuBarExtra](https://developer.apple.com/documentation/swiftui/menubarextra)
- [Apple: Adopting Liquid Glass](https://developer.apple.com/documentation/TechnologyOverviews/adopting-liquid-glass)
- [Apple WWDC25: Meet Liquid Glass](https://developer.apple.com/videos/play/wwdc2025/219/)
- [Apple WWDC25: Get to know the new design system](https://developer.apple.com/videos/play/wwdc2025/356/)
- [Apple: Xcode 26 release notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-26-release-notes)
- [Microsoft: Windows app development](https://learn.microsoft.com/en-us/windows/apps/)
- [Microsoft: System backdrops, Mica and Acrylic](https://learn.microsoft.com/en-us/windows/apps/develop/ui/system-backdrops)
- [Microsoft: Acrylic material](https://learn.microsoft.com/en-us/windows/apps/design/style/acrylic)
- [Microsoft: Dialogs and flyouts](https://learn.microsoft.com/en-us/windows/apps/design/controls/dialogs-and-flyouts/)
- [Microsoft: Notifications and the notification area](https://learn.microsoft.com/en-us/windows/win32/shell/notification-area)
- [Microsoft: Shell_NotifyIcon](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shell_notifyiconw)
- [Microsoft: Notification area UX guidance](https://learn.microsoft.com/en-us/windows/win32/uxguide/winenv-notification)
- [Windows App SDK issue: modern system tray icon](https://github.com/microsoft/WindowsAppSDK/issues/713)
- [Microsoft: Windows developer FAQ](https://learn.microsoft.com/en-us/windows/apps/get-started/windows-developer-faq)
- [Microsoft: Package a WinUI app with single-project MSIX](https://learn.microsoft.com/en-us/windows/apps/windows-app-sdk/single-project-msix)

