import AppKit
import Foundation
import SwiftUI
import ServiceManagement
@preconcurrency import UserNotifications

private enum MultiServerAttempt: Sendable {
    /// One server's fetch group for the selected period: usage, optional active-time
    /// (nil = the endpoint failed/404 this cycle), and quota. The glance sources are not
    /// fetched per-server in v1.1 (no combined multi-server face exists in the contract).
    case success(CompanionServerSettings, usage: UsageResponse, activeMs: Int?, quota: QuotaResponse)
    case failure(CompanionServerSettings, busy: Bool, wrongService: Bool)
}

/// Companion store: holds connection state, the decoded snapshot, the refresh
/// scheduler, and settings. All mutations happen on the main actor.
@MainActor
final class CompanionStore: NSObject, ObservableObject {
    @Published private(set) var connectionState: ConnectionState = .connecting
    @Published private(set) var snapshot: Snapshot? = nil
    @Published private(set) var lastError: String? = nil
    @Published var settings: CompanionSettings
    @Published var quotaView: QuotaView = .low

    private let client: TokdashClient
    private var refreshTask: Task<Void, Never>?
    private var lastFetchAt: Date?
    // Data generation time from the API (Today.timestamp), used for freshness.
    private var lastDataTime: Date?
    private var failedServerLabels: [String] = []
    private var failedServerIDs = Set<String>()
    private var serverFailureCounts: [String: Int] = [:]

    // Last-good per section, retained across refreshes for partial-state rendering.
    // The usage-side last-goods belong to the *selected period* and are cleared the
    // moment the segment changes (contract rule 2: while the new window is in flight
    // the hero/delta/ranks/glance show skeletons; quota stays exactly as it was).
    private var lastUsage: UsageResponse?
    private var lastActiveMs: Int?
    private var lastInsights: InsightsResponse?
    private var lastStats: StatsResponse?
    private var lastPerServer: [PerServerUsage] = []
    private var lastQuota: QuotaResponse?

    // Refresh scheduler: 60s while open, 10min while closed, backoff on failure,
    // 15s short retry while a section is in partial failure.
    private var failures = 0
    private var partial = false
    // @Published: the open/closed window also decides freshnessText's "· stale" suffix,
    // so the transition must re-render the footer.
    @Published private var isOpen = false
    private var scheduler: Timer?
    // Low-quota notification dedup + crossing detection.
    private var notifiedKeys = Set<String>()
    private var prevQuotaLeft: [String: Double] = [:]

    // Update checking. `updateStatus` drives only the Settings status line; the gear badge
    // reads `updateAvailableVersion` (persisted) so it survives a relaunch and can't be
    // cleared by a later checking/failed state.
    @Published private(set) var updateStatus: UpdateStatus = .idle
    private let releases = GitHubReleasesClient()
    private var updateTask: Task<Void, Never>?
    private var updateCheckInFlight = false
    // Supersedes an older in-flight check rather than letting both write state back.
    private var updateCheckGeneration = 0

    override init() {
        var loaded = CompanionSettings.load()
        // Repair a blank/malformed base URL saved by an earlier build so the client can't
        // point at nothing, and persist the fix so it isn't re-applied every launch.
        if !Self.isValidBaseURL(loaded.baseURL) {
            loaded.baseURL = CompanionSettings.defaultBaseURL
            loaded.save()
        }
        let url = URL(string: loaded.baseURL) ?? URL(string: CompanionSettings.defaultBaseURL)!
        // Resolve the display language before the first view render so the launch state is in
        // the right language (the store owns this so a later change can republish and re-render).
        L10n.current = L10n.resolve(loaded.language)
        self.settings = loaded
        self.client = TokdashClient(baseURL: url)
        super.init()
        restorePendingUpdate()
    }

    /// Re-publish a previously-found update at launch. The 24h throttle means the next
    /// check can be most of a day away, and the spec requires the badge to persist until
    /// the app is updated or the version is skipped - so it has to come back from disk,
    /// not from the next network round-trip.
    private func restorePendingUpdate() {
        guard let version = updateAvailableVersion, let url = settings.availableUpdateURL else { return }
        updateStatus = .available(version: version, url: url)
    }

    /// Apply a new language setting: update the global ``L10n.current``, persist, and republish
    /// so every view reading a localized string re-renders live (no restart).
    func applyLanguage(_ setting: AppLanguage) {
        L10n.current = L10n.resolve(setting)
        settings.language = setting  // @Published -> objectWillChange, re-renders views
        settings.save()
    }

    /// Short name for the configured server, shown beside the connection state.
    /// Loopback reads "Local"; anything else uses the host's first DNS label, so a
    /// Tailscale URL like https://wsl.tail76535.ts.net/tokdash reads "wsl" rather than
    /// claiming to be local. Bare IPs are shown as-is (no meaningful label to extract).
    nonisolated static func serverLabel(for urlString: String) -> String {
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let host = URL(string: trimmed)?.host?.lowercased(), !host.isEmpty else { return L10n.t("local") }
        if host == "localhost" || host == "127.0.0.1" || host == "::1" { return L10n.t("local") }
        // An IPv4/IPv6 literal has no name to shorten; splitting it would be misleading.
        if host.allSatisfy({ $0.isNumber || $0 == "." }) || host.contains(":") { return host }
        let first = host.split(separator: ".").first.map(String.init) ?? host
        return first.isEmpty ? host : first
    }

    var serverLabel: String {
        let count = settings.servers.filter(\.enabled).count
        return count > 1 ? L10n.t("servers_count", count) : Self.serverLabel(for: settings.baseURL)
    }

    /// Connection state for display. Only the connected state is prefixed with the
    /// server label; the failure states are about reachability, not which host.
    var connectionLabel: String {
        connectionState == .connected ? L10n.t("server_connected", serverLabel) : connectionState.label
    }

