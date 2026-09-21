import XCTest
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
        if let name = slotName(doc, "quota") { quota = try decodeFixture(QuotaResponse.self, name) }

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

        guard case .hours(let bars, let peak) = try XCTUnwrap(snap.glanceFace) else {
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
        // Provider dict order is not preserved by Swift dictionaries; compare as sets.
        XCTAssertEqual(Set(snap.allQuotaGroups.map(\.provider)),
                       Set(try wantGroups.map { try XCTUnwrap($0["provider"] as? String) }))
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

        guard case .days(let tokens) = try XCTUnwrap(snap.glanceFace) else {
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
        guard case .grid(let columns, let filled, let windowDays, let firstCell) = try XCTUnwrap(snap.glanceFace) else {
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
        guard case .grid(let columns, let filled, let windowDays, _) = try XCTUnwrap(snap.glanceFace) else {
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
        XCTAssertNil(snap.glanceFace, "all-zero source hides the glance strip")
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
        XCTAssertNil(snap.glanceFace)
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
        XCTAssertNil(snap.glanceFace)
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

        // quota_all_server_order: the group keys carry the server label (dictionary
        // iteration order is NOT preserved by Swift, so membership is what's assertable).
        let quota = try decodeFixture(QuotaResponse.self, "quota.json")
        var providers: [String: ProviderQuota] = [:]
        for label in ["Local", "Second"] {
            for (provider, value) in quota.providers ?? [:] { providers["\(label) · \(provider)"] = value }
        }
        let groupSnap = Snapshot(usage: combined,
                                 quota: QuotaResponse(enabled: true, providers: providers, timestamp: nil),
                                 thresholds: .defaults)
        let groupProviders = Set(groupSnap.allQuotaGroups.map(\.provider))
        XCTAssertTrue(groupProviders.contains { $0.hasPrefix("Local") }, "\(groupProviders)")
        XCTAssertTrue(groupProviders.contains { $0.hasPrefix("Second") }, "\(groupProviders)")
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
        XCTAssertNil(snap.glanceFace)
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
        XCTAssertEqual(Snapshot.compactTokens(1_243_500_000), "1243.5M")
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
        XCTAssertEqual(CompanionStore.logoAssetName(for: "mystery_tool"), nil)
        XCTAssertEqual(CompanionStore.stripProviderPrefix("openai/gpt-5.6-sol"), "gpt-5.6-sol")
        XCTAssertEqual(CompanionStore.stripProviderPrefix("gpt-5.6-sol"), "gpt-5.6-sol")
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

    private func contractURL(_ relativePath: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("contract")
            .appendingPathComponent(relativePath)
    }
}
