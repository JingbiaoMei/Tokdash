import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: CompanionStore
    @State private var baseURL: String = ""
    @State private var launchAtLogin: Bool = false
    @State private var lowQuotaNotifications: Bool = false
    @State private var fiveHourThreshold: Double = 20
    @State private var weeklyThreshold: Double = 10
    @State private var otherThreshold: Double = 15

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
        .onChange(of: baseURL) { _ in saveSettings() }
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
        store.settings.baseURL = baseURL
        store.settings.launchAtLogin = launchAtLogin
        store.settings.lowQuotaNotifications = lowQuotaNotifications
        store.settings.thresholds = QuotaThresholds(
            fiveHour: fiveHourThreshold,
            weekly: weeklyThreshold,
            other: otherThreshold
        )
        store.settings.save()
    }
}