    /// True when a base URL is usable: an absolute http/https URL with a host. Every
    /// write path (settings save, updateBaseURL) validates with this so a bad value can
    /// never be persisted and re-applied on the next launch. Mirrors Windows IsValidBaseURL.
    nonisolated static func isValidBaseURL(_ s: String) -> Bool {
        guard let url = URL(string: s.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty else { return false }
        return true
    }

    /// Rebuild the client with a new base URL (called when settings change). Only
    /// accepts an absolute http/https URL; otherwise the current client is kept.
    func updateBaseURL(_ urlString: String) {
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.isValidBaseURL(trimmed), let url = URL(string: trimmed) else { return }
        Task {
            await client.updateBaseURL(url)
            refresh()
        }
    }

    // MARK: - Period segment

    /// The hero segment control's selection (persisted as `selectedPeriod`, default
    /// today). Views read `settings.selectedPeriod` and change it only via
    /// ``selectPeriod(_:)``.
    var selectedPeriod: UsagePeriod { settings.selectedPeriod }

    /// Generation counter for the delayed skeleton in ``selectPeriod(_:)``: a superseded
    /// switch's pending timer must never collapse the newer switch's sections.
    private var usageGen = 0

    /// Select a hero period. The choice persists, and selecting a different segment
    /// fires the whole fetch group for the new window immediately. While it is in
    /// flight the previous period's data stays on screen (anti-flash); only if the fetch
    /// is still in flight after ~150 ms do the hero/delta/rank blocks and the glance
    /// drop to their loading skeleton (the usage-side last-good is then dropped). Quota
    /// and connectivity stay exactly as they were (contract rule 2, delayed-skeleton
    /// clause).
    func selectPeriod(_ period: UsagePeriod) {
        guard settings.selectedPeriod != period else { return }
        settings.selectedPeriod = period
        settings.save()
        lastUsage = nil
        lastActiveMs = nil
        lastInsights = nil
        lastStats = nil
        lastPerServer = []
        // Delayed skeleton: the current snapshot keeps the previous period's data while
        // the new period is in flight, so a fast fetch never visibly collapses the
        // sections. The skeleton goes up only if the fetch is STILL in flight after
        // 150 ms - the guard below re-reads the snapshot once the delay elapses.
        usageGen += 1
        let gen = usageGen
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 150_000_000)
            guard let self, gen == self.usageGen else { return }
            // Once the new period's result has published - success OR failure - the fetch
            // is done as far as the UI is concerned. Only a snapshot still stamped with
            // the PREVIOUS period means the fetch is truly in flight: show the skeleton.
            guard let current = self.snapshot, current.period != period else { return }
            self.snapshot = Snapshot(period: period, usage: nil, activeMs: nil,
                                     insights: nil, stats: nil,
                                     quota: current.quota, thresholds: current.thresholds,
                                     components: self.settings.components, now: Self.now,
                                     usageFailed: false, quotaFailed: current.quotaFailed,
                                     perServer: [], showPerServerRows: current.showPerServerRows)
        }
        refresh()
    }

    // MARK: - Server diagnostics (Settings only)

    /// The server's `runtime_version`, read on Settings open. nil until fetched.
    @Published private(set) var serverRuntimeVersion: String?
    /// The version for the "Server update available: v{latest}" row; nil renders nothing.
    @Published private(set) var serverUpdateBadgeVersion: String?
    private var serverDiagnosticsTask: Task<Void, Never>?

    /// Settings-open probe: `GET /api/version`, then - only when the server itself has
    /// update-check enabled - `GET /api/update-check`. Failures are silent, nothing is
    /// ever POSTed, and this never runs in the flyout or on a schedule.
    func fetchServerUpdateInfo() {
        serverDiagnosticsTask?.cancel()
        serverDiagnosticsTask = Task { [weak self] in
            guard let self else { return }
            do {
                let version = try await self.client.serverVersion()
                guard !Task.isCancelled else { return }
                self.serverRuntimeVersion = version.runtimeVersion
                self.serverUpdateBadgeVersion = nil
                guard version.updateCheckEnabled == true else { return }
                let check = try await self.client.serverUpdateCheck()
                guard !Task.isCancelled else { return }
                self.serverUpdateBadgeVersion = Self.serverBadgeVersion(from: check)
            } catch {
                // Silent: the badge is a courtesy, not a feature the user can act on here.
            }
        }
    }

    /// Badge rule: `enabled && update_available && latest != null`. `enabled == false`
    /// means the server's owner has not given update-check consent: render nothing and
    /// never try to change that. Pure so the gate is unit-testable without a client.
    nonisolated static func serverBadgeVersion(from check: ServerUpdateCheckResponse) -> String? {
        guard check.enabled == true, check.updateAvailable == true,
              let latest = check.latest, !latest.isEmpty else { return nil }
        return latest
    }

    // MARK: - Refresh

    /// Rebuild the current snapshot from last-good data with the (possibly new)
    /// thresholds, so the Low view re-evaluates immediately without a network refresh.
    func applyThresholds() {
        guard let q = lastQuota, q.enabled else { return }
        var snap = Snapshot(period: settings.selectedPeriod, usage: lastUsage, activeMs: lastActiveMs,
                            insights: lastInsights, stats: lastStats, quota: q,
                            thresholds: settings.thresholds, components: settings.components,
                            now: Self.now,
                            perServer: lastPerServer, showPerServerRows: showPerServerRows)
        if let cur = snapshot {
            snap.usageFailed = cur.usageFailed
            snap.quotaFailed = cur.quotaFailed
        }
        snapshot = snap
    }

    /// Rebuild after a Components toggle change. Cannot share applyThresholds'
    /// quota-enabled guard: the hero components gate even when quota tracking is off.
    func applyComponentsChange() {
        snapshot = Snapshot(period: settings.selectedPeriod, usage: lastUsage, activeMs: lastActiveMs,
                            insights: lastInsights, stats: lastStats,
                            quota: lastQuota ?? .empty, thresholds: settings.thresholds,
                            components: settings.components, now: Self.now,
                            usageFailed: snapshot?.usageFailed ?? false,
                            quotaFailed: snapshot?.quotaFailed ?? false,
                            perServer: lastPerServer, showPerServerRows: showPerServerRows)
    }

    /// Manual / immediate refresh. Cancels any in-flight refresh and reschedules
    /// after it completes (so a failure applies backoff rather than the old timer).
    /// A cancelled (superseded) task does not reschedule, so it can't clobber its
    /// replacement's timer.
    func refresh() {
        refreshTask?.cancel()
        refreshTask = Task { await runRefresh(); guard !Task.isCancelled else { return }; reschedule() }
    }

    private func runRefresh() async {
        let enabledServers = settings.servers.filter(\.enabled)
        if enabledServers.count > 1 {
            await runMultiServerRefresh(enabledServers)
            return
        }
        failedServerLabels = []
        failedServerIDs = []
        serverFailureCounts = [:]
        do {
            let health = try await client.health()
            guard health.service == "tokdash" else {
                // Wrong service: back off so an open flyout doesn't tight-loop the address.
                failures += 1
                partial = false
                connectionState = .wrongService
                return
            }
            connectionState = .connected

            // Fetch each section independently so one failed request no longer
            // discards the others, for the SELECTED period (contract rule 2). Active-time
            // and the glance source are optional decorations: their fetch helpers swallow
            // every failure into nil and never warn (rule 6). A component whose toggle is
            // off does not fetch its source at all.
            let period = settings.selectedPeriod
            let glanceSource = Self.glanceSource(for: period, components: settings.components,
                                                 today: Date(), calendar: .current)
            async let usageAttempt = Self.fetchUsage(client, period: period)
            async let quotaAttempt = client.quota()
            async let activeAttempt = Self.activeTimeOptional(client, period: period)
            async let glanceAttempt = Self.fetchGlance(client, source: glanceSource)

            var usageFailed = false, usageBusy = false, quotaFailed = false, quotaBusy = false
            do { lastUsage = try await usageAttempt } catch let e as TokdashError { usageFailed = true; if case .busy = e { usageBusy = true } } catch { usageFailed = true }
            do { lastQuota = try await quotaAttempt } catch let e as TokdashError { quotaFailed = true; if case .busy = e { quotaBusy = true } } catch { quotaFailed = true }
            lastActiveMs = await activeAttempt
            let glance = await glanceAttempt
            lastInsights = glance.insights
            lastStats = glance.stats

            if Task.isCancelled { return }

            let snap = rebuildSnapshot(usageFailed: usageFailed, quotaFailed: quotaFailed)
            self.lastError = nil
            self.connectionState = .connected

            let allFailed = usageFailed && quotaFailed
            if allFailed {
                // Health ok but both real endpoints failed. If both were 503, the service
                // is busy: show the Busy banner + dimmed last-good, not Connected.
                if usageBusy && quotaBusy { connectionState = .busy }
                failures += 1
                partial = false
            } else {
                lastFetchAt = Date()
                // Data time: prefer the API timestamp (naive UTC parsed via parseTimestamp),
                // else fall back to fetch time minus the cache age, else fetch time. Spec §freshness.
                if let ts = lastUsage?.timestamp,
                   let parsed = Self.parseTimestamp(ts) {
                    lastDataTime = parsed
                } else if let age = lastUsage?.responseCache?.ageSeconds {
                    lastDataTime = (lastFetchAt ?? Date()).addingTimeInterval(-age)
                } else {
                    lastDataTime = lastFetchAt
                }
                failures = 0
                partial = usageFailed || quotaFailed // partial -> 15s short retry
                let fresh = evaluateLowQuotaNotifications(snap)
                if !fresh.isEmpty { postLowQuotaNotification(fresh) }
                postCreditExpiryNotificationsIfNeeded(snap)
            }
        } catch let error as TokdashError {
            if Task.isCancelled { return }
            failures += 1
            partial = false
            applyError(error)
        } catch {
            if Task.isCancelled { return }
            failures += 1
            partial = false
            applyError(.other(error))
        }
    }

    // MARK: - Per-period fetch helpers

    /// Usage for the selected period. Week uses `date_from`/`date_to` (local Monday ..
    /// today); `period=week` is a rolling 7-day window and is never sent.
    nonisolated static func usageRequestPath(for period: UsagePeriod, today: Date, calendar: Calendar) -> String {
        guard period == .week else { return "/api/usage?period=\(period.token)" }
        let (from, to) = weekRange(today: today, calendar: calendar)
        return "/api/usage?date_from=\(from)&date_to=\(to)"
    }

    private nonisolated static func fetchUsage(_ client: TokdashClient, period: UsagePeriod) async throws -> UsageResponse {
        if period == .week {
            let (from, to) = weekRange(today: Date(), calendar: Calendar.current)
            return try await client.usageRange(from: from, to: to)
        }
        return try await client.usage(period: period.token)
    }

    /// Active-time is optional (rule 6): any failure/404 is nil, silently.
    private nonisolated static func activeTimeOptional(_ client: TokdashClient, period: UsagePeriod) async -> Int? {
        do {
            let response: ActiveTimeResponse
            if period == .week {
                let (from, to) = weekRange(today: Date(), calendar: Calendar.current)
                response = try await client.activeTimeRange(from: from, to: to)
            } else {
                response = try await client.activeTime(period: period.token)
            }
            return response.activeMs
        } catch {
            return nil
        }
    }

    /// The glance's single source for the period, or nil when no face will render.
    /// Component-off means no request at all (glance-off / histogram-off cycles contain
    /// no /api/insights or /api/stats request). Failure yields nil silently (rule 6).
    enum GlanceSource: Equatable, Sendable {
        case insightsHourly
        case insightsDaily
        case stats

        /// Endpoint path the source hits - used by the contract tests' requests_absent rule.
        var requestPath: String {
            switch self {
            case .insightsHourly, .insightsDaily: return "/api/insights"
            case .stats: return "/api/stats"
            }
        }
    }

    nonisolated static func glanceSource(for period: UsagePeriod, components: CompanionComponents,
                                         today: Date, calendar: Calendar) -> GlanceSource? {
        guard components.activityGlance else { return nil }
        switch period {
        case .today: return components.activityHistogramTodayWeek ? .insightsHourly : nil
        case .week: return components.activityHistogramTodayWeek ? .insightsDaily : nil
        case .month, .year: return .stats
        }
    }

    private nonisolated static func fetchGlance(_ client: TokdashClient, source: GlanceSource?) async
        -> (insights: InsightsResponse?, stats: StatsResponse?) {
        guard let source else { return (nil, nil) }
        do {
            switch source {
            case .insightsHourly:
                return (try await client.insightsHourlyToday(), nil)
            case .insightsDaily:
                let (from, to) = weekRange(today: Date(), calendar: Calendar.current)
                return (try await client.insightsDaily(from: from, to: to), nil)
            case .stats:
                return (nil, try await client.stats())
            }
        } catch {
            return (nil, nil)
        }
    }

    /// Rebuild and publish the snapshot from the stored last-goods. Also returns it so
    /// callers can evaluate notifications on the exact snapshot they just published.
    @discardableResult
    private func rebuildSnapshot(usageFailed: Bool, quotaFailed: Bool) -> Snapshot {
        let snap = Snapshot(period: settings.selectedPeriod, usage: lastUsage, activeMs: lastActiveMs,
                            insights: lastInsights, stats: lastStats,
                            quota: lastQuota ?? .empty, thresholds: settings.thresholds,
                            components: settings.components, now: Self.now,
                            usageFailed: usageFailed, quotaFailed: quotaFailed,
                            perServer: lastPerServer, showPerServerRows: showPerServerRows)
        snapshot = snap
        return snap
    }

    /// Per-server rows render only with the component on and more than one enabled
    /// server (one server would only echo the hero).
    var showPerServerRows: Bool {
        settings.components.perServerRows && settings.servers.filter(\.enabled).count > 1
    }

    private func runMultiServerRefresh(_ servers: [CompanionServerSettings]) async {
        typealias ServerResult = (server: CompanionServerSettings, usage: UsageResponse, activeMs: Int?, quota: QuotaResponse)
        let period = settings.selectedPeriod
        let attempts: [MultiServerAttempt] = await withTaskGroup(of: MultiServerAttempt.self) { group in
            for server in servers {
                group.addTask {
                    guard let url = URL(string: server.baseURL) else { return .failure(server, busy: false, wrongService: false) }
                    let client = TokdashClient(baseURL: url)
                    do {
                        let health = try await client.health()
                        guard health.service == "tokdash" else { return .failure(server, busy: false, wrongService: true) }
                        async let usage = Self.fetchUsage(client, period: period)
                        async let quota = client.quota()
                        // Active time is an optional decoration (rule 6): a failed read
                        // yields nil and the combined hero drops the segment.
                        async let active = Self.activeTimeOptional(client, period: period)
                        let values = try await (usage, quota, active)
                        return .success(server, usage: values.0, activeMs: values.2, quota: values.1)
                    } catch let error as TokdashError {
                        if case .busy = error { return .failure(server, busy: true, wrongService: false) }
                        return .failure(server, busy: false, wrongService: false)
                    } catch {
                        return .failure(server, busy: false, wrongService: false)
                    }
                }
            }
            var values: [MultiServerAttempt] = []
            for await value in group { values.append(value) }
            return values
        }
        if Task.isCancelled { return }
        let results: [ServerResult] = attempts.compactMap { attempt in
            guard case let .success(server, usage, activeMs, quota) = attempt else { return nil }
            return (server, usage, activeMs, quota)
        }
        failedServerIDs = Set(attempts.compactMap { attempt in
            guard case let .failure(server, _, _) = attempt else { return nil }
            return server.id
        })
        failedServerLabels = servers.filter { failedServerIDs.contains($0.id) }.map(\.label)
        for server in servers {
            if failedServerIDs.contains(server.id) { serverFailureCounts[server.id, default: 0] += 1 }
            else { serverFailureCounts.removeValue(forKey: server.id) }
        }
        serverFailureCounts = serverFailureCounts.filter { key, _ in servers.contains(where: { $0.id == key }) }
        guard !results.isEmpty else {
            failures += 1
            partial = false
            if attempts.allSatisfy({ if case .failure(_, busy: true, wrongService: false) = $0 { return true }; return false }) {
                connectionState = .busy
                lastError = "Tokdash is busy"
            } else if attempts.allSatisfy({ if case .failure(_, busy: false, wrongService: true) = $0 { return true }; return false }) {
                connectionState = .wrongService
            } else {
                connectionState = .offline
            }
            return
        }
        let usage = Self.combineUsage(results.map(\.usage))
        // Merged display order must not follow task-group completion order: it is
        // settings order x wire order (pins: expected/multi-server.json
        // quota_all_server_order + §All view "provider order as detected").
        let quota = Self.mergedQuota(servers.compactMap { server in
            results.first { $0.server.id == server.id }.map { ($0.server.label, $0.quota) }
        })
        lastUsage = usage; lastQuota = quota
        lastInsights = nil; lastStats = nil
        // Every enabled server feeds the sum, not just the ones that answered: a server
        // missing from `results` (failed/unreachable) must make `failed` true so a known-
        // partial sum can never render (contract §Active time, "never a partial sum").
        lastActiveMs = Self.combinedActiveMs(servers.map { server in
            let result = results.first { $0.server.id == server.id }
            return (result?.activeMs, result == nil)
        })
        // Per-server rows in settings order, from this same fan-out - never an extra
        // request (contract §Per-server rows). Failed servers stay listed as "unreachable".
        lastPerServer = Self.perServerRows(servers: servers, results: results.map { ($0.server, $0.usage) },
                                           failedIDs: failedServerIDs)
        let snap = rebuildSnapshot(usageFailed: false, quotaFailed: false)
        connectionState = .connected; lastFetchAt = Date()
        lastDataTime = results.compactMap { result in
            result.usage.timestamp.flatMap(Self.parseTimestamp)
        }.min() ?? lastFetchAt
        failures = 0; partial = results.count != servers.count
        let fresh = evaluateLowQuotaNotifications(snap)
        if !fresh.isEmpty { postLowQuotaNotification(fresh) }
        postCreditExpiryNotificationsIfNeeded(snap)
    }

    /// Multi-server active-time sum (contract §Active time): sum `active_ms` across
    /// reachable servers, but if *any* enabled server lacks active-time data this cycle
    /// (failed endpoint or unreachable), drop the segment rather than present a
    /// known-partial sum. Pure so it is unit-testable.
    nonisolated static func combinedActiveMs(_ perServer: [(activeMs: Int?, failed: Bool)]) -> Int? {
        var total = 0
        for entry in perServer {
            if entry.failed { return nil }
            guard let ms = entry.activeMs else { return nil }
            total += ms
        }
        return total
    }

    /// Per-server rows in settings order; unreachable servers keep their row with the
    /// "unreachable" tag, never dimmed numbers. Pure/testable.
    nonisolated static func perServerRows(servers: [CompanionServerSettings],
                                          results: [(server: CompanionServerSettings, usage: UsageResponse)],
                                          failedIDs: Set<String>) -> [PerServerUsage] {
        servers.map { server in
            if failedIDs.contains(server.id) { return PerServerUsage(label: server.label, usage: nil) }
            guard let result = results.first(where: { $0.server.id == server.id }) else {
                return PerServerUsage(label: server.label, usage: nil)
            }
            return PerServerUsage(label: server.label, usage: result.usage)
        }
    }

    /// Merge per-server quota payloads into the display model: keys get the server
    /// label prefix, and the group order is settings order x wire order (pins:
    /// expected/multi-server.json quota_all_server_order + §All view "provider
    /// order as detected"; the COMPANION_API.md intro lists "server ordering" as
    /// one of that case file's pins).
    nonisolated static func mergedQuota(_ results: [(label: String, quota: QuotaResponse)]) -> QuotaResponse {
        var providers: [String: ProviderQuota] = [:]
        var order: [String] = []
        for result in results {
            let wire = result.quota.providerWireOrder ?? (result.quota.providers?.keys.sorted() ?? [])
            for provider in wire {
                guard let value = result.quota.providers?[provider] else { continue }
                let key = "\(result.label) · \(provider)"
                providers[key] = value
                // Duplicate labels ("Local" left on two servers) keep ONE group at
                // its first position with the last server's values - exactly what
                // the Windows Dictionary collapses to.
                if !order.contains(key) { order.append(key) }
            }
        }
        return QuotaResponse(enabled: results.contains(where: { $0.quota.enabled }),
                             providers: providers, timestamp: nil,
                             providerWireOrder: order)
    }

    nonisolated static func combineUsage(_ rows: [UsageResponse]) -> UsageResponse {
        var tools: [String: (tokens: Int, cost: Double)] = [:]
        var models: [String: (tokens: Int, cost: Double)] = [:]
        for row in rows {
            for (name, item) in row.byTool ?? [:] { let old = tools[name] ?? (0, 0); tools[name] = (old.tokens + item.tokens, old.cost + item.cost) }
            for item in row.combinedModels ?? row.topModels ?? [] { let old = models[item.name] ?? (0, 0); models[item.name] = (old.tokens + item.tokens, old.cost + item.cost) }
        }
        let totalCost = rows.reduce(0) { $0 + $1.totalCost }
        let totalTokens = rows.reduce(0) { $0 + $1.totalTokens }
        let totalMessages = rows.reduce(0) { $0 + $1.totalMessages }
        // Multi-server delta row: recompute each pct from summed current and previous
        // totals; omit a metric when any contributing server omits its *_prev
        // (contract §Full delta row).
        func combined(_ current: Double, _ prev: (Comparison) -> Double?) -> (prev: Double?, pct: Double?) {
            guard !rows.isEmpty else { return (nil, nil) }
            // Either absence shape drops the metric: the whole `comparison` object,
            // or just this metric's `*_prev` on any contributing server.
            var prevs: [Double] = []
            for row in rows {
                guard let comparison = row.comparison, let value = prev(comparison) else { return (nil, nil) }
                prevs.append(value)
            }
            let sum = prevs.reduce(0, +)
            return (sum, sum > 0 ? (current - sum) / sum * 100 : nil)
        }
        let cost = combined(totalCost) { $0.costPrev }
        let tokens = combined(Double(totalTokens)) { $0.tokensPrev }
        let messages = combined(Double(totalMessages)) { $0.messagesPrev }
        // Mirror the server's own shape: combinedModels is the full list ranked by
        // tokens, topModels its first five, topModelsByCost the five by cost. This
        // used to hand back one cost-sorted uncapped list under all three names.
        let merged = models.map { ModelAgg(name: $0.key, tokens: $0.value.tokens, cost: $0.value.cost) }
        let byTokens = merged.sorted {
            if $0.tokens != $1.tokens { return $0.tokens > $1.tokens }
            if $0.cost != $1.cost { return $0.cost > $1.cost }
            return $0.name < $1.name
        }
        let byCost = merged.sorted {
            if $0.cost != $1.cost { return $0.cost > $1.cost }
            if $0.tokens != $1.tokens { return $0.tokens > $1.tokens }
            return $0.name < $1.name
        }
        return UsageResponse(period: rows.first?.period ?? "", totalTokens: totalTokens, totalCost: totalCost,
            totalMessages: totalMessages, byTool: tools.mapValues { ToolAgg(tokens: $0.tokens, cost: $0.cost) },
            topModels: Array(byTokens.prefix(5)), topModelsByCost: Array(byCost.prefix(5)),
            combinedModels: byTokens,
            comparison: Comparison(tokensPct: tokens.pct, costPct: cost.pct, messagesPct: messages.pct,
                                   costPrev: cost.prev, tokensPrev: tokens.prev, messagesPrev: messages.prev),
            timestamp: rows.compactMap(\.timestamp).min())
    }

    private func applyError(_ error: TokdashError) {
        switch error {
        case .busy:
            connectionState = .busy
            lastError = "Tokdash is busy"
        case .offline, .timeout:
            connectionState = .offline
            lastError = "Tokdash is not reachable"
        case .httpStatus(let code):
            if code == 503 {
                connectionState = .busy
                lastError = "Tokdash is busy"
            } else {
                connectionState = .offline
                lastError = "HTTP \(code)"
            }
        default:
            connectionState = .offline
            lastError = "\(error)"
        }
    }

    // MARK: - Scheduler

    /// Begin the resident refresh scheduler. Called once at app launch.
    func startScheduler() {
        observeWake()
        refresh()
    }

    private var observesWake = false

    /// On wake, fire one coalesced refresh (refresh() cancels any in-flight request)
    /// so stale post-sleep data refreshes promptly. Periodic work was naturally paused
    /// while asleep - timers don't fire. Spec §cadence.
    private func observeWake() {
        guard !observesWake else { return }
        observesWake = true
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(didWake(_:)),
            name: NSWorkspace.didWakeNotification,
            object: nil
        )
    }

    @objc private func didWake(_ notification: Notification) {
        refresh()
    }

    /// Notify the scheduler the popover opened/closed (changes cadence).
    func setOpen(_ open: Bool) {
        isOpen = open
        reschedule()
    }

    private func reschedule() {
        scheduler?.invalidate()
        // Ride the existing refresh cadence instead of adding a second timer. This is
        // called far more often than daily, but shouldAutoCheck's 24h throttle is what
        // actually rate-limits the request, and it returns immediately when not due.
        checkForUpdates(manual: false)
        let now = Date()
        let enabled = settings.servers.filter(\.enabled)
        let delay = enabled.count > 1
            ? Self.minimumDelay(enabled.map { server in Self.computeDelay(open: isOpen, failures: serverFailureCounts[server.id, default: 0], partial: false, lastFetch: lastFetchAt, now: now) })
            : Self.computeDelay(open: isOpen, failures: failures, partial: partial, lastFetch: lastFetchAt, now: now)
        guard delay > 0 else { refresh(); return }
        scheduler = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in self.refresh() }
        }
    }

    /// Pure delay computation for the refresh scheduler. Backoff 15/30/60/300s after
    /// consecutive failures; 15s short retry while a section is partially failing;
    /// otherwise 60s while open (immediately if data is stale) and 10min while closed.
    nonisolated static func computeDelay(open: Bool, failures: Int, partial: Bool, lastFetch: Date?, now: Date) -> TimeInterval {
        if failures > 0 {
            let backoff = [15.0, 30.0, 60.0, 300.0]
            return backoff[min(failures - 1, backoff.count - 1)]
        }
        if partial { return 15 }
        if open {
            guard let last = lastFetch else { return 0 }
            let since = now.timeIntervalSince(last)
            return since >= 60 ? 0 : 60 - since
        }
        return 600
    }

    nonisolated static func minimumDelay(_ delays: [TimeInterval]) -> TimeInterval { delays.min() ?? 600 }

    /// Parse the API `timestamp`, which may be a full ISO 8601 string with offset/Z or
    /// a naive UTC datetime with fractional seconds and no timezone (e.g.
    /// "2026-07-28T17:57:43.500951"). ISO8601DateFormatter.withInternetDateTime rejects
    /// the naive form, so retry after appending "Z" (assume UTC). Spec §freshness.
    nonisolated static func parseTimestamp(_ s: String) -> Date? {
        let normalized = normalizeFractionalSeconds(s)
        let withFrac = ISO8601DateFormatter()
        withFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let noFrac = ISO8601DateFormatter()
        noFrac.formatOptions = [.withInternetDateTime]
        if let d = withFrac.date(from: normalized) { return d }
        if let d = noFrac.date(from: normalized) { return d }
        if let d = withFrac.date(from: normalized + "Z") { return d }
        if let d = noFrac.date(from: normalized + "Z") { return d }
        return nil
    }

    /// ISO8601DateFormatter.withFractionalSeconds only accepts exactly three fractional
    /// digits, but the server emits six ("…:43.500951"). Truncate/pad the fraction to
    /// three so the parse can't silently miss and fall back to the cache-age path.
    private nonisolated static func normalizeFractionalSeconds(_ s: String) -> String {
        guard let dot = s.firstIndex(of: ".") else { return s }
        let start = s.index(after: dot)
        let digits = s[start...].prefix(while: { $0.isASCII && $0.isNumber })
        guard !digits.isEmpty else { return s }
        let three = digits.count >= 3
            ? String(digits.prefix(3))
            : String(digits) + String(repeating: "0", count: 3 - digits.count)
        let rest = s[s.index(start, offsetBy: digits.count)...]
        return String(s[...dot]) + three + String(rest)
    }

    /// Clock seam for the reset-credits rules ("the clock for 'days remaining' is the
    /// client's own; tests freeze it to the payload timestamp"). Production always nil.
    /// Mirrors the ``CompanionSettings/pathOverride`` test-seam style.
    nonisolated(unsafe) static var clockOverride: Date?
    nonisolated static var now: Date { clockOverride ?? Date() }

    // MARK: - Low-quota notifications

    /// Notify only on a crossing from above to at-or-below the threshold, evaluated
    /// over ALL windows (not just the displayed top two). Dedup by
    /// (provider, account, bucket, reset epoch, threshold); a new reset epoch re-arms.
    /// Buckets without a reset time are suppressed (spec §7). Not called for offline/
    /// busy (only on a successful health check) or recovery (only above->below).
    /// Returns the freshly-crossed rows; the caller posts the notification (testable).
    internal func evaluateLowQuotaNotifications(_ snap: Snapshot) -> [QuotaRow] {
        guard settings.lowQuotaNotifications, snap.quota.enabled, !snap.quotaFailed else { return [] }
        var fresh: [QuotaRow] = []
        var current = Set<String>()
        for r in snap.allQuotaGroups.flatMap({ $0.rows }) {
            guard let resets = r.resetsAt else { continue } // suppress buckets without a reset time
            if r.failed { continue } // suppress rows whose own bucket failed (last-known, unreliable for alerts)
            let epoch = Int(resets.timeIntervalSince1970)
            let canonicalProvider = r.provider.components(separatedBy: " · ").last ?? r.provider
            let stateKey = "\(canonicalProvider)|\(r.account)|\(r.bucket)|\(epoch)"
            current.insert(stateKey)
            let threshold = settings.thresholds.threshold(for: r.canonicalBucket)
            let isLow = r.left <= threshold
            if isLow, let prev = prevQuotaLeft[stateKey], prev > threshold {
                let notifyKey = "\(stateKey)|\(threshold)"
                if notifiedKeys.insert(notifyKey).inserted { fresh.append(r) }
            }
            prevQuotaLeft[stateKey] = r.left
        }
        // Re-arm: drop state for windows no longer reported (reset epoch advanced / dropped).
        for k in prevQuotaLeft.keys where !current.contains(k) { prevQuotaLeft.removeValue(forKey: k) }
        return fresh
    }

    private func postLowQuotaNotification(_ rows: [QuotaRow]) {
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = L10n.t("notif_low_title")
            if rows.count == 1, let r = rows.first {
                content.body = L10n.t("notif_low_single", r.provider, r.displayBucketLabel, Int(r.left))
            } else if let r = rows.first {
                content.body = L10n.t("notif_low_multi", rows.count, r.provider, r.displayBucketLabel, Int(r.left))
            }
            content.userInfo = ["openQuota": true]
            let id = "tokdash-low-quota-\(Date().timeIntervalSince1970)"
            let req = UNNotificationRequest(identifier: id, content: content,
                                            trigger: UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false))
            center.add(req)
        }
    }

    // MARK: - Reset-credit expiry notifications

    /// One credit whose last 48 hours has begun (or will, given the frozen clock).
    struct CreditAlertItem: Sendable {
        let provider: String        // display name, e.g. "Codex"
        let count: Int              // provider's available_count
        let clause: String          // localized "in 2 d" / "tomorrow" / "today"
        let expiresAt: Date
    }

    /// Reset credits ride the SAME low-quota opt-in and the same scheduled quota read
    /// (no extra polling, rule 8). Notify once a future credit enters its last 48
    /// hours; dedup by (provider, credit id, expires_at) - a credit carries its own
    /// identity, so no re-arm rule is needed. Suppressed while the provider's group
    /// failed: last-known credit data is not a basis for an "expire in" warning.
    /// Requires the `resetCredits` component (it gates the row AND the notification).
    internal func evaluateResetCreditNotifications(_ snap: Snapshot) -> [CreditAlertItem] {
        guard settings.lowQuotaNotifications, snap.components.resetCredits, snap.quota.enabled else { return [] }
        var fresh: [CreditAlertItem] = []
        for group in snap.allQuotaGroups {
            guard let prov = group.providerEntry, !group.failed,
                  let credits = prov.resetCredits, (credits.availableCount ?? 0) >= 1 else { continue }
            let canonical = group.canonicalProvider.lowercased()
            guard canonical == "codex" else { continue }
            for credit in credits.credits ?? [] {
                guard let raw = credit.expiresAt, let expiry = Self.parseTimestamp(raw),
                      expiry > snap.now,
                      expiry.timeIntervalSince(snap.now) <= 48 * 3600 else { continue }
                let key = "credit|\(canonical)|\(credit.id ?? "")|\(Int(expiry.timeIntervalSince1970))"
                if notifiedKeys.insert(key).inserted {
                    fresh.append(CreditAlertItem(provider: group.provider,
                                                 count: credits.availableCount ?? 0,
                                                 clause: Self.creditsClause(expiry: expiry, now: snap.now),
                                                 expiresAt: expiry))
                }
            }
        }
        return fresh
    }

    private func postCreditExpiryNotificationsIfNeeded(_ snap: Snapshot) {
        let fresh = evaluateResetCreditNotifications(snap)
        guard !fresh.isEmpty, let first = fresh.first else { return }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = L10n.t("notif_credits_title")
            content.body = L10n.t("notif_credits_body", first.count, first.clause)
            // Click opens the quota section in the All view (where the row lives).
            content.userInfo = ["openQuotaAll": true]
            let id = "tokdash-credit-expiry-\(Date().timeIntervalSince1970)"
            let req = UNNotificationRequest(identifier: id, content: content,
                                            trigger: UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false))
            center.add(req)
        }
    }

    /// "Days remaining" clause from the soonest FUTURE expiry, floored to whole days:
    /// >= 2 d -> "in {d} d"; 1..<2 d -> "tomorrow"; < 1 d -> "today". Expired entries
    /// are ignored by the caller for both the row clause and the notification.
    nonisolated static func creditsClause(expiry: Date, now: Date) -> String {
        let remaining = expiry.timeIntervalSince(now)
        let days = Int(remaining / 86_400)
        if days >= 2 { return L10n.t("credits_in_days", days) }
        if remaining >= 86_400 { return L10n.t("credits_tomorrow") }
        return L10n.t("credits_today")
    }

    // MARK: - Period calendar math (week = local Monday .. today, NEVER period=week)

    /// "yyyy-MM-dd" in the calendar's timezone. POSIX locale so a non-Latin-digit locale
    /// can never corrupt a query parameter.
    nonisolated static func dayString(_ date: Date, calendar: Calendar) -> String {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.calendar = calendar
        fmt.timeZone = calendar.timeZone
        fmt.dateFormat = "yyyy-MM-dd"
        return fmt.string(from: date)
    }

    nonisolated static func date(fromDayString s: String, calendar: Calendar) -> Date? {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.calendar = calendar
        fmt.timeZone = calendar.timeZone
        fmt.dateFormat = "yyyy-MM-dd"
        return fmt.date(from: s)
    }

    /// Start-of-day Monday on or before `date`. First-weekday-independent: computed from
    /// the weekday index (Mon = 2 in Gregorian), not `calendar.firstWeekday`.
    nonisolated static func startOfWeekMonday(_ date: Date, calendar: Calendar) -> Date {
        let start = calendar.startOfDay(for: date)
        let weekday = calendar.component(.weekday, from: start) // Sun = 1 ... Sat = 7
        let offset = (weekday + 5) % 7                          // Mon -> 0, Sun -> 6
        return calendar.date(byAdding: .day, value: -offset, to: start) ?? start
    }

    /// `date_from`/`date_to` pair: local Monday .. today. Contract §Period windows.
    nonisolated static func weekRange(today: Date, calendar: Calendar) -> (from: String, to: String) {
        (dayString(startOfWeekMonday(today, calendar: calendar), calendar: calendar),
         dayString(today, calendar: calendar))
    }

    // MARK: - Active time ladder (E-active)

    /// "active <1 m" / "active 42 m" / "active 3 h 12 m" / "active 74 d 5 h". Zero and
    /// absent data render NO segment at all (the caller checks), never "active 0 m".
    /// Input is MILLISECONDS (contract: every duration field is ms).
    nonisolated static func activeText(activeMs: Int) -> String {
        let seconds = activeMs / 1000
        if seconds < 60 { return L10n.t("active_label", L10n.t("dur_lt1m")) }
        if seconds < 3600 { return L10n.t("active_label", L10n.t("dur_m", seconds / 60)) }
        if seconds < 86_400 {
            return L10n.t("active_label", L10n.t("dur_hm", seconds / 3600, (seconds % 3600) / 60))
        }
        return L10n.t("active_label", L10n.t("dur_dh", seconds / 86_400, (seconds % 86_400) / 3600))
    }

    // MARK: - Tool display names and logos (E3)

    /// Display names for the by_tool keys the server actually emits; anything unknown
    /// gets the id capitalized, never a blank row.
    nonisolated static func toolDisplayName(for tool: String) -> String {
        switch tool.lowercased() {
        case "codex": return "Codex"
        case "claude": return "Claude"
        case "kimi": return "Kimi"
        case "opencode": return "OpenCode"
        case "openclaw": return "OpenClaw"
        case "gemini": return "Gemini"
        default:
            guard let first = tool.first else { return tool }
            return first.uppercased() + tool.dropFirst()
        }
    }

    /// Asset-catalog image name for a tool id, nil when no mark ships for it - a tool
    /// without a shipped logo renders text-only (never a placeholder).
    nonisolated static func logoAssetName(for tool: String) -> String? {
        switch tool.lowercased() {
        case "codex": return "AgentCodex"
        case "claude": return "AgentClaude"
        case "kimi": return "AgentKimi"
        case "opencode": return "AgentOpenCode"
        case "gemini": return "AgentGemini"
        case "openclaw": return "AgentOpenClaw"
        default: return nil
        }
    }

    /// "openai/gpt-5.6-sol" -> "gpt-5.6-sol". Model rows strip the provider prefix.
    nonisolated static func stripProviderPrefix(_ model: String) -> String {
        model.split(separator: "/").last.map(String.init) ?? model
    }

    // MARK: - Activity glance faces (E9)

    /// Pure face selection so the contract tests can pin every period/component combo
    /// without a live server. All-zero faces return nil (component hides itself).
    /// The WEEK face anchors on `now` (the snapshot clock) - contract §Activity glance
    /// pins the face as "7 columns Mon..today"; a stale payload must not silently
    /// present last week's columns, it renders the current week (all-zero -> hidden).
    /// The GRID faces anchor on the payload's newest date, which the contract sanctions
    /// for the trailing-90/180-day windows.
    nonisolated static func glanceFace(period: UsagePeriod, insights: InsightsResponse?, stats: StatsResponse?,
                                       components: CompanionComponents,
                                       calendar: Calendar, now: Date) -> Snapshot.GlanceFace? {
        guard components.activityGlance else { return nil }
        switch period {
        case .today:
            guard components.activityHistogramTodayWeek, let insights else { return nil }
            return hourFace(insights)
        case .week:
            guard components.activityHistogramTodayWeek, let insights else { return nil }
            return dayFace(daily: insights.daily, now: now, calendar: calendar)
        case .month, .year:
            return gridFace(stats: stats, windowDays: period == .month ? 90 : 180, calendar: calendar)
        }
    }

    /// 24 hourly bars, index = hour; the server's sparse buckets map onto their hour.
    private nonisolated static func hourFace(_ insights: InsightsResponse) -> Snapshot.GlanceFace? {
        let buckets = insights.hourly?.buckets ?? []
        guard !buckets.isEmpty else { return nil }
        var bars = [Int](repeating: 0, count: 24)
        for bucket in buckets {
            guard let hour = bucket.hour, (0...23).contains(hour) else { continue }
            bars[hour] = bucket.tokens ?? 0
        }
        if bars.allSatisfy({ $0 == 0 }) { return nil }
        return .hours(bars: bars, peakHour: insights.hourly?.peakHour)
    }

    /// Mon..Sun columns of the week containing `now` (the snapshot clock), with the
    /// later days of the current week reading as empty until they happen. The facet is
    /// sparse (no entry = no usage), so missing days render as empty zero columns, not
    /// skipped ones. Anchoring on the clock - not the newest payload date - keeps the
    /// face honest when the daily facet lags behind today.
    private nonisolated static func dayFace(daily: [DailyPoint]?, now: Date,
                                            calendar: Calendar) -> Snapshot.GlanceFace? {
        guard let daily, !daily.isEmpty else { return nil }
        let monday = startOfWeekMonday(now, calendar: calendar)
        var tokens: [Int] = []
        for offset in 0..<7 {
            guard let day = calendar.date(byAdding: .day, value: offset, to: monday) else { tokens.append(0); continue }
            let key = dayString(day, calendar: calendar)
            tokens.append(daily.first { $0.date == key }?.tokens ?? 0)
        }
        if tokens.allSatisfy({ $0 == 0 }) { return nil }
        return .days(tokens: tokens)
    }

    /// GitHub-style contribution grid: windowDays trailing days ending at the NEWEST
    /// date in the payload (not `Date()` - the tests and offline cycles have no clock
    /// truth), column-major weeks starting Monday, out-of-window cells nil.
    private nonisolated static func gridFace(stats: StatsResponse?, windowDays: Int,
                                             calendar: Calendar) -> Snapshot.GlanceFace? {
        let contributions = stats?.contributions ?? []
        guard !contributions.isEmpty else { return nil }
        let dated: [(date: Date, intensity: Int)] = contributions.compactMap { c in
            guard let raw = c.date, let date = date(fromDayString: raw, calendar: calendar) else { return nil }
            return (date, min(4, max(0, c.intensity ?? 0)))
        }
        guard let anchor = dated.map(\.date).max(),
              let windowStart = calendar.date(byAdding: .day, value: -(windowDays - 1), to: anchor)
        else { return nil }
        let gridStart = startOfWeekMonday(windowStart, calendar: calendar)
        let intensityByDay = Dictionary(dated.map { (dayString($0.date, calendar: calendar), $0.intensity) },
                                        uniquingKeysWith: { $0 + $1 })
        var columns: [[Int?]] = []
        var week = 0
        while true {
            guard let columnStart = calendar.date(byAdding: .day, value: week * 7, to: gridStart) else { break }
            var column: [Int?] = []
            var anyCell = false
            for dow in 0..<7 {
                guard let cell = calendar.date(byAdding: .day, value: dow, to: columnStart) else { column.append(nil); continue }
                if cell < windowStart || cell > anchor { column.append(nil); continue }
                anyCell = true
                column.append(intensityByDay[dayString(cell, calendar: calendar)] ?? 0)
            }
            guard anyCell else { break } // past the anchor: remaining columns are all nil
            columns.append(column)
            week += 1
            if week > 53 { break } // 180-day window is ~27 columns; hard stop for safety
        }
        let filled = columns.flatMap { $0 }.compactMap { $0 }.filter { $0 > 0 }.count
        if filled == 0 { return nil }
        return .grid(columns: columns, filledCells: filled, windowDays: windowDays,
                     firstCellDate: dayString(gridStart, calendar: calendar))
    }

    // MARK: - Update checking

    /// The running app's marketing version ("0.1.4"), read from the bundle so it can never
    /// drift from what was shipped.
    nonisolated static var currentVersion: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0.0.0"
    }

    /// The version the badge is for, or nil when there's nothing to show. Derived (not
    /// stored) so the three ways it can go away - installing the update, skipping the
    /// version, a check finding us current - all fall out of one rule. Opening Settings is
    /// deliberately NOT one of them.
    var updateAvailableVersion: String? {
        guard let available = settings.availableUpdateVersion,
              available != settings.skippedUpdateVersion,
              let candidate = UpdateChecker.parseVersion(available),
              let current = UpdateChecker.parseVersion(Self.currentVersion),
              UpdateChecker.isNewer(candidate, than: current) else { return nil }
        return available
    }

    /// Whether to draw the red dot on the Settings gear. Never true for checking, offline,
    /// malformed-response, or rate-limited states: those aren't news the user can act on.
    var showsUpdateBadge: Bool { updateAvailableVersion != nil }

    /// Accessibility label + tooltip for the gear, which changes when an update is pending
    /// (the dot alone carries no meaning to VoiceOver).
    var settingsAccessibilityLabel: String {
        showsUpdateBadge ? L10n.t("settings_update_available") : L10n.t("settings")
    }

    var lastUpdateCheckText: String { UpdateChecker.lastCheckedText(settings.lastUpdateCheckAt) }

    /// Run an update check.
    ///
    /// `manual` (the Settings "Check now" button) bypasses the 24h throttle, shows a
    /// checking state, and reports failures. A scheduled check is throttled, silent on
    /// failure, and - because it runs in its own task off the refresh path - can never
    /// change ``connectionState``.
    func checkForUpdates(manual: Bool) {
        if !manual {
            guard !updateCheckInFlight else { return }
            guard UpdateChecker.shouldAutoCheck(enabled: settings.automaticUpdateChecks,
                                                lastCheck: settings.lastUpdateCheckAt,
                                                now: Date()) else { return }
        }
        updateTask?.cancel()
        updateCheckGeneration += 1
        let generation = updateCheckGeneration
        updateCheckInFlight = true
        if manual { updateStatus = .checking }
        updateTask = Task { [weak self] in
            guard let self else { return }
            let result: Result<[GitHubRelease], Error>
            do { result = .success(try await self.releases.fetchReleases()) }
            catch { result = .failure(error) }
            // A superseded check must not write state back over its replacement's.
            guard !Task.isCancelled, generation == self.updateCheckGeneration else { return }
            self.updateCheckInFlight = false
            switch result {
            case .success(let releases): self.applyReleases(releases, manual: manual)
            case .failure(let error): self.applyUpdateFailure(error, manual: manual)
            }
        }
    }

    private func applyReleases(_ releases: [GitHubRelease], manual: Bool) {
        settings.lastUpdateCheckAt = Date()
        guard let newest = UpdateChecker.newestCompanionRelease(in: releases),
              // A bundle version that doesn't parse fails CLOSED (no badge): claiming an
              // update we can't compare against would be worse than staying quiet.
              let current = UpdateChecker.parseVersion(Self.currentVersion),
              UpdateChecker.isNewer(newest.version, than: current) else {
            settings.availableUpdateVersion = nil
            settings.availableUpdateURL = nil
            updateStatus = .upToDate
            settings.save()
            return
        }
        let version = UpdateChecker.versionString(newest.version)
        let url = UpdateChecker.releaseURL(for: newest.release, version: newest.version)
        settings.availableUpdateVersion = version
        settings.availableUpdateURL = url
        updateStatus = .available(version: version, url: url)
        settings.save()
    }

    private func applyUpdateFailure(_ error: Error, manual: Bool) {
        // Stamp the timestamp on failure too, so "at most once every 24 hours" holds while
        // offline: without it the scheduler would retry GitHub every refresh tick and walk
        // straight into the rate limit.
        settings.lastUpdateCheckAt = Date()
        settings.save()
        // A failed check never clears a known-available update, and a scheduled failure
        // leaves the status line exactly as it was.
        guard manual else { return }
        updateStatus = .failed(UpdateChecker.failureText((error as? UpdateCheckError) ?? .other))
    }

    /// Persist the automatic-check opt-in. Turning it on checks immediately rather than
    /// waiting up to a day for the first tick.
    func setAutomaticUpdateChecks(_ enabled: Bool) {
        guard settings.automaticUpdateChecks != enabled else { return }
        settings.automaticUpdateChecks = enabled
        settings.save()
        if enabled { checkForUpdates(manual: false) }
    }

    /// Dismiss the badge for this version only. A later release re-arms it. `settings` is
    /// @Published and `showsUpdateBadge` derives from it, so this assignment re-renders the
    /// gear and the Settings section on its own.
    func skipUpdate(version: String) {
        settings.skippedUpdateVersion = version
        settings.save()
    }

    // Test hooks: drive the two state-application paths without a network round-trip.
    internal func applyReleasesForTesting(_ releases: [GitHubRelease], manual: Bool) {
        applyReleases(releases, manual: manual)
    }

    internal func applyUpdateFailureForTesting(_ error: Error, manual: Bool) {
        applyUpdateFailure(error, manual: manual)
    }

    internal func setUpdateStatusForTesting(_ status: UpdateStatus) {
        updateStatus = status
    }

    /// Open the release page. Re-validated at the point of use so a persisted URL from an
    /// older build still can't send the browser somewhere else.
    func openUpdatePage() {
        guard let raw = settings.availableUpdateURL,
              UpdateChecker.isValidReleaseURL(raw),
              let url = URL(string: raw) else { return }
        NSWorkspace.shared.open(url)
    }

    // MARK: - Launch at login

    /// Register/unregister the app for launch at login via SMAppService (macOS 13+).
    func setLaunchAtLogin(_ enabled: Bool) {
        let service = SMAppService.mainApp
        do {
            if enabled { try service.register() } else { try service.unregister() }
            settings.launchAtLogin = enabled
        } catch {
            settings.launchAtLogin = (service.status == .enabled)
        }
        settings.save()
    }

    var freshnessText: String {
        guard let last = lastDataTime ?? lastFetchAt else {
            return connectionState == .connecting ? "" : L10n.t("no_data_yet")
        }
        let age = Date().timeIntervalSince(last)
        var text: String
        if age < 60 { text = L10n.t("updated_just_now") }
        else if age < 3600 { text = L10n.t("updated_min_ago", Int(age / 60)) }
        else if age < 86400 { text = L10n.t("updated_h_ago", Int(age / 3600)) }
        else { text = L10n.t("updated_d_ago", Int(age / 86400)) }
        // Append "· stale" only when last-good data is older than the refresh window
        // (60s while open, 600s while closed) and the last fetch failed (offline/busy).
        let window: TimeInterval = isOpen ? 60 : 600
        if (connectionState == .offline || connectionState == .busy) && age > window { text += L10n.t("stale_suffix") }
        if !failedServerLabels.isEmpty { text += " · " + L10n.t("servers_unavailable", failedServerLabels.joined(separator: ", ")) }
        return text
    }

    /// Live label for the menu-bar item: reflects connection state and usage.
    /// Period-neutral wording: the hero number behind it is whichever period is selected.
    var tooltipText: String {
        if let snap = snapshot, (snap.usage?.totalTokens ?? 0) > 0 {
            return L10n.t("tooltip_usage", snap.costText, snap.tokensCompact)
        }
        switch connectionState {
        case .connecting: return L10n.t("tooltip_connecting")
        case .connected: return L10n.t("tooltip_no_usage")
        case .busy: return L10n.t("tooltip_busy")
        case .offline: return L10n.t("tooltip_offline")
        case .wrongService: return L10n.t("tooltip_not_tokdash")
        }
    }
}

