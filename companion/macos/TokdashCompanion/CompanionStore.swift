import AppKit
import Foundation
import SwiftUI
import ServiceManagement
import UserNotifications

/// Companion store: holds connection state, the decoded snapshot, the refresh
/// scheduler, and settings. All mutations happen on the main actor.
@MainActor
final class CompanionStore: ObservableObject {
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
    private var isOpen = false
    private var scheduler: Timer?
    // Low-quota notification dedup + crossing detection.
    private var notifiedKeys = Set<String>()
    private var prevQuotaLeft: [String: Double] = [:]

    init() {
        let loaded = CompanionSettings.load()
        let url = URL(string: loaded.baseURL) ?? URL(string: "http://127.0.0.1:55423")!
        self.settings = loaded
        self.client = TokdashClient(baseURL: url)
    }

    /// Rebuild the client with a new base URL (called when settings change). Only
    /// accepts an absolute http/https URL; otherwise the current client is kept.
    func updateBaseURL(_ urlString: String) {
        guard let url = URL(string: urlString),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https", url.host != nil else { return }
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
                if let ts = lastToday?.timestamp,
                   let parsed = ISO8601DateFormatter().date(from: ts) {
                    lastDataTime = parsed
                } else {
                    lastDataTime = lastFetchAt
                }
                failures = 0
                partial = todayFailed || monthFailed || quotaFailed // partial -> 15s short retry
                evaluateLowQuotaNotifications(snap)
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

    private var wakeObserver: NSObjectProtocol?

    /// On wake, fire one coalesced refresh (refresh() cancels any in-flight request)
    /// so stale post-sleep data refreshes promptly. Periodic work was naturally paused
    /// while asleep - timers don't fire. Spec §cadence.
    private func observeWake() {
        guard wakeObserver == nil else { return }
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    /// Notify the scheduler the popover opened/closed (changes cadence).
    func setOpen(_ open: Bool) {
        isOpen = open
        reschedule()
    }

    private func reschedule() {
        scheduler?.invalidate()
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

    // MARK: - Low-quota notifications

    /// Notify only on a crossing from above to at-or-below the threshold, evaluated
    /// over ALL windows (not just the displayed top two). Dedup by
    /// (provider, account, bucket, reset epoch, threshold); a new reset epoch re-arms.
    /// Buckets without a reset time are suppressed (spec §7). Not called for offline/
    /// busy (only on a successful health check) or recovery (only above->below).
    private func evaluateLowQuotaNotifications(_ snap: Snapshot) {
        guard settings.lowQuotaNotifications, snap.quota.enabled, !snap.quotaFailed else { return }
        var fresh: [QuotaRow] = []
        var current = Set<String>()
        for r in snap.allQuotaGroups.flatMap({ $0.rows }) {
            guard let resets = r.resetsAt else { continue } // suppress buckets without a reset time
            let epoch = Int(resets.timeIntervalSince1970)
            let stateKey = "\(r.provider)|\(r.account)|\(r.bucket)|\(epoch)"
            current.insert(stateKey)
            let threshold = settings.thresholds.threshold(for: r.bucket)
            let isLow = r.left <= threshold
            if isLow, let prev = prevQuotaLeft[stateKey], prev > threshold {
                let notifyKey = "\(stateKey)|\(threshold)"
                if notifiedKeys.insert(notifyKey).inserted { fresh.append(r) }
            }
            prevQuotaLeft[stateKey] = r.left
        }
        // Re-arm: drop state for windows no longer reported (reset epoch advanced / dropped).
        for k in prevQuotaLeft.keys where !current.contains(k) { prevQuotaLeft.removeValue(forKey: k) }
        if !fresh.isEmpty { postLowQuotaNotification(fresh) }
    }

    private func postLowQuotaNotification(_ rows: [QuotaRow]) {
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = "Tokdash - low quota"
            if rows.count == 1, let r = rows.first {
                content.body = "\(r.provider) \(r.bucketLabel) is at \(Int(r.left))% remaining."
            } else if let r = rows.first {
                content.body = "\(rows.count) subscription windows are low. \(r.provider) \(r.bucketLabel) at \(Int(r.left))%."
            }
            content.userInfo = ["openQuota": true]
            let id = "tokdash-low-quota-\(Date().timeIntervalSince1970)"
            let req = UNNotificationRequest(identifier: id, content: content,
                                            trigger: UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false))
            center.add(req)
        }
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
            return connectionState == .connecting ? "" : "No data yet"
        }
        let age = Date().timeIntervalSince(last)
        var text: String
        if age < 60 { text = "Updated just now" }
        else if age < 3600 { text = "Updated \(Int(age / 60)) min ago" }
        else if age < 86400 { text = "Updated \(Int(age / 3600)) h ago" }
        else { text = "Updated \(Int(age / 86400)) d ago" }
        // Offline/Busy show last-good data - mark it stale.
        if connectionState == .offline || connectionState == .busy { text += " · stale" }
        return text
    }

