import SwiftUI

/// The combined spend-first surface: period segment + hero (cost, delta row, top
/// ranks, activity glance), quota section (with inline Low/All selector and the
/// reset-credits row), per-server rows, action row, freshness footer.
/// One surface, no view switching. Matches the approved UI_DEMO.html.
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
            HeroSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            Divider().opacity(0.4)
            QuotaSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            if let snap = store.snapshot, snap.showPerServerRows, !snap.perServer.isEmpty {
                Divider().opacity(0.4)
                PerServerSection(snap: snap)
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

    var body: some View {
        HStack(spacing: 8) {
            Image("MenuBarIcon")
                .resizable()
                .renderingMode(.template)
                .scaledToFit()
                .frame(width: 14, height: 15)
            Text("Tokdash")
                .font(.system(size: 13, weight: .semibold))
            HStack(spacing: 5) {
                Circle()
                    .fill(store.connectionState.dotColor)
                    .frame(width: 7, height: 7)
                Text(store.connectionLabel)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            SettingsLink {
                Image(systemName: "gearshape")
                    .font(.system(size: 13))
                    // Static dot, deliberately not animated: this is an ambient "there's
                    // something in Settings" marker, not an alert. Drawn as an overlay so
                    // it can't shift the header's layout when it appears.
                    .overlay(alignment: .topTrailing) {
                        if store.showsUpdateBadge {
                            Circle()
                                .fill(Color.red)
                                .frame(width: 6, height: 6)
                                .offset(x: 3, y: -2)
                        }
                    }
            }
            .buttonStyle(.plain)
            .help(store.settingsAccessibilityLabel)
            // The dot means nothing to VoiceOver, so the label carries it instead.
            .accessibilityLabel(store.settingsAccessibilityLabel)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

private struct BannerSection: View {
    @EnvironmentObject var store: CompanionStore

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
                            Button(L10n.t("retry")) { store.refresh() }
                                .font(.system(size: 12))
                        }
                        SettingsLink {
                            Text(L10n.t("settings"))
                        }
                        .font(.system(size: 12))
                    }
                }
            }
            Spacer()
        }
    }

    private var bannerTitle: String {
        switch store.connectionState {
        case .offline: return L10n.t("banner_offline_title")
        case .busy: return L10n.t("banner_busy_title")
        case .wrongService: return L10n.t("banner_wrong_title")
        default: return ""
        }
    }

    private var bannerBody: String {
        switch store.connectionState {
        case .offline: return L10n.t("banner_offline_body")
        case .busy: return L10n.t("banner_busy_body")
        case .wrongService: return L10n.t("banner_wrong_body")
        default: return ""
        }
    }
}