enum ConnectionState {
    case connecting
    case connected
    case busy
    case offline
    case wrongService

    var label: String {
        switch self {
        case .connecting: return L10n.t("connecting")
        // The server label prefix is added by CompanionStore.connectionLabel, which is
        // the only thing that knows the configured base URL.
        case .connected: return L10n.t("connected")
        case .busy: return L10n.t("busy")
        case .offline: return L10n.t("offline")
        case .wrongService: return L10n.t("not_tokdash")
        }
    }

    var dotColor: Color {
        switch self {
        case .connecting: return .orange
        case .connected: return .green
        case .busy: return .orange
        case .offline, .wrongService: return .red
        }
    }
}

enum QuotaView { case low, all }

/// One render pass over the selected period. The hero, delta row, ranks and glance
/// all read the snapshot for `period`; quota is period-independent. `usage == nil`
/// means the selected period's first fetch is still in flight (loading skeleton).
struct Snapshot {
    let period: UsagePeriod
    let usage: UsageResponse?
    let activeMs: Int?
    let insights: InsightsResponse?
    let stats: StatsResponse?
    let quota: QuotaResponse
    let thresholds: QuotaThresholds
    let components: CompanionComponents
    /// Clock this snapshot was built with (test-freezable). Drives the credits clause.
    let now: Date

