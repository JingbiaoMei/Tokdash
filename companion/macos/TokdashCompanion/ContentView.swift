import SwiftUI

/// The combined spend-first surface: Today hero, month context, quota section
/// (with inline Low/All selector), activity line, action row, freshness footer.
/// One surface, no view switching. Matches the approved UI_CONCEPT.html.
struct ContentView: View {
    @EnvironmentObject var store: CompanionStore
    @Environment(\.openSettings) private var openSettings

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
        store.connectionState == .offline || store.connectionState == .busy
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

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: store.connectionState == .offline ? "exclamationmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(store.connectionState == .offline ? .red : .orange)
                .font(.system(size: 14))
            VStack(alignment: .leading, spacing: 2) {
                Text(bannerTitle)
                    .font(.system(size: 13, weight: .semibold))
                Text(bannerBody)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
    }

    private var bannerTitle: String {
        switch store.connectionState {
        case .offline: return "Tokdash is not reachable"
        case .busy: return "Tokdash is busy - retrying"
        default: return ""
        }
    }

    private var bannerBody: String {
        switch store.connectionState {
        case .offline: return "Start Tokdash, or check the server address in Settings."
        case .busy: return "Last data shown below. Backing off automatically."
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
                Text("No usage recorded today")
                    .font(.system(size: 14, weight: .medium))
                Text("Tokdash is running. Today's totals will appear as tools report usage.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            } else if let snap = store.snapshot {
                Text(snap.todayCostText)
                    .font(.system(size: 30, weight: .semibold))
                    .monospacedDigit()
                Text("\(snap.todayTokensCompact) tokens · \(snap.today.totalMessages) messages")
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
                Text(snap.monthCostText)
                    .font(.system(size: 12.5, weight: .semibold))
                Text("\(snap.monthTokensCompact) tokens")
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
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
                if !snap.quota.enabled {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Subscription tracking is off")
                            .font(.system(size: 12.5))
                            .foregroundStyle(.secondary)
                        Button("Open Dashboard") { openDashboard() }
                            .font(.system(size: 12))
                    }
                } else if store.quotaView == .low {
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
                                ForEach(group.rows) { row in
                                    QuotaRowView(row: row, showProvider: false)
                                }
                            }
                        }
                    }
                    .frame(maxHeight: 172)
                }
            } else if store.connectionState == .connecting {
                Text("…")
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity((store.connectionState == .offline || store.connectionState == .busy) ? 0.45 : 1)
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
                Text("\(Int(row.left))% left")
                    .font(.system(size: 12.5, weight: .semibold))
                    .monospacedDigit()
                Text(row.resetsText)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
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

    private var label: String {
        showProvider ? "\(row.provider) · \(row.bucketLabel)" : row.bucketLabel
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
                openDashboard()
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

private func openDashboard() {
    if let url = URL(string: "http://127.0.0.1:55423/") {
        NSWorkspace.shared.open(url)
    }
}

extension Comparable {
    func clamped(to range: ClosedRange<Self>) -> Self {
        min(max(self, range.lowerBound), range.upperBound)
    }
}
