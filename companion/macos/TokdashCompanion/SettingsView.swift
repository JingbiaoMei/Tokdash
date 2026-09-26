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
    @State private var components: CompanionComponents = CompanionComponents()
    @State private var rankRows: Int = 3
    @State private var serverSaveTasks: [String: Task<Void, Never>] = [:]
    @State private var pendingRemovalID: String?
    @State private var editingAddress: String?
    @State private var addingAddress = false
    @State private var addressHostID: String?
    @State private var addressInput = ""
    @State private var addressError = ""
    @State private var checkingAddress = false
    @State private var routeProbes: [String: RouteProbe] = [:]
    @State private var checkingRoutes = false

    var body: some View {
        Form {
            Section(L10n.t("section_servers")) {
                VStack(spacing: 10) {
                    ForEach($servers) { $server in
                        serverCard($server)
                    }
                }
                Button {
                    editingAddress = nil; addressHostID = nil; addressInput = ""; addressError = ""; addingAddress = true
                } label: {
                    Label(L10n.t("add_server"), systemImage: "plus")
                }
                .buttonStyle(.bordered)
                .help(L10n.t("route_hint"))
                Text(L10n.t("route_hint")).font(.caption).foregroundStyle(.secondary)
                Button(L10n.t("route_check")) { Task { await checkRoutes() } }
                    .disabled(checkingRoutes)
            }
            Section(L10n.t("section_startup")) {
                Toggle(L10n.t("launch_at_login"), isOn: $launchAtLogin)
            }
            Section(L10n.t("section_notifications")) {
                Toggle(L10n.t("low_quota_notifications"), isOn: $lowQuotaNotifications)
                    .help(L10n.t("low_quota_hint"))
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
            // v1.1 feature components (Settings schema v3). All six default on; a v2
            // file decodes to all-on, so upgrading never silently hides a feature.
            Section(L10n.t("section_components")) {
                componentToggle("comp_full_delta_row", desc: "comp_full_delta_row_desc", isOn: $components.fullDeltaRow)
                componentToggle("comp_top_ranks", desc: "comp_top_ranks_desc", isOn: $components.topRanks)
                // Rows per top-ranks list (contract §Top ranks): one shared count for tools
                // and models, 3..8; the flyout grows to fit automatically.
                Slider(value: Binding(get: { Double(rankRows) }, set: { rankRows = Int($0) }), in: 3...8, step: 1) {
                    Text(L10n.t("rank_rows", rankRows))
                }
                componentToggle("comp_reset_credits", desc: "comp_reset_credits_desc", isOn: $components.resetCredits)
                componentToggle("comp_activity_glance", desc: "comp_activity_glance_desc", isOn: $components.activityGlance)
                componentToggle("comp_histogram_today_week", desc: "comp_histogram_today_week_desc", isOn: $components.activityHistogramTodayWeek)
                componentToggle("comp_per_server_rows", desc: "comp_per_server_rows_desc", isOn: $components.perServerRows)
            }
            Section(L10n.t("section_updates")) {
                Text(L10n.t("update_current_version", CompanionStore.currentVersion))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle(L10n.t("update_auto_check"), isOn: $automaticUpdateChecks)
                    .help(L10n.t("update_auto_check_hint"))
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

                // E7: the SERVER's own runtime version and update badge, read on every
                // Settings open (GET /api/version, then /api/update-check only when the
                // server says update checks are enabled). Both are Settings-only reads,
                // both fail silently, and the companion never POSTs.
                if let runtime = store.serverRuntimeVersion {
                    HStack {
                        Text(L10n.t("server_runtime_row"))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text(L10n.t("server_runtime_version", runtime))
                            .font(.caption)
                            .monospacedDigit()
                    }
                }
                if let badge = store.serverUpdateBadgeVersion {
                    Label(L10n.t("server_update_available", badge), systemImage: "arrow.down.circle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Section(L10n.t("section_language")) {
                Picker(L10n.t("section_language"), selection: $language) {
                    ForEach(AppLanguage.allCases, id: \.self) { lang in
                        Text(lang.displayName).tag(lang)
                    }
                }
                .help(L10n.t("language_hint"))
            }
        }
        .formStyle(.grouped)
        .toggleStyle(BlueSettingsSwitch())
        .tint(.blue)
        .accentColor(.blue)
        .background(SettingsWindowSize())
        .scrollContentBackground(.hidden)
        .padding(20)
        // A grouped Form is a ScrollView: with only a fixed width its ideal height is
        // tiny, which is what made the window open small and (without
        // .windowResizability) impossible to drag larger. This gives a real default and
        // a draggable range; the Form scrolls when the window is short.
        .frame(minWidth: 480, idealWidth: 480, maxWidth: 680,
               minHeight: 420, idealHeight: CompanionLayout.settingsIdealHeight, maxHeight: .infinity)
        // Match the clean white content surface used by standard settings windows in
        // light mode while retaining a readable system-managed surface in dark mode.
        .background(Color(nsColor: .textBackgroundColor).ignoresSafeArea())
        .onAppear {
            loadSettings()
            store.fetchServerUpdateInfo()
        }
        .onChange(of: launchAtLogin) { _, _ in saveSettings() }
        .onChange(of: components) { _, _ in
            saveSettings()
            store.applyComponentsChange() // toggled components appear without a refetch
        }
        .onChange(of: rankRows) { _, _ in
            saveSettings()
            store.applyComponentsChange() // both rank lists re-slice without a refetch
        }
        .onChange(of: lowQuotaNotifications) { _, _ in saveSettings() }
        .onChange(of: fiveHourThreshold) { _, _ in saveSettings() }
        .onChange(of: weeklyThreshold) { _, _ in saveSettings() }
        .onChange(of: otherThreshold) { _, _ in saveSettings() }
        .onChange(of: language) { _, _ in saveSettings() }
        .onChange(of: automaticUpdateChecks) { _, _ in saveSettings() }
        .task {
            while !Task.isCancelled {
                await checkRoutes()
                try? await Task.sleep(for: .seconds(30))
            }
        }
        .sheet(isPresented: $addingAddress) {
            VStack(alignment: .leading, spacing: 14) {
                Text(L10n.t("route_add")).font(.headline)
                Text(L10n.t("route_add_hint")).font(.caption).foregroundStyle(.secondary)
                TextField(L10n.t("route_address"), text: $addressInput).textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await addAddress() } }
                if !addressError.isEmpty { Text(addressError).font(.caption).foregroundStyle(.red) }
                HStack {
                    Spacer()
                    Button(L10n.t("cancel")) { addingAddress = false }.disabled(checkingAddress)
                    Button(L10n.t("route_add")) { Task { await addAddress() } }
                        .disabled(checkingAddress || addressInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }.padding(24).frame(width: 420)
        }
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

    /// One Components toggle with its caption line (mock: title + short gray note).
    private func componentToggle(_ titleKey: String, desc: String, isOn: Binding<Bool>) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Toggle(L10n.t(titleKey), isOn: isOn)
            Text(L10n.t(desc))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func serverCard(_ server: Binding<CompanionServerSettings>) -> some View {
        let id = server.wrappedValue.id
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Toggle("", isOn: Binding(get: { server.wrappedValue.enabled }, set: { value in
                    if value || servers.filter(\.enabled).count > 1 { server.wrappedValue.enabled = value; saveSettings() }
                }))
                    .labelsHidden().frame(width: 36)
                    .accessibilityLabel(L10n.t("enable_server", server.wrappedValue.label))
                Button { editingAddress = nil; addressHostID = id; addressInput = ""; addressError = ""; addingAddress = true } label: {
                    Image(systemName: "plus")
                }.buttonStyle(.borderless).help(L10n.t("route_add"))
                    .accessibilityLabel(L10n.t("route_add"))
                TextField(L10n.t("server_name_placeholder"), text: server.label)
                    .labelsHidden().textFieldStyle(.plain).fontWeight(.semibold)
                    .multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
            }
            ForEach(server.wrappedValue.addresses, id: \.self) { address in
                routeRow(server, address: address)
            }
            Picker("", selection: Binding(get: { server.wrappedValue.preferredRoute ?? "" }, set: {
                server.wrappedValue.preferredRoute = $0.isEmpty ? nil : $0; saveSettings()
            })) {
                Text(L10n.t("route_auto")).tag("")
                ForEach(server.wrappedValue.addresses, id: \.self) { Text($0).tag($0) }
            }.labelsHidden().accessibilityLabel(L10n.t("route_pin"))
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color(nsColor: .separatorColor).opacity(0.45)))
        .onChange(of: server.wrappedValue.label) { _, _ in scheduleServerSave(id) }
    }

    private func routeRow(_ server: Binding<CompanionServerSettings>, address: String) -> some View {
        let good = TokdashClient.selectRoutes(server.wrappedValue, probes: server.wrappedValue.addresses.compactMap { routeProbes[$0] })
        let probe = good.first { $0.address == address }
        let active = good.first { $0.address == server.wrappedValue.preferredRoute }
            ?? good.min { $0.milliseconds < $1.milliseconds }
        return HStack(spacing: 8) {
            Circle().fill(probe?.health != nil ? Color.green : Color.secondary.opacity(0.5)).frame(width: 6, height: 6)
            Button { addressHostID = server.wrappedValue.id; editingAddress = address; addressInput = address; addressError = ""; addingAddress = true } label: {
                Text(address).lineLimit(1).truncationMode(.middle).frame(maxWidth: .infinity, alignment: .leading)
            }.buttonStyle(.plain).help(address)
            if let probe, probe.health != nil {
                Text("\(max(1, Int(probe.milliseconds))) ms").monospacedDigit().foregroundStyle(.secondary)
                if active?.address == address { Text(L10n.t("route_active")).foregroundStyle(.blue) }
            } else { Text(L10n.t(checkingRoutes && routeProbes[address] == nil ? "testing" : "route_offline")).foregroundStyle(.secondary) }
            Button {
                if server.wrappedValue.addresses.count == 1 { requestRemoval(of: server.wrappedValue) }
                else {
                    let remaining = server.wrappedValue.addresses.filter { $0 != address }
                    server.wrappedValue.baseURL = remaining[0]; server.wrappedValue.routes = Array(remaining.dropFirst())
                    if server.wrappedValue.preferredRoute == address { server.wrappedValue.preferredRoute = nil }
                    saveSettings()
                }
            } label: { Image(systemName: "xmark").font(.system(size: 10)) }
                .buttonStyle(.borderless).help(L10n.t("route_remove"))
                .accessibilityLabel(L10n.t("route_remove"))
                .disabled(servers.count == 1 && server.wrappedValue.addresses.count == 1)
        }.font(.caption).padding(.leading, 4)
    }

    private func checkRoutes() async {
        guard !checkingRoutes else { return }
        checkingRoutes = true
        defer { checkingRoutes = false }
        let snapshot = servers
        let addresses = snapshot.flatMap(\.addresses)
        let results = await withTaskGroup(of: RouteProbe.self) { group in
            for address in Set(addresses) { group.addTask { await TokdashClient.probe(address) } }
            var probes: [RouteProbe] = []
            for await probe in group { probes.append(probe) }
            return probes
        }
        guard !Task.isCancelled, snapshot == servers else { return }
        routeProbes = Dictionary(uniqueKeysWithValues: results.map { ($0.address, $0) })
        var merged: [CompanionServerSettings] = []
        for var server in snapshot {
            let identity = server.instanceId ?? routeProbes[server.addresses.first ?? ""]?.health?.instanceId
            if let identity, !identity.isEmpty {
                server.instanceId = identity
                let verified = server.addresses.contains { routeProbes[$0]?.health?.instanceId == identity }
                if verified, let index = merged.firstIndex(where: { other in
                    other.instanceId == identity && other.addresses.contains { routeProbes[$0]?.health?.instanceId == identity }
                }) {
                    merged[index].routes = Array(Set(merged[index].routes + server.addresses)).sorted()
                    merged[index].enabled = merged[index].enabled || server.enabled
                    continue
                }
            }
            merged.append(server)
        }
        if merged != servers { servers = merged; saveSettings() }
    }

    private func addAddress() async {
        guard !checkingAddress else { return }
        checkingAddress = true
        defer { checkingAddress = false }
        let input = addressInput.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let candidates = input.contains("://") ? [input] : ["https://" + input, "http://" + input]
        var found: RouteProbe?
        for candidate in candidates where CompanionStore.isValidBaseURL(candidate) {
            let probe = await TokdashClient.probe(candidate)
            if probe.health != nil { found = probe; break }
        }
        // Explicit URLs may be registered while offline. They cannot become a fallback
        // until a later health response proves the saved host identity.
        if found == nil, input.contains("://"), CompanionStore.isValidBaseURL(input) {
            found = RouteProbe(address: input, health: nil, milliseconds: 0)
        }
        guard let probe = found else { addressError = L10n.t("test_unreachable"); return }
        await checkRoutes()
        let identity = probe.health?.instanceId
        let target = addressHostID.flatMap { id in servers.firstIndex { $0.id == id } }
            ?? identity.flatMap { value in servers.firstIndex { $0.instanceId == value && !value.isEmpty } }
        if let index = target {
            if probe.health != nil {
                // A concurrent periodic check may still be running. Establish the target's
                // identity directly before appending, rather than racing that check.
                if servers[index].instanceId == nil {
                    let original = await TokdashClient.probe(servers[index].baseURL)
                    guard index < servers.count, servers[index].id == addressHostID else { return }
                    servers[index].instanceId = original.health?.instanceId
                }
                let replacingLegacy = editingAddress != nil && servers[index].addresses.count == 1
                    && identity == nil && servers[index].instanceId == nil
                guard replacingLegacy || (identity != nil && identity?.isEmpty == false && identity == servers[index].instanceId) else {
                    addressError = L10n.t("route_mismatch"); return
                }
            }
            var addresses = servers[index].addresses.filter { $0 != editingAddress }
            if !addresses.contains(probe.address) { addresses.append(probe.address) }
            servers[index].baseURL = addresses[0]; servers[index].routes = Array(addresses.dropFirst())
            if let editingAddress, servers[index].preferredRoute == editingAddress {
                servers[index].preferredRoute = probe.address
            }
        } else {
            if servers.contains(where: { $0.addresses.contains(probe.address) }) { addingAddress = false; return }
            var server = CompanionServerSettings.make(baseURL: probe.address)
            server.instanceId = identity; servers.append(server)
        }
        routeProbes[probe.address] = probe
        editingAddress = nil; addingAddress = false; saveSettings()
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

    private func loadSettings() {
        servers = store.settings.servers
        launchAtLogin = store.settings.launchAtLogin
        lowQuotaNotifications = store.settings.lowQuotaNotifications
        fiveHourThreshold = store.settings.thresholds.fiveHour
        weeklyThreshold = store.settings.thresholds.weekly
        otherThreshold = store.settings.thresholds.other
        language = store.settings.language
        automaticUpdateChecks = store.settings.automaticUpdateChecks
        components = store.settings.components
        rankRows = store.settings.rankRows
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
        let urlChanged = validServers != store.settings.servers
        let launchChanged = launchAtLogin != store.settings.launchAtLogin

        // Only persist the URL when it's a valid absolute http/https URL.
        store.settings.servers = validServers
        store.settings.lowQuotaNotifications = lowQuotaNotifications
        store.settings.components = components
        store.settings.rankRows = rankRows
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
        serverSaveTasks[id]?.cancel()
        serverSaveTasks[id] = nil
        saveSettings()
    }
}

/// AppKit desaturates native switches in inactive windows even with an explicit tint.
/// Draw the track directly; expose the same native Toggle accessibility semantics.
private struct BlueSettingsSwitch: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 0) {
            configuration.label
            Spacer(minLength: 0)
            Button { configuration.isOn.toggle() } label: {
                ZStack(alignment: configuration.isOn ? .trailing : .leading) {
                    Capsule().fill(configuration.isOn
                        ? Color(red: 0, green: 0.478, blue: 1)
                        : Color(nsColor: .tertiaryLabelColor).opacity(0.4))
                    Circle().fill(.white).shadow(color: .black.opacity(0.12), radius: 1, y: 1)
                        .padding(2).frame(width: 20, height: 20)
                }.frame(width: 36, height: 20).contentShape(Capsule())
            }
            .buttonStyle(.plain)
        }
        .accessibilityRepresentation {
            Toggle(isOn: configuration.$isOn) { configuration.label }.toggleStyle(.switch)
        }
    }
}
