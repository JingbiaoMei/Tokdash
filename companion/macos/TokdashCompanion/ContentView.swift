import SwiftUI

/// The combined spend-first surface: period segment + hero (cost, delta row, top
/// ranks, activity glance), quota section (with inline Low/All selector and the
/// reset-credits row), per-server rows, and action row.
/// One surface, no view switching. Matches the approved UI_DEMO.html.
struct ContentView: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        // No section dividers (review round: flat rows, whitespace only) - the
        // section paddings keep the groups legible without hairlines.
        VStack(spacing: 0) {
            HeaderSection()
            if showsBanner {
                BannerSection()
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
            }
            HeroSection()
                .padding(.horizontal, 16)
                .padding(.top, 4)
                .padding(.bottom, 6)
            QuotaSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
            if let snap = store.snapshot, snap.showPerServerRows, !snap.perServer.isEmpty {
                PerServerSection(snap: snap)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 6)
            }
            ActionBarSection()
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
        }
        .padding(.vertical, 2)
        .contextMenu {
            Button(L10n.t("quit")) { NSApplication.shared.terminate(nil) }
        }
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
        .padding(.vertical, 6)
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
        // Hard width: long offline/wrong-service banner text (any locale) wraps instead
        // of driving the flyout's fitting width past the popover.
        .frame(width: CompanionLayout.popoverWidth - 32, alignment: .leading)
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

/// Compact period switch (Day | Week | Month | Year), styled after the Windows
/// flyout's SegBtn track: label-sized pill buttons, ~2/3 the width of the native
/// segmented control, so the row fits the fixed 268pt content width next to the
/// period kicker. Selection is carried by weight + fill + an accessibility trait,
/// never by color alone.
private struct PeriodSwitch: View {
    @Binding var selection: UsagePeriod

    var body: some View {
        HStack(spacing: 1) {
            ForEach(UsagePeriod.allCases, id: \.self) { period in
                Button { selection = period } label: {
                    pillLabel(period)
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(period == selection ? .isSelected : [])
            }
        }
        .padding(2)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color.secondary.opacity(0.12)))
    }

    // Its own function: the whole builder inlined into the ForEach overran the
    // type-checker (same C-annotated overload error the rank rows hit once before).
    @ViewBuilder
    private func pillLabel(_ period: UsagePeriod) -> some View {
        let selected = period == selection
        let ink: AnyShapeStyle = selected ? AnyShapeStyle(.primary) : AnyShapeStyle(.secondary)
        let fill: Color = selected ? Color(nsColor: .textBackgroundColor) : .clear
        Text(L10n.t(period.segmentKey))
            .font(.system(size: 11, weight: selected ? .semibold : .regular))
            .foregroundStyle(ink)
            .lineLimit(1)
            .minimumScaleFactor(0.9)
            .padding(.horizontal, 3)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 3)
            .background(fill, in: RoundedRectangle(cornerRadius: 5))
            .contentShape(Rectangle())
    }
}

/// A clipped date viewport keeps long month/week labels from displacing the controls.
/// Only overflowing labels move: pause at each end, then scroll back at a readable pace.
struct ScrollingKicker: View {
    let text: String
    var body: some View {
        ScrollingLine(text: text, font: .systemFont(ofSize: 10, weight: .semibold),
                      color: .secondary, tracking: 0.4, height: 14)
            .frame(width: 63)
    }
}

/// One-line viewport shared by date labels and reset-credit notices.
struct ScrollingLine: View {
    let text: String
    let font: NSFont
    let color: Color
    var tracking: CGFloat = 0
    var height: CGFloat = 16
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var startedAt = Date()

    var body: some View {
        GeometryReader { geometry in
            let measured = NSAttributedString(string: text, attributes: [
                .font: font, .kern: tracking
            ]).size().width
            let overflow = max(0, ceil(measured) - geometry.size.width)
            TimelineView(.animation(minimumInterval: 1.0 / 30, paused: overflow == 0 || reduceMotion)) { context in
                Text(text)
                    .font(Font(font))
                    .foregroundStyle(color)
                    .tracking(tracking)
                    .fixedSize()
                    .offset(x: reduceMotion ? 0 : Self.offset(
                        elapsed: context.date.timeIntervalSince(startedAt), overflow: overflow))
                    .frame(width: geometry.size.width, height: height, alignment: .leading)
                    .clipped()
            }
        }
        .frame(height: height)
        .onChange(of: text) { startedAt = Date() }
        .help(text)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text)
    }

    static func offset(elapsed: TimeInterval, overflow: CGFloat) -> CGFloat {
        guard overflow > 0 else { return 0 }
        let pause = 1.4
        let travel = Double(overflow) / 18
        let phase = max(0, elapsed).truncatingRemainder(dividingBy: 2 * (pause + travel))
        if phase < pause { return 0 }
        if phase < pause + travel { return -CGFloat((phase - pause) * 18) }
        if phase < 2 * pause + travel { return -overflow }
        return -overflow + CGFloat((phase - 2 * pause - travel) * 18)
    }
}

