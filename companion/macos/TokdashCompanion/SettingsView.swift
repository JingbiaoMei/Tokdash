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
    @State private var testResult: ConnectionTest = .idle
    @State private var testTask: Task<Void, Never>?

    /// Result of the Settings "Test" button. Probes the URL in the field, not the saved
    /// one, so a bad address can be caught before committing it.
    private enum ConnectionTest: Equatable {
        case idle
        case testing
        case ok(String)
        case failed(String)
    }

    var body: some View {
        Form {
            Section("Server") {
                HStack(spacing: 8) {
                    TextField("Base URL", text: $baseURL)
                        .textFieldStyle(.roundedBorder)
                    Button("Test") { runConnectionTest() }
                        .disabled(!CompanionStore.isValidBaseURL(baseURL) || testResult == .testing)
                }
                testResultView
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
        .onChange(of: baseURL) { _, _ in
            // Debounce the URL: don't reconnect on every keystroke.
            urlDebounce?.cancel()
            urlDebounce = Task {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if !Task.isCancelled { saveSettings() }
            }
        }
        .onChange(of: launchAtLogin) { _, _ in saveSettings() }
        .onChange(of: lowQuotaNotifications) { _, _ in saveSettings() }
        .onChange(of: fiveHourThreshold) { _, _ in saveSettings() }
        .onChange(of: weeklyThreshold) { _, _ in saveSettings() }
        .onChange(of: otherThreshold) { _, _ in saveSettings() }
    }

    @ViewBuilder private var testResultView: some View {
        switch testResult {
        case .idle:
            EmptyView()
        case .testing:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Testing…").font(.caption).foregroundStyle(.secondary)
            }
        case .ok(let detail):
            Label(detail, systemImage: "checkmark.circle.fill")
                .font(.caption)
                .foregroundStyle(.green)
        case .failed(let reason):
            Label(reason, systemImage: "xmark.circle.fill")
                .font(.caption)
                .foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Probe the URL currently in the field with its own short-lived client, so testing
    /// never disturbs the live connection or persists an address that turns out to be bad.
    private func runConnectionTest() {
        let candidate = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard CompanionStore.isValidBaseURL(candidate), let url = URL(string: candidate) else {
            testResult = .failed("Enter an absolute http:// or https:// URL.")
            return
        }
        testTask?.cancel()
        testResult = .testing
        testTask = Task {
            let client = TokdashClient(baseURL: url)
            do {
                let health = try await client.health()
                if Task.isCancelled { return }
                // A reachable server that isn't Tokdash is a failure, not a success -
                // otherwise a proxy or a wrong port would test green.
                testResult = health.service == "tokdash"
                    ? .ok("Connected to \(CompanionStore.serverLabel(for: candidate)) · Tokdash \(health.version)")
                    : .failed("Reachable, but not a Tokdash server.")
            } catch {
                if Task.isCancelled { return }
                testResult = .failed("Couldn't reach it: \(error.localizedDescription)")
            }
        }
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
        let urlValid = CompanionStore.isValidBaseURL(trimmed)
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
}