    // Per-section status from the latest refresh. A failed section keeps its
    // last-good data (held by the store) and the UI shows an inline warning.
    var usageFailed: Bool = false
    var quotaFailed: Bool = false

    // Per-server hero values from this cycle's fan-out (empty for a single server;
    // this cycle only, no persistence - contract §Per-server rows).
    var perServer: [PerServerUsage] = []
    var showPerServerRows: Bool = false

    init(period: UsagePeriod = .today,
         usage: UsageResponse? = nil,
         activeMs: Int? = nil,
         insights: InsightsResponse? = nil,
         stats: StatsResponse? = nil,
         quota: QuotaResponse = .empty,
         thresholds: QuotaThresholds = .defaults,
         components: CompanionComponents = CompanionComponents(),
         now: Date = Date(),
         usageFailed: Bool = false,
         quotaFailed: Bool = false,
         perServer: [PerServerUsage] = [],
         showPerServerRows: Bool = false) {
        self.period = period; self.usage = usage; self.activeMs = activeMs
        self.insights = insights; self.stats = stats; self.quota = quota
        self.thresholds = thresholds; self.components = components; self.now = now
        self.usageFailed = usageFailed; self.quotaFailed = quotaFailed
        self.perServer = perServer; self.showPerServerRows = showPerServerRows
    }