    /// Live label for the menu-bar item: reflects connection state and usage.
    var tooltipText: String {
        if let snap = snapshot, snap.today.totalTokens > 0 {
            return "Tokdash - Today \(snap.todayCostText) · \(snap.todayTokensCompact) tokens"
        }
        switch connectionState {
        case .connecting: return "Tokdash - connecting…"
        case .connected: return "Tokdash - No usage yet"
        case .busy: return "Tokdash - Busy"
        case .offline: return "Tokdash - Offline"
        case .wrongService: return "Tokdash - Not Tokdash"
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
        case .connecting: return "Connecting…"
        case .connected: return "Local · Connected"
        case .busy: return "Busy"
        case .offline: return "Offline"
        case .wrongService: return "Not Tokdash"
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
        let dir = pct <= 0 ? "below" : "above"
        return String(format: "%d%% %@ yesterday", Int(abs), dir)
    }

    var activityText: String? {
        let leadTool = today.byTool?.max(by: { $0.value.cost < $1.value.cost })?.key
        let leadModel = (today.combinedModels ?? today.topModels ?? [])
            .max(by: { $0.cost < $1.cost })
        guard let tool = leadTool, let model = leadModel else { return nil }
        let modelName = model.name.split(separator: "/").last.map(String.init) ?? model.name
        return "Most used today  \(tool) · \(modelName)"
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
    /// (status != "ok") is flagged so the All view can render an inline warning above
    /// its last-known rows (spec §7), not a full-surface failure.
    var allQuotaGroups: [(provider: String, rows: [QuotaRow], failed: Bool)] {
        guard quota.enabled else { return [] }
        let providers = quota.providers ?? [:]
        return providers.compactMap { (name, prov) -> (provider: String, rows: [QuotaRow], failed: Bool)? in
            let display = name.capitalized
            let estimated = prov.estimated ?? false
            let failed = !Self.isProviderOk(prov.status)
            let rows = (prov.buckets ?? []).compactMap { QuotaRow(provider: display, bucket: $0, estimated: estimated, failed: failed) }
            guard !rows.isEmpty else { return nil }
            return (display, rows, failed)
        }
    }

    // A provider is healthy when its status is absent (older servers) or "ok"; any
    // other value means its quota couldn't be refreshed this cycle. Spec §7.
    private static func isProviderOk(_ status: String?) -> Bool {
        guard let s = status, !s.isEmpty else { return true }
        return s.lowercased() == "ok"
    }
}

struct QuotaRow: Identifiable {
    let id = UUID()
    let provider: String
    let bucket: String
    let bucketLabel: String
    let left: Double
    let resetsAt: Date?
    let estimated: Bool
    let account: String
    let hasPercent: Bool
    let failed: Bool

    init(provider: String, bucket: BucketQuota, estimated: Bool = false, failed: Bool = false) {
        self.provider = provider
        self.bucket = bucket.bucket
        self.bucketLabel = bucket.bucketLabel ?? bucket.bucket
        self.left = bucket.remainingPercent ?? 100
        self.resetsAt = bucket.resetsAt.map { Date(timeIntervalSince1970: TimeInterval($0)) }
        self.estimated = estimated
        self.account = bucket.account ?? ""
        self.hasPercent = bucket.remainingPercent != nil
        self.failed = failed
    }

    func isLow(thresholds: QuotaThresholds) -> Bool {
        hasPercent && left <= thresholds.threshold(for: bucket)
    }

    var resetsText: String {
        guard let resetsAt else { return "" }
        let fmt = DateFormatter()
        let cal = Calendar.current
        if cal.isDateInToday(resetsAt) {
            fmt.dateFormat = "HH:mm"
            return "resets \(fmt.string(from: resetsAt))"
        }
        if cal.isDateInTomorrow(resetsAt) {
            return "resets tomorrow"
        }
        let inWeek = cal.dateInterval(of: .weekOfYear, for: Date())?.contains(resetsAt) ?? false
        fmt.dateFormat = inWeek ? "EEE" : "MMM d"
        return "resets \(fmt.string(from: resetsAt))"
    }
}

// MARK: - Settings

struct CompanionSettings: Codable {
    var baseURL: String = "http://127.0.0.1:55423"
    var launchAtLogin: Bool = false
    var lowQuotaNotifications: Bool = false
    var thresholds: QuotaThresholds = .defaults

    static let defaultsURL: URL = {
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
