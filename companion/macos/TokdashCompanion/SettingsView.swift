import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: CompanionStore
    @State private var baseURL: String = ""
    @State private var launchAtLogin: Bool = false
    @State private var lowQuotaNotifications: Bool = false
    @State private var fiveHourThreshold: Double = 20
    @State private var weeklyThreshold: Double = 10
    @State private var otherThreshold: Double = 15
    @State private var language: AppLanguage = .system
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
            Section(L10n.t("section_server")) {
                HStack(spacing: 8) {
                    TextField(L10n.t("base_url"), text: $baseURL)
                        .textFieldStyle(.roundedBorder)
                    Button(L10n.t("test")) { runConnectionTest() }
                        .disabled(!CompanionStore.isValidBaseURL(baseURL) || testResult == .testing)
                }
                testResultView
                Text(L10n.t("server_hint"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section(L10n.t("section_startup")) {
                Toggle(L10n.t("launch_at_login"), isOn: $launchAtLogin)
            }
            Section(L10n.t("section_notifications")) {
                Toggle(L10n.t("low_quota_notifications"), isOn: $lowQuotaNotifications)
                Text(L10n.t("low_quota_hint"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section(L10n.t("section_thresholds")) {
                Slider(value: $fiveHourThreshold, in: 5...50, step: 1) {
                    Text(L10n.t("threshold_5h", Int(fiveHourThreshold)))
                }
                Slider(value: $weeklyThreshold, in: 5...50, step: 1) {
                    Text(L10n.t("threshold_weekly", Int(weeklyThreshold)))
                }
                Slider(value: $otherThreshold, in: 5...50, step: 1) {
                    Text(L10n.t("threshold_other", Int(otherThreshold)))
                }
            }
            Section(L10n.t("section_language")) {
                Picker(L10n.t("section_language"), selection: $language) {
                    ForEach(AppLanguage.allCases, id: \.self) { lang in
                        Text(lang.displayName).tag(lang)
                    }
                }
                Text(L10n.t("language_hint"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
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
        .onChange(of: language) { _, _ in saveSettings() }
    }

    @ViewBuilder private var testResultView: some View {
        switch testResult {
        case .idle:
            EmptyView()
        case .testing:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(L10n.t("testing")).font(.caption).foregroundStyle(.secondary)
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
            testResult = .failed(L10n.t("test_bad_url"))
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
                    ? .ok(L10n.t("test_ok", CompanionStore.serverLabel(for: candidate), health.version))
                    : .failed(L10n.t("test_not_tokdash"))
            } catch {
                if Task.isCancelled { return }
                testResult = .failed(L10n.t("test_reachable_error", error.localizedDescription))
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
        language = store.settings.language
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
        if language != store.settings.language { store.applyLanguage(language) }
        store.settings.save()
        if thresholdsChanged { store.applyThresholds() } // rebuild the Low view immediately
        if launchChanged { store.setLaunchAtLogin(launchAtLogin) }
        if urlChanged { store.updateBaseURL(trimmed) }
    }
}