    /// True while the selected period's data has never landed and no failure has been
    /// reported: the hero/delta/rank blocks and glance show their loading skeleton.
    var usageLoading: Bool { usage == nil && !usageFailed }
    var isEmptyUsage: Bool { usage?.totalTokens == 0 }

    var kickerText: String { L10n.t(period.kickerKey) }

    var perServerRows: [PerServerRow] { perServer.map(PerServerRow.init) }

    var costText: String { String(format: "$%.2f", usage?.totalCost ?? 0) }
    var tokensCompact: String { Self.compactTokens(usage?.totalTokens ?? 0) }

    /// Hero secondary line: "18.7M tokens · 248 messages · active 3 h 12 m".
    /// The active segment follows the ladder in CompanionStore.activeText (zero and
    /// absent data render NO segment, never "active 0 m"); usage failure swaps it for
    /// the shipped " · retrying" suffix.
    var subLine: String {
        var suffix = usageFailed ? L10n.t("today_retrying_suffix") : ""
        if suffix.isEmpty, let active = activeSegmentText {
            suffix = " · " + active
        }
        return L10n.t("today_tokens_messages", tokensCompact, usage?.totalMessages ?? 0, suffix)
    }

    var activeSegmentText: String? {
        guard let ms = activeMs, ms > 0 else { return nil }
        return CompanionStore.activeText(activeMs: ms)
    }

