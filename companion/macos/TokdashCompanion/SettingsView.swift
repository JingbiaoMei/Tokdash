import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: CompanionStore
    @State private var servers: [CompanionServerSettings] = []
    @State private var launchAtLogin: Bool = false
    @State private var lowQuotaNotifications: Bool = false
    @State private var fiveHourThreshold: Double = 20
    @State private var weeklyThreshold: Double = 10
    @State private var otherThreshold: Double = 15
    @State private var language: AppLanguage = .system
    @State private var automaticUpdateChecks: Bool = false
    @State private var serverSaveTasks: [String: Task<Void, Never>] = [:]
    @State private var testResults: [String: ConnectionTest] = [:]
    @State private var testTasks: [String: Task<Void, Never>] = [:]
    @State private var pendingRemovalID: String?

    /// Result of the Settings "Test" button. Probes the URL in the field, not the saved
    /// one, so a bad address can be caught before committing it.
    private enum ConnectionTest: Equatable {
        case idle
        case testing
        case ok(String)
        case failed(message: String, detail: String)
    }

    var body: some View {
        Form {
            Section(L10n.t("section_servers")) {
                VStack(spacing: 10) {
                    ForEach($servers) { $server in
                        serverCard($server)
                    }
                }
                Button {
                    servers.append(.make(baseURL: CompanionSettings.defaultBaseURL))
                    saveSettings()
                } label: {
                    Label(L10n.t("add_server"), systemImage: "plus")
                }
                .buttonStyle(.bordered)
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
            Section(L10n.t("section_updates")) {
                Text(L10n.t("update_current_version", CompanionStore.currentVersion))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle(L10n.t("update_auto_check"), isOn: $automaticUpdateChecks)
                Text(L10n.t("update_auto_check_hint"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                updateStatusView
                HStack(spacing: 8) {
                    Button(L10n.t("update_check_now")) { store.checkForUpdates(manual: true) }
                        .disabled(store.updateStatus == .checking)
                    if let version = store.updateAvailableVersion {
                        Button(L10n.t("update_view")) { store.openUpdatePage() }
                        Button(L10n.t("update_skip")) { store.skipUpdate(version: version) }
                    }
                }
                Text(store.lastUpdateCheckText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
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
        .scrollContentBackground(.hidden)
        .padding(20)
        .frame(width: 480)
        // Match the clean white content surface used by standard settings windows in
        // light mode while retaining a readable system-managed surface in dark mode.
        .background(Color(nsColor: .textBackgroundColor).ignoresSafeArea())
        .onAppear { loadSettings() }
        .onChange(of: launchAtLogin) { _, _ in saveSettings() }
        .onChange(of: lowQuotaNotifications) { _, _ in saveSettings() }
        .onChange(of: fiveHourThreshold) { _, _ in saveSettings() }
        .onChange(of: weeklyThreshold) { _, _ in saveSettings() }
        .onChange(of: otherThreshold) { _, _ in saveSettings() }
        .onChange(of: language) { _, _ in saveSettings() }
        .onChange(of: automaticUpdateChecks) { _, _ in saveSettings() }
        .alert(L10n.t("remove_last_enabled_title"), isPresented: Binding(
            get: { pendingRemovalID != nil },
            set: { if !$0 { pendingRemovalID = nil } }
        )) {
            Button(L10n.t("remove"), role: .destructive) {
                guard let id = pendingRemovalID else { return }
                pendingRemovalID = nil
                removeServer(id, keepOneEnabled: true)
            }
            Button(L10n.t("cancel"), role: .cancel) { pendingRemovalID = nil }
        } message: {
            Text(L10n.t("remove_last_enabled_message"))
        }
    }

    private func serverCard(_ server: Binding<CompanionServerSettings>) -> some View {
        let id = server.wrappedValue.id
        let displayName = server.wrappedValue.label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? L10n.t("server_unnamed")
            : server.wrappedValue.label
        let isEnabled = server.wrappedValue.enabled
        let isOnlyEnabled = isEnabled && servers.filter(\.enabled).count == 1

        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Toggle("", isOn: server.enabled)
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .tint(.blue)
                    .disabled(isOnlyEnabled)
                    .help(isOnlyEnabled ? L10n.t("keep_one_server_enabled") : L10n.t("enable_server", displayName))
                    .accessibilityLabel(L10n.t("enable_server", displayName))
                    .accessibilitySortPriority(5)

                TextField("", text: server.label, prompt: Text(L10n.t("server_name_placeholder")))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1)
                    .multilineTextAlignment(.leading)
                    .frame(minWidth: 100, maxWidth: .infinity)
                    .layoutPriority(1)
                    .accessibilityLabel(L10n.t("server_name_placeholder"))
                    .accessibilitySortPriority(4)

                if servers.count > 1 {
                    Button {
                        requestRemoval(of: server.wrappedValue)
                    } label: {
                        Image(systemName: "minus.circle.fill")
                            .font(.system(size: 16))
                            .frame(width: 28, height: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .help(L10n.t("remove_server", displayName))
                    .accessibilityLabel(L10n.t("remove_server", displayName))
                    .accessibilitySortPriority(1)
                }
            }

            HStack(spacing: 8) {
                Text(L10n.t("base_url"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 60, alignment: .trailing)

                TextField("", text: server.baseURL, prompt: Text(L10n.t("base_url")))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1)
                    .multilineTextAlignment(.leading)
                    .frame(minWidth: 180, maxWidth: .infinity)
                    .layoutPriority(1)
                    .help(server.wrappedValue.baseURL)
                    .accessibilityLabel(L10n.t("base_url"))
                    .accessibilitySortPriority(3)

                Button(L10n.t("test")) { runConnectionTest(server.wrappedValue) }
                    .buttonStyle(.bordered)
                    .frame(width: 64)
                    .disabled(server.wrappedValue.baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                              || testResults[id] == .testing)
                    .accessibilityLabel(L10n.t("test_server", displayName))
                    .accessibilitySortPriority(2)
            }
            .opacity(isEnabled ? 1 : 0.55)

            HStack(spacing: 8) {
                Color.clear.frame(width: 60, height: 1)
                testResultView(testResults[id] ?? .idle)
            }
            .frame(height: 16)
            .opacity(isEnabled ? 1 : 0.55)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color(nsColor: .separatorColor).opacity(0.45), lineWidth: 1)
        )
        .onChange(of: server.wrappedValue.baseURL) { _, _ in
            testTasks[id]?.cancel()
            testTasks[id] = nil
            testResults[id] = .idle
            scheduleServerSave(id)
        }
        .onChange(of: server.wrappedValue.label) { _, _ in
            scheduleServerSave(id)
        }
        .onChange(of: server.wrappedValue.enabled) { _, _ in
            saveSettings()
        }
    }

    /// Update status line. An available version outranks a `failed`/`idle` status: a
    /// manual check that later fails must not hide an update we already know about.
    @ViewBuilder private var updateStatusView: some View {
        if store.updateStatus == .checking {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(L10n.t("update_checking")).font(.caption).foregroundStyle(.secondary)
            }
        } else if let version = store.updateAvailableVersion {
            Label(L10n.t("update_available", version), systemImage: "arrow.down.circle.fill")
                .font(.caption)
                .foregroundStyle(.orange)
        } else if let skipped = store.settings.skippedUpdateVersion,
                  skipped == store.settings.availableUpdateVersion {
            Text(L10n.t("update_skipped"))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        } else if case .failed(let reason) = store.updateStatus {
            Label(reason, systemImage: "exclamationmark.circle.fill")
                .font(.caption)
                .foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
        } else if store.updateStatus == .upToDate {
            Label(L10n.t("update_up_to_date"), systemImage: "checkmark.circle.fill")
                .font(.caption)
                .foregroundStyle(.green)
        } else {
            Text(L10n.t("update_manual_hint"))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private func testResultView(_ result: ConnectionTest) -> some View {
        switch result {
        case .idle:
            Label(L10n.t("server_not_tested"), systemImage: "circle")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        case .testing:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(L10n.t("testing"))
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        case .ok(let detail):
            Label(detail, systemImage: "checkmark.circle.fill")
                .font(.system(size: 11))
                .foregroundStyle(.green)
        case .failed(let message, let detail):
            Label(message, systemImage: "xmark.octagon.fill")
                .font(.system(size: 11))
                .foregroundStyle(.red)
                .lineLimit(1)
                .help(detail)
        }
    }

    /// Probe the URL currently in the field with its own short-lived client, so testing
    /// never disturbs the live connection or persists an address that turns out to be bad.
    private func runConnectionTest(_ server: CompanionServerSettings) {
        let candidate = server.baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard CompanionStore.isValidBaseURL(candidate), let url = URL(string: candidate) else {
            let message = L10n.t("test_bad_url")
            testResults[server.id] = .failed(message: message, detail: message)
            announceTestResult(message)
            return
        }
        testTasks[server.id]?.cancel()
        testResults[server.id] = .testing
        testTasks[server.id] = Task {
            let client = TokdashClient(baseURL: url)
            let startedAt = Date()
            do {
                let health = try await client.health()
                if Task.isCancelled { return }
                // A reachable server that isn't Tokdash is a failure, not a success -
                // otherwise a proxy or a wrong port would test green.
                if health.service == "tokdash" {
                    let elapsed = max(1, Int(Date().timeIntervalSince(startedAt) * 1_000))
                    let message = L10n.t("test_reachable_latency", elapsed)
                    testResults[server.id] = .ok(message)
                    announceTestResult(message)
                } else {
                    let detail = L10n.t("test_not_tokdash")
                    let message = L10n.t("test_invalid_response")
                    testResults[server.id] = .failed(message: message, detail: detail)
                    announceTestResult(message)
                }
            } catch {
                if Task.isCancelled { return }
                let message: String
                switch error as? TokdashError {
                case .timeout:
                    message = L10n.t("test_timed_out")
                case .badResponse, .decode:
                    message = L10n.t("test_invalid_response")
                default:
                    message = L10n.t("test_unreachable")
                }
                testResults[server.id] = .failed(message: message, detail: error.localizedDescription)
                announceTestResult(message)
            }
        }
    }

    private func announceTestResult(_ message: String) {
        AccessibilityNotification.Announcement(message).post()
    }

    private func loadSettings() {
        servers = store.settings.servers
        launchAtLogin = store.settings.launchAtLogin
        lowQuotaNotifications = store.settings.lowQuotaNotifications
        fiveHourThreshold = store.settings.thresholds.fiveHour
        weeklyThreshold = store.settings.thresholds.weekly
        otherThreshold = store.settings.thresholds.other
        language = store.settings.language
        automaticUpdateChecks = store.settings.automaticUpdateChecks
    }

    private func scheduleServerSave(_ id: String) {
        serverSaveTasks[id]?.cancel()
        serverSaveTasks[id] = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard !Task.isCancelled else { return }
            saveSettings()
            serverSaveTasks[id] = nil
        }
    }

    private func saveSettings() {
        let validServers = servers.map { server -> CompanionServerSettings in
            var copy = server
            copy.baseURL = copy.baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            return copy
        }.filter { CompanionStore.isValidBaseURL($0.baseURL) }
        // During the 500 ms debounce a URL is commonly incomplete. Keep the last
        // persisted registry intact until every visible row is valid; never silently
        // drop the row the user is still editing.
        guard validServers.count == servers.count, validServers.contains(where: { $0.enabled }) else { return }
        let urlChanged = validServers.first(where: { $0.enabled })?.baseURL != store.settings.baseURL
        let launchChanged = launchAtLogin != store.settings.launchAtLogin

        // Only persist the URL when it's a valid absolute http/https URL.
        store.settings.servers = validServers
        store.settings.lowQuotaNotifications = lowQuotaNotifications
        let thresholds = QuotaThresholds(fiveHour: fiveHourThreshold, weekly: weeklyThreshold, other: otherThreshold)
        let thresholdsChanged = store.settings.thresholds != thresholds
        store.settings.thresholds = thresholds
        if language != store.settings.language { store.applyLanguage(language) }
        // setAutomaticUpdateChecks persists and kicks the first check when turned on, so
        // it must run before the blanket save below rather than through it.
        store.setAutomaticUpdateChecks(automaticUpdateChecks)
        store.settings.save()
        if thresholdsChanged { store.applyThresholds() } // rebuild the Low view immediately
        if launchChanged { store.setLaunchAtLogin(launchAtLogin) }
        if urlChanged { store.updateBaseURL(store.settings.baseURL) }
    }

    private func requestRemoval(of server: CompanionServerSettings) {
        if server.enabled && servers.filter(\.enabled).count == 1 {
            pendingRemovalID = server.id
        } else {
            removeServer(server.id)
        }
    }

    private func removeServer(_ id: String, keepOneEnabled: Bool = false) {
        guard servers.count > 1 else { return }
        servers.removeAll { $0.id == id }
        if keepOneEnabled, !servers.contains(where: \.enabled), !servers.isEmpty {
            servers[0].enabled = true
        }
        testTasks[id]?.cancel()
        testTasks[id] = nil
        testResults[id] = nil
        serverSaveTasks[id]?.cancel()
        serverSaveTasks[id] = nil
        saveSettings()
    }
}
