import XCTest
import SwiftUI
import AppKit
@testable import TokdashCompanion

/// v1.1 contract loader + unit tests.
///
/// `testEveryExpectedCaseFile` walks EVERY file in `contract/expected/` and asserts
/// its pinned strings against the pure v1.1 surfaces (Snapshot formatting, the delta
/// row, the ranks, the credits row, the glance faces, the per-server rows) and the
/// English L10n table. Fixture slots `null` / `"503"` / `"pending"` are decoded as
/// "endpoint returned nothing" / "endpoint failed" / "still in flight".
///
/// Deterministic clock: snapshots are built with `now` frozen to the quota payload's
/// `timestamp` (contract §Reset credits), and every calendar computation runs in a
/// fixed UTC Gregorian calendar so CI TZ cannot flip a Monday.
final class ContractV11Tests: XCTestCase {

    override class func setUp() {
        super.setUp()
        TestSettings.install()
    }

    override func setUp() {
        super.setUp()
        L10n.current = .english
    }

    // MARK: - Loaders

    private var utcCalendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        c.firstWeekday = 1 // Sunday-first, like every en_US calendar; Monday math is weekday-arithmetic based
        return c
    }

    /// Every case fixture ends Sun 2026-07-26; the week face anchors on the clock, so
    /// tests freeze it to the fixture timestamp (the contract's clock convention).
    private var frozenNow: Date {
        try! XCTUnwrap(CompanionStore.date(fromDayString: "2026-07-26", calendar: utcCalendar))
    }

    /// The glance face for a loaded snapshot, pinned to the fixed UTC calendar.
    /// Snapshot's production accessor uses the machine calendar (mirroring Windows,
    /// whose live clock carries the user's offset); asserting through the pure
    /// static here keeps the pinned faces CI-timezone-immune.
    private func face(of snap: Snapshot) -> Snapshot.GlanceFace? {
        CompanionStore.glanceFace(period: snap.period, insights: snap.insights, stats: snap.stats,
                                  components: snap.components, calendar: utcCalendar, now: snap.now)
    }

    private func expectedDocument(_ name: String) throws -> [String: Any] {
        let data = try Data(contentsOf: contractURL("expected/\(name).json"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func fixtureData(_ name: String) throws -> Data {
        try Data(contentsOf: contractURL("fixtures/\(name)"))
    }

    private func decodeFixture<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        try JSONDecoder().decode(type, from: try fixtureData(name))
    }

    /// Fixture-slot decode: nil/"503"/"pending" all yield nil (nothing landed this cycle).
    private func slotName(_ doc: [String: Any], _ key: String) -> String? {
        guard let fixtures = doc["fixtures"] as? [String: Any] else { return nil }
        let raw = fixtures[key]
        if raw is NSNull { return nil }
        guard let name = raw as? String else { return nil }
        if name == "503" || name == "pending" { return nil }
        return name
    }

    /// A slot is FAILED for both "503" and JSON null - contract §Expected behavior
    /// cases: "null means the endpoint returns nothing at all this cycle (treated as
    /// a failure)". Mirrors the Windows loader (JSON null -> "fail").
    private func slotFailed(_ doc: [String: Any], _ key: String) -> Bool {
        let raw = (doc["fixtures"] as? [String: Any])?[key]
        if raw is NSNull { return true }
        return raw as? String == "503"
    }

    private func period(of doc: [String: Any]) -> UsagePeriod {
        UsagePeriod(rawValue: doc["period"] as? String ?? "today") ?? .today
    }

    /// settings_overrides.components merged over the all-on v3 defaults.
    private func components(of doc: [String: Any]) throws -> CompanionComponents {
        var comps = CompanionComponents()
        let overrides = ((doc["settings_overrides"] as? [String: Any])?["components"]) as? [String: Bool] ?? [:]
        for (key, value) in overrides {
            switch key {
            case "fullDeltaRow": comps.fullDeltaRow = value
            case "topRanks": comps.topRanks = value
            case "resetCredits": comps.resetCredits = value
            case "activityGlance": comps.activityGlance = value
            case "activityHistogramTodayWeek": comps.activityHistogramTodayWeek = value
            case "perServerRows": comps.perServerRows = value
            default: XCTFail("unknown component key in settings_overrides: \(key)")
            }
        }
        return comps
    }

    /// Builds the observable snapshot for a single-server case document.
    private func snapshot(for doc: [String: Any]) throws -> Snapshot {
        var usage: UsageResponse?
        if let name = slotName(doc, "usage") { usage = try decodeFixture(UsageResponse.self, name) }
        var activeMs: Int?
        if let name = slotName(doc, "active_time") {
            activeMs = try decodeFixture(ActiveTimeResponse.self, name).activeMs
        }
        var insights: InsightsResponse?
        if let name = slotName(doc, "insights") { insights = try decodeFixture(InsightsResponse.self, name) }
        var stats: StatsResponse?
        if let name = slotName(doc, "stats") { stats = try decodeFixture(StatsResponse.self, name) }
        var quota: QuotaResponse = .empty
        if let name = slotName(doc, "quota") {
            // Raw-bytes path: captures the provider wire order, which the All-view
            // group order is pinned against (contract §All view).
            quota = try QuotaResponse.decode(from: try fixtureData(name))
        }

        // Frozen clock = quota payload timestamp (contract: "tests freeze it to the
        // payload timestamp"); quota-disabled/absent payloads fall back to a fixed date.
        let now = quota.timestamp.map { Date(timeIntervalSince1970: TimeInterval($0)) }
            ?? Date(timeIntervalSince1970: 1_785_080_120)

        return Snapshot(period: period(of: doc),
                        usage: usage,
                        activeMs: activeMs,
                        insights: insights,
                        stats: stats,
                        quota: quota,
                        thresholds: .defaults,
                        components: try components(of: doc),
                        now: now,
                        usageFailed: slotFailed(doc, "usage"),
                        quotaFailed: slotFailed(doc, "quota"))
    }

    private func sub(_ document: [String: Any], _ key: String) throws -> [String: Any] {
        try XCTUnwrap(document[key] as? [String: Any])
    }

    private func codexGroup(_ snap: Snapshot) -> (provider: String, canonical: String, reset: ResetCredits?, failed: Bool)? {
        guard let g = snap.allQuotaGroups.first(where: { $0.canonicalProvider.lowercased() == "codex" }) else { return nil }
        return (g.provider, g.canonicalProvider, g.providerEntry?.resetCredits, g.failed)
    }

    /// Renders the pinned credits row exactly the All-view branch does.
    private func creditsRowText(_ snap: Snapshot) -> String? {
        guard let g = codexGroup(snap) else { return nil }
        return snap.creditsNotice(providerDisplay: g.provider, canonicalProvider: g.canonical,
                                  resetCredits: g.reset)
    }

    // MARK: - The case loader: every file in contract/expected/

    // @MainActor: the credits case builds a CompanionStore to exercise the
    // notification evaluator; the rest of the loader is pure.
    @MainActor
    func testEveryExpectedCaseFileIsAsserted() throws {
        let dir = contractURL("expected").path
        let files = try XCTUnwrap(FileManager.default.contentsOfDirectory(atPath: dir))
            .filter { $0.hasSuffix(".json") }
            .map { String($0.dropLast(".json".count)) }
            .sorted()
        XCTAssertEqual(files.count, 20, "expected/ file count changed; extend the loader")
        for name in files {
            let doc = try expectedDocument(name)
            let expected = try sub(doc, "expected")
            switch name {
            case "healthy": try assertHealthy(doc, expected)
            case "healthy-week": try assertHealthyWeek(doc, expected)
            case "healthy-month": try assertHealthyMonth(doc, expected)
            case "healthy-year": try assertHealthyYear(doc, expected)
            case "empty": try assertEmpty(doc, expected)
            case "active-zero": try assertActiveZero(doc, expected)
            case "credits": try assertCredits(doc, expected)
            case "delta-row-off": try assertDeltaRowOff(doc, expected)
            case "glance-off": try assertGlanceOff(doc, expected)
            case "histogram-off": try assertHistogramOff(doc, expected)
            case "per-server": try assertPerServer(expected)
            case "multi-server": try assertMultiServer(expected)
            case "quota-disabled": try assertQuotaDisabled(doc, expected)
            case "loading": try assertLoading(expected)
            case "offline": try assertOffline(expected)
            case "busy": try assertBusy(expected)
            case "wrong-service": try assertWrongService(expected)
            case "partial": try assertPartial(doc, expected)
            case "partial-failure": try assertPartialFailure(doc, expected)
            case "provider-error": try assertProviderError(doc, expected)
            default: XCTFail("case file not covered by the loader: \(name)")
            }
        }
    }

    // MARK: - Individual cases

    private func assertHealthy(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let today = try sub(expected, "today")
        XCTAssertEqual(snap.costText, try XCTUnwrap(today["cost"] as? String))
        XCTAssertEqual(snap.tokensCompact, try XCTUnwrap(today["tokens_compact"] as? String))
        XCTAssertEqual(String(snap.usage?.totalMessages ?? -1), try XCTUnwrap(today["messages"] as? String))
        XCTAssertEqual(snap.activeSegmentText, try XCTUnwrap(today["active"] as? String))
        XCTAssertEqual(snap.deltaRowText, try XCTUnwrap(expected["delta_row"] as? String))

        let ranks = try sub(expected, "top_ranks")
        XCTAssertEqual(snap.toolsKickerText, ranks["kicker_tools"] as? String)
        XCTAssertEqual(snap.modelsKickerText, ranks["kicker_models"] as? String)
        XCTAssertEqual(snap.topTools.map { "\($0.label) \($0.valueText)" },
                       try XCTUnwrap(ranks["tools"] as? [String]))
        XCTAssertEqual(snap.topModels.map { "\($0.label) \($0.valueText)" },
                       try XCTUnwrap(ranks["models"] as? [String]))
        // Codex must render from the transparency-shipped mark; unknown ids get none.
        XCTAssertEqual(snap.topTools.first?.logoAsset, "AgentCodex")

        guard case .hours(let bars, let peak) = try XCTUnwrap(face(of: snap)) else {
            return XCTFail("today must show the hour face")
        }
        XCTAssertEqual(bars.count, 24)
        let glance = try sub(expected, "glance")
        XCTAssertEqual(peak.map { L10n.t("peak_caption", $0) }, glance["caption"] as? String)

        XCTAssertNil(snap.creditsNotice(providerDisplay: "Codex", canonicalProvider: "codex", resetCredits: nil))
        XCTAssertNil(creditsRowText(snap))
        XCTAssertTrue(snap.perServer.isEmpty, "single server shows no per-server rows")

        let low = try sub(expected, "quota_low")
        let lowRows = try XCTUnwrap(low["rows"] as? [[String: Any]])
        let actual = snap.lowQuotaRows
        XCTAssertEqual(actual.count, lowRows.count)
        for (row, want) in zip(actual, lowRows) {
            // Byte-exact: the contract pins these strings ("Strings are pinned under
            // the English locale"), casing included - the Windows suite asserts the
            // same bytes. Any display-label drift must fail here, not silently pass.
            // The ⚠ prefix in the pinned label is flyout decoration for Failed rows.
            let pinned = try XCTUnwrap(want["label"] as? String).replacingOccurrences(of: "⚠ ", with: "")
            XCTAssertEqual("\(row.provider) · \(row.displayBucketLabel)", pinned)
            XCTAssertEqual(row.left, try XCTUnwrap(want["left"] as? Double), accuracy: 0.001)
            XCTAssertEqual(row.estimated, want["estimated"] as? Bool)
        }

        let all = try sub(expected, "quota_all")
        let wantGroups = try XCTUnwrap(all["groups"] as? [[String: Any]])
        // Ordered: the contract pins the group order to the wire's provider order
        // ("provider order as detected"), the decode path carries that order
        // (QuotaResponse.wireProviderKeys), and the Windows loader asserts the same
        // sequence order-sensitively.
        XCTAssertEqual(snap.allQuotaGroups.map(\.provider),
                       try wantGroups.map { try XCTUnwrap($0["provider"] as? String) })
        for want in wantGroups {
            let name = try XCTUnwrap(want["provider"] as? String)
            let group = try XCTUnwrap(snap.allQuotaGroups.first { $0.provider == name })
            let wantRows = try XCTUnwrap(want["rows"] as? [[String: Any]])
            XCTAssertEqual(group.rows.count, wantRows.count, "\(name): row count")
            // Ordered and byte-exact: rows follow the server's `buckets` array on both
            // platforms, so index i must carry the pinned label i - verbatim, casing
            // included (Windows asserts the same bytes order-sensitively).
            for (idx, wantRow) in wantRows.enumerated() {
                let row = group.rows[idx]
                XCTAssertEqual(row.displayBucketLabel, try XCTUnwrap(wantRow["label"] as? String),
                               "\(name): row \(idx) label")
                XCTAssertEqual(row.left, try XCTUnwrap(wantRow["left"] as? Double), accuracy: 0.001,
                               "\(name): row \(idx) left")
            }
        }

        let seg = try sub(expected, "period_segment")
        XCTAssertEqual(try XCTUnwrap(seg["labels"] as? [String]),
                       UsagePeriod.allCases.map { L10n.t($0.segmentKey) })
        XCTAssertEqual(snap.kickerText, "TODAY")
        XCTAssertTrue(snap.components.perServerRows) // defaults all-on
        XCTAssertFalse(snap.usageLoading)
        XCTAssertTrue(L10n.t("updated_just_now").hasPrefix(try XCTUnwrap(expected["freshness_contains"] as? String)))
    }

    private func assertHealthyWeek(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let today = try sub(expected, "today")
        XCTAssertEqual(snap.costText, today["cost"] as? String)
        XCTAssertEqual(snap.tokensCompact, today["tokens_compact"] as? String)
        XCTAssertEqual(String(snap.usage?.totalMessages ?? -1), today["messages"] as? String)
        XCTAssertEqual(snap.activeSegmentText, today["active"] as? String)
        XCTAssertEqual(snap.deltaRowText, expected["delta_row"] as? String)

        let ranks = try sub(expected, "top_ranks")
        XCTAssertEqual(snap.toolsKickerText, ranks["kicker_tools"] as? String)
        XCTAssertEqual(snap.topTools.map { "\($0.label) \($0.valueText)" }, ranks["tools"] as? [String])

        // Week request must be the calendar week via date_from/date_to, never period=week.
        let req = try sub(expected, "week_request")
        XCTAssertEqual(req["uses_date_range"] as? Bool, true)
        let thursday = try XCTUnwrap(CompanionStore.date(fromDayString: "2026-07-23", calendar: utcCalendar))
        let path = CompanionStore.usageRequestPath(for: .week, today: thursday, calendar: utcCalendar)
        XCTAssertTrue(path.contains("date_from=2026-07-20"), path)
        XCTAssertTrue(path.contains("date_to=2026-07-23"), path)
        XCTAssertFalse(path.contains("period=week"), path)
        let from = try XCTUnwrap(CompanionStore.date(fromDayString: "2026-07-20", calendar: utcCalendar))
        XCTAssertEqual(utcCalendar.component(.weekday, from: from), 2, "date_from_weekday must be Mon")

        guard case .days(let tokens) = try XCTUnwrap(face(of: snap)) else {
            return XCTFail("week must show the day face")
        }
        let glance = try sub(expected, "glance")
        XCTAssertEqual(tokens.count, try XCTUnwrap(glance["columns"] as? Int))
        let labels = try XCTUnwrap(glance["labels"] as? [String])
        XCTAssertEqual(labels,
                       ["wd_mon", "wd_tue", "wd_wed", "wd_thu", "wd_fri", "wd_sat", "wd_sun"].map { L10n.t($0) })
        // The daily data misses 2026-07-21 - a Tuesday - so exactly ONE column is
        // empty, and the expected file names it.
        XCTAssertEqual(tokens.filter { $0 == 0 }.count,
                       (glance["missing_days"] as? [String] ?? ["x"]).count,
                       "exactly one empty column")
        XCTAssertEqual(glance["missing_days"] as? [String], ["Tue"])
    }

    private func assertHealthyMonth(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let today = try sub(expected, "today")
        XCTAssertEqual(snap.costText, today["cost"] as? String)
        XCTAssertEqual(snap.tokensCompact, today["tokens_compact"] as? String)
        XCTAssertEqual(snap.activeSegmentText, today["active"] as? String)
        XCTAssertEqual(snap.deltaRowText, expected["delta_row"] as? String)
        let ranks = try sub(expected, "top_ranks")
        XCTAssertEqual(snap.toolsKickerText, ranks["kicker_tools"] as? String)

        let glance = try sub(expected, "glance")
        guard case .grid(let columns, let filled, let windowDays, let firstCell) = try XCTUnwrap(face(of: snap)) else {
            return XCTFail("month must show the grid face")
        }
        XCTAssertEqual(windowDays, try XCTUnwrap(glance["window_days"] as? Int))
        XCTAssertEqual(filled, try XCTUnwrap(glance["filled_cells"] as? Int))
        for column in columns { XCTAssertEqual(column.count, 7) }
        let first = try XCTUnwrap(CompanionStore.date(fromDayString: firstCell, calendar: utcCalendar))
        XCTAssertEqual(utcCalendar.component(.weekday, from: first), 2, "first_column_weekday Mon")
        XCTAssertFalse(columns.isEmpty)
    }

    private func assertHealthyYear(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let today = try sub(expected, "today")
        XCTAssertEqual(snap.costText, today["cost"] as? String)
        XCTAssertEqual(snap.tokensCompact, today["tokens_compact"] as? String)
        XCTAssertEqual(String(snap.usage?.totalMessages ?? -1), today["messages"] as? String)
        XCTAssertEqual(snap.activeSegmentText, today["active"] as? String)
        XCTAssertNil(snap.deltaRowText, "all-null *_pct hides the whole row")
        XCTAssertNil(snap.comparisonLine, "no comparison either")

        let ranks = try sub(expected, "top_ranks")
        XCTAssertEqual(snap.topTools.map { "\($0.label) \($0.valueText)" }, ranks["tools"] as? [String])
        XCTAssertEqual(snap.topTools.last?.label, "OpenCode")
        XCTAssertEqual(snap.topTools.last?.logoAsset, "AgentOpenCode")

        let glance = try sub(expected, "glance")
        guard case .grid(let columns, let filled, let windowDays, _) = try XCTUnwrap(face(of: snap)) else {
            return XCTFail("year must show the grid face")
        }
        XCTAssertEqual(windowDays, try XCTUnwrap(glance["window_days"] as? Int))
        XCTAssertEqual(filled, try XCTUnwrap(glance["filled_cells"] as? Int))
        // 2026-01-26 (Mon, first column) .. 2026-07-26 (Sun, anchor) = 26 columns.
        XCTAssertEqual(columns.count, 26)
    }

    private func assertEmpty(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let today = try sub(expected, "today")
        XCTAssertTrue(snap.isEmptyUsage)
        XCTAssertEqual(L10n.t("no_usage_period", L10n.t(snap.period.suffixKey)),
                       today["hero_title"] as? String)
        XCTAssertTrue(L10n.t("tokdash_running").contains(try XCTUnwrap(today["hero_sub_contains"] as? String)))
        XCTAssertNil(snap.deltaRowText)
        XCTAssertTrue(snap.topTools.isEmpty)
        XCTAssertTrue(snap.topModels.isEmpty)
        XCTAssertNil(face(of: snap), "all-zero source hides the glance strip")
        XCTAssertNil(snap.activeSegmentText)
    }

    private func assertActiveZero(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let today = try sub(expected, "today")
        XCTAssertEqual(snap.costText, today["cost"] as? String)
        XCTAssertNil(snap.activeSegmentText, "active_ms 0 renders no segment, never 'active 0 m'")
        XCTAssertFalse(snap.subLine.contains("active"))
    }

    @MainActor
    private func assertCredits(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        // Frozen clock = quota payload timestamp (1785080120); credit-a is exactly +48 h.
        XCTAssertEqual(snap.now.timeIntervalSince1970, 1_785_080_120, accuracy: 0.5)
        XCTAssertEqual(creditsRowText(snap), expected["credits_row"] as? String)
        XCTAssertEqual(expected["credits_row_view"] as? String, "all-only")
        // Low view never shows it: enforced by the view (row lives only in the All branch);
        // data-side the component/quota/count gates still pass, which the row pin proves.

        let store = CompanionStore()
        store.settings.lowQuotaNotifications = true
        let armed = store.evaluateResetCreditNotifications(snap)
        XCTAssertEqual(armed.count, 1, "credit-a enters its last 48 h (dedup id: only one per credit)")
        XCTAssertEqual(armed.first?.provider, "Codex")
        XCTAssertEqual(armed.first?.clause, "in 2 d")
        // Dedup: second evaluation is empty.
        XCTAssertTrue(store.evaluateResetCreditNotifications(snap).isEmpty)
    }

    private func assertDeltaRowOff(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        XCTAssertFalse(snap.components.fullDeltaRow)
        XCTAssertNil(snap.deltaPieces, "with fullDeltaRow off no delta row renders")
        XCTAssertEqual(snap.comparisonLine, expected["hero_comparison"] as? String)
        XCTAssertEqual(snap.comparisonDirection, expected["hero_comparison_direction"] as? String == "down" ? -1 : 1)
    }

    private func assertGlanceOff(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        XCTAssertNil(face(of: snap))
        // requests_absent: component-off => the source isn't even selected, so no request.
        let comps = try components(of: doc)
        for period in UsagePeriod.allCases {
            XCTAssertNil(CompanionStore.glanceSource(for: period, components: comps, today: Date(), calendar: utcCalendar),
                         "\(period): no request when activityGlance is off")
        }
        let today = try sub(expected, "today")
        XCTAssertEqual(snap.costText, today["cost"] as? String, "hero unaffected")
    }

    private func assertHistogramOff(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let comps = try components(of: doc)
        XCTAssertNil(CompanionStore.glanceSource(for: .today, components: comps, today: Date(), calendar: utcCalendar))
        XCTAssertNil(CompanionStore.glanceSource(for: .week, components: comps, today: Date(), calendar: utcCalendar))
        // month/year grids unaffected
        XCTAssertEqual(CompanionStore.glanceSource(for: .month, components: comps, today: Date(), calendar: utcCalendar), .stats)
        XCTAssertEqual(CompanionStore.glanceSource(for: .year, components: comps, today: Date(), calendar: utcCalendar), .stats)
        let snap = try snapshot(for: doc)
        XCTAssertNil(face(of: snap))
    }

    private func server(_ id: String, _ label: String) -> CompanionServerSettings {
        CompanionServerSettings(id: id, label: label, baseURL: "http://127.0.0.1:55423", enabled: true)
    }

    private func assertPerServer(_ expected: [String: Any]) throws {
        let usage = try decodeFixture(UsageResponse.self, "usage-today.json")
        let servers = [server("local", "Local"), server("second", "Second")]
        // Second has no active-time payload -> the sum is dropped, never guessed.
        XCTAssertNil(CompanionStore.combinedActiveMs([(11_520_000, false), (nil, false)]))

        let combined = CompanionStore.combineUsage([usage, usage])
        let snap = Snapshot(usage: combined, quota: .empty)
        XCTAssertEqual(snap.costText, try XCTUnwrap(sub(expected, "today")["cost"] as? String))
        XCTAssertEqual(String(combined.totalTokens), try XCTUnwrap(sub(expected, "today")["tokens_exact"] as? String))
        XCTAssertNil(snap.activeSegmentText)

        let rows = CompanionStore.perServerRows(servers: servers,
                                                results: [(servers[0], usage), (servers[1], usage)],
                                                failedIDs: [])
        let want = try XCTUnwrap(expected["per_server_rows"] as? [[String: Any]])
        XCTAssertEqual(rows.count, want.count)
        for (row, pin) in zip(rows, want) {
            XCTAssertEqual(row.label, pin["label"] as? String)
            XCTAssertEqual(row.costText, pin["cost"] as? String)
            XCTAssertEqual(row.tokensCompact, pin["tokens_compact"] as? String)
        }
        XCTAssertEqual(L10n.t("per_server_footnote"), expected["per_server_footnote"] as? String)
        XCTAssertEqual(L10n.t("servers_count", 2), expected["connection"] as? String)

        // Unreachable server keeps its row with the plain "unreachable" tag.
        let down = CompanionStore.perServerRows(servers: servers, results: [(servers[1], usage)],
                                                failedIDs: ["local"])
        let downRow = PerServerRow(down[0])
        XCTAssertFalse(downRow.reachable)
        XCTAssertEqual(downRow.valueText, "unreachable")
    }

    private func assertMultiServer(_ expected: [String: Any]) throws {
        let usage = try decodeFixture(UsageResponse.self, "usage-today.json")
        let active = try decodeFixture(ActiveTimeResponse.self, "active-time-today.json")
        let both = try XCTUnwrap(CompanionStore.combinedActiveMs([(active.activeMs, false), (active.activeMs, false)]))
        XCTAssertEqual(CompanionStore.activeText(activeMs: both), "active 6 h 24 m")

        let combined = CompanionStore.combineUsage([usage, usage])
        let snap = Snapshot(usage: combined, activeMs: both, quota: .empty)
        XCTAssertEqual(snap.deltaRowText, expected["delta_row"] as? String)

        let servers = [server("local", "Local"), server("second", "Second")]
        let rows = CompanionStore.perServerRows(servers: servers,
                                                results: [(servers[0], usage), (servers[1], usage)],
                                                failedIDs: [])
        XCTAssertEqual(rows.map(\.label), ["Local", "Second"], "settings order")

        // quota_all_server_order: merged groups run in SETTINGS order, and each
        // server's providers keep their wire order (pinned by this case file's
        // quota_all_server_order + §All view "provider order as detected").
        let quota = try QuotaResponse.decode(from: try fixtureData("quota.json"))
        XCTAssertEqual(quota.providerWireOrder, ["codex", "claude", "kimi"], "wire order decode")
        let merged = CompanionStore.mergedQuota([("Local", quota), ("Second", quota)])
        let groupSnap = Snapshot(usage: combined, quota: merged, thresholds: .defaults)
        XCTAssertEqual(groupSnap.allQuotaGroups.map(\.provider),
                       ["Local · Codex", "Local · Claude", "Local · Kimi",
                        "Second · Codex", "Second · Claude", "Second · Kimi"])
        let serverOrder = try XCTUnwrap(expected["quota_all_server_order"] as? [String])
        var seenLabels: [String] = []
        for name in merged.providerWireOrder ?? [] {
            let label = String(name.split(separator: " ·", maxSplits: 1,
                                          omittingEmptySubsequences: false).first ?? "")
            if seenLabels.last != label { seenLabels.append(label) }
        }
        XCTAssertEqual(seenLabels, serverOrder, "pinned server order")
        XCTAssertTrue(groupSnap.lowQuotaRows.allSatisfy { !$0.provider.contains(" · ") },
                      "deduped Low labels omit the server prefix")

        XCTAssertEqual(expected["delay_rule"] as? String, "minimum per-server delay")
        XCTAssertTrue(L10n.t("servers_unavailable", "Local").hasPrefix("Unavailable"))
    }

    private func assertQuotaDisabled(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let section = try sub(expected, "quota_section")
        XCTAssertEqual(L10n.t("tracking_off"), section["message"] as? String)
        XCTAssertEqual(L10n.t("open_dashboard"), "Open Dashboard")
        XCTAssertTrue(section["has_open_dashboard"] as? Bool == true)
        XCTAssertFalse(snap.quota.enabled)
        XCTAssertTrue(snap.allQuotaGroups.isEmpty)
        XCTAssertEqual(snap.costText, try XCTUnwrap(sub(expected, "today")["cost"] as? String))
        XCTAssertNil(creditsRowText(snap), "no credits row with tracking off")
    }

    private func assertLoading(_ expected: [String: Any]) throws {
        XCTAssertEqual(L10n.t("connecting"), try XCTUnwrap(expected["connection"] as? String))
        // First fetch in flight: hero/sub/quota skeletons, and the segment stays visible.
        let snap = Snapshot()
        XCTAssertTrue(snap.usageLoading, "skeletons_present: hero cost/sub")
        XCTAssertFalse(snap.quota.enabled)
        XCTAssertEqual(expected["segment_always_visible"] as? Bool, true)
        XCTAssertEqual(try XCTUnwrap(expected["skeletons_present"] as? [String]).count, 3)
    }

    private func assertOffline(_ expected: [String: Any]) throws {
        let banner = try sub(expected, "banner")
        XCTAssertEqual(L10n.t("banner_offline_title"), banner["title"] as? String)
        XCTAssertTrue(L10n.t("banner_offline_body").contains(try XCTUnwrap(banner["body_contains"] as? String)))
        XCTAssertEqual(L10n.t("offline"), expected["connection"] as? String)
        XCTAssertEqual(try XCTUnwrap(banner["actions"] as? [String]), [L10n.t("retry"), L10n.t("settings")])
        XCTAssertEqual(L10n.t("stale_suffix"), " · stale")
    }

    private func assertBusy(_ expected: [String: Any]) throws {
        let banner = try sub(expected, "banner")
        XCTAssertEqual(L10n.t("busy"), expected["connection"] as? String)
        XCTAssertEqual(L10n.t("banner_busy_title"), banner["title"] as? String)
        XCTAssertTrue(L10n.t("banner_busy_body").contains(try XCTUnwrap(banner["body_contains"] as? String)))
        // 503 maps to the busy state in the client (see TokdashClient.get) - and the
        // loader-side shape: usage "503" means usageFailed with no new data.
        let doc: [String: Any] = ["period": "today",
                                  "fixtures": ["usage": "503", "active_time": "503", "insights": "503", "quota": "503"]]
        let snap = try snapshot(for: doc)
        XCTAssertTrue(snap.usageFailed)
        XCTAssertTrue(snap.quotaFailed)
        XCTAssertFalse(snap.usageLoading, "failed is not loading")
    }

    private func assertWrongService(_ expected: [String: Any]) throws {
        XCTAssertEqual(L10n.t("banner_wrong_title"), expected["message"] as? String)
        // "no other calls": structural - runRefresh checks health.service before
        // scheduling ANY of usage/active/quota/glance (single-server path).
        let health = try decodeFixture(HealthResponse.self, "health-wrong-service.json")
        XCTAssertNotEqual(health.service, "tokdash")
    }

    private func assertPartial(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc) // usage OK, active 503, insights 503, quota 503
        XCTAssertEqual(snap.costText, try XCTUnwrap(sub(expected, "today")["cost"] as? String))
        XCTAssertTrue(snap.quotaFailed)
        XCTAssertFalse(snap.usageFailed)
        XCTAssertTrue(L10n.t("quota_unavailable").contains(try XCTUnwrap(expected["inline_warning_contains"] as? String)))
        // active-time and insights are OPTIONAL sections: silent nil, no warning of their own.
        XCTAssertNil(snap.activeSegmentText)
        XCTAssertNil(face(of: snap))
        XCTAssertFalse(snap.subLine.contains("retrying"), "usage itself succeeded")
    }

    private func assertPartialFailure(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let section = try sub(expected, "quota_section")
        let groups = snap.allQuotaGroups
        let minimax = try XCTUnwrap(groups.first { $0.canonicalProvider.lowercased() == "minimax" })
        XCTAssertTrue(minimax.failed, "provider_failures includes Minimax")
        let claude = try XCTUnwrap(groups.first { $0.canonicalProvider.lowercased() == "claude" })
        XCTAssertFalse(claude.failed)
        XCTAssertTrue(section["rows_visible"] as? Bool == true)
        XCTAssertFalse(section["full_surface_failure"] as? Bool == true, "group warning, not whole-section failure")
        XCTAssertTrue(L10n.t("couldnt_refresh").contains(try XCTUnwrap(section["inline_warning_contains"] as? String)))

        let low = try sub(expected, "quota_low")
        let want = try XCTUnwrap(low["rows"] as? [[String: Any]])
        let rows = snap.lowQuotaRows
        XCTAssertEqual(rows.count, want.count)
        for (row, pin) in zip(rows, want) {
            XCTAssertEqual(row.left, try XCTUnwrap(pin["left"] as? Double), accuracy: 0.001)
            XCTAssertEqual(row.failed, pin["failed"] as? Bool)
            // The ⚠ prefix in the pinned label is the view's doing (QuotaRowView adds
            // it for failed rows in the Low view); the data-side flag is pinned here.
            XCTAssertTrue(((pin["label"] as? String) ?? "").contains(row.provider))
        }
        XCTAssertNil(creditsRowText(snap), "partial-failure has no codex credits")
    }

    private func assertProviderError(_ doc: [String: Any], _ expected: [String: Any]) throws {
        let snap = try snapshot(for: doc)
        let section = try sub(expected, "quota_section")
        let groups = snap.allQuotaGroups
        XCTAssertTrue(try XCTUnwrap(groups.first { $0.canonicalProvider.lowercased() == "codex" }).failed)
        for name in ["Claude", "Kimi"] {
            XCTAssertFalse(try XCTUnwrap(groups.first { $0.provider == name }).failed, "\(name) stays healthy")
        }
        XCTAssertTrue(section["rows_visible"] as? Bool == true)
        XCTAssertNil(creditsRowText(snap), "provider-error codex carries no reset_credits")
        XCTAssertEqual(snap.costText, try XCTUnwrap(sub(expected, "today")["cost"] as? String))
    }

    // MARK: - Unit: ladder, compact notation, delta math, credits clause

    private func testEnglishLocale(_ body: () throws -> Void) rethrows {
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }
        try body()
    }

    func testActiveTimeLadder() {
        testEnglishLocale {
            XCTAssertEqual(CompanionStore.activeText(activeMs: 59_000), "active <1 m")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 60_000), "active 1 m")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 3_599_999), "active 59 m")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 3_600_000), "active 1 h 0 m")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 11_520_000), "active 3 h 12 m")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 86_399_999), "active 23 h 59 m")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 86_400_000), "active 1 d 0 h")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 188_400_000), "active 2 d 4 h")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 850_800_000), "active 9 d 20 h")
            XCTAssertEqual(CompanionStore.activeText(activeMs: 6_411_600_000), "active 74 d 5 h")
        }
    }

    func testCompactRoundingAndTrimming() {
        XCTAssertEqual(Snapshot.compactTokens(249_669), "250k", "rounds to the shown precision")
        XCTAssertEqual(Snapshot.compactTokens(779_000), "779k")
        XCTAssertEqual(Snapshot.compactTokens(12_982_308), "13M", "'.0' trims: 13.0M renders 13M")
        XCTAssertEqual(Snapshot.compactTokens(281_000_000), "281M")
        XCTAssertEqual(Snapshot.compactTokens(1_243_500_000), "1.2B")
        XCTAssertEqual(Snapshot.compactTokens(18_700_000), "18.7M")
    }

    func testDeltaGlyphValueAndAssembly() {
        XCTAssertEqual(Snapshot.deltaGlyph(12), "▲")
        XCTAssertEqual(Snapshot.deltaGlyph(-12), "▼")
        XCTAssertEqual(Snapshot.deltaGlyph(0), "±", "exactly zero gets the flat mark")
        XCTAssertEqual(Snapshot.deltaValue(-11.7), 12, "abs(round(-11.7))")
        XCTAssertEqual(Snapshot.deltaValue(11.7), 12)
        XCTAssertEqual(Snapshot.deltaValue(0), 0)
        testEnglishLocale {
            XCTAssertEqual(L10n.t("delta_cost", "▼", 12), "▼ 12% cost")
            XCTAssertEqual(L10n.t("delta_msgs", "▲", 3), "▲ 3% msgs")
        }
        // A null metric is omitted; all three null hides the row.
        let one = UsageResponse(comparison: Comparison(tokensPct: nil, costPct: -12, messagesPct: nil,
                                                       costPrev: 1, tokensPrev: nil, messagesPrev: nil))
        XCTAssertEqual(Snapshot(usage: one).deltaPieces?.count, 1)
        let none = UsageResponse(comparison: Comparison(tokensPct: nil, costPct: nil, messagesPct: nil,
                                                        costPrev: nil, tokensPrev: nil, messagesPrev: nil))
        XCTAssertNil(Snapshot(usage: none).deltaPieces)
        XCTAssertNil(Snapshot(usage: one, components: { var c = CompanionComponents(); c.fullDeltaRow = false; return c }()).deltaPieces,
                     "component off: no row even with data")
    }

    func testCreditsClauseFloors() {
        testEnglishLocale {
            let now = Date(timeIntervalSince1970: 1_785_080_120)
            func clause(_ seconds: TimeInterval) -> String {
                CompanionStore.creditsClause(expiry: now.addingTimeInterval(seconds), now: now)
            }
            XCTAssertEqual(clause(48 * 3600), "in 2 d")
            XCTAssertEqual(clause(2.9 * 86_400), "in 2 d", "floored")
            XCTAssertEqual(clause(36 * 3600), "tomorrow")
            XCTAssertEqual(clause(86_400 + 1), "tomorrow")
            XCTAssertEqual(clause(86_399), "today")
            XCTAssertEqual(clause(1), "today")
        }
    }

    func testClaudeResetCreditsEpochIntExpiresAt() throws {
        // LIVE SHAPE: claude encodes expires_at as EPOCH SECONDS where codex encodes an
        // ISO string. While the DTO typed it `String?`, this single int failed the whole
        // quota decode - both platforms showed "quota data not available". The custom
        // ResetCredit decoder takes both shapes, and the extra server keys (tier,
        // credential_path, title, resets_left, clears, status) decode-ignore, not reject.
        let json = """
        {"enabled":true,"providers":{
          "claude":{"estimated":true,"tier":"max","credential_path":"/home/u/.claude","buckets":[
            {"account":"default","bucket":"5h","bucket_label":"5-hour window","remaining_percent":71.0,"resets_at":1782919500}],
            "reset_credits":{"available_count":1,"credits":[
              {"id":"r1","title":"Weekly limit reset","resets_left":1,"clears":false,"status":"active",
               "expires_at":1785252920}]}},
          "codex":{"buckets":[
            {"account":"a","bucket":"5h","remaining_percent":50.0,"resets_at":1}],
            "reset_credits":{"available_count":1,"credits":[
              {"id":"c","expires_at":"2026-07-28T15:35:20Z"}]}}
        },"timestamp":1785080120}
        """
        let q = try QuotaResponse.decode(from: Data(json.utf8))
        let claudeCredit = try XCTUnwrap(q.providers?["claude"]?.resetCredits?.credits?.first)
        let codexCredit = try XCTUnwrap(q.providers?["codex"]?.resetCredits?.credits?.first)
        XCTAssertEqual(codexCredit.expiresAt, "2026-07-28T15:35:20Z", "ISO passes through")
        XCTAssertEqual(claudeCredit.expiresAt, "2026-07-28T15:35:20Z",
                       "epoch int (1785252920) normalizes to the very instant the codex ISO names")

        // The whole payload survived, and the row renders under claude - credits are no
        // longer codex-only. Frozen clock 2026-07-26T15:35:20Z puts the expiry 2 days out.
        let snap = Snapshot(quota: q, now: Date(timeIntervalSince1970: 1_785_080_120))
        testEnglishLocale {
            XCTAssertEqual(snap.creditsNotice(providerDisplay: "Claude", canonicalProvider: "claude",
                                              resetCredits: q.providers?["claude"]?.resetCredits),
                           "⚡ Claude · 1 reset credits · expire in 2 d")
        }
    }

    // MARK: - Unit: week math and request paths

    func testWeekMondayMathAndRequestPath() throws {
        let thursday = try XCTUnwrap(CompanionStore.date(fromDayString: "2026-07-23", calendar: utcCalendar))
        let monday = CompanionStore.startOfWeekMonday(thursday, calendar: utcCalendar)
        XCTAssertEqual(CompanionStore.dayString(monday, calendar: utcCalendar), "2026-07-20")
        let mondayItself = try XCTUnwrap(CompanionStore.date(fromDayString: "2026-07-20", calendar: utcCalendar))
        XCTAssertEqual(CompanionStore.dayString(CompanionStore.startOfWeekMonday(mondayItself, calendar: utcCalendar),
                                                calendar: utcCalendar), "2026-07-20")
        let sunday = try XCTUnwrap(CompanionStore.date(fromDayString: "2026-07-26", calendar: utcCalendar))
        XCTAssertEqual(CompanionStore.dayString(CompanionStore.startOfWeekMonday(sunday, calendar: utcCalendar),
                                                calendar: utcCalendar), "2026-07-20")

        let range = CompanionStore.weekRange(today: thursday, calendar: utcCalendar)
        XCTAssertEqual(range.from, "2026-07-20")
        XCTAssertEqual(range.to, "2026-07-23")

        XCTAssertEqual(CompanionStore.usageRequestPath(for: .today, today: thursday, calendar: utcCalendar),
                       "/api/usage?period=today")
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .month, today: thursday, calendar: utcCalendar),
                       "/api/usage?period=month")
        let weekPath = CompanionStore.usageRequestPath(for: .week, today: thursday, calendar: utcCalendar)
        XCTAssertEqual(weekPath, "/api/usage?date_from=2026-07-20&date_to=2026-07-23")
        XCTAssertFalse(weekPath.contains("period=week"))
    }

    // MARK: - Unit: E12 instance stepper (contract §Instance stepper)

    private func utcDay(_ s: String) -> Date {
        try! XCTUnwrap(CompanionStore.date(fromDayString: s, calendar: utcCalendar))
    }

    func testE12EarlierLimitPinsWalkback() {
        XCTAssertEqual(CompanionStore.earlierLimit(for: .today), 13)
        XCTAssertEqual(CompanionStore.earlierLimit(for: .week), 8)
        XCTAssertEqual(CompanionStore.earlierLimit(for: .month), 11)
        XCTAssertEqual(CompanionStore.earlierLimit(for: .year), 2)
    }

    func testE12SteppedDatesAreFullElapsedCalendarWindows() throws {
        let thu = utcDay("2026-09-24") // Thursday; its week starts Mon 2026-09-21
        XCTAssertNil(CompanionStore.steppedDates(period: .today, offset: 0, today: thu, calendar: utcCalendar),
                     "present is not stepped")

        func ranged(_ period: UsagePeriod, _ offset: Int, _ today: Date, _ from: String, _ to: String,
                    file: StaticString = #filePath, line: UInt = #line) {
            let r = CompanionStore.steppedRange(period: period, offset: offset, today: today, calendar: utcCalendar)
            XCTAssertEqual(r?.from, from, file: file, line: line)
            XCTAssertEqual(r?.to, to, file: file, line: line)
        }

        ranged(.today, 1, thu, "2026-09-23", "2026-09-23")
        ranged(.today, 13, thu, "2026-09-11", "2026-09-11")
        ranged(.week, 1, thu, "2026-09-14", "2026-09-20")
        ranged(.week, 2, thu, "2026-09-07", "2026-09-13")
        // A Sunday rolls back to its own week's Monday BEFORE stepping (no straddling).
        ranged(.week, 1, utcDay("2026-09-27"), "2026-09-14", "2026-09-20")
        // Weeks may cross years: Jan 7 2026 (Wed) Monday is Jan 5; one back starts Dec 29.
        ranged(.week, 1, utcDay("2026-01-07"), "2025-12-29", "2026-01-04")
        ranged(.month, 1, thu, "2026-08-01", "2026-08-31")
        ranged(.month, 11, thu, "2025-10-01", "2025-10-31")
        ranged(.month, 1, utcDay("2026-01-15"), "2025-12-01", "2025-12-31")
        ranged(.year, 1, thu, "2025-01-01", "2025-12-31")
        ranged(.year, 2, thu, "2024-01-01", "2024-12-31")
    }

    func testE12UsageRequestPathSteppedAlwaysUsesDates() {
        let thu = utcDay("2026-09-24")
        // Present instances keep the shipped §Period windows wire forms.
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .today, today: thu, calendar: utcCalendar),
                       "/api/usage?period=today")
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .month, today: thu, calendar: utcCalendar),
                       "/api/usage?period=month")
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .week, today: thu, calendar: utcCalendar),
                       "/api/usage?date_from=2026-09-21&date_to=2026-09-24")
        // Stepped: explicit full elapsed window, never period=.
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .today, today: thu, calendar: utcCalendar, offset: 1),
                       "/api/usage?date_from=2026-09-23&date_to=2026-09-23")
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .week, today: thu, calendar: utcCalendar, offset: 2),
                       "/api/usage?date_from=2026-09-07&date_to=2026-09-13")
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .month, today: thu, calendar: utcCalendar, offset: 1),
                       "/api/usage?date_from=2026-08-01&date_to=2026-08-31")
        XCTAssertEqual(CompanionStore.usageRequestPath(for: .year, today: thu, calendar: utcCalendar, offset: 2),
                       "/api/usage?date_from=2024-01-01&date_to=2024-12-31")
    }

    func testE12InstanceKickerWordsOneBackDatesFurtherBack() {
        let thu = utcDay("2026-09-24")
        testEnglishLocale {
            func kicker(_ period: UsagePeriod, _ offset: Int, _ today: Date = Date(timeIntervalSince1970: 0)) -> String {
                CompanionStore.instanceKicker(period: period, offset: offset,
                                              today: today == Date(timeIntervalSince1970: 0) ? thu : today,
                                              calendar: utcCalendar)
            }
            // Present kickers stay byte-identical.
            XCTAssertEqual(kicker(.today, 0), "TODAY")
            XCTAssertEqual(kicker(.week, 0), "THIS WEEK")
            XCTAssertEqual(kicker(.month, 0), "THIS MONTH")
            XCTAssertEqual(kicker(.year, 0), "THIS YEAR")
            // One back: the localized words, uppercased to kicker style.
            XCTAssertEqual(kicker(.today, 1), "YESTERDAY")
            XCTAssertEqual(kicker(.week, 1), "LAST WEEK")
            XCTAssertEqual(kicker(.month, 1), "LAST MONTH")
            XCTAssertEqual(kicker(.year, 1), "LAST YEAR")
            // Further back: calendar labels.
            XCTAssertEqual(kicker(.today, 3), "SEP 21")
            XCTAssertEqual(kicker(.week, 2), "SEP 7 – 13")
            XCTAssertEqual(kicker(.week, 2, utcDay("2026-10-12")), "SEP 28 – OCT 4")
            XCTAssertEqual(kicker(.week, 2, utcDay("2026-01-14")), "DEC 29 – JAN 4, 2026")
            XCTAssertEqual(kicker(.month, 2), "JUL 2026")
            XCTAssertEqual(kicker(.month, 11), "OCT 2025")
            XCTAssertEqual(kicker(.year, 2), "2024")
        }
    }

    func testE12InstanceKickerChinese() {
        let thu = utcDay("2026-09-24")
        L10n.current = .zhHans // setUp resets to .english after every test
        XCTAssertEqual(CompanionStore.instanceKicker(period: .today, offset: 0, today: thu, calendar: utcCalendar), "今日")
        XCTAssertEqual(CompanionStore.instanceKicker(period: .today, offset: 1, today: thu, calendar: utcCalendar), "昨日")
        XCTAssertEqual(CompanionStore.instanceKicker(period: .week, offset: 1, today: thu, calendar: utcCalendar), "上周")
        XCTAssertEqual(CompanionStore.instanceKicker(period: .today, offset: 3, today: thu, calendar: utcCalendar), "9月21日")
        // CJK forms keep both operands of a range (the "Sep 7 – 13" ellipsis is English-only).
        XCTAssertEqual(CompanionStore.instanceKicker(period: .week, offset: 2, today: thu, calendar: utcCalendar), "9月7日 – 9月13日")
        XCTAssertEqual(CompanionStore.instanceKicker(period: .month, offset: 2, today: thu, calendar: utcCalendar), "2026年7月")
        XCTAssertEqual(CompanionStore.instanceKicker(period: .year, offset: 2, today: thu, calendar: utcCalendar), "2024")
        // Cross-year CJK ranges carry the year on both operands.
        XCTAssertEqual(CompanionStore.instanceKicker(period: .week, offset: 2, today: utcDay("2026-01-14"), calendar: utcCalendar),
                       "2025年12月29日 – 2026年1月4日")
    }

    func testE12GlanceSteppedWeekWindowsOnInstancesWeek() throws {
        let daily = [DailyPoint(date: "2026-09-07", tokens: 5, intensity: nil),
                     DailyPoint(date: "2026-09-13", tokens: 7, intensity: nil),
                     // A day from the CURRENT week must not leak into the stepped instance.
                     DailyPoint(date: "2026-09-21", tokens: 99, intensity: nil)]
        let insights = InsightsResponse(hourly: nil, daily: daily)
        guard case .days(let tokens)? = CompanionStore.glanceFace(period: .week, insights: insights, stats: nil,
                                                                  components: CompanionComponents(),
                                                                  calendar: utcCalendar, now: utcDay("2026-09-24"),
                                                                  offset: 2) else {
            return XCTFail("stepped week face expected")
        }
        XCTAssertEqual(tokens.first, 5, "Mon of the stepped week")
        XCTAssertEqual(tokens.last, 7, "Sun of the stepped week")
        XCTAssertFalse(tokens.contains(99), "current-week data does not leak")
    }

    func testE12SteppedMonthGridCalendarBoundedYearHidden() throws {
        // A sparse rolling series ending 2026-09-22 (the 365-day edge is 2025-09-23).
        let stats = StatsResponse(contributions: [
            Contribution(date: "2025-09-23", totals: nil, intensity: 1),
            Contribution(date: "2026-08-15", totals: nil, intensity: 3),
            Contribution(date: "2026-08-31", totals: nil, intensity: 2),
            Contribution(date: "2026-09-22", totals: nil, intensity: 1),
        ])
        let thu = utcDay("2026-09-24")
        // Stepped month: exactly Aug 1..31, Mon-start columns clamped to the month.
        guard case .grid(let columns, let filled, let windowDays, _)? =
            CompanionStore.glanceFace(period: .month, insights: nil, stats: stats,
                                      components: CompanionComponents(), calendar: utcCalendar,
                                      now: thu, offset: 1) else {
            return XCTFail("stepped month grid expected")
        }
        XCTAssertEqual(windowDays, 31, "exactly Aug 1..31")
        XCTAssertEqual(filled, 2, "Aug 15 + Aug 31 only")
        XCTAssertEqual(columns.count, 6, "Jul 27 clamped column + 5 weeks, Aug 31 tail column")
        // Any stepped year starts outside a rolling 365-day series -> silently hidden.
        XCTAssertNil(CompanionStore.glanceFace(period: .year, insights: nil, stats: stats,
                                               components: CompanionComponents(), calendar: utcCalendar,
                                               now: thu, offset: 1))
        XCTAssertNil(CompanionStore.glanceFace(period: .year, insights: nil, stats: stats,
                                               components: CompanionComponents(), calendar: utcCalendar,
                                               now: thu, offset: 2))
        // Present year view is unaffected: the trailing 180-day grid still renders.
        XCTAssertNotNil(CompanionStore.glanceFace(period: .year, insights: nil, stats: stats,
                                                  components: CompanionComponents(), calendar: utcCalendar,
                                                  now: thu))
    }

    @MainActor
    func testE12StoreStepperWalksClampsAndReanchors() throws {
        CompanionStore.clockOverride = utcDay("2026-09-24")
        defer { CompanionStore.clockOverride = nil }
        let store = CompanionStore()
        // Present: › inert, ‹ available.
        XCTAssertEqual(store.periodOffset, 0)
        XCTAssertFalse(store.canStepLater)
        XCTAssertTrue(store.canStepEarlier)
        // Walk the day past its 13-step limit and back.
        for _ in 0..<20 { store.stepPeriod(1) }
        XCTAssertEqual(store.periodOffset, 13, "clamped at the walk-back limit")
        XCTAssertFalse(store.canStepEarlier, "‹ inert at the limit")
        store.stepPeriod(-1)
        XCTAssertEqual(store.periodOffset, 12)
        XCTAssertEqual(store.currentKickerText, "SEP 12")
        // Selecting a segment re-anchors to the present - even the SAME segment.
        store.selectPeriod(.today)
        XCTAssertEqual(store.periodOffset, 0)
        XCTAssertFalse(store.canStepLater)
        XCTAssertEqual(store.currentKickerText, "TODAY")
        // Stepped year kicker through the store-level surface.
        store.selectPeriod(.year)
        store.stepPeriod(2)
        XCTAssertEqual(store.periodOffset, 2)
        XCTAssertEqual(store.currentKickerText, "2024")
    }

    func testE12WarmupPlanTwoUpSkipsVisitedStopsAtLimit() {
        func plan(_ p: UsagePeriod, _ off: Int, _ done: Set<Int> = []) -> [Int] {
            CompanionStore.warmupPlan(period: p, offset: off) { done.contains($0) }
        }
        XCTAssertEqual(plan(.today, 0), [1, 2])
        XCTAssertEqual(plan(.year, 0), [1, 2])
        XCTAssertEqual(plan(.today, 1, [0, 1, 2]), [3], "visited + warmed ones skip")
        XCTAssertEqual(plan(.year, 0, [1, 2]), [], "already-warmed ones never re-warm")
        XCTAssertEqual(plan(.today, 13), [], "at the walk-back limit nothing remains")
        XCTAssertEqual(plan(.year, 1), [2], "year's limit 2 leaves one step past")
    }

    func testGlanceSourceGating() {
        let all = CompanionComponents()
        XCTAssertEqual(CompanionStore.glanceSource(for: .today, components: all, today: Date(), calendar: utcCalendar), .insightsHourly)
        XCTAssertEqual(CompanionStore.glanceSource(for: .week, components: all, today: Date(), calendar: utcCalendar), .insightsDaily)
        XCTAssertEqual(CompanionStore.glanceSource(for: .month, components: all, today: Date(), calendar: utcCalendar), .stats)
        XCTAssertEqual(CompanionStore.glanceSource(for: .year, components: all, today: Date(), calendar: utcCalendar), .stats)

        var glanceOff = CompanionComponents(); glanceOff.activityGlance = false
        for period in UsagePeriod.allCases {
            XCTAssertNil(CompanionStore.glanceSource(for: period, components: glanceOff, today: Date(), calendar: utcCalendar))
        }

        var histOff = CompanionComponents(); histOff.activityHistogramTodayWeek = false
        XCTAssertNil(CompanionStore.glanceSource(for: .today, components: histOff, today: Date(), calendar: utcCalendar))
        XCTAssertNil(CompanionStore.glanceSource(for: .week, components: histOff, today: Date(), calendar: utcCalendar))
        XCTAssertEqual(CompanionStore.glanceSource(for: .month, components: histOff, today: Date(), calendar: utcCalendar), .stats)
    }

    func testGlanceFacesHideWhenAllZero() throws {
        let empty = try decodeFixture(InsightsResponse.self, "insights-empty.json")
        XCTAssertNil(CompanionStore.glanceFace(period: .today, insights: empty, stats: nil,
                                               components: CompanionComponents(), calendar: utcCalendar, now: frozenNow))
        XCTAssertNil(CompanionStore.glanceFace(period: .month, insights: nil, stats: nil,
                                               components: CompanionComponents(), calendar: utcCalendar, now: frozenNow))
        // histogram off means no face even with data present.
        var histOff = CompanionComponents(); histOff.activityHistogramTodayWeek = false
        let today = try decodeFixture(InsightsResponse.self, "insights-today.json")
        XCTAssertNil(CompanionStore.glanceFace(period: .today, insights: today, stats: nil,
                                               components: histOff, calendar: utcCalendar, now: frozenNow))
        guard case .hours(let bars, let peak) = CompanionStore.glanceFace(period: .today, insights: today, stats: nil,
                                                                          components: CompanionComponents(), calendar: utcCalendar, now: frozenNow) else {
            return XCTFail("healthy today shows the hour face")
        }
        XCTAssertEqual(bars.count, 24)
        XCTAssertEqual(peak, 14)
    }

    func testGridFacesFromSharedStatsFixture() throws {
        let stats = try decodeFixture(StatsResponse.self, "stats-contributions.json")
        guard case .grid(let columns, let filled90, let days90, let first90) = try XCTUnwrap(
            CompanionStore.glanceFace(period: .month, insights: nil, stats: stats,
                                      components: CompanionComponents(), calendar: utcCalendar, now: frozenNow)) else {
            return XCTFail("month grid")
        }
        XCTAssertEqual(days90, 90)
        XCTAssertEqual(filled90, 68)
        XCTAssertEqual(first90, "2026-04-27")
        XCTAssertEqual(columns.count, 13, "13 Monday-started columns cover 90 days ending Sun 2026-07-26")
        let outOfWindow = columns.flatMap { $0 }.filter { $0 == nil }.count
        XCTAssertGreaterThan(outOfWindow, 0, "pre-window cells stay nil")

        guard case .grid(_, let filled180, let days180, _) = try XCTUnwrap(
            CompanionStore.glanceFace(period: .year, insights: nil, stats: stats,
                                      components: CompanionComponents(), calendar: utcCalendar, now: frozenNow)) else {
            return XCTFail("year grid")
        }
        XCTAssertEqual(days180, 180)
        XCTAssertEqual(filled180, 143)
    }

    func testDayFaceAnchorsToTheClocksWeek() throws {
        let weekly = try decodeFixture(InsightsResponse.self, "insights-week.json")
        guard case .days(let tokens) = try XCTUnwrap(
            CompanionStore.glanceFace(period: .week, insights: weekly, stats: nil,
                                      components: CompanionComponents(), calendar: utcCalendar, now: frozenNow)) else {
            return XCTFail("week day face")
        }
        XCTAssertEqual(tokens.count, 7)
        // Data misses 2026-07-21 = Tuesday (index 1). Frozen clock Sun 2026-07-26 sits
        // in the same Mon..today window the fixture was fetched for, so the empty
        // column's POSITION is deterministic.
        XCTAssertEqual(tokens[0], 11_200_000)
        XCTAssertEqual(tokens[1], 0)
        XCTAssertEqual(tokens[2], 13_400_000)

        // The face is Mon..TODAY (contract §Activity glance): a daily facet that lags
        // into last week must not be presented as the current week's activity.
        let stale = try XCTUnwrap(CompanionStore.date(fromDayString: "2026-08-02", calendar: utcCalendar))
        XCTAssertNil(CompanionStore.glanceFace(period: .week, insights: weekly, stats: nil,
                                               components: CompanionComponents(), calendar: utcCalendar, now: stale),
                     "a stale payload reads all-zero for the current week and hides")
    }

    // MARK: - Unit: multi-server helpers and badges

    func testCombinedActiveMsRules() {
        XCTAssertEqual(CompanionStore.combinedActiveMs([(100, false), (200, false)]), 300)
        XCTAssertNil(CompanionStore.combinedActiveMs([(100, false), (nil, false)]), "any missing data drops the sum")
        XCTAssertNil(CompanionStore.combinedActiveMs([(100, false), (200, true)]), "any failed server drops the sum")
        XCTAssertNil(CompanionStore.combinedActiveMs([(nil, false)]))
    }

    func testServerBadgeVersionGate() {
        XCTAssertNotNil(CompanionStore.serverBadgeVersion(from:
            ServerUpdateCheckResponse(enabled: true, updateAvailable: true, latest: "1.2.3")))
        XCTAssertNil(CompanionStore.serverBadgeVersion(from:
            ServerUpdateCheckResponse(enabled: false, updateAvailable: true, latest: "1.2.3")))
        XCTAssertNil(CompanionStore.serverBadgeVersion(from:
            ServerUpdateCheckResponse(enabled: true, updateAvailable: false, latest: "1.2.3")))
        XCTAssertNil(CompanionStore.serverBadgeVersion(from:
            ServerUpdateCheckResponse(enabled: true, updateAvailable: true, latest: nil)))
    }

    func testToolNamesAndProviderPrefixStrip() {
        XCTAssertEqual(CompanionStore.toolDisplayName(for: "codex"), "Codex")
        XCTAssertEqual(CompanionStore.toolDisplayName(for: "opencode"), "OpenCode")
        XCTAssertEqual(CompanionStore.toolDisplayName(for: "mystery_tool"), "Mystery_tool")
        XCTAssertEqual(CompanionStore.logoAssetName(for: "codex"), "AgentCodex")
        XCTAssertEqual(CompanionStore.logoAssetName(for: "openclaw"), "AgentOpenClaw")
        XCTAssertEqual(CompanionStore.logoAssetName(for: "mystery_tool"), nil)
        XCTAssertEqual(CompanionStore.toolDisplayName(for: "pi_agent"), "Pi")
        XCTAssertEqual(CompanionStore.toolDisplayName(for: "dsh"), "DeepSeek Harness")
        XCTAssertEqual(CompanionStore.stripProviderPrefix("openai/gpt-5.6-sol"), "gpt-5.6-sol")
        XCTAssertEqual(CompanionStore.stripProviderPrefix("gpt-5.6-sol"), "gpt-5.6-sol")
    }

    /// Every source_name the scanner can emit (src/tokdash/sources/coding_tools.py) maps to
    /// a shipped mark - except the two documented text-only ids: mimo (wordmark-only brand,
    /// illegible at row height) and devin (no brand art shipped anywhere). Mirrors Windows
    /// ToolLogo_Map_Covers_Every_Scanner_Tool_And_Pins_Assets.
    func testToolLogoMarkMap() {
        let expected: [(String, String?)] = [
            ("opencode", "AgentOpenCode"), ("kilocode", "AgentKilocode"), ("cline", "AgentCline"),
            ("codex", "AgentCodex"), ("claude", "AgentClaude"), ("gemini_cli", "AgentGemini"),
            ("antigravity_cli", "AgentAntigravity"), ("amp", "AgentAmp"), ("kimi", "AgentKimi"),
            ("grok", "AgentGrok"), ("pi_agent", "AgentPi"), ("omp", "AgentOmp"),
            ("copilot_cli", "AgentCopilot"), ("hermes", "AgentHermes"), ("mimo", nil),
            ("zcode", "AgentZai"), ("qoder", "AgentQoder"), ("qoder_cli", "AgentQoder"),
            ("dsh", "AgentDsh"), ("reasonix", "AgentReasonix"), ("workbuddy", "AgentWorkbuddy"),
            ("zed", "AgentZed"), ("qwen_code", "AgentQwenCode"), ("muse", "AgentMuse"),
            ("crush", "AgentCrush"), ("minimax", "AgentMiniMax"), ("devin", nil),
            // Aliases the web brand map normalizes onto the same marks.
            ("claude_code", "AgentClaude"), ("gemini", "AgentGemini"), ("pi", "AgentPi"),
            ("copilot", "AgentCopilot"), ("github_copilot_cli", "AgentCopilot"),
            ("antigravity", "AgentAntigravity"), ("cursor", "AgentCursor"),
        ]
        let assets = #filePath.split(separator: "/").dropLast(2).joined(separator: "/")
            + "/TokdashCompanion/Assets.xcassets"
        let fm = FileManager.default
        for (tool, mark) in expected {
            XCTAssertEqual(CompanionStore.logoAssetName(for: tool), mark, "tool id \(tool)")
            if let mark {
                XCTAssertTrue(fm.fileExists(atPath: "\(assets)/\(mark).imageset"), "\(mark).imageset missing")
            }
        }
        // Dark-ink marks ship a dark-appearance variant in the imageset (the AgentZai
        // pattern) - the same pre-inverted pixels the Windows *-dark.png copies carry.
        for mark in ["AgentCline", "AgentHermes", "AgentOmp", "AgentZed", "AgentCursor"] {
            XCTAssertTrue(fm.fileExists(atPath: "\(assets)/\(mark).imageset")
                          && (try? String(contentsOfFile: "\(assets)/\(mark).imageset/Contents.json"))?.contains("luminosity") == true,
                          "\(mark) must carry a dark-appearance variant")
        }
    }

    /// The quota All-view header marks, pinned against the web brand map. Regression:
    /// MiniMax once wore the MiMo wordmark - MiMo is a separate provider and must never
    /// stand in. Mirrors Windows QuotaLogo_Mark_Map_Matches_The_Web_Brand_Map.
    func testQuotaLogoMarkMap() {
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "minimax"), "AgentMiniMax")
        XCTAssertNil(CompanionStore.quotaLogoAssetName(for: "mimo"), "no mimo quota provider exists; never borrow its wordmark")
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "codex"), "AgentCodex")
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "claude"), "AgentClaude")
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "kimi"), "AgentKimi")
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "grok"), "AgentGrok")
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "zai"), "AgentZai")
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "opencode_go"), "AgentOpenCode")
        XCTAssertEqual(CompanionStore.quotaLogoAssetName(for: "antigravity"), "AgentAntigravity")
        XCTAssertNil(CompanionStore.quotaLogoAssetName(for: "commandcode"))

        // Every returned mark must exist in the asset catalog source - the map and the
        // packaged marks cannot drift apart.
        let assets = #filePath.split(separator: "/").dropLast(2).joined(separator: "/")
            + "/TokdashCompanion/Assets.xcassets"
        let fm = FileManager.default
        for mark in ["AgentCodex", "AgentClaude", "AgentKimi", "AgentGrok", "AgentZai",
                     "AgentMiniMax", "AgentOpenCode", "AgentAntigravity"] {
            XCTAssertTrue(fm.fileExists(atPath: "\(assets)/\(mark).imageset"), "\(mark).imageset missing")
        }
        // The MiniMax imageset must carry minimax.png itself: it used to bundle a file
        // literally named mimo.png - that is the exact regression this pins.
        XCTAssertTrue(fm.fileExists(atPath: "\(assets)/AgentMiniMax.imageset/minimax.png"))
        XCTAssertFalse(fm.fileExists(atPath: "\(assets)/AgentMiniMax.imageset/mimo.png"),
                       "AgentMiniMax must not bundle the MiMo wordmark")
    }

    /// Rank shares are percent of the FULL list; rounding is away-from-zero on both
    /// platforms (mirrors Windows ShareOf), and a zero-sum list never yields NaN.
    func testRankShareHelperMirrorsWindows() {
        XCTAssertEqual(Snapshot.share(tokens: 550, total: 1000).1, "55%")
        XCTAssertEqual(Snapshot.share(tokens: 2, total: 3).1, "67%")
        XCTAssertEqual(Snapshot.share(tokens: 1, total: 3).1, "33%")
        let zero = Snapshot.share(tokens: 0, total: 0)
        XCTAssertEqual(zero.0, 0.0)
        XCTAssertEqual(zero.1, "0%")
    }

    // MARK: - Unit: settings schema v3

    func testSettingsV2MigratesToV3WithAllComponentsOn() throws {
        let data = Data("""
        { "version": 2, "servers": [ { "id": "a", "label": "Local", "baseUrl": "http://127.0.0.1:55423", "enabled": true } ],
          "lowQuotaNotifications": true }
        """.utf8)
        let settings = try JSONDecoder().decode(CompanionSettings.self, from: data)
        XCTAssertTrue(settings.lowQuotaNotifications)
        XCTAssertTrue(settings.components.fullDeltaRow)
        XCTAssertTrue(settings.components.topRanks)
        XCTAssertTrue(settings.components.resetCredits)
        XCTAssertTrue(settings.components.activityGlance)
        XCTAssertTrue(settings.components.activityHistogramTodayWeek)
        XCTAssertTrue(settings.components.perServerRows)
        XCTAssertEqual(settings.selectedPeriod, .today)

        let roundTripped = try JSONDecoder().decode(CompanionSettings.self, from: try JSONEncoder().encode(settings))
        // Version is pinned to 3 on every write.
        let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: try JSONEncoder().encode(settings)) as? [String: Any])
        XCTAssertEqual(encoded["version"] as? Int, 3)
        XCTAssertEqual(roundTripped.selectedPeriod, .today)

        var edited = settings
        edited.selectedPeriod = .week
        edited.components.activityGlance = false
        let saved = try JSONDecoder().decode(CompanionSettings.self, from: try JSONEncoder().encode(edited))
        XCTAssertEqual(saved.selectedPeriod, .week)
        XCTAssertFalse(saved.components.activityGlance)
        XCTAssertTrue(saved.components.topRanks)
    }

    func testSettingsForwardCompatUnknownKeysAndComponents() throws {
        // A future build writes a segment and component we do not know: forward-compat
        // decode keeps the file, defaults the unknown, ignores the extra keys.
        let data = Data("""
        { "version": 4, "servers": [ { "id": "a", "label": "Local", "baseUrl": "http://x", "enabled": true } ],
          "selectedPeriod": "quarter",
          "components": { "fullDeltaRow": false, "hyperLocal": true },
          "brandNewField": 7 }
        """.utf8)
        let settings = try JSONDecoder().decode(CompanionSettings.self, from: data)
        XCTAssertEqual(settings.selectedPeriod, .today, "unknown period token falls back, file survives")
        XCTAssertFalse(settings.components.fullDeltaRow)
        XCTAssertTrue(settings.components.topRanks)

        // Partial components objects (contract settings_overrides shape) keep every
        // unlisted component ON.
        let partial = try JSONDecoder().decode(CompanionComponents.self,
                                               from: Data(#"{ "fullDeltaRow": false }"#.utf8))
        XCTAssertFalse(partial.fullDeltaRow)
        XCTAssertTrue(partial.activityGlance)
        XCTAssertTrue(partial.perServerRows)
    }

    // MARK: - Wire provider order (All-view group order source)

    func testWireProviderKeysIsAStructuralScan() throws {
        // The fixture's own document order.
        XCTAssertEqual(QuotaResponse.wireProviderKeys(in: try fixtureData("quota.json")),
                       ["codex", "claude", "kimi"])
        // String VALUES are scanned as strings, never as structure: braces, colons,
        // escaped quotes and even the word "providers" inside a value must not move
        // the key scan.
        let tricky = #"""
        {"enabled":true,"status_detail":"failed parsing providers: {\"x\": 1} {",
         "providers":{"zeta":{"buckets":[{"bucket":"5h","label":"5{h}: \"quoted\""}]},
                      "alpha":{"provider":"alpha"}},
         "timestamp":1}
        """#
        XCTAssertEqual(QuotaResponse.wireProviderKeys(in: Data(tricky.utf8)), ["zeta", "alpha"])
        // Absent / null / non-object payloads yield nil - the view falls back to a
        // sorted (deterministic) order instead of an arbitrary one.
        XCTAssertNil(QuotaResponse.wireProviderKeys(in: Data(#"{"enabled":true,"timestamp":1}"#.utf8)))
        XCTAssertNil(QuotaResponse.wireProviderKeys(in: Data(#"{"providers":null}"#.utf8)))
        XCTAssertNil(QuotaResponse.wireProviderKeys(in: Data("[]".utf8)))
    }

    func testQuotaDecodeCarriesTheWireOrder() throws {
        let q = try QuotaResponse.decode(from: try fixtureData("quota.json"))
        XCTAssertEqual(q.providerWireOrder, ["codex", "claude", "kimi"])
        XCTAssertEqual(q.providers?.count, 3)
    }

    func testWireProviderKeysUnescapesKeys() throws {
        // Returned keys must EQUAL what JSONDecoder produced from the same bytes,
        // or the merged lookup misses and the provider silently vanishes. Covers
        // escaped backslash before the closing quote, quote/backslash/slash, the
        // short escapes, \uXXXX, a surrogate pair, and raw non-ASCII.
        // The é keys arrive as SIX-CHARACTER escape text and the emoji as a
        // surrogate PAIR: the scanner must decode them to the same Swift
        // strings JSONDecoder yields from the same bytes.
        let json = #"{"providers":{"back\\":{"b":[{"bucket":"weekly"}]},"a\"b\\c\/d":{"b":[]},"caf\u00e9":{"b":[]},"cAf\u00e9":{"b":[]},"\ud83d\ude00":{"b":[]},"tab\tnew":{"b":[]}}"#
        XCTAssertEqual(QuotaResponse.wireProviderKeys(in: Data(json.utf8)),
                       [#"back\"#, #"a"b\c/d"#, "café", "cAfé", "😀", "tab\tnew"])
    }

    func testEscapedProviderKeySurvivesMerge() throws {
        // The provider id arrives as six-character escape text: the merged lookup
        // matches only if the scanner unescaped it to exactly what JSONDecoder
        // decoded from the same bytes.
        let json = "{\"enabled\":true,\"providers\":{\"caf\\u00e9\":{\"estimated\":false,\"buckets\":[{\"bucket\":\"weekly\"}]}}}"
        let q = try QuotaResponse.decode(from: Data(json.utf8))
        XCTAssertEqual(q.providerWireOrder, ["café"])
        let snap = Snapshot(quota: CompanionStore.mergedQuota([("S", q)]), thresholds: .defaults)
        XCTAssertEqual(snap.allQuotaGroups.map(\.provider), ["S · Café"],
                       "an escaped key must not drop out of the merged All view")
    }

    func testDuplicateServerLabelsCollapseToOneGroup() throws {
        // Two enabled servers both left at the default label: one group per
        // provider, first position wins (the Windows Dictionary collapses the same
        // way) - never the same group rendered twice.
        let q = try QuotaResponse.decode(from: try fixtureData("quota.json"))
        let merged = CompanionStore.mergedQuota([("Local", q), ("Local", q)])
        XCTAssertEqual(merged.providerWireOrder, ["Local · codex", "Local · claude", "Local · kimi"])
        let snap = Snapshot(quota: merged, thresholds: .defaults)
        XCTAssertEqual(snap.allQuotaGroups.map(\.provider),
                       ["Local · Codex", "Local · Claude", "Local · Kimi"])
    }

    func testAllQuotaGroupsDedupsRepeatedWireKeys() throws {
        // A payload repeating a provider key: JSONDecoder keeps the last value,
        // the scanner sees every occurrence; the view renders ONE group, once,
        // at its first position.
        var q = try QuotaResponse.decode(from: try fixtureData("quota.json"))
        q.providerWireOrder = ["codex", "claude", "kimi", "codex"]
        let snap = Snapshot(quota: q, thresholds: .defaults)
        XCTAssertEqual(snap.allQuotaGroups.map(\.provider), ["Codex", "Claude", "Kimi"])
    }

    // MARK: - Evidence render (skipped unless the sentinel file exists)

    /// Renders the REAL flyout UI (ContentView + real store) against the fixture
    /// HTTP server and writes a PNG - the macOS real-system evidence artifact.
    /// Activation is a sentinel file containing the output path (xcodebuild
    /// launches the test process with a clean environment, so shell env vars
    /// never reach it). Off by default so normal suite runs never touch the
    /// network or the screen.
    @MainActor
    func testEvidenceRenderFlyoutPNG() async throws {
        let sentinel = URL(fileURLWithPath: "/tmp/tokdash-evidence-png.txt")
        // Line 1: output PNG path. Line 2 (optional): UsagePeriod token - xcodebuild's
        // test runner does not forward shell env vars, so the period knob is the file.
        // Line 3 (optional): rankRows (3...8) to evidence the top-ranks growth layout.
        // Line 4 (optional): E12 instance offset - steps the selected granularity back
        // N units after the present snapshot lands, evidencing the kicker stepper.
        let sentinelLines = ((try? String(contentsOf: sentinel, encoding: .utf8)) ?? "")
            .split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let outPath = sentinelLines.first?.trimmingCharacters(in: .whitespacesAndNewlines)
        let period = sentinelLines.count > 1 ? sentinelLines[1].trimmingCharacters(in: .whitespacesAndNewlines) : "today"
        let rankRows = min(8, max(3, Int(sentinelLines.count > 2
            ? (sentinelLines[2].trimmingCharacters(in: .whitespacesAndNewlines) ?? "") ?? "" : "") ?? 3))
        let stepOffset = sentinelLines.count > 3
            ? max(0, Int(sentinelLines[3].trimmingCharacters(in: .whitespacesAndNewlines) ?? "") ?? 0) : 0
        guard let outPath, !outPath.isEmpty else {
            throw XCTSkip("evidence render disabled (write output path to \(sentinel.path))")
        }
        let settingsURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("tokdash-evidence-settings.json")
        try """
        {"version":3,"servers":[{"id":"evidence","label":"Evidence","baseUrl":"http://127.0.0.1:8123","enabled":true}],"language":"english","selectedPeriod":"\(period)","rankRows":\(rankRows)}
        """.write(to: settingsURL, atomically: true, encoding: .utf8)
        // Restore the process-global seams on every exit path: XCTest method order
        // is not a guarantee to rely on, and a leaked pathOverride would silently
        // point other tests' stores at the evidence settings file.
        let previousOverride = CompanionSettings.pathOverride
        let previousLanguage = L10n.current
        defer {
            CompanionSettings.pathOverride = previousOverride
            L10n.current = previousLanguage
        }
        CompanionSettings.pathOverride = settingsURL
        L10n.current = .english

        let store = CompanionStore()
        store.refresh()
        let deadline = Date().addingTimeInterval(12)
        while (store.snapshot == nil || store.connectionState != .connected) && Date() < deadline {
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        let snap = try XCTUnwrap(store.snapshot, "fixture server never produced a snapshot")
        XCTAssertEqual(store.connectionState, .connected, "fixture server must answer /health")
        XCTAssertFalse(snap.usageLoading)

        // E12: step the instance back before rendering, so the PNG evidences the kicker
        // stepper + the stepped kicker over real held data. The fixture server answers
        // every window with the same payload - this render evidences the UI delta; the
        // window math itself is pinned by the suite's stepped-path tests.
        if stepOffset > 0 {
            store.stepPeriod(stepOffset)
            let stepDeadline = Date().addingTimeInterval(12)
            while (store.snapshot?.instanceOffset != stepOffset || store.snapshot?.usageLoading == true)
                && Date() < stepDeadline {
                try await Task.sleep(nanoseconds: 200_000_000)
            }
            XCTAssertEqual(store.snapshot?.instanceOffset, stepOffset, "stepped snapshot must arrive")
        }

        // The shipped root view at the shipped popover width, hosted in a real
        // NSWindow like the NSPanel hosts it, then rasterized through AppKit's own
        // cacheDisplay path. ImageRenderer is NOT used: it does not rasterize the
        // AppKit-backed segmented Pickers (they come out as empty placeholders),
        // while cacheDisplay draws the real control cells.
        let width = CompanionLayout.popoverWidth
        let root = ContentView()
            .environmentObject(store)
            .frame(width: width)
            .background(Color(NSColor.windowBackgroundColor))
        let host = NSHostingView(rootView: root)
        host.frame = NSRect(x: 0, y: 0, width: width, height: 1)
        host.layoutSubtreeIfNeeded()
        let fitting = host.fittingSize
        host.frame = NSRect(x: 0, y: 0, width: width, height: max(fitting.height, 100))
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = host
        // Close on every exit path - an assertion failure mid-capture must not
        // leave a flyout window floating on the developer's screen.
        defer { window.orderOut(nil) }
        window.orderFrontRegardless()
        try await Task.sleep(nanoseconds: 600_000_000)  // one draw pass
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            XCTFail("no bitmap rep for caching display")
            return
        }
        host.cacheDisplay(in: host.bounds, to: rep)
        let png = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
        try png.write(to: URL(fileURLWithPath: outPath))
        NSLog("EVIDENCE-PNG \(outPath) \(rep.pixelsWide)x\(rep.pixelsHigh)")

        // Second artifact: the All quota view, where the E2 reset-credits row lives
        // (the Low view never shows it - provider context, not a window).
        store.quotaView = .all
        host.needsDisplay = true
        try await Task.sleep(nanoseconds: 600_000_000)
        let allPath = (outPath as NSString).deletingPathExtension + "-all." +
            (outPath as NSString).pathExtension
        host.frame = NSRect(x: 0, y: 0, width: width, height: max(host.fittingSize.height, 100))
        window.setFrame(host.frame, display: false)
        guard let rep2 = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            XCTFail("no bitmap rep for caching display (all view)")
            return
        }
        host.cacheDisplay(in: host.bounds, to: rep2)
        let png2 = try XCTUnwrap(rep2.representation(using: .png, properties: [:]))
        try png2.write(to: URL(fileURLWithPath: allPath))
        NSLog("EVIDENCE-PNG \(allPath) \(rep2.pixelsWide)x\(rep2.pixelsHigh)")
    }

    private func contractURL(_ relativePath: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("contract")
            .appendingPathComponent(relativePath)
    }
}