    static func compactTokens(_ value: Int) -> String {
        if value >= 1_000_000 {
            var text = String(format: "%.1f", Double(value) / 1_000_000)
            if text.hasSuffix(".0") { text = String(text.dropLast(2)) }
            return text + "M"
        }
        if value >= 1_000 {
            // Round - not floor - to the shown precision (contract §Token compact
            // notation): 249_669 renders "250k".
            return "\(Int((Double(value) / 1000).rounded()))k"
        }
        return "\(value)"
    }

    // MARK: Full delta row (E1)

    /// One metric of the delta line; `direction` colors the span (-1 down / 0 flat / +1 up).
    struct DeltaPiece: Equatable {
        let text: String
        let direction: Int
    }

    var deltaSentence: String { L10n.t(period.vsKey) }

    /// `{glyph} {pct}% cost · {glyph} {pct}% tokens · {glyph} {pct}% msgs {sentence}`.
    /// A null metric is omitted; all three null hides the row entirely (healthy-year).
    var deltaPieces: [DeltaPiece]? {
        guard components.fullDeltaRow, let comparison = usage?.comparison else { return nil }
        var pieces: [DeltaPiece] = []
        if let pct = comparison.costPct {
            pieces.append(Self.deltaPiece(L10n.t("delta_cost", Self.deltaGlyph(pct), Self.deltaValue(pct)), pct: pct))
        }
        if let pct = comparison.tokensPct {
            pieces.append(Self.deltaPiece(L10n.t("delta_tokens", Self.deltaGlyph(pct), Self.deltaValue(pct)), pct: pct))
        }
        if let pct = comparison.messagesPct {
            pieces.append(Self.deltaPiece(L10n.t("delta_msgs", Self.deltaGlyph(pct), Self.deltaValue(pct)), pct: pct))
        }
        return pieces.isEmpty ? nil : pieces
    }

    /// Flat text of the delta line (what the contract tests pin).
    var deltaRowText: String? {
        guard let pieces = deltaPieces else { return nil }
        return pieces.map(\.text).joined(separator: " · ") + " " + deltaSentence
    }

    /// Glyph: `▲` for > 0, `▼` for < 0, `±` for exactly 0.
    nonisolated static func deltaGlyph(_ pct: Double) -> String {
        pct > 0 ? "▲" : (pct < 0 ? "▼" : "±")
    }

    /// abs(round(pct)): -11.7 renders 12.
    nonisolated static func deltaValue(_ pct: Double) -> Int {
        abs(Int(pct.rounded()))
    }

    private nonisolated static func deltaPiece(_ text: String, pct: Double) -> DeltaPiece {
        DeltaPiece(text: text, direction: pct > 0 ? 1 : (pct < 0 ? -1 : 0))
    }

    /// The shipped single comparison line, cost-only and worded ("12% below
    /// yesterday") - what the hero shows with `fullDeltaRow` off (1.0.2 behavior,
    /// except the sentence now follows the selected segment like every period string).
    var comparisonLine: String? {
        guard !components.fullDeltaRow, let pct = usage?.comparison?.costPct else { return nil }
        let word = L10n.t(period.wordKey)
        return pct <= 0
            ? L10n.t("comparison_below", Int(abs(pct)), word)
            : L10n.t("comparison_above", Int(abs(pct)), word)
    }

    /// Color signal for the cost-only line: -1 "below" / +1 "above" (nil with no line).
    var comparisonDirection: Int? {
        guard comparisonLine != nil, let pct = usage?.comparison?.costPct else { return nil }
        return pct < 0 ? -1 : (pct > 0 ? 1 : 0)
    }

    // MARK: Top ranks (E3)

    struct RankEntry: Identifiable, Equatable {
        let id: String
        let label: String
        let valueText: String
        let logoAsset: String?
        /// Share 0...1 of ALL tokens in the SAME list (mock §ranks); the bar width and
        /// `pctText` are the same number, so bar and label always agree. Top-3 shares
        /// need not add up to 100% - the denominator is the full list.
        let fraction: Double
        /// `fraction` as a rounded percent string ("62%"); "0%" for zero-sum lists.
        let pctText: String
    }

    /// Share helper mirroring Windows `ShareOf`: zero-sum lists render an empty bar and
    /// "0%", never NaN. Rounding is away-from-zero on both platforms, so the two apps
    /// print the same percent for the same data.
    nonisolated static func share(tokens: Int, total: Int) -> (Double, String) {
        let frac = total > 0 ? Double(tokens) / Double(total) : 0
        return (frac, "\(Int((frac * 100).rounded()))%")
    }

    /// by_tool sorted by tokens descending, top 3. Labels are display names, values
    /// compact tokens; a tool id with no shipped mark gets NO logo (never a placeholder).
    var topTools: [RankEntry] {
        guard components.topRanks, let usage else { return [] }
        let sorted = (usage.byTool ?? [:]).sorted {
            if $0.value.tokens != $1.value.tokens { return $0.value.tokens > $1.value.tokens }
            return $0.key < $1.key
        }
        let total = (usage.byTool ?? [:]).values.reduce(0) { $0 + $1.tokens }
        return sorted.prefix(3).map { entry in
            let (frac, pct) = Self.share(tokens: entry.value.tokens, total: total)
            return RankEntry(id: entry.key,
                             label: CompanionStore.toolDisplayName(for: entry.key),
                             valueText: Self.compactTokens(entry.value.tokens),
                             logoAsset: CompanionStore.logoAssetName(for: entry.key),
                             fraction: frac, pctText: pct)
        }
    }

    /// First three of combined_models (tokens-ranked) - never a cost sort - with the
    /// provider prefix stripped and no logos on model rows. Percentage denominator is
    /// the full combined_models list.
    var topModels: [RankEntry] {
        guard components.topRanks, let usage else { return [] }
        let list = usage.combinedModels ?? usage.topModels ?? []
        let total = list.reduce(0) { $0 + $1.tokens }
        return list.prefix(3).map { model in
            let (frac, pct) = Self.share(tokens: model.tokens, total: total)
            return RankEntry(id: model.name,
                             label: CompanionStore.stripProviderPrefix(model.name),
                             valueText: Self.compactTokens(model.tokens),
                             logoAsset: nil,
                             fraction: frac, pctText: pct)
        }
    }

    var toolsKickerText: String { L10n.t("top_tools_kicker", L10n.t(period.suffixKey)) }
    var modelsKickerText: String { L10n.t("top_models_kicker", L10n.t(period.suffixKey)) }

    // MARK: Reset credits (E2)

    /// The quiet row under the provider group in the All view: rendered only when the
    /// component is on, quota tracking is enabled, the provider is Codex, and
    /// available_count >= 1. The Low view never shows it (provider context, not a window).
    func creditsNotice(providerDisplay: String, canonicalProvider: String,
                       resetCredits: ResetCredits?) -> String? {
        guard components.resetCredits, quota.enabled,
              canonicalProvider.lowercased() == "codex",
              let resetCredits, (resetCredits.availableCount ?? 0) >= 1 else { return nil }
        return Self.creditsRowText(provider: providerDisplay, reset: resetCredits, now: now)
    }

    /// "⚡ Codex · 2 reset credits · expire in 2 d" from the soonest FUTURE credit;
    /// expired entries are ignored, and with no future expiry the whole clause (the
    /// "expire" word included) drops away.
    static func creditsRowText(provider: String, reset: ResetCredits, now: Date) -> String {
        let count = reset.availableCount ?? 0
        let futures = (reset.credits ?? []).compactMap { credit -> Date? in
            guard let raw = credit.expiresAt, let date = CompanionStore.parseTimestamp(raw), date > now else { return nil }
            return date
        }
        guard let soonest = futures.min() else {
            return L10n.t("credits_row_plain", provider, count)
        }
        return L10n.t("credits_row", provider, count, CompanionStore.creditsClause(expiry: soonest, now: now))
    }

    // MARK: Activity glance (E9)

    enum GlanceFace: Equatable {
        case hours(bars: [Int], peakHour: Int?)
        case days(tokens: [Int])   // exactly 7 columns, Mon..Sun, gaps = 0
        case grid(columns: [[Int?]], filledCells: Int, windowDays: Int, firstCellDate: String)
    }

    var glanceFace: GlanceFace? {
        CompanionStore.glanceFace(period: period, insights: insights, stats: stats,
                                  components: components, calendar: .current, now: now)
    }

    /// Windows below their low-quota threshold, sorted by remaining ascending.
    var lowQuotaRows: [QuotaRow] {
        guard quota.enabled else { return [] }
        // Flatten allQuotaGroups so each row keeps its provider name and the
        // provider-level Estimated flag. The old allQuotaRows helper dropped both.
        let rows = allQuotaGroups.flatMap { $0.rows }
            .filter { $0.isLow(thresholds: thresholds) }
            .sorted(by: { $0.left < $1.left })
        let grouped = Dictionary(grouping: rows) { row in
            let provider = row.provider.components(separatedBy: " · ").last ?? row.provider
            return "\(provider)|\(row.account)|\(row.bucket)|\(row.left)|\(row.resetsAt?.timeIntervalSince1970 ?? -1)"
        }
        return grouped.values.map { group in
            guard group.count > 1, let first = group.first else { return group[0] }
            let provider = first.provider.components(separatedBy: " · ").last ?? first.provider
            return QuotaRow(copying: first, provider: provider)
        }.sorted(by: { $0.left < $1.left }).prefix(2).map { $0 }
    }

    /// All windows grouped by provider (provider order as detected). A failed provider
    /// is flagged so the All view can render an inline warning above its last-known rows
    /// (spec §7), not a full-surface failure. GROUP failure = status != "ok" OR a non-empty
    /// status_detail (e.g. stale_token, even when status is "ok"); a provider with several
    /// credentials reports the detail for the whole provider, so this stays broad.
    var allQuotaGroups: [(provider: String, canonicalProvider: String, rows: [QuotaRow], failed: Bool,
                          providerEntry: ProviderQuota?)] {
        guard quota.enabled else { return [] }
        let providers = quota.providers ?? [:]
        // Provider order as detected (contract §All view): Foundation dictionaries
        // carry no order, so the decode path supplies the wire key sequence and the
        // fan-out merge supplies settings-order x wire-order. With neither, sort:
        // deterministic beats arbitrary-per-launch.
        let names: [String]
        if let wire = quota.providerWireOrder {
            // Dedup preserving first occurrence: duplicate server labels (two
            // settings entries both labeled "Local") or duplicate provider keys
            // would otherwise render the same group twice with the later dict
            // value, where the pre-order dict silently collapsed them - and the
            // Windows twin (plain Dictionary) still collapses them.
            var seen = Set<String>()
            names = wire.filter { providers[$0] != nil && seen.insert($0).inserted }
                + providers.keys.filter { !seen.contains($0) }.sorted()
        } else {
            names = providers.keys.sorted()
        }
        return names.compactMap { name -> (provider: String, canonicalProvider: String, rows: [QuotaRow], failed: Bool, providerEntry: ProviderQuota?)? in
            guard let prov = providers[name] else { return nil }
            let nameParts = name.components(separatedBy: " · ")
            let canonicalProvider = nameParts.last ?? name
            let display = nameParts.count == 1
                ? canonicalProvider.capitalized
                : nameParts.dropLast().joined(separator: " · ") + " · " + canonicalProvider.capitalized
            let estimated = prov.estimated ?? false
            let failed = !Self.isProviderOk(prov.status) || !(prov.statusDetail?.isEmpty ?? true)
            var rows = (prov.buckets ?? []).compactMap {
                QuotaRow(provider: display, bucket: $0, estimated: estimated,
                         failed: Self.isRowFailed(bucket: $0, provider: prov, groupFailed: failed))
            }
            if canonicalProvider.lowercased() == "antigravity" { rows = Self.antigravityPools(rows) }
            guard !rows.isEmpty else { return nil }
            return (display, canonicalProvider, rows, failed, prov)
        }
    }

    // "ok" or absent (older servers) is healthy; any other value means that quota
    // couldn't be refreshed this cycle. Spec §7.
    private static func isProviderOk(_ status: String?) -> Bool {
        guard let s = status, !s.isEmpty else { return true }
        return s.lowercased() == "ok"
    }

