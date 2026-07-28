import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: CompanionStore
    @State private var baseURL: String = ""
    @State private var launchAtLogin: Bool = false
    @State private var lowQuotaNotifications: Bool = false
    @State private var fiveHourThreshold: Double = 20
    @State private var weeklyThreshold: Double = 10
    @State private var otherThreshold: Double = 15
    @State private var urlDebounce: Task<Void, Never>?

    var body: some View {
        Form {
            Section("Server") {
                TextField("Base URL", text: $baseURL)
                    .textFieldStyle(.roundedBorder)
                Text("Default: http://127.0.0.1:55423. Tailscale HTTPS URLs are supported.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Startup") {
                Toggle("Launch at Login", isOn: $launchAtLogin)
            }
            Section("Notifications") {
                Toggle("Low-quota notifications", isOn: $lowQuotaNotifications)
                Text("Notifies when a subscription window crosses its threshold. Opt-in.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Quota Alert Thresholds (% remaining)") {
                Slider(value: $fiveHourThreshold, in: 5...50, step: 1) {
                    Text("5-hour: \(Int(fiveHourThreshold))%")
                }
                Slider(value: $weeklyThreshold, in: 5...50, step: 1) {
                    Text("Weekly: \(Int(weeklyThreshold))%")
                }
                Slider(value: $otherThreshold, in: 5...50, step: 1) {
                    Text("Default: \(Int(otherThreshold))%")
                }
            }
        }
        .formStyle(.grouped)
        .padding(20)
        .frame(width: 380)
        .onAppear { loadSettings() }
        .onChange(of: baseURL) { _ in
            // Debounce the URL: don't reconnect on every keystroke.
            urlDebounce?.cancel()
            urlDebounce = Task {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if !Task.isCancelled { saveSettings() }
            }
        }
        .onChange(of: launchAtLogin) { _ in saveSettings() }
        .onChange(of: lowQuotaNotifications) { _ in saveSettings() }
        .onChange(of: fiveHourThreshold) { _ in saveSettings() }
        .onChange(of: weeklyThreshold) { _ in saveSettings() }
        .onChange(of: otherThreshold) { _ in saveSettings() }
    }

    private func loadSettings() {
        baseURL = store.settings.baseURL
        launchAtLogin = store.settings.launchAtLogin
        lowQuotaNotifications = store.settings.lowQuotaNotifications
        fiveHourThreshold = store.settings.thresholds.fiveHour
        weeklyThreshold = store.settings.thresholds.weekly
        otherThreshold = store.settings.thresholds.other
    }

    private func saveSettings() {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let urlValid = isValidURL(trimmed)
        let urlChanged = urlValid && trimmed != store.settings.baseURL
        let launchChanged = launchAtLogin != store.settings.launchAtLogin

        // Only persist the URL when it's a valid absolute http/https URL.
        if urlValid { store.settings.baseURL = trimmed }
        store.settings.lowQuotaNotifications = lowQuotaNotifications
        let thresholds = QuotaThresholds(fiveHour: fiveHourThreshold, weekly: weeklyThreshold, other: otherThreshold)
        let thresholdsChanged = store.settings.thresholds != thresholds
        store.settings.thresholds = thresholds
        store.settings.save()
        if thresholdsChanged { store.applyThresholds() } // rebuild the Low view immediately
        if launchChanged { store.setLaunchAtLogin(launchAtLogin) }
        if urlChanged { store.updateBaseURL(trimmed) }
    }

    private func isValidURL(_ s: String) -> Bool {
        guard let url = URL(string: s),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              url.host != nil else { return false }
        return true
    }
}