/// E12 kicker stepper: two caption-sized chevrons immediately after the hero kicker.
/// ‹ walks the selected granularity's instances into the past, ‹'s mirror walks back
/// toward the present; each is inert (dimmed, disabled) at its bound - the walk-back
/// limit and the present (contract §Instance stepper). Mirrors the Windows flyout's
/// KickerStepBtn.
private struct KickerStepper: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        HStack(spacing: 2) {
            stepButton(systemName: "chevron.left", enabled: store.canStepEarlier,
                       name: L10n.t("step_earlier")) { store.stepPeriod(-1) }
            stepButton(systemName: "chevron.right", enabled: store.canStepLater,
                       name: L10n.t("step_later")) { store.stepPeriod(1) }
        }
        .fixedSize()
    }

    // Its own function: keeps the ForEach-free builder off the type-checker's
    // overloaded-expression limit (same trap the PeriodSwitch pills hit).
    private func stepButton(systemName: String, enabled: Bool, name: String,
                            action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 8, weight: .semibold))
                .foregroundStyle(.secondary)
                .frame(width: 12, height: 12)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 0.95 : 0.3)
        .accessibilityLabel(name)
        .help(name)
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
                HStack(spacing: 5) {
                    ScrollingKicker(text: store.snapshot?.kickerText ?? store.currentKickerText)
                    KickerStepper()
                }
                .frame(width: 94, alignment: .leading)
                // Reserve the switch's width independently of the date and selection.
                PeriodSwitch(selection: Binding(get: { store.selectedPeriod },
                                                set: { store.selectPeriod($0) }))
                    .frame(width: 166)
            }
            if let snap = store.snapshot {
                heroBody(snap)
            } else if store.connectionState == .connecting {
                skeleton
            }
        }
        // Hard width, not maxWidth: the hero contains GeometryReader rows (share bars),
        // which report a tiny ideal size and let unbounded one-line text ideals (sub-line,
        // delta row) drive the flyout's fitting width instead of the popover width -
        // long week/month rows measured wider than 300pt and the whole body rendered
        // offset inside the panel, text clipped at both borders (user-visible d/w/m/y bug).
        // A concrete width forces every child to lay out at the popover's real content
        // width in EVERY layout pass, so the texts wrap and the bars size from real space.
        .frame(width: CompanionLayout.popoverWidth - 32, alignment: .leading)
        .opacity((store.connectionState == .offline || store.connectionState == .busy) ? 0.45 : 1)
    }

    @ViewBuilder
    private func heroBody(_ snap: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            if snap.usageLoading {
                skeleton
                if snap.components.topRanks {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(0..<3) { _ in
                            RoundedRectangle(cornerRadius: 3)
                                .fill(.quaternary).frame(height: 12)
                        }
                    }.padding(.top, 8).accessibilityHidden(true)
                }
                if snap.components.activityGlance &&
                    (snap.period == .month || snap.period == .year || snap.components.activityHistogramTodayWeek) {
                    RoundedRectangle(cornerRadius: 4)
                        .fill(.quaternary).frame(height: 48)
                        .padding(.top, 8).accessibilityHidden(true)
                }
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
                    // One line, slightly scaled if long: a wrap that splits "3 h 12 m"
                    // across lines read as broken; the flyout is fixed-width so the
                    // scale range is bounded in practice.
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                    .fixedSize(horizontal: false, vertical: true)
                deltaLine(snap)
                if snap.components.topRanks && (!snap.topTools.isEmpty || !snap.topModels.isEmpty) {
                    VStack(alignment: .leading, spacing: 7) {
                        if !snap.topTools.isEmpty { RankBlock(kicker: snap.toolsKickerText, entries: snap.topTools) }
                        if !snap.topModels.isEmpty { RankBlock(kicker: snap.modelsKickerText, entries: snap.topModels, showLogo: false) }
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
            // Full delta row (E1): down green / up red / flat grey, period sentence after.
            // A null metric is omitted; all-null hides the row. ONE concatenated Text, not an
            // HStack: segments keep their own colors, and the line re-wraps as a single
            // paragraph (week/month rows must wrap to a second line, not overflow).
            Self.deltaText(pieces: pieces, sentence: snap.deltaSentence)
                .font(.system(size: 12))
                .monospacedDigit()
                .fixedSize(horizontal: false, vertical: true)
        } else if let line = snap.comparisonLine, let direction = snap.comparisonDirection {
            // fullDeltaRow off: the shipped cost-only line, now period-following.
            Text(line)
                .font(.system(size: 12.5))
                .foregroundStyle(Self.pieceColor(direction))
        }
    }

    private static func deltaText(pieces: [Snapshot.DeltaPiece], sentence: String) -> Text {
        var line = Text("")
        for (i, piece) in pieces.enumerated() {
            if i > 0 { line = line + Text(" · ").foregroundColor(.secondary) }
            line = line + Text(piece.text).foregroundColor(pieceColor(piece.direction))
        }
        return line + Text(" " + sentence).foregroundColor(.secondary)
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

/// Top-3 list under the hero (E3): fixed name cell (optional 12px mark lives INSIDE it),
/// share bar, then percent directly after the bar and the compact token amount trailing.
/// Model rows never carry logos and sit flush; the fixed cell gives both strips the same
/// bar start, so tools and models bars line up with each other.
private struct RankBlock: View {
    let kicker: String
    let entries: [Snapshot.RankEntry]
    // Tools own the marks; model rows have none and render flush to the leading edge.
    // A tool row that ships no mark still reserves the slot inside the cell, so tool
    // names stay aligned with each other.
    var showLogo: Bool = true

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(kicker)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.4)
            ForEach(entries) { entry in
                rankRow(entry)
            }
        }
    }

    // One row, its own function: the full builder inlined in ForEach overruns the
    // type-checker (C-annotated overload error on the whole list).
    @ViewBuilder
    private func rankRow(_ entry: Snapshot.RankEntry) -> some View {
        HStack(spacing: 6) {
            // Fixed name cell WITH the mark inside: a collapsed mark costs no column, so
            // the bar's leading edge is identical in the tools and models strips and
            // model names sit flush at the section's left edge.
            HStack(spacing: 6) {
                if showLogo {
                    if let asset = entry.logoAsset, let ns = NSImage(named: asset) {
                        // Missing assets degrade to text-only, never a broken-image box.
                        logo(asset: asset, ns: ns)
                    } else {
                        // A frame on an EMPTY Group collapses, so the placeholder is a real view.
                        Color.clear.frame(width: 12, height: 12)
                    }
                }
                Text(entry.label)
                    .font(.system(size: 11.5, weight: .medium))
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            .frame(width: 100, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.secondary.opacity(0.14))
                    Capsule().fill(Color.accentColor)
                        .frame(width: geo.size.width * entry.fraction)
                }
            }
            // Year rows carry long values and long model names alike: the bar keeps a
            // minimum width instead of being squeezed out by the trailing text.
            .frame(minWidth: 20)
            .frame(height: 3)
            // Pct rides the bar's end; the token amount trails. Fixed widths (narrower
            // than before - bar got the difference) keep every bar the same length.
            Text(entry.pctText)
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .monospacedDigit()
                .frame(width: 30, alignment: .trailing)
            Text(entry.valueText)
                .font(.system(size: 11.5))
                .foregroundStyle(.tertiary)
                .monospacedDigit()
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: 52, alignment: .trailing)
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
                // Hour axis: 24 columns can't each carry a label at flyout width, so
                // label every 6th hour plus the last column - same sparse axis as the
                // Windows flyout. Blank labels keep the column grid aligned under the bars.
                HStack(spacing: 2) {
                    ForEach(0..<24, id: \.self) { i in
                        // lineLimit+fixedSize: a two-digit label is wider than one 24-way
                        // column; without them SwiftUI compresses "12"/"18" to two wrapped
                        // lines (caught in the real-window evidence capture).
                        Text([0, 6, 12, 18, 23].contains(i) ? "\(i)" : "")
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .fixedSize()
                            .frame(maxWidth: .infinity)
                    }
                }
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
        // Hard width: a long server label must never widen the flyout past the popover.
        .frame(width: CompanionLayout.popoverWidth - 32, alignment: .leading)
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
                        Button(L10n.t("open_dashboard")) { openDashboard(baseURL: store.dashboardBaseURL) }
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
        // Hard width for the same reason as HeroSection: one unbounded row ideal must
        // never widen the whole flyout past the popover.
        .frame(width: CompanionLayout.popoverWidth - 32, alignment: .leading)
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
                    // Multi-server payloads sectionize by server (contract §All view):
                    // one muted header per server, bare provider groups under it.
                    // Single-server = one header-less section - looks exactly as before.
                    ForEach(snap.allQuotaServerSections, id: \.server) { section in
                        if !section.server.isEmpty {
                            Text(section.server.uppercased())
                                .font(.system(size: 10, weight: .semibold))
                                .tracking(0.4)
                                .foregroundStyle(.secondary)
                        }
                        ForEach(section.groups, id: \.provider) { group in
                        // Mock design: 14px provider mark + name. Providers without a
                        // shipped mark (commandcode) render text-only, never a placeholder.
                        let bare = group.serverLabel.isEmpty ? group.provider
                            : (group.provider.components(separatedBy: " · ").last ?? group.provider)
                        HStack(spacing: 6) {
                            if let asset = CompanionStore.quotaLogoAssetName(for: group.canonicalProvider),
                               let ns = NSImage(named: asset) {
                                quotaLogo(asset: asset, ns: ns)
                            }
                            Text(bare)
                                .font(.system(size: 12, weight: .semibold))
                        }
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
                        // Reset credits (E2): the quiet amber row lives under its
                        // provider group (Codex, Claude Code) in the All view only - the
                        // Low view is a window context, not a provider context. Gated
                        // inside creditsNotice (component, quota.enabled, credits
                        // present, available_count >= 1).
                        if let note = snap.creditsNotice(providerDisplay: bare,
                                                         canonicalProvider: group.canonicalProvider,
                                                         resetCredits: group.providerEntry?.resetCredits) {
                            HStack(alignment: .center, spacing: 8) {
                                ScrollingLine(text: note, font: .systemFont(ofSize: 11.5),
                                              color: Color(red: 0.72, green: 0.48, blue: 0.05))
                                // Static decoration, not part of the pinned row string
                                // (COMPANION_API.md "Reset credits").
                                Text(L10n.t("credits_use_or_lose"))
                                    .font(.system(size: 10))
                                    .foregroundStyle(.secondary)
                                    .fixedSize()
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
                        }   // ForEach(section.groups) - group content kept at one indent step for a smaller diff
                    }       // ForEach(allQuotaServerSections)
                }
            }
            .frame(minHeight: CompanionLayout.quotaMinHeight, maxHeight: CompanionLayout.quotaMaxHeight)
        }
    }

    /// Provider mark for the All-view group header, 14pt tall like the demo. Dark-ink marks
    /// (codex, grok) render as templates so they follow the label color - the same rule the
    /// web dashboard applies in dark; brand-colored marks keep their original pixels.
    /// Mirrors RankBlock.logo at the All-view scale.
    @ViewBuilder
    private func quotaLogo(asset: String, ns: NSImage) -> some View {
        if asset == "AgentCodex" || asset == "AgentGrok" {
            Image(nsImage: ns)
                .resizable()
                .renderingMode(.template)
                .aspectRatio(contentMode: .fit)
                .frame(height: 14)
                .foregroundStyle(.primary)
        } else {
            Image(nsImage: ns)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(height: 14)
        }
    }
}