    // ROW failure drives the inline ⚠ and notification eligibility. buckets[].status is
    // always "ok" (the server only writes failure statuses to the filtered-out "api"
    // bucket), so freshness is the real discriminator: a row is last-known when the
    // provider's failure is NEWER than the row's data. Strict "<" makes same-cycle
    // equality count as fresh, which is what rescues a healthy credential's window when a
    // sibling credential is broken - every credential in a cycle shares capturedAt.
    // Missing timestamps (older servers) fall back to the group rather than silently
    // un-suppressing. Spec §7.
    /// Antigravity reports one bucket per model, which floods the list. The web dashboard
    /// collapses them into two pools and shows the worst remaining in each; the companion
    /// matches. Pool labels use the short forms ("Gemini" / "Claude/GPT") so the narrow
    /// flyout can also show the auto-determined window ("Gemini · Weekly"); the web dashboard
    /// keeps the long forms under its own subtitle. Falls back to the raw rows if nothing
    /// matches, so an unrecognised model can never silently vanish.
    static func antigravityPools(_ rows: [QuotaRow]) -> [QuotaRow] {
        let pools: [(key: String, label: String, test: (String) -> Bool)] = [
            ("gemini", "Gemini", { $0.contains("gemini") }),
            ("claude", "Claude/GPT", { $0.contains("claude") || $0.contains("gpt") || $0.contains("oss") }),
        ]
        var out: [QuotaRow] = []
        for pool in pools {
            let matching = rows.filter { pool.test("\($0.bucketLabel) \($0.bucket)".lowercased()) && $0.hasPercent }
            guard let worst = matching.min(by: { $0.left < $1.left }) else { continue }
            out.append(QuotaRow(copying: worst, bucket: "pool:\(pool.key)", bucketLabel: pool.label))
        }
        return out.isEmpty ? rows : out
    }

    /// ROW failure, judged against the failure that actually applies to this row.
    ///
    /// `prov.statusAt` is the newest error of ANY credential behind the card, which is
    /// right for the group warning and wrong for one row. A permanently broken sibling
    /// advances it every cycle, while a bucket that is not reported every cycle keeps an
    /// older capturedAt — and those buckets are ordinary, not edge cases: Claude's
    /// `limits` carries `weekly_scoped_opus` only once Opus has been used, and MiniMax's
    /// per-model buckets come and go with the models called. Judged against the provider,
    /// that marks the healthy install's rows last-known and drops them out of low-quota
    /// notification for as long as the sibling stays broken. So a row whose OWN credential
    /// is healthy is fresh, whatever a sibling did. Spec §7.
    ///
    /// Everything else falls back to the group rather than un-suppressing a row that may
    /// well be stale: `accounts` absent (single-credential provider, or a pre-`accounts`
    /// server), a row naming an account with no entry, or a missing timestamp.
    private static func isRowFailed(bucket: BucketQuota, provider prov: ProviderQuota,
                                    groupFailed: Bool) -> Bool {
        guard groupFailed else { return false }
        if let entry = accountEntry(prov, account: bucket.account) {
            guard isAccountFailed(entry) else { return false }
            guard let captured = bucket.capturedAt,
                  let status = entry.statusAt ?? prov.statusAt else { return true }
            return captured < status
        }
        guard let captured = bucket.capturedAt, let status = prov.statusAt else { return true }
        return captured < status
    }

    private static func accountEntry(_ prov: ProviderQuota, account: String?) -> AccountQuota? {
        guard let accounts = prov.accounts, let account, !account.isEmpty else { return nil }
        return accounts.first { $0.account == account }
    }

    // Same rule as a group: status present and not "ok", OR a non-empty statusDetail.
    // `status` alone is not a verdict — a credential with a live error still reports
    // `status: "ok"` once any of its window rows sorts after its "api" row. Spec §7.
    private static func isAccountFailed(_ entry: AccountQuota) -> Bool {
        if let detail = entry.statusDetail, !detail.isEmpty, detail.lowercased() != "ok" {
            return true
        }
        return !isProviderOk(entry.status)
    }
}

struct QuotaRow: Identifiable {
    let id = UUID()
    let provider: String
    let bucket: String
    let bucketLabel: String
    let left: Double
    let resetsAt: Date?
    let capturedAt: Date?
    let estimated: Bool
    let account: String
    let hasPercent: Bool
    let failed: Bool

    /// Drop a trailing "window" from a server bucket label: the flyout is narrow and the
    /// word carries no information ("5-hour window" -> "5-hour", "7-day window" -> "7-day").
    /// Applied at display time so stored labels from older servers shorten too. Labels that
    /// don't end in it (MiniMax "5-hour", Kimi "Weekly") pass through unchanged.
    static func displayLabel(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespaces)
        if s.lowercased().hasSuffix(" window") {
            let shortened = String(s.dropLast(" window".count)).trimmingCharacters(in: .whitespaces)
            if !shortened.isEmpty { s = shortened }
        }
        // Codex names metered features "GPT-<ver>-Codex-<feature>", which eats the whole
        // row at flyout width. Keep only the feature and the window: "Spark · 5-hour".
        // Only applies to "<name> · <window>" labels whose name is hyphenated, so plain
        // names (MiniMax "Video · Weekly") and bare windows ("5-hour") are untouched.
        let parts = s.components(separatedBy: " · ")
        if parts.count == 2, parts[0].contains("-"),
           let feature = parts[0].split(separator: "-").last.map(String.init), !feature.isEmpty {
            s = "\(feature) · \(parts[1])"
        }
        return s
    }

    /// Copy with a new bucket id + label, used to present a pooled Antigravity row.
    init(copying other: QuotaRow, bucket: String, bucketLabel: String) {
        self.provider = other.provider
        self.bucket = bucket
        self.bucketLabel = bucketLabel
        self.left = other.left
        self.resetsAt = other.resetsAt
        self.capturedAt = other.capturedAt
        self.estimated = other.estimated
        self.account = other.account
        self.hasPercent = other.hasPercent
        self.failed = other.failed
    }

    init(copying other: QuotaRow, provider: String) {
        self.provider = provider; self.bucket = other.bucket; self.bucketLabel = other.bucketLabel
        self.left = other.left; self.resetsAt = other.resetsAt; self.capturedAt = other.capturedAt
        self.estimated = other.estimated; self.account = other.account; self.hasPercent = other.hasPercent
        self.failed = other.failed
    }

    init(provider: String, bucket: BucketQuota, estimated: Bool = false, failed: Bool = false) {
        self.provider = provider
        self.bucket = bucket.bucket
        self.bucketLabel = Self.displayLabel(bucket.bucketLabel ?? bucket.bucket)
        self.left = bucket.remainingPercent ?? 100
        self.resetsAt = bucket.resetsAt.map { Date(timeIntervalSince1970: TimeInterval($0)) }
        self.capturedAt = bucket.capturedAt.map { Date(timeIntervalSince1970: TimeInterval($0)) }
        self.estimated = estimated
        self.account = bucket.account ?? ""
        self.hasPercent = bucket.remainingPercent != nil
        self.failed = failed
    }

    func isLow(thresholds: QuotaThresholds) -> Bool {
        hasPercent && left <= thresholds.threshold(for: canonicalBucket)
    }

    /// Canonical bucket id used for threshold lookup and Claude's normalized display label.
    /// Claude's usage API emits ids like
    /// ``session`` (the 5-hour window) and ``weekly_scoped`` / ``weekly_scoped_<model>``
    /// (weekly), plus legacy ``five_hour`` / ``seven_day``. None match the threshold patterns,
    /// so Claude would otherwise land in the 15% "other" bucket instead of 20% / 10% like
    /// Codex. The notification dedup key keeps the original bucket id. Scoped to the claude
    /// provider so Codex/MiniMax/Kimi/Antigravity classification is untouched.
    var canonicalBucket: String {
        Self.normalizeBucketForThreshold(provider: provider, bucket: bucket, label: bucketLabel)
    }

    static func normalizeBucketForThreshold(provider: String, bucket: String, label: String) -> String {
        let canonicalProvider = provider.components(separatedBy: " · ").last ?? provider
        guard canonicalProvider.lowercased() == "claude" else { return bucket }
        let combined = "\(bucket) \(label)".lowercased()
        if combined.contains("session") || combined.contains("five_hour") || combined.contains("five hour")
            || combined.contains("5h") || combined.contains("5-hour") {
            return "5h"
        }
        if combined.contains("week") || combined.contains("seven_day") || combined.contains("seven day")
            || combined.contains("7-day") || combined.contains("7d") {
            return "weekly"
        }
        return bucket
    }

    /// User-facing quota-window label. Claude's API calls its five-hour window "Session" and
    /// its general weekly window "Weekly All"; normalize those to the standard 5-hour / Weekly
    /// labels. Model-scoped weekly windows keep their descriptive label (for example, Fable).
    /// Resolve this at render time so a language change is live.
    var displayBucketLabel: String {
        let p = provider.lowercased()
        if p == "antigravity" {
            // Pooled rows carry the (short) pool name as bucketLabel; append the
            // auto-determined window so the bar reads "Gemini · Weekly". Mirrors the web
            // dashboard's pool subtitle + window label in one narrow-flyout label.
            return "\(bucketLabel) · \(antigravityWindowLabel)"
        }
        guard p == "claude" else { return bucketLabel }
        if bucket.lowercased().hasPrefix("weekly_scoped") { return bucketLabel }
        // Byte-exact parity with the Windows formatter: only Claude's two special windows
        // get the standardized wording. A plain "weekly" bucket passes through verbatim -
        // the contract's expected fixtures pin it as "weekly", not the forced "Weekly".
        let id = bucket.lowercased()
        if id.contains("session") || id.contains("five hour") || id.contains("five_hour")
            || id.contains("5-hour") || id.contains("5h") { return L10n.t("window_5h") }
        if "\(bucket) \(bucketLabel)".lowercased().replacingOccurrences(of: "_", with: " ").contains("weekly all") {
            return L10n.t("window_weekly")
        }
        return bucketLabel
    }

    /// Antigravity's API returns a single window per model - whichever (5-hour or weekly)
    /// currently binds the pool - with no explicit duration field, so the window is inferred
    /// from the reset time. A 5-hour window can never reset more than 5h out, so a reset
    /// beyond the threshold is weekly. The reverse is imperfect: a weekly window in its final
    /// <8h also reads as "5-hour" (self-correcting after the reset; the API exposes no
    /// duration field to disambiguate it). Measured from `capturedAt` (stable; matches the web
    /// dashboard) with a `now` fallback for older servers. Mirrors antigravityWindowLabel
    /// in the web dashboard.
    var antigravityWindowLabel: String {
        guard let resetsAt else { return L10n.t("window_5h") }
        let remaining: TimeInterval
        if let capturedAt {
            remaining = resetsAt.timeIntervalSince(capturedAt)
        } else {
            remaining = resetsAt.timeIntervalSinceNow
        }
        return Self.antigravityWindowLabel(forSecondsUntilReset: remaining)
    }

    static func antigravityWindowLabel(forSecondsUntilReset remaining: TimeInterval) -> String {
        // 8h gives skew/rounding margin above the 5h window max before treating a reset as
        // weekly; a weekly window in its final <8h is mislabeled "5-hour" (self-correcting).
        remaining > 8 * 3600 ? L10n.t("window_weekly") : L10n.t("window_5h")
    }

    var resetsText: String {
        guard let resetsAt else { return "" }
        return Self.resetsText(forRemaining: resetsAt.timeIntervalSinceNow)
    }

    /// Relative reset text from seconds-remaining, rounded down to the whole unit (matches
    /// the freshness footer's truncation). <2h -> minutes (<120); <1d -> hours; longer -> days,
    /// so a weekly window reads "resets in 3 days" rather than "resets in 94 hours". A single
    /// unit throughout (no "3d 22h" combinations). A past/stale ``resetsAt`` (window already
    /// rolled over) degrades to "resets soon" until the next refresh re-arms it. Pure so it is
    /// unit-testable without a clock. Mirrors formatResetCountdownFromSeconds in the web
    /// dashboard — the tier boundaries must stay in lockstep or the same window reads
    /// differently on the two surfaces.
    static func resetsText(forRemaining remaining: TimeInterval) -> String {
        if remaining < 60 { return L10n.t("resets_soon") }
        if remaining < 7200 {
            let mins = Int(remaining / 60)
            return L10n.t("resets_in_minutes", mins, mins == 1 ? "" : L10n.pluralS)
        }
        if remaining < 86400 {
            let hours = Int(remaining / 3600)
            return L10n.t("resets_in_hours", hours, hours == 1 ? "" : L10n.pluralS)
        }
        let days = Int(remaining / 86400)
        return L10n.t("resets_in_days", days, days == 1 ? "" : L10n.pluralS)
    }
}

