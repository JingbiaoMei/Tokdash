import AppKit
import Foundation
import SwiftUI
import ServiceManagement
@preconcurrency import UserNotifications

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

    // Last-good per section, retained across refreshes for partial-state rendering.
    private var lastToday: UsageResponse?
    private var lastMonth: UsageResponse?
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

    var serverLabel: String { Self.serverLabel(for: settings.baseURL) }

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

    // MARK: - Refresh

    /// Rebuild the current snapshot from last-good data with the (possibly new)
    /// thresholds, so the Low view re-evaluates immediately without a network refresh.
    func applyThresholds() {
        guard let q = lastQuota, q.enabled else { return }
        var snap = Snapshot(today: lastToday ?? .empty, month: lastMonth ?? .empty,
                            quota: q, thresholds: settings.thresholds)
        if let cur = snapshot {
            snap.todayFailed = cur.todayFailed
            snap.monthFailed = cur.monthFailed
            snap.quotaFailed = cur.quotaFailed
        }
        snapshot = snap
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
            // discards the other two. Last-good is retained per section; a failed
            // section keeps its previous data and the UI shows an inline warning.
            async let todayAttempt = client.usage(period: "today")
            async let monthAttempt = client.usage(period: "month")
            async let quotaAttempt = client.quota()

            var todayFailed = false, monthFailed = false, quotaFailed = false
            var todayBusy = false, monthBusy = false, quotaBusy = false
            do { lastToday = try await todayAttempt } catch let e as TokdashError { todayFailed = true; if case .busy = e { todayBusy = true } } catch { todayFailed = true }
            do { lastMonth = try await monthAttempt } catch let e as TokdashError { monthFailed = true; if case .busy = e { monthBusy = true } } catch { monthFailed = true }
            do { lastQuota = try await quotaAttempt } catch let e as TokdashError { quotaFailed = true; if case .busy = e { quotaBusy = true } } catch { quotaFailed = true }

            if Task.isCancelled { return }

            var snap = Snapshot(today: lastToday ?? .empty, month: lastMonth ?? .empty,
                                quota: lastQuota ?? .empty, thresholds: settings.thresholds)
            snap.todayFailed = todayFailed
            snap.monthFailed = monthFailed
            snap.quotaFailed = quotaFailed
            self.snapshot = snap
            self.lastError = nil
            self.connectionState = .connected

            let allFailed = todayFailed && monthFailed && quotaFailed
            if allFailed {
                // Health ok but every data endpoint failed. If all were 503, the service
                // is busy: show the Busy banner + dimmed last-good, not Connected.
                if todayBusy && monthBusy && quotaBusy { connectionState = .busy }
                failures += 1
                partial = false
            } else {
                lastFetchAt = Date()
                // Data time: prefer the API timestamp (naive UTC parsed via parseTimestamp),
                // else fall back to fetch time minus the cache age, else fetch time. Spec §freshness.
                if let ts = lastToday?.timestamp,
                   let parsed = Self.parseTimestamp(ts) {
                    lastDataTime = parsed
                } else if let age = lastToday?.responseCache?.ageSeconds {
                    lastDataTime = (lastFetchAt ?? Date()).addingTimeInterval(-age)
                } else {
                    lastDataTime = lastFetchAt
                }
                failures = 0
                partial = todayFailed || monthFailed || quotaFailed // partial -> 15s short retry
                let fresh = evaluateLowQuotaNotifications(snap)
                if !fresh.isEmpty { postLowQuotaNotification(fresh) }
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
        let delay = Self.computeDelay(open: isOpen, failures: failures, partial: partial, lastFetch: lastFetchAt, now: Date())
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
            let stateKey = "\(r.provider)|\(r.account)|\(r.bucket)|\(epoch)"
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
        return text
    }

    /// Live label for the menu-bar item: reflects connection state and usage.
    var tooltipText: String {
        if let snap = snapshot, snap.today.totalTokens > 0 {
            return L10n.t("tooltip_today", snap.todayCostText, snap.todayTokensCompact)
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

struct Snapshot {
    let today: UsageResponse
    let month: UsageResponse
    let quota: QuotaResponse
    let thresholds: QuotaThresholds

    // Per-section status from the latest refresh. A failed section keeps its
    // last-good data (held by the store) and the UI shows an inline warning.
    var todayFailed: Bool = false
    var monthFailed: Bool = false
    var quotaFailed: Bool = false

    var todayCostText: String { String(format: "$%.2f", today.totalCost) }
    var monthCostText: String { String(format: "$%.2f", month.totalCost) }

    var todayTokensCompact: String { Self.compactTokens(today.totalTokens) }
    var monthTokensCompact: String { Self.compactTokens(month.totalTokens) }

    /// Today secondary line: "18.7M tokens · 248 messages" (+ " · retrying" on partial failure).
    var todaySubLine: String {
        let suffix = todayFailed ? L10n.t("today_retrying_suffix") : ""
        return L10n.t("today_tokens_messages", todayTokensCompact, today.totalMessages, suffix)
    }

    static func compactTokens(_ value: Int) -> String {
        if value >= 1_000_000 {
            return String(format: "%.1fM", Double(value) / 1_000_000)
        }
        if value >= 1_000 {
            return "\(value / 1000)k"
        }
        return "\(value)"
    }

    var comparisonText: String? {
        guard let pct = today.comparison?.costPct else { return nil }
        let abs = abs(pct)
        return pct <= 0 ? L10n.t("comparison_below", Int(abs)) : L10n.t("comparison_above", Int(abs))
    }

    var activityText: String? {
        let leadTool = today.byTool?.max(by: { $0.value.cost < $1.value.cost })?.key
        let leadModel = (today.combinedModels ?? today.topModels ?? [])
            .max(by: { $0.cost < $1.cost })
        guard let tool = leadTool, let model = leadModel else { return nil }
        let modelName = model.name.split(separator: "/").last.map(String.init) ?? model.name
        return L10n.t("most_used_today", tool, modelName)
    }

    /// Windows below their low-quota threshold, sorted by remaining ascending.
    var lowQuotaRows: [QuotaRow] {
        guard quota.enabled else { return [] }
        // Flatten allQuotaGroups so each row keeps its provider name and the
        // provider-level Estimated flag. The old allQuotaRows helper dropped both.
        return allQuotaGroups.flatMap { $0.rows }
            .filter { $0.isLow(thresholds: thresholds) }
            .sorted(by: { $0.left < $1.left })
            .prefix(2)
            .map { $0 }
    }

    /// All windows grouped by provider (provider order as detected). A failed provider
    /// is flagged so the All view can render an inline warning above its last-known rows
    /// (spec §7), not a full-surface failure. GROUP failure = status != "ok" OR a non-empty
    /// status_detail (e.g. stale_token, even when status is "ok"); a provider with several
    /// credentials reports the detail for the whole provider, so this stays broad.
    var allQuotaGroups: [(provider: String, rows: [QuotaRow], failed: Bool)] {
        guard quota.enabled else { return [] }
        let providers = quota.providers ?? [:]
        return providers.compactMap { (name, prov) -> (provider: String, rows: [QuotaRow], failed: Bool)? in
            let display = name.capitalized
            let estimated = prov.estimated ?? false
            let failed = !Self.isProviderOk(prov.status) || !(prov.statusDetail?.isEmpty ?? true)
            var rows = (prov.buckets ?? []).compactMap {
                QuotaRow(provider: display, bucket: $0, estimated: estimated,
                         failed: Self.isRowFailed(capturedAt: $0.capturedAt, statusAt: prov.statusAt, groupFailed: failed))
            }
            if name.lowercased() == "antigravity" { rows = Self.antigravityPools(rows) }
            guard !rows.isEmpty else { return nil }
            return (display, rows, failed)
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

    private static func isRowFailed(capturedAt: Int?, statusAt: Int?, groupFailed: Bool) -> Bool {
        guard groupFailed else { return false }
        guard let captured = capturedAt, let status = statusAt else { return true }
        return captured < status
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
        guard provider.lowercased() == "claude" else { return bucket }
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
        switch canonicalBucket {
        case "5h": return L10n.t("window_5h")
        case "weekly": return L10n.t("window_weekly")
        default: return bucketLabel
        }
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

// MARK: - Settings

struct CompanionSettings: Codable {
    static let defaultBaseURL = "http://127.0.0.1:55423"

    var baseURL: String = CompanionSettings.defaultBaseURL
    var launchAtLogin: Bool = false
    var lowQuotaNotifications: Bool = false
    var thresholds: QuotaThresholds = .defaults
    var language: AppLanguage = .system
    /// Update checking is opt-in: the companion contacts no third party until asked.
    var automaticUpdateChecks: Bool = false
    /// Last check ATTEMPT (success or failure) - the 24h throttle reads this.
    var lastUpdateCheckAt: Date? = nil
    /// Last version found newer than this build, and its validated release page. Persisted
    /// so the gear badge survives a relaunch between daily checks.
    var availableUpdateVersion: String? = nil
    var availableUpdateURL: String? = nil
    /// A version the user explicitly skipped; suppresses the badge for that version only.
    var skippedUpdateVersion: String? = nil

    private enum CodingKeys: String, CodingKey {
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
    }

    init(
        baseURL: String = CompanionSettings.defaultBaseURL,
        launchAtLogin: Bool = false,
        lowQuotaNotifications: Bool = false,
        thresholds: QuotaThresholds = .defaults,
        language: AppLanguage = .system,
        automaticUpdateChecks: Bool = false,
        lastUpdateCheckAt: Date? = nil,
        availableUpdateVersion: String? = nil,
        availableUpdateURL: String? = nil,
        skippedUpdateVersion: String? = nil
    ) {
        self.baseURL = baseURL
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
        baseURL = try values.decodeIfPresent(String.self, forKey: .baseURL) ?? Self.defaultBaseURL
        launchAtLogin = try values.decodeIfPresent(Bool.self, forKey: .launchAtLogin) ?? false
        lowQuotaNotifications = try values.decodeIfPresent(Bool.self, forKey: .lowQuotaNotifications) ?? false
        thresholds = try values.decodeIfPresent(QuotaThresholds.self, forKey: .thresholds) ?? .defaults
        language = try values.decodeIfPresent(AppLanguage.self, forKey: .language) ?? .system
        automaticUpdateChecks = try values.decodeIfPresent(Bool.self, forKey: .automaticUpdateChecks) ?? false
        lastUpdateCheckAt = try values.decodeIfPresent(Date.self, forKey: .lastUpdateCheckAt)
        availableUpdateVersion = try values.decodeIfPresent(String.self, forKey: .availableUpdateVersion)
        availableUpdateURL = try values.decodeIfPresent(String.self, forKey: .availableUpdateURL)
        skippedUpdateVersion = try values.decodeIfPresent(String.self, forKey: .skippedUpdateVersion)
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(baseURL, forKey: .baseURL)
        try values.encode(launchAtLogin, forKey: .launchAtLogin)
        try values.encode(lowQuotaNotifications, forKey: .lowQuotaNotifications)
        try values.encode(thresholds, forKey: .thresholds)
        try values.encode(language, forKey: .language)
        try values.encode(automaticUpdateChecks, forKey: .automaticUpdateChecks)
        try values.encodeIfPresent(lastUpdateCheckAt, forKey: .lastUpdateCheckAt)
        try values.encodeIfPresent(availableUpdateVersion, forKey: .availableUpdateVersion)
        try values.encodeIfPresent(availableUpdateURL, forKey: .availableUpdateURL)
        try values.encodeIfPresent(skippedUpdateVersion, forKey: .skippedUpdateVersion)
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
