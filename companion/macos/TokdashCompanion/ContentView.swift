import SwiftUI

/// The combined spend-first surface: Today hero, month context, quota section
/// (with inline Low/All selector), activity line, action row, freshness footer.
/// One surface, no view switching. Matches the approved UI_CONCEPT.html.
struct ContentView: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        VStack(spacing: 0) {
            HeaderSection()
            Divider().opacity(0.4)
            if showsBanner {
                BannerSection()
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                Divider().opacity(0.4)
            }
            TodayHeroSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            Divider().opacity(0.4)
            MonthContextSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            Divider().opacity(0.4)
            QuotaSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            if let activity = store.snapshot?.activityText, store.connectionState != .connecting {
                Divider().opacity(0.4)
                ActivitySection(text: activity)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
            }
            Divider().opacity(0.4)
            ActionBarSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            Divider().opacity(0.4)
            FreshnessFooter()
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
        }
        .padding(.vertical, 4)
    }

    private var showsBanner: Bool {
        store.connectionState == .offline || store.connectionState == .busy || store.connectionState == .wrongService
    }
}

private struct HeaderSection: View {
    @EnvironmentObject var store: CompanionStore
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "chart.bar.fill")
                .font(.system(size: 13, weight: .semibold))
            Text("Tokdash")
                .font(.system(size: 13, weight: .semibold))
            HStack(spacing: 5) {
                Circle()
                    .fill(store.connectionState.dotColor)
                    .frame(width: 7, height: 7)
                Text(store.connectionState.label)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                openSettings()
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 13))
            }
            .buttonStyle(.plain)
            .help("Settings")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

private struct BannerSection: View {
    @EnvironmentObject var store: CompanionStore
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: store.connectionState == .busy ? "exclamationmark.triangle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(store.connectionState == .busy ? .orange : .red)
                .font(.system(size: 14))
            VStack(alignment: .leading, spacing: 2) {
                Text(bannerTitle)
                    .font(.system(size: 13, weight: .semibold))
                Text(bannerBody)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                // Retry + Settings actions per spec §3 (busy auto-retries, no buttons).
                if store.connectionState == .offline || store.connectionState == .wrongService {
                    HStack(spacing: 8) {
                        if store.connectionState == .offline {
                            Button("Retry") { store.refresh() }
                                .font(.system(size: 12))
                        }
                        Button("Settings") { openSettings() }
                            .font(.system(size: 12))
                    }
                }
            }
            Spacer()
        }
    }

    private var bannerTitle: String {
        switch store.connectionState {
        case .offline: return "Tokdash is not reachable"
        case .busy: return "Tokdash is busy - retrying"
        case .wrongService: return "This address is not a Tokdash service"
        default: return ""
        }
    }

    private var bannerBody: String {
        switch store.connectionState {
        case .offline: return "Start Tokdash, or check the server address in Settings."
        case .busy: return "Last data shown below. Backing off automatically."
        case .wrongService: return "Check that the server address in Settings points at a Tokdash instance."
        default: return ""
        }
    }
}

private struct TodayHeroSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("TODAY")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            if store.connectionState == .connecting && store.snapshot == nil {
                Text("…")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(.tertiary)
            } else if let snap = store.snapshot, snap.today.totalTokens == 0 {
                Text(snap.todayFailed ? "Today's data unavailable" : "No usage recorded today")
                    .font(.system(size: 14, weight: .medium))
                Text(snap.todayFailed ? "Will retry shortly." : "Tokdash is running. Today's totals will appear as tools report usage.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            } else if let snap = store.snapshot {
                Text(snap.todayCostText)
                    .font(.system(size: 30, weight: .semibold))
                    .monospacedDigit()
                Text("\(snap.todayTokensCompact) tokens · \(snap.today.totalMessages) messages\(snap.todayFailed ? " · retrying" : "")")
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
                if let cmp = snap.comparisonText {
                    Text(cmp)
                        .font(.system(size: 12.5))
                        .foregroundStyle(.green)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity((store.connectionState == .offline || store.connectionState == .busy) ? 0.45 : 1)
    }
}

private struct MonthContextSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        HStack(spacing: 10) {
            Text(monthLabel)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            if let snap = store.snapshot {
                if snap.monthFailed && snap.month.totalTokens > 0 {
                    // Keep last-good month visible with a retrying note (don't hide it as "-").
                    Text(snap.monthCostText)
                        .font(.system(size: 12.5, weight: .semibold))
                    Text("\(snap.monthTokensCompact) tokens · retrying")
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                } else if snap.monthFailed {
                    Text("-")
                        .font(.system(size: 12.5, weight: .semibold))
                    Text("retrying")
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                } else {
                    Text(snap.monthCostText)
                        .font(.system(size: 12.5, weight: .semibold))
                    Text("\(snap.monthTokensCompact) tokens")
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                }
            } else if store.connectionState == .connecting {
                Text("…")
                    .foregroundStyle(.tertiary)
            }
            Spacer()
        }
        .opacity((store.connectionState == .offline || store.connectionState == .busy) ? 0.45 : 1)
    }

    private var monthLabel: String {
        let fmt = DateFormatter()
        fmt.dateFormat = "MMMM"
        return fmt.string(from: Date()).uppercased()
    }
}