/// Period segment (always visible) + hero + delta row + top ranks + activity glance.
/// The whole usage side shows one skeleton while the selected period's first fetch is
/// in flight; quota and connectivity live in their own sections, untouched (rule 2).
private struct HeroSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(L10n.t(store.selectedPeriod.kickerKey))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .tracking(0.5)
                    .fixedSize()
                Spacer()
                // Today | Week | Month | Year - persisted selection, fires the fetch group
                // via selectPeriod, and stays visible in EVERY state (loading too).
                // Small control size: at the shipped 300pt width the regular-size
                // 4-segment picker leaves the kicker column ~20pt and it collapses to
                // one character per line (seen in the real-system evidence render).
                Picker("", selection: Binding(get: { store.selectedPeriod },
                                              set: { store.selectPeriod($0) })) {
                    ForEach(UsagePeriod.allCases, id: \.self) { period in
                        Text(L10n.t(period.segmentKey)).tag(period)
                    }
                }
                .pickerStyle(.segmented)
                .controlSize(.small)
                .labelsHidden()
                .fixedSize()
            }
            if let snap = store.snapshot {
                heroBody(snap)
            } else if store.connectionState == .connecting {
                skeleton
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity((store.connectionState == .offline || store.connectionState == .busy) ? 0.45 : 1)
    }

    @ViewBuilder
    private func heroBody(_ snap: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            if snap.usageLoading {
                skeleton
            } else if snap.usage == nil || snap.isEmptyUsage {
                // No data at all this period: failure title or the quiet empty state.
                Text(snap.usageFailed
                        ? L10n.t(snap.period.unavailableKey)
                        : L10n.t("no_usage_period", L10n.t(snap.period.suffixKey)))
                    .font(.system(size: 14, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
                Text(snap.usageFailed ? L10n.t("will_retry_shortly") : L10n.t("tokdash_running"))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(snap.costText)
                    .font(.system(size: 30, weight: .semibold))
                    .monospacedDigit()
                Text(snap.subLine)
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
                deltaLine(snap)
                if snap.components.topRanks && (!snap.topTools.isEmpty || !snap.topModels.isEmpty) {
                    VStack(alignment: .leading, spacing: 7) {
                        if !snap.topTools.isEmpty { RankBlock(kicker: snap.toolsKickerText, entries: snap.topTools) }
                        if !snap.topModels.isEmpty { RankBlock(kicker: snap.modelsKickerText, entries: snap.topModels) }
                    }
                    .padding(.top, 8)
                }
                if snap.components.activityGlance, let face = snap.glanceFace {
                    GlanceSection(face: face)
                        .padding(.top, 8)
                }
            }
        }
    }

    @ViewBuilder
    private func deltaLine(_ snap: Snapshot) -> some View {
        if let pieces = snap.deltaPieces {
            // Full delta row (E1): per-metric spans, down green / up red / flat grey,
            // period sentence after. A null metric is omitted; all-null hides the row.
            HStack(spacing: 0) {
                ForEach(pieces.indices, id: \.self) { i in
                    if i > 0 {
                        Text(" · ").font(.system(size: 12)).foregroundStyle(.tertiary)
                    }
                    Text(pieces[i].text)
                        .font(.system(size: 12))
                        .monospacedDigit()
                        .foregroundStyle(Self.pieceColor(pieces[i].direction))
                }
                Text(" " + snap.deltaSentence)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            .fixedSize(horizontal: false, vertical: true)
        } else if let line = snap.comparisonLine, let direction = snap.comparisonDirection {
            // fullDeltaRow off: the shipped cost-only line, now period-following.
            Text(line)
                .font(.system(size: 12.5))
                .foregroundStyle(Self.pieceColor(direction))
        }
    }

    private var skeleton: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("…")
                .font(.system(size: 30, weight: .semibold))
                .foregroundStyle(.tertiary)
            Text("…")
                .font(.system(size: 12.5))
                .foregroundStyle(.tertiary)
        }
    }

    static func pieceColor(_ direction: Int) -> Color {
        if direction < 0 { return .green }
        if direction > 0 { return .red }
        return .secondary
    }
}

/// Top-3 list under the hero (E3): optional 12px mark, name, share bar, compact value.
/// Model rows never carry logos; a tool without a shipped mark renders text-only.
private struct RankBlock: View {
    let kicker: String
    let entries: [Snapshot.RankEntry]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(kicker)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.4)
            ForEach(entries) { entry in
                HStack(spacing: 6) {
                    if let asset = entry.logoAsset, let ns = NSImage(named: asset) {
                        // Missing assets degrade to text-only, never a broken-image box.
                        logo(asset: asset, ns: ns)
                    }
                    Text(entry.label)
                        .font(.system(size: 11.5, weight: .medium))
                        .lineLimit(1)
                        .frame(width: 96, alignment: .leading)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.secondary.opacity(0.14))
                            Capsule().fill(Color.accentColor)
                                .frame(width: max(2, geo.size.width * entry.fraction))
                        }
                    }
                    .frame(height: 3)
                    Text(entry.valueText)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
        }
    }

    @ViewBuilder
    private func logo(asset: String, ns: NSImage) -> some View {
        // The Codex mark ships as a black silhouette on transparency: template rendering
        // makes it follow the label color (same rule the web dashboard applies in dark).
        // The remaining marks are opaque or brand-colored - templating them would render
        // solid silhouettes - so they keep their original pixels.
        if asset == "AgentCodex" {
            Image(nsImage: ns)
                .resizable()
                .renderingMode(.template)
                .aspectRatio(contentMode: .fit)
                .frame(width: 12, height: 12)
                .foregroundStyle(.primary)
        } else {
            Image(nsImage: ns)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(width: 12, height: 12)
        }
    }
}

