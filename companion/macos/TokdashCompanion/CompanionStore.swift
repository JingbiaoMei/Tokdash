import Foundation
import SwiftUI

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

    init() {
        self.settings = CompanionSettings.load()
        let url = URL(string: settings.baseURL) ?? URL(string: "http://127.0.0.1:55423")!
        self.client = TokdashClient(baseURL: url)
    }

    /// Rebuild the client with a new base URL (called when settings change).
    func updateBaseURL(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        Task { await client.updateBaseURL(url) }
        refresh()
    }

    // MARK: - Refresh

    func refresh() {
        refreshTask?.cancel()
        refreshTask = Task { await runRefresh() }
    }

    private func runRefresh() async {
        do {
            let health = try await client.health()
            guard health.service == "tokdash" else {
                connectionState = .wrongService
                return
            }
            connectionState = .connected

            async let today = client.usage(period: "today")
            async let month = client.usage(period: "month")
            async let quota = client.quota()

            let snap = try await Snapshot(
                today: today,
                month: month,
                quota: quota,
                thresholds: settings.thresholds
            )
            if Task.isCancelled { return }
            self.snapshot = snap
            self.lastFetchAt = Date()
            self.lastError = nil
            self.connectionState = .connected
        } catch let error as TokdashError {
            applyError(error)
        } catch {
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

    var freshnessText: String {
        guard let last = lastFetchAt else {
            return connectionState == .connecting ? "" : "No data yet"
        }
        let age = Date().timeIntervalSince(last)
        if age < 60 { return "Updated just now" }
        if age < 3600 { return "Updated \(Int(age / 60)) min ago" }
        if age < 86400 { return "Updated \(Int(age / 3600)) h ago" }
        return "Updated \(Int(age / 86400)) d ago"
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
        return allQuotaRows.filter { $0.isLow(thresholds: thresholds) }
            .sorted(by: { $0.left < $1.left })
            .prefix(2)
            .map { $0 }
    }

    /// All windows grouped by provider (provider order as detected).
    var allQuotaGroups: [(provider: String, rows: [QuotaRow])] {
        guard quota.enabled else { return [] }
        let providers = quota.providers ?? [:]
        return providers.compactMap { (name, prov) -> (String, [QuotaRow])? in
            let rows = (prov.buckets ?? []).compactMap { QuotaRow(provider: name, bucket: $0) }
            guard !rows.isEmpty else { return nil }
            return (name.capitalized, rows)
        }
    }

    var allQuotaRows: [QuotaRow] {
        quota.providers?.values.flatMap { prov in
            (prov.buckets ?? []).compactMap { QuotaRow(provider: "", bucket: $0) }
        } ?? []
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

    init(provider: String, bucket: BucketQuota) {
        self.provider = provider
        self.bucket = bucket.bucket
        self.bucketLabel = bucket.bucketLabel ?? bucket.bucket
        self.left = bucket.remainingPercent ?? 100
        self.resetsAt = bucket.resetsAt.map { Date(timeIntervalSince1970: TimeInterval($0)) }
        self.estimated = false
    }

    func isLow(thresholds: QuotaThresholds) -> Bool {
        left <= thresholds.threshold(for: bucket)
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

struct QuotaThresholds: Codable {
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