private struct QuotaSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(store.quotaView == .low ? "SUBSCRIPTION" : "ALL SUBSCRIPTIONS")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .tracking(0.5)
                Spacer()
                Picker("", selection: $store.quotaView) {
                    Text("Low").tag(QuotaView.low)
                    Text("All").tag(QuotaView.all)
                }
                .pickerStyle(.segmented)
                .frame(width: 92)
                .labelsHidden()
            }
            if let snap = store.snapshot {
                if snap.quotaFailed {
                    HStack(spacing: 7) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                            .font(.system(size: 12))
                        Text("Quota data unavailable - will retry shortly.")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button("Retry now") { store.refresh() }
                            .font(.system(size: 12))
                    }
                    // Last-good quota remains visible (dimmed) below the warning.
                    if snap.quota.enabled {
                        Divider().opacity(0.3)
                        quotaBody(for: snap).opacity(0.6)
                    }
                } else if !snap.quota.enabled {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Subscription tracking is off")
                            .font(.system(size: 12.5))
                            .foregroundStyle(.secondary)
                        Button("Open Dashboard") { openDashboard(baseURL: store.settings.baseURL) }
                            .font(.system(size: 12))
                    }
                } else {
                    quotaBody(for: snap)
                }
            } else if store.connectionState == .connecting {
                Text("…")
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity((store.connectionState == .offline || store.connectionState == .busy) ? 0.45 : 1)
    }

    @ViewBuilder
    private func quotaBody(for snap: Snapshot) -> some View {
        if store.quotaView == .low {
            if snap.lowQuotaRows.isEmpty {
                Text("No subscription window is below its alert threshold.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(snap.lowQuotaRows) { row in
                    QuotaRowView(row: row, showProvider: true)
                }
            }
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(snap.allQuotaGroups, id: \.provider) { group in
                        Text(group.provider)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                        // A failed provider shows an inline warning above its last-known
                        // rows, not a full-surface failure (spec §7).
                        if group.failed {
                            HStack(spacing: 6) {
                                Image(systemName: "exclamationmark.triangle.fill")
                                    .foregroundStyle(.orange)
                                    .font(.system(size: 10))
                                Text("Couldn't refresh - showing last known")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        ForEach(group.rows) { row in
                            QuotaRowView(row: row, showProvider: false)
                        }
                    }
                }
            }
            .frame(maxHeight: 172)
        }
    }
}

private struct QuotaRowView: View {
    let row: QuotaRow
    let showProvider: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Text(label)
                    .font(.system(size: 12.5, weight: .medium))
                if row.estimated {
                    Text("Estimated")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 4)
                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(.secondary.opacity(0.4)))
                }
                Spacer()
                if row.hasPercent {
                    Text("\(Int(row.left))% left")
                        .font(.system(size: 12.5, weight: .semibold))
                        .monospacedDigit()
                }
                Text(row.resetsText)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            // Buckets without a remaining_percent render without a bar.
            if row.hasPercent {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 2)
                            .fill(Color.secondary.opacity(0.14))
                        RoundedRectangle(cornerRadius: 2)
                            .fill(barColor)
                            .frame(width: geo.size.width * (row.left / 100).clamped(to: 0...1))
                    }
                }
                .frame(height: 4)
            }
        }
    }

    private var label: String {
        let base = showProvider ? "\(row.provider) · \(row.bucketLabel)" : row.bucketLabel
        // A failed provider's rows get a ⚠ prefix so the Low view (no group header)
        // still signals the warning inline.
        return row.failed ? "⚠ \(base)" : base
    }

    private var barColor: Color {
        if row.left < 25 { return .red }
        if row.left < 50 { return .orange }
        return .green
    }
}

private struct ActivitySection: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ActionBarSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        HStack(spacing: 8) {
            Button {
                openDashboard(baseURL: store.settings.baseURL)
            } label: {
                Label("Open Dashboard", systemImage: "arrow.up.right.square")
            }
            .buttonStyle(.borderedProminent)
            Spacer()
            Button {
                store.refresh()
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.bordered)
            .help("Refresh")
        }
    }
}

private struct FreshnessFooter: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        HStack {
            Text(store.freshnessText)
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
            Spacer()
            Button("Quit") {
                NSApplication.shared.terminate(nil)
            }
            .buttonStyle(.plain)
            .font(.system(size: 11))
            .foregroundStyle(.tertiary)
        }
    }
}

private func openDashboard(baseURL: String) {
    let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
    let urlString = trimmed.isEmpty ? "http://127.0.0.1:55423" : trimmed
    if let url = URL(string: urlString) {
        NSWorkspace.shared.open(url)
    }
}

extension Comparable {
    func clamped(to range: ClosedRange<Self>) -> Self {
        min(max(self, range.lowerBound), range.upperBound)
    }
}