/// Activity glance (E9): hour bars / Mon-Sun day columns / 90-180 day grid.
private struct GlanceSection: View {
    let face: Snapshot.GlanceFace
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(kicker)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.4)
            switch face {
            case .hours(let bars, let peakHour):
                barsView(values: bars)
                if let peak = peakHour {
                    Text(L10n.t("peak_caption", peak))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                }
            case .days(let tokens):
                barsView(values: tokens)
                HStack(spacing: 2) {
                    ForEach(["wd_mon", "wd_tue", "wd_wed", "wd_thu", "wd_fri", "wd_sat", "wd_sun"], id: \.self) { key in
                        Text(L10n.t(key))
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity)
                    }
                }
            case .grid(let columns, _, let windowDays, _):
                gridView(columns: columns, windowDays: windowDays)
            }
        }
    }

    private var kicker: String {
        switch face {
        case .hours: return L10n.t("glance_kicker_hours")
        case .days: return L10n.t("glance_kicker_days")
        case .grid(_, _, let windowDays, _):
            return L10n.t(windowDays == 90 ? "glance_kicker_90" : "glance_kicker_180")
        }
    }

    /// 24 hour bars or 7 day columns. All-zero faces never reach the view (the face is
    /// nil); zero bars here are the GAPS of a partly-filled week, kept as invisible
    /// stubs so the remaining columns keep their weekday position.
    private func barsView(values: [Int]) -> some View {
        let maxVal = max(values.max() ?? 1, 1)
        return GeometryReader { geo in
            HStack(alignment: .bottom, spacing: 2) {
                ForEach(values.indices, id: \.self) { i in
                    RoundedRectangle(cornerRadius: 1)
                        .fill(values[i] > 0 ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(Color.clear))
                        .frame(height: values[i] > 0
                                ? max(2, geo.size.height * CGFloat(values[i]) / CGFloat(maxVal))
                                : 2)
                        .frame(maxWidth: .infinity)
                }
            }
        }
        .frame(height: 26)
    }

    private func gridView(columns: [[Int?]], windowDays: Int) -> some View {
        let cell: CGFloat = windowDays == 90 ? 9 : 5
        let gap: CGFloat = windowDays == 90 ? 3 : 2
        return HStack(alignment: .top, spacing: gap) {
            ForEach(columns.indices, id: \.self) { c in
                VStack(spacing: gap) {
                    ForEach(0..<7, id: \.self) { r in
                        let v = r < columns[c].count ? columns[c][r] : nil
                        RoundedRectangle(cornerRadius: windowDays == 90 ? 2 : 1)
                            .fill(v == nil
                                    ? AnyShapeStyle(Color.clear)
                                    : AnyShapeStyle(GlancePalette.cellColor(intensity: v!, dark: colorScheme == .dark)))
                            .frame(width: cell, height: cell)
                    }
                }
            }
        }
    }
}

/// Grid intensity ramp from the approved mock, light and dark variants.
private enum GlancePalette {
    // rgba(60,60,67,0.08) #A6D8B0 #5BB977 #166F37
    private static let light: [(Double, Double, Double, Double)] = [
        (60 / 255, 60 / 255, 67 / 255, 0.08),
        (166 / 255, 216 / 255, 176 / 255, 1),
        (91 / 255, 185 / 255, 119 / 255, 1),
        (22 / 255, 111 / 255, 55 / 255, 1),
    ]
    // rgba(235,235,245,0.10) #4CAE68 #30A74C #32D74B
    private static let dark: [(Double, Double, Double, Double)] = [
        (235 / 255, 235 / 255, 245 / 255, 0.10),
        (76 / 255, 174 / 255, 104 / 255, 1),
        (48 / 255, 167 / 255, 76 / 255, 1),
        (50 / 255, 215 / 255, 75 / 255, 1),
    ]

    static func cellColor(intensity: Int, dark: Bool) -> Color {
        let ramp = dark ? Self.dark : Self.light
        // intensity 4 (peak quartile server-side) clamps to the darkest shipped step.
        let c = ramp[min(max(intensity, 0), 3)]
        return Color(.sRGB, red: c.0, green: c.1, blue: c.2, opacity: c.3)
    }
}

/// Per-server rows (E10), from the SAME fan-out as the hero - never an extra request.
private struct PerServerSection: View {
    let snap: Snapshot