private struct QuotaRowView: View {
    let row: QuotaRow
    let showProvider: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            // Mock layout: the reset time sits right after the name (it describes the
            // window, not the bar), the percentage anchors the trailing edge.
            HStack(spacing: 8) {
                Text(label)
                    .font(.system(size: 12.5, weight: .medium))
                    .lineLimit(1)
                    .truncationMode(.tail)
                if row.estimated {
                    Text(L10n.t("estimated"))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 4)
                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(.secondary.opacity(0.4)))
                        // Never wrap the pill ("Estima / ted"): it keeps its intrinsic
                        // width and the row name yields the space instead.
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                }
                Text(row.resetsText)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    // Reset text wins the space fight: an absolute "resets Sat 19:25"
                    // cut to "resets S…" is worse than a long window name ellipsizing.
                    .layoutPriority(1)
                Spacer(minLength: 8)
                if row.hasPercent {
                    Text(L10n.t("percent_left", Int(row.left)))
                        .font(.system(size: 12.5, weight: .semibold))
                        .monospacedDigit()
                        .layoutPriority(2)  // the number never truncates; the name does
                }
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

    /// Fixed status ramp from the UI demo (identical in light and dark, so a red bar
    /// means the same thing on every surface): fine #30A74C, mid #FF9F0A, low #FF453A.
    /// Same tier boundaries as Formatter.QuotaBarClass / Windows QuotaBarColor.
    private var barColor: Color {
        if row.left < 25 { return Color(red: 255/255, green: 69/255, blue: 58/255) }
        if row.left < 50 { return Color(red: 255/255, green: 159/255, blue: 10/255) }
        return Color(red: 48/255, green: 167/255, blue: 76/255)
    }
}

private struct ActionBarSection: View {
    @EnvironmentObject var store: CompanionStore

    var body: some View {
        HStack(spacing: 8) {
            Button {
                openDashboard(baseURL: store.dashboardBaseURL)
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