// MARK: - Period / components / per-server value types

/// The flyout's single selected period. `token` goes on the wire (`period=`);
/// the key sets localize every period-dependent string, so no format string ever
/// hardcodes "today" (contract: period strings follow the selected segment).
enum UsagePeriod: String, Codable, CaseIterable, Sendable {
    case today
    case week
    case month
    case year

    var token: String { rawValue }
    var segmentKey: String { "period_\(rawValue)" }
    var kickerKey: String {
        switch self {
        case .today: return "today"
        case .week: return "kicker_week"
        case .month: return "kicker_month"
        case .year: return "kicker_year"
        }
    }
    /// Hero-rank suffix: "today" / "this week" / "this month" / "this year".
    var suffixKey: String { "suffix_\(rawValue)" }
    /// Delta-row sentence: "vs yesterday" / "vs last week" / ...
    var vsKey: String {
        switch self {
        case .today: return "vs_yesterday"
        case .week: return "vs_last_week"
        case .month: return "vs_last_month"
        case .year: return "vs_last_year"
        }
    }
    /// Cost-only comparison word: "yesterday" / "last week" / ...
    var wordKey: String {
        switch self {
        case .today: return "word_yesterday"
        case .week: return "word_last_week"
        case .month: return "word_last_month"
        case .year: return "word_last_year"
        }
    }
    /// Failure-hero title key: "Today's data unavailable" / "This week's data unavailable" / ...
    var unavailableKey: String { "unavailable_\(rawValue)" }
}

/// Settings v3 "Components" toggles (contract schema). Every key defaults true and a
/// whole MISSING components object means all-on, so a v2 file upgrades untouched.
struct CompanionComponents: Codable, Equatable, Sendable {
    var fullDeltaRow = true
    var topRanks = true
    var resetCredits = true
    var activityGlance = true
    var activityHistogramTodayWeek = true
    var perServerRows = true

    init() {}

    private enum CodingKeys: String, CodingKey {
        case fullDeltaRow, topRanks, resetCredits, activityGlance
        case activityHistogramTodayWeek, perServerRows
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        fullDeltaRow = try values.decodeIfPresent(Bool.self, forKey: .fullDeltaRow) ?? true
        topRanks = try values.decodeIfPresent(Bool.self, forKey: .topRanks) ?? true
        resetCredits = try values.decodeIfPresent(Bool.self, forKey: .resetCredits) ?? true
        activityGlance = try values.decodeIfPresent(Bool.self, forKey: .activityGlance) ?? true
        activityHistogramTodayWeek = try values.decodeIfPresent(Bool.self, forKey: .activityHistogramTodayWeek) ?? true
        perServerRows = try values.decodeIfPresent(Bool.self, forKey: .perServerRows) ?? true
    }
}

/// One server's contribution to this cycle's hero, kept for the per-server rows.
/// `usage == nil` marks an unreachable server (never dimmed numbers).
struct PerServerUsage: Sendable {
    let label: String
    let usage: UsageResponse?

    var reachable: Bool { usage != nil }
    var costText: String { String(format: "$%.2f", usage?.totalCost ?? 0) }
    var tokensCompact: String { Snapshot.compactTokens(usage?.totalTokens ?? 0) }
}

/// View-ready per-server row: label + "cost · compact tokens", or label + "unreachable".
struct PerServerRow: Identifiable, Equatable {
    let id: String     // server label doubles as identity for a single cycle
    let label: String
    let valueText: String
    let reachable: Bool

    init(_ usage: PerServerUsage) {
        id = usage.label
        label = usage.label
        reachable = usage.reachable
        valueText = usage.reachable
            ? "\(usage.costText) · \(usage.tokensCompact)"
            : L10n.t("server_unreachable_short")
    }
}

// MARK: - Settings

struct CompanionSettings: Codable {
    static let defaultBaseURL = "http://127.0.0.1:55423"

    var version: Int = 3
    var servers: [CompanionServerSettings] = [.make(baseURL: CompanionSettings.defaultBaseURL)]
    var baseURL: String {
        get { servers.first(where: { $0.enabled })?.baseURL ?? servers.first?.baseURL ?? Self.defaultBaseURL }
        set {
            if servers.isEmpty { servers = [.make(baseURL: newValue)] }
            else { servers[0].baseURL = newValue }
        }
    }
    var launchAtLogin: Bool = false
    var lowQuotaNotifications: Bool = false
    var thresholds: QuotaThresholds = .defaults
    var language: AppLanguage = .system
    /// On by default - the Settings checkbox is the opt-out, not an opt-in.
    var automaticUpdateChecks: Bool = true
    /// Last check ATTEMPT (success or failure) - the 24h throttle reads this.
    var lastUpdateCheckAt: Date? = nil
    /// Last version found newer than this build, and its validated release page. Persisted
    /// so the gear badge survives a relaunch between daily checks.
    var availableUpdateVersion: String? = nil
    var availableUpdateURL: String? = nil
    /// A version the user explicitly skipped; suppresses the badge for that version only.
    var skippedUpdateVersion: String? = nil
    /// v3: the six feature components (contract schema) and the selected segment.
    var components: CompanionComponents = CompanionComponents()
    /// v3: persisted separately from components - it's a preference, not a toggle.
    var selectedPeriod: UsagePeriod = .today

    private enum CodingKeys: String, CodingKey {
        case version
        case servers
        case baseURL
        case launchAtLogin
        case lowQuotaNotifications
        case thresholds
        case language
        case automaticUpdateChecks
        case lastUpdateCheckAt
        case availableUpdateVersion
        case availableUpdateURL
        case skippedUpdateVersion
        case components
        case selectedPeriod
    }

    init(
        baseURL: String = CompanionSettings.defaultBaseURL,
        launchAtLogin: Bool = false,
        lowQuotaNotifications: Bool = false,
        thresholds: QuotaThresholds = .defaults,
        language: AppLanguage = .system,
        automaticUpdateChecks: Bool = true,
        lastUpdateCheckAt: Date? = nil,
        availableUpdateVersion: String? = nil,
        availableUpdateURL: String? = nil,
        skippedUpdateVersion: String? = nil,
        components: CompanionComponents = CompanionComponents(),
        selectedPeriod: UsagePeriod = .today
    ) {
        self.version = 3
        self.components = components
        self.selectedPeriod = selectedPeriod
        self.servers = [.make(baseURL: baseURL)]
        self.launchAtLogin = launchAtLogin
        self.lowQuotaNotifications = lowQuotaNotifications
        self.thresholds = thresholds
        self.language = language
        self.automaticUpdateChecks = automaticUpdateChecks
        self.lastUpdateCheckAt = lastUpdateCheckAt
        self.availableUpdateVersion = availableUpdateVersion
        self.availableUpdateURL = availableUpdateURL
        self.skippedUpdateVersion = skippedUpdateVersion
    }

    /// v0.1.0 settings predate the language field, and v0.1.4 predates the update fields.
    /// Decode every existing preference and default only the absent ones so upgrading
    /// never resets the server URL or opt-ins.
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        version = 3
        if let decoded = try values.decodeIfPresent([CompanionServerSettings].self, forKey: .servers), !decoded.isEmpty {
            servers = decoded
        } else {
            let legacy = try values.decodeIfPresent(String.self, forKey: .baseURL) ?? Self.defaultBaseURL
            servers = [.make(baseURL: legacy)]
        }
        launchAtLogin = try values.decodeIfPresent(Bool.self, forKey: .launchAtLogin) ?? false
        lowQuotaNotifications = try values.decodeIfPresent(Bool.self, forKey: .lowQuotaNotifications) ?? false
        thresholds = try values.decodeIfPresent(QuotaThresholds.self, forKey: .thresholds) ?? .defaults
        language = try values.decodeIfPresent(AppLanguage.self, forKey: .language) ?? .system
        automaticUpdateChecks = try values.decodeIfPresent(Bool.self, forKey: .automaticUpdateChecks) ?? true
        lastUpdateCheckAt = try values.decodeIfPresent(Date.self, forKey: .lastUpdateCheckAt)
        availableUpdateVersion = try values.decodeIfPresent(String.self, forKey: .availableUpdateVersion)
        availableUpdateURL = try values.decodeIfPresent(String.self, forKey: .availableUpdateURL)
        skippedUpdateVersion = try values.decodeIfPresent(String.self, forKey: .skippedUpdateVersion)
        // v2 files have no components/selectedPeriod: absent means all-on / today, so
        // upgrading never silently disables a shipped feature (contract schema v3).
        components = try values.decodeIfPresent(CompanionComponents.self, forKey: .components) ?? CompanionComponents()
        // Decode the period as a string first: a value written by a FUTURE build (a new
        // segment) falls back to today instead of throwing away the whole settings file.
        let periodRaw = try values.decodeIfPresent(String.self, forKey: .selectedPeriod)
        selectedPeriod = periodRaw.flatMap(UsagePeriod.init(rawValue:)) ?? .today
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(3, forKey: .version)
        try values.encode(servers, forKey: .servers)
        try values.encode(launchAtLogin, forKey: .launchAtLogin)
        try values.encode(lowQuotaNotifications, forKey: .lowQuotaNotifications)
        try values.encode(thresholds, forKey: .thresholds)
        try values.encode(language, forKey: .language)
        try values.encode(automaticUpdateChecks, forKey: .automaticUpdateChecks)
        try values.encodeIfPresent(lastUpdateCheckAt, forKey: .lastUpdateCheckAt)
        try values.encodeIfPresent(availableUpdateVersion, forKey: .availableUpdateVersion)
        try values.encodeIfPresent(availableUpdateURL, forKey: .availableUpdateURL)
        try values.encodeIfPresent(skippedUpdateVersion, forKey: .skippedUpdateVersion)
        // Schema v3: components and the selected hero segment must persist too -
        // omitting them here silently resets every toggle and the segment on relaunch.
        try values.encode(components, forKey: .components)
        try values.encode(selectedPeriod.rawValue, forKey: .selectedPeriod)
    }

    /// Test seam: when set, settings are read and written here instead of the user's real
    /// file. Nil in production. The test bundle installs a temp path before any store is
    /// constructed, so a test can neither read the developer's own settings (which would
    /// make assertions depend on their machine) nor write to them.
    nonisolated(unsafe) static var pathOverride: URL?

    static var defaultsURL: URL { pathOverride ?? productionURL }

    private static let productionURL: URL = {
        let fm = FileManager.default
        let appSupport = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let dir = appSupport.appendingPathComponent("TokdashCompanion", isDirectory: true)
        if !fm.fileExists(atPath: dir.path) {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir.appendingPathComponent("settings.json")
    }()

    static func load() -> CompanionSettings {
        guard let data = try? Data(contentsOf: defaultsURL),
              let s = try? JSONDecoder().decode(CompanionSettings.self, from: data) else {
            return CompanionSettings()
        }
        return s
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            try? data.write(to: Self.defaultsURL)
        }
    }
}

struct CompanionServerSettings: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var label: String
    var baseURL: String
    var enabled: Bool

    private enum CodingKeys: String, CodingKey {
        case id
        case label
        case baseURL = "baseUrl"
        case enabled
    }

    static func make(baseURL: String) -> CompanionServerSettings {
        CompanionServerSettings(
            id: UUID().uuidString,
            label: CompanionStore.serverLabel(for: baseURL),
            baseURL: baseURL,
            enabled: true
        )
    }
}

struct QuotaThresholds: Codable, Equatable {
    var fiveHour: Double
    var weekly: Double
    var other: Double

    static let defaults = QuotaThresholds(fiveHour: 20, weekly: 10, other: 15)

    func threshold(for bucket: String) -> Double {
        let b = bucket.lowercased()
        if b.contains("5h") || b.contains("5-hour") || b == "5h" { return fiveHour }
        if b.contains("week") || b == "weekly" || b == "7d" { return weekly }
        return other
    }
}