    @EnvironmentObject var store: CompanionStore

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(L10n.t("per_server_kicker", L10n.t(snap.period.suffixKey)))
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.4)
                .textCase(.uppercase)
            ForEach(snap.perServerRows) { row in
                HStack(spacing: 8) {
                    Text(row.label)
                        .font(.system(size: 12))
                        .lineLimit(1)
                    Spacer()
                    Text(row.valueText)
                        .font(.system(size: 12))
                        .monospacedDigit()
                        // "unreachable" is plain secondary text, never dimmed numbers.
                        .foregroundStyle(.secondary)
                }
            }
            Text(L10n.t("per_server_footnote"))
                .font(.system(size: 10.5))
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity((store.connectionState == .offline || store.connectionState == .busy) ? 0.45 : 1)
    }
}

private struct QuotaSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(store.quotaView == .low ? L10n.t("subscription") : L10n.t("all_subscriptions"))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .tracking(0.5)
                Spacer()
                Picker("", selection: $store.quotaView) {
                    Text(L10n.t("low")).tag(QuotaView.low)
                    Text(L10n.t("all")).tag(QuotaView.all)
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
                        Text(L10n.t("quota_unavailable"))
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button(L10n.t("retry_now")) { store.refresh() }
                            .font(.system(size: 12))
                    }
                    // Last-good quota remains visible (dimmed) below the warning.
                    if snap.quota.enabled {
                        Divider().opacity(0.3)
                        quotaBody(for: snap).opacity(0.6)
                    }
                } else if !snap.quota.enabled {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(L10n.t("tracking_off"))
                            .font(.system(size: 12.5))
                            .foregroundStyle(.secondary)
                        Button(L10n.t("open_dashboard")) { openDashboard(baseURL: store.settings.baseURL) }
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
                Text(L10n.t("no_low_windows"))
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
                                Text(L10n.t("couldnt_refresh"))
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        ForEach(group.rows) { row in
                            QuotaRowView(row: row, showProvider: false)
                        }
                        // Reset credits (E2): the quiet amber row lives under the Codex
                        // group in the All view only - the Low view is a window context,
                        // not a provider context. Gated inside creditsNotice (component,
                        // quota.enabled, codex, available_count >= 1).
                        if let note = snap.creditsNotice(providerDisplay: group.provider,
                                                         canonicalProvider: group.canonicalProvider,
                                                         resetCredits: group.providerEntry?.resetCredits) {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(note)
                                    .font(.system(size: 11.5))
                                Spacer(minLength: 8)
                                // Static decoration, not part of the pinned row string
                                // (COMPANION_API.md "Reset credits").
                                Text(L10n.t("credits_use_or_lose"))
                                    .font(.system(size: 10))
                                    .foregroundStyle(.secondary)
                            }
                            .foregroundStyle(Color(red: 0.72, green: 0.48, blue: 0.05))
                            .padding(.leading, 7)
                            .padding(.vertical, 2)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .overlay(alignment: .leading) {
                                Rectangle()
                                    .fill(Color.orange.opacity(0.55))
                                    .frame(width: 2)
                            }
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .frame(minHeight: CompanionLayout.quotaMinHeight, maxHeight: CompanionLayout.quotaMaxHeight)
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
                    Text(L10n.t("estimated"))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 4)
                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(.secondary.opacity(0.4)))
                }
                Spacer()
                if row.hasPercent {
                    Text(L10n.t("percent_left", Int(row.left)))
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
        let bucketLabel = row.displayBucketLabel
        let base = showProvider ? "\(row.provider) · \(bucketLabel)" : bucketLabel
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

private struct ActionBarSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        HStack(spacing: 8) {
            Button {
                openDashboard(baseURL: store.settings.baseURL)
            } label: {
                Label(L10n.t("open_dashboard"), systemImage: "arrow.up.right.square")
            }
            .buttonStyle(.borderedProminent)
            Spacer()
            Button {
                store.refresh()
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.bordered)
            .help(L10n.t("refresh"))
        }
    }
}

private struct FreshnessFooter: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        HStack {
            Text(store.freshnessText)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            Spacer()
            Button(L10n.t("quit")) {
                NSApplication.shared.terminate(nil)
            }
            .buttonStyle(.plain)
            .font(.system(size: 11))
            .foregroundStyle(.secondary)
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
