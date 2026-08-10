import XCTest
@testable import TokdashCompanion

final class SnapshotTests: XCTestCase {

    /// Redirect settings persistence to a temp file before any store is built: constructing
    /// a CompanionStore loads the settings file, and its invalid-base-URL repair writes one.
    override class func setUp() {
        super.setUp()
        TestSettings.install()
    }

    @MainActor
    func testMenuBarIconHasExplicitStatusItemSizeAndTemplateRendering() {
        XCTAssertEqual(CompanionMenuBarIcon.artworkSize, NSSize(width: 15, height: 16))
        XCTAssertEqual(CompanionMenuBarIcon.image.size, NSSize(width: 15, height: 20))
        XCTAssertTrue(CompanionMenuBarIcon.image.isTemplate)
    }

    func testCompactTokens() {
        XCTAssertEqual(Snapshot.compactTokens(0), "0")
        XCTAssertEqual(Snapshot.compactTokens(999), "999")
        XCTAssertEqual(Snapshot.compactTokens(249669), "249k")
        XCTAssertEqual(Snapshot.compactTokens(18_700_000), "18.7M")
    }

    func testQuotaThresholdMapping() {
        let t = QuotaThresholds.defaults
        XCTAssertEqual(t.threshold(for: "5h"), 20)
        XCTAssertEqual(t.threshold(for: "5-hour"), 20)
        XCTAssertEqual(t.threshold(for: "weekly"), 10)
        XCTAssertEqual(t.threshold(for: "7d"), 10)
        XCTAssertEqual(t.threshold(for: "other"), 15)
    }

    func testIsLow() {
        let t = QuotaThresholds.defaults
        let lowFiveHour = makeRow(bucket: "5h", left: 14)
        let okFiveHour = makeRow(bucket: "5h", left: 25)
        let lowWeekly = makeRow(bucket: "weekly", left: 8)
        let okWeekly = makeRow(bucket: "weekly", left: 12)
        XCTAssertTrue(lowFiveHour.isLow(thresholds: t))
        XCTAssertFalse(okFiveHour.isLow(thresholds: t))
        XCTAssertTrue(lowWeekly.isLow(thresholds: t))
        XCTAssertFalse(okWeekly.isLow(thresholds: t))
    }

    func testClaudeBucketNormalizationForThreshold() {
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }

        let t = QuotaThresholds.defaults  // 5h=20, weekly=10, other=15

        // Claude's real bucket ids map to the canonical windows so they share Codex's thresholds
        // instead of falling into the 15% "other" bucket. Generic windows use the standard
        // companion names, while model-scoped weekly windows keep their model label.
        let session = makeClaudeRow(bucket: "session", label: "Session", left: 14)
        XCTAssertEqual(session.canonicalBucket, "5h")
        XCTAssertEqual(session.displayBucketLabel, "5-hour")
        XCTAssertTrue(session.isLow(thresholds: t), "session -> 5h threshold (20%); 14% is low")
        XCTAssertFalse(makeClaudeRow(bucket: "session", label: "Session", left: 25).isLow(thresholds: t))

        let weeklyScoped = makeClaudeRow(bucket: "weekly_scoped_opus", label: "Opus", left: 8)
        XCTAssertEqual(weeklyScoped.canonicalBucket, "weekly")
        XCTAssertEqual(weeklyScoped.displayBucketLabel, "Opus")
        XCTAssertTrue(weeklyScoped.isLow(thresholds: t), "weekly_scoped -> weekly threshold (10%); 8% is low")
        XCTAssertEqual(makeClaudeRow(bucket: "weekly_scoped_fable", label: "Fable", left: 8).displayBucketLabel, "Fable")
        XCTAssertEqual(makeClaudeRow(bucket: "weekly_all", label: "Weekly All", left: 8).displayBucketLabel, "Weekly")

        // Legacy fallback bucket ids from the older API shape.
        XCTAssertEqual(makeClaudeRow(bucket: "five_hour", label: "5-hour", left: 10).canonicalBucket, "5h")
        XCTAssertEqual(makeClaudeRow(bucket: "seven_day", label: "7-day", left: 10).canonicalBucket, "weekly")

        // An unrecognised Claude bucket falls through to "other" - we don't guess its window.
        let unknown = makeClaudeRow(bucket: "usage_claude_sonnet_4", label: "Claude Sonnet 4", left: 10)
        XCTAssertEqual(unknown.canonicalBucket, "usage_claude_sonnet_4")
        XCTAssertTrue(unknown.isLow(thresholds: t), "unknown -> other (15%); 10% is low")

        // Non-Claude providers are untouched: a "session" bucket on another provider stays itself.
        XCTAssertEqual(makeRow(bucket: "session", left: 10).canonicalBucket, "session")
    }

    func testResetsTextIsRelative() {
        // Pin English: resetsText now routes through L10n, so the assertions are locale-stable.
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }

        // Pure form: seconds-remaining -> text, no clock dependency.
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: -10), "resets soon")   // past/stale
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 0), "resets soon")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 59), "resets soon")     // sub-minute
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 60), "resets in 1 minute")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 119), "resets in 1 minute")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 120), "resets in 2 minutes")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 5400), "resets in 90 minutes")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 7199), "resets in 119 minutes", "max minute value stays under 120")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 7200), "resets in 2 hours")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 18000), "resets in 5 hours")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 86399), "resets in 23 hours", "max hour value stays under 24")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 86400), "resets in 1 day")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 129600), "resets in 1 day", "1.5d floors to the whole unit")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 259200), "resets in 3 days")
        // The antigravity weekly case that motivated the days tier: 3d22h reads as days here
        // and as "resets in 3 days" on the web dashboard, not "resets in 94 hours".
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: (3 * 24 + 22) * 3600), "resets in 3 days")
        XCTAssertEqual(QuotaRow.resetsText(forRemaining: 7 * 24 * 3600), "resets in 7 days")

        // A nil resets_at renders nothing (bucket without a reset time).
        XCTAssertEqual(makeRow(bucket: "5h", left: 10).resetsText, "")
    }

    func testL10nChineseTranslationsAndParity() {
        let saved = L10n.current
        L10n.current = .zhHans
        defer { L10n.current = saved }

        // A few representative values, including format strings with arguments. Note %% in the
        // template collapses to a single % after String(format:).
        XCTAssertEqual(L10n.t("today"), "今日")
        XCTAssertEqual(L10n.t("tracking_off"), "订阅跟踪已关闭")
        XCTAssertEqual(L10n.t("percent_left", 14), "剩余 14%")
        XCTAssertEqual(L10n.t("server_connected", "wsl"), "wsl · 已连接")
        XCTAssertEqual(CompanionStore.serverLabel(for: "http://127.0.0.1:55423"), "本地")
        XCTAssertEqual(L10n.t("comparison_below", 12), "低于昨日 12%")
        XCTAssertEqual(L10n.t("resets_in_hours", 5, ""), "5 小时后重置")
        XCTAssertEqual(L10n.t("resets_in_days", 3, ""), "3 天后重置")
        XCTAssertEqual(makeClaudeRow(bucket: "session", label: "Session", left: 14).displayBucketLabel, "5 小时")
        XCTAssertEqual(makeClaudeRow(bucket: "weekly_all", label: "Weekly All", left: 8).displayBucketLabel, "每周")
        XCTAssertEqual(makeClaudeRow(bucket: "weekly_scoped_fable", label: "Fable", left: 8).displayBucketLabel, "Fable")

        // English still resolves with an explicit choice.
        L10n.current = .english
        XCTAssertEqual(L10n.t("today"), "TODAY")
        XCTAssertEqual(L10n.t("percent_left", 14), "14% left")

        // Every English key has a Chinese translation (no silent fallback to English).
        let enKeys = Set(L10n.keys(for: .english))
        let zhKeys = Set(L10n.keys(for: .zhHans))
        XCTAssertEqual(enKeys.subtracting(zhKeys), [], "zh-Hans is missing keys: \(enKeys.subtracting(zhKeys).sorted())")
    }

    func testSystemLanguageUsesPrimaryPreferenceOnly() {
        XCTAssertEqual(L10n.resolve(.system, preferredLanguages: ["en-GB", "zh-Hans"]), .english)
        XCTAssertEqual(L10n.resolve(.system, preferredLanguages: ["zh-Hans", "en-GB"]), .zhHans)
        XCTAssertEqual(L10n.resolve(.system, preferredLanguages: []), .english)
        XCTAssertEqual(L10n.resolve(.zhHans, preferredLanguages: ["en-GB"]), .zhHans)
    }

    func testLegacySettingsDecodePreservesPreferencesAndDefaultsLanguage() throws {
        let data = Data("""
        {
          "baseURL": "https://wsl.example.test/tokdash",
          "launchAtLogin": true,
          "lowQuotaNotifications": true,
          "thresholds": {
            "fiveHour": 27,
            "weekly": 13,
            "other": 19
          }
        }
        """.utf8)

        let settings = try JSONDecoder().decode(CompanionSettings.self, from: data)
        XCTAssertEqual(settings.baseURL, "https://wsl.example.test/tokdash")
        XCTAssertTrue(settings.launchAtLogin)
        XCTAssertTrue(settings.lowQuotaNotifications)
        XCTAssertEqual(settings.thresholds, QuotaThresholds(fiveHour: 27, weekly: 13, other: 19))
        XCTAssertEqual(settings.language, .system)

        let migratedData = try JSONEncoder().encode(settings)
        let migrated = try JSONDecoder().decode(CompanionSettings.self, from: migratedData)
        XCTAssertEqual(migrated.baseURL, settings.baseURL)
        XCTAssertEqual(migrated.language, .system)
    }

    // MARK: - Refresh scheduler timing

    func testComputeDelay() {
        let now = Date(timeIntervalSince1970: 1000)
        let stale = Date(timeIntervalSince1970: 940)   // 60s ago
        let fresh = Date(timeIntervalSince1970: 990)   // 10s ago

        // Closed: every 10 minutes.
        XCTAssertEqual(CompanionStore.computeDelay(open: false, failures: 0, partial: false, lastFetch: stale, now: now), 600)
        // Open + stale (>=60s): immediately.
        XCTAssertEqual(CompanionStore.computeDelay(open: true, failures: 0, partial: false, lastFetch: stale, now: now), 0)
        // Open + fresh: wait the remainder of 60s.
        XCTAssertEqual(CompanionStore.computeDelay(open: true, failures: 0, partial: false, lastFetch: fresh, now: now), 50)
        // Open + no prior data: immediately.
        XCTAssertEqual(CompanionStore.computeDelay(open: true, failures: 0, partial: false, lastFetch: nil, now: now), 0)
        // Partial failure: 15s short retry, open or closed.
        XCTAssertEqual(CompanionStore.computeDelay(open: true, failures: 0, partial: true, lastFetch: fresh, now: now), 15)
        XCTAssertEqual(CompanionStore.computeDelay(open: false, failures: 0, partial: true, lastFetch: fresh, now: now), 15)
        // Backoff: 15 / 30 / 60 / 300 (caps); takes precedence over partial.
        XCTAssertEqual(CompanionStore.computeDelay(open: true, failures: 1, partial: false, lastFetch: stale, now: now), 15)
        XCTAssertEqual(CompanionStore.computeDelay(open: true, failures: 2, partial: false, lastFetch: stale, now: now), 30)
        XCTAssertEqual(CompanionStore.computeDelay(open: true, failures: 3, partial: false, lastFetch: stale, now: now), 60)
        XCTAssertEqual(CompanionStore.computeDelay(open: false, failures: 4, partial: true, lastFetch: stale, now: now), 300)
    }

    // MARK: - Quota Low-view correctness

    func testLowQuotaRowsPreserveProviderAndEstimated() {
        let quota = QuotaResponse(
            enabled: true,
            providers: ["codex": ProviderQuota(estimated: true, buckets: [
                BucketQuota(bucket: "5h", bucketLabel: "5-hour", remainingPercent: 14, resetsAt: nil, account: "default")
            ])],
            timestamp: nil
        )
        let snap = Snapshot(today: .empty, month: .empty, quota: quota, thresholds: .defaults)
        let low = snap.lowQuotaRows

        XCTAssertEqual(low.count, 1)
        XCTAssertEqual(low[0].provider, "Codex")
        XCTAssertTrue(low[0].estimated, "Low view must preserve the provider-level estimated flag")
        XCTAssertEqual(low[0].left, 14)
    }

    func testLowQuotaRowsTakeTwoLowestSorted() {
        let quota = QuotaResponse(
            enabled: true,
            providers: ["codex": ProviderQuota(estimated: false, buckets: [
                BucketQuota(bucket: "5h", bucketLabel: "5h", remainingPercent: 8, resetsAt: nil, account: nil),
                BucketQuota(bucket: "weekly", bucketLabel: "weekly", remainingPercent: 5, resetsAt: nil, account: nil),
                BucketQuota(bucket: "other", bucketLabel: "other", remainingPercent: 2, resetsAt: nil, account: nil),
            ])],
            timestamp: nil
        )
        let snap = Snapshot(today: .empty, month: .empty, quota: quota, thresholds: .defaults)
        let low = snap.lowQuotaRows

        XCTAssertEqual(low.count, 2)
        XCTAssertEqual(low[0].left, 2)
        XCTAssertEqual(low[1].left, 5)
    }

    func testSharedMultiServerFixturePinsCombineLowDedupAndMinimumDelay() throws {
        let expectedData = try Data(contentsOf: contractURL("expected/multi-server.json"))
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: expectedData) as? [String: Any])
        let expected = try XCTUnwrap(document["expected"] as? [String: Any])
        let expectedToday = try XCTUnwrap(expected["today"] as? [String: Any])
        let usageData = try Data(contentsOf: contractURL("fixtures/usage-today.json"))
        let today = try JSONDecoder().decode(UsageResponse.self, from: usageData)
        let combined = CompanionStore.combineUsage([today, today])

        XCTAssertEqual(String(combined.totalTokens), expectedToday["tokens_exact"] as? String)
        XCTAssertEqual(String(combined.totalMessages), expectedToday["messages"] as? String)
        XCTAssertEqual(Snapshot(today: combined, month: .empty, quota: .empty, thresholds: .defaults).todayCostText,
                       expectedToday["cost"] as? String)
        XCTAssertEqual(Int((combined.comparison?.costPct ?? 0).rounded()), -12)

        let quotaData = try Data(contentsOf: contractURL("fixtures/quota.json"))
        let quota = try JSONDecoder().decode(QuotaResponse.self, from: quotaData)
        var providers: [String: ProviderQuota] = [:]
        for label in ["Local", "Second"] {
            for (provider, value) in quota.providers ?? [:] { providers["\(label) · \(provider)"] = value }
        }
        let snap = Snapshot(today: combined, month: .empty,
                            quota: QuotaResponse(enabled: true, providers: providers, timestamp: nil), thresholds: .defaults)
        let expectedLow = try XCTUnwrap(expected["quota_low"] as? [String: Any])
        XCTAssertEqual(expectedLow["dedupe_identical_subscriptions"] as? Bool, true)
        XCTAssertLessThanOrEqual(snap.lowQuotaRows.count, try XCTUnwrap(expectedLow["visible_count_max"] as? Int))
        XCTAssertTrue(snap.lowQuotaRows.allSatisfy { !$0.provider.contains(" · ") })
        let lowerCaseLabelSnap = Snapshot(today: .empty, month: .empty,
            quota: QuotaResponse(enabled: true, providers: ["wsl · codex": try XCTUnwrap(quota.providers?["codex"])], timestamp: nil),
            thresholds: .defaults)
        XCTAssertEqual(lowerCaseLabelSnap.allQuotaGroups.first?.provider, "wsl · Codex")

        XCTAssertEqual(expected["delay_rule"] as? String, "minimum per-server delay")
        XCTAssertEqual(CompanionStore.minimumDelay([
            CompanionStore.computeDelay(open: true, failures: 2, partial: false, lastFetch: nil, now: Date()),
            CompanionStore.computeDelay(open: true, failures: 3, partial: false, lastFetch: nil, now: Date()),
        ]), 30)
    }

    private func contractURL(_ relativePath: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("contract")
            .appendingPathComponent(relativePath)
    }

    private func makeRow(bucket: String, left: Double) -> QuotaRow {
        QuotaRow(provider: "test", bucket: BucketQuota(
            bucket: bucket,
            bucketLabel: bucket,
            remainingPercent: left,
            resetsAt: nil,
            account: "default"
        ))
    }

    private func makeClaudeRow(bucket: String, label: String, left: Double) -> QuotaRow {
        QuotaRow(provider: "Claude", bucket: BucketQuota(
            bucket: bucket,
            bucketLabel: label,
            remainingPercent: left,
            resetsAt: nil,
            account: "default"
        ))
    }

    // MARK: - Provider-level quota failures (spec §7)

    func testProviderFailureFlagsGroupAndRows() {
        // Group failure (provider status != "ok" OR a non-empty status_detail) drives the
        // header warning; row failure compares each bucket's capturedAt against the
        // provider's statusAt. Every bucket reports status "ok" - that field cannot
        // discriminate, which is exactly why freshness does. codex is fully failed (its
        // bucket predates statusAt); antigravity is partially failed (one bucket captured
        // in the failing cycle, one older); claude is healthy.
        let statusAt = 1785030000
        let quota = QuotaResponse(
            enabled: true,
            providers: [
                "codex": ProviderQuota(estimated: false, buckets: [
                    BucketQuota(bucket: "5h", bucketLabel: "5h", remainingPercent: 14, resetsAt: nil, account: "a", capturedAt: statusAt - 30000)
                ], status: "error", statusDetail: "unreachable", statusAt: statusAt),
                "antigravity": ProviderQuota(estimated: false, buckets: [
                    BucketQuota(bucket: "5h", bucketLabel: "5h", remainingPercent: 80, resetsAt: nil, account: "c", capturedAt: statusAt),
                    BucketQuota(bucket: "weekly", bucketLabel: "weekly", remainingPercent: 5, resetsAt: nil, account: "d", capturedAt: statusAt - 30000)
                ], status: "ok", statusDetail: "stale_token", statusAt: statusAt),
                "claude": ProviderQuota(estimated: true, buckets: [
                    BucketQuota(bucket: "5h", bucketLabel: "5h", remainingPercent: 71, resetsAt: nil, account: "b", capturedAt: statusAt)
                ])
            ],
            timestamp: nil
        )
        let snap = Snapshot(today: .empty, month: .empty, quota: quota, thresholds: .defaults)
        let groups = snap.allQuotaGroups
        let codex = groups.first(where: { $0.provider == "Codex" })!
        let antigravity = groups.first(where: { $0.provider == "Antigravity" })!
        let claude = groups.first(where: { $0.provider == "Claude" })!
        XCTAssertTrue(codex.failed, "error-status provider must be flagged failed")
        XCTAssertTrue(antigravity.failed, "status=ok with non-empty status_detail must be flagged failed")
        XCTAssertFalse(claude.failed, "absent-status provider with no detail must not be failed")
        XCTAssertTrue(codex.rows.allSatisfy { $0.failed }, "capturedAt < statusAt -> last-known")
        XCTAssertFalse(antigravity.rows.first(where: { $0.bucket == "5h" })!.failed, "capturedAt == statusAt is fresh, not failed")
        XCTAssertTrue(antigravity.rows.first(where: { $0.bucket == "weekly" })!.failed, "the older sibling window is last-known")
        XCTAssertFalse(claude.rows.contains { $0.failed }, "a healthy provider never flags rows")

        // Low view: rows carry their own flag, so the ⚠ lands only on the last-known ones.
        let low = snap.lowQuotaRows
        XCTAssertEqual(low.count, 2)
        XCTAssertEqual(low[0].provider, "Antigravity")
        XCTAssertTrue(low[0].failed, "the last-known window keeps the inline warning")
        XCTAssertEqual(low[1].provider, "Codex")
        XCTAssertTrue(low[1].failed)
    }

    // MARK: - Low-quota notification evaluation (spec §7)

    @MainActor
    private func makeStore(notifications: Bool) -> CompanionStore {
        let store = CompanionStore()
        store.settings.lowQuotaNotifications = notifications
        store.settings.thresholds = .defaults
        return store
    }

    private func makeQuotaSnapshot(remaining: Double, status: String = "ok", detail: String? = nil, resetsAt: Int? = 1782910800) -> Snapshot {
        let quota = QuotaResponse(enabled: true, providers: [
            "codex": ProviderQuota(estimated: false, buckets: [
                BucketQuota(bucket: "5h", bucketLabel: "5-hour", remainingPercent: remaining, resetsAt: resetsAt, account: "a")
            ], status: status, statusDetail: detail)
        ], timestamp: nil)
        return Snapshot(today: .empty, month: .empty, quota: quota, thresholds: .defaults)
    }

    @MainActor
    func testEvaluateLowQuotaCrossesAndDedups() {
        let store = makeStore(notifications: true)
        XCTAssertTrue(store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 80)).isEmpty, "above threshold: no alert")
        let fresh = store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 10))
        XCTAssertEqual(fresh.count, 1, "crossing below threshold fires once")
        XCTAssertEqual(fresh.first?.left ?? -1, 10, accuracy: 0.001)
        XCTAssertTrue(store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 10)).isEmpty, "dedup: no repeat at same level")
    }

    @MainActor
    func testEvaluateLowQuotaSuppressesMissingResetAndFailedProvider() {
        let store = makeStore(notifications: true)
        _ = store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 80))  // baseline above

        XCTAssertTrue(store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 10, resetsAt: nil)).isEmpty, "missing resets_at suppressed")
        XCTAssertTrue(store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 10, status: "error", detail: "unreachable")).isEmpty, "failed-provider rows suppressed")
    }

    @MainActor
    func testEvaluateLowQuotaOptIn() {
        let store = makeStore(notifications: false)
        _ = store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 80))
        XCTAssertTrue(store.evaluateLowQuotaNotifications(makeQuotaSnapshot(remaining: 10)).isEmpty, "opt-in: no alerts when disabled")
    }

    private static let statusAt = 1785030000

    // The motivating shape: a provider with several credentials where one is broken. The
    // server reports status "ok" + status_detail "stale_token" for the whole provider (its
    // recovery suppression is ok_at > status_at, and every credential in a cycle shares
    // captured_at, so the detail can't clear while the sibling stays broken). Both buckets
    // report status "ok" - only capturedAt vs statusAt separates them.
    private func makePartiallyFailedSnapshot(okRemaining: Double, staleRemaining: Double, statusAt: Int? = SnapshotTests.statusAt) -> Snapshot {
        let quota = QuotaResponse(enabled: true, providers: [
            "minimax": ProviderQuota(estimated: false, buckets: [
                // Refreshed in the same cycle as the failure -> fresh.
                BucketQuota(bucket: "global_general_5h", bucketLabel: "Global 5-hour", remainingPercent: okRemaining, resetsAt: 1782910800, account: "global", capturedAt: SnapshotTests.statusAt),
                // Last observed before the failure -> last-known.
                BucketQuota(bucket: "cn_general_5h", bucketLabel: "CN 5-hour", remainingPercent: staleRemaining, resetsAt: 1782910800, account: "cn", capturedAt: SnapshotTests.statusAt - 30000),
            ], status: "ok", statusDetail: "stale_token", statusAt: statusAt)
        ], timestamp: nil)
        return Snapshot(today: .empty, month: .empty, quota: quota, thresholds: .defaults)
    }

    // A fully failed provider: every bucket predates the failure, so no row is eligible.
    private func makeFullyFailedSnapshot(remaining: Double) -> Snapshot {
        let quota = QuotaResponse(enabled: true, providers: [
            "codex": ProviderQuota(estimated: false, buckets: [
                BucketQuota(bucket: "5h", bucketLabel: "5-hour", remainingPercent: remaining, resetsAt: 1782910800, account: "a", capturedAt: SnapshotTests.statusAt - 30000)
            ], status: "error", statusDetail: "fetch_error", statusAt: SnapshotTests.statusAt)
        ], timestamp: nil)
        return Snapshot(today: .empty, month: .empty, quota: quota, thresholds: .defaults)
    }

    @MainActor
    func testPartiallyFailedProviderAlertsHealthyRowOnly() {
        // Group failed (header warning) must not silence a sibling window that refreshed
        // in the same cycle: only the row whose data predates the failure is suppressed.
        let store = makeStore(notifications: true)
        let baseline = makePartiallyFailedSnapshot(okRemaining: 80, staleRemaining: 80)
        let group = baseline.allQuotaGroups.first!
        XCTAssertTrue(group.failed, "a non-empty status_detail still warns on the provider header")
        XCTAssertFalse(group.rows.first(where: { $0.bucket == "global_general_5h" })!.failed, "capturedAt == statusAt is fresh, not failed")
        XCTAssertTrue(group.rows.first(where: { $0.bucket == "cn_general_5h" })!.failed, "capturedAt < statusAt is last-known, so it keeps the inline warning")

        XCTAssertTrue(store.evaluateLowQuotaNotifications(baseline).isEmpty, "above threshold: no alert")
        let fresh = store.evaluateLowQuotaNotifications(makePartiallyFailedSnapshot(okRemaining: 10, staleRemaining: 10))
        XCTAssertEqual(fresh.count, 1, "the healthy sibling still fires; the stale row stays suppressed")
        XCTAssertEqual(fresh.first?.bucket, "global_general_5h")
    }

    @MainActor
    func testFullyFailedProviderSuppressesItsLastKnownRows() {
        // Regression guard: every bucket predates the failure, so nothing may alert on
        // stale data even though buckets[].status is "ok".
        let store = makeStore(notifications: true)
        let baseline = makeFullyFailedSnapshot(remaining: 80)
        XCTAssertTrue(baseline.allQuotaGroups.first!.rows.allSatisfy { $0.failed }, "rows older than statusAt are all last-known")

        _ = store.evaluateLowQuotaNotifications(baseline)
        XCTAssertTrue(store.evaluateLowQuotaNotifications(makeFullyFailedSnapshot(remaining: 10)).isEmpty,
                      "a fully failed provider's last-known rows never alert")
    }

    @MainActor
    func testMissingStatusAtFallsBackToTheGroup() {
        // Older servers omit status_at; without it the freshness comparison is impossible,
        // so keep today's behavior (group failed -> every row failed) rather than
        // un-suppressing rows that may well be stale.
        let store = makeStore(notifications: true)
        let baseline = makePartiallyFailedSnapshot(okRemaining: 80, staleRemaining: 80, statusAt: nil)
        XCTAssertTrue(baseline.allQuotaGroups.first!.rows.allSatisfy { $0.failed }, "no statusAt -> fall back to the group")

        _ = store.evaluateLowQuotaNotifications(baseline)
        XCTAssertTrue(store.evaluateLowQuotaNotifications(makePartiallyFailedSnapshot(okRemaining: 10, staleRemaining: 10, statusAt: nil)).isEmpty,
                      "the fallback suppresses every row of a failed provider")
    }

    // MARK: - API timestamp parsing (spec §freshness)

    func testParseTimestampNaiveFormsAreUTC() {
        // The server emits a naive UTC datetime with six fractional digits;
        // ISO8601DateFormatter only accepts three, so the fraction is normalized first.
        let epoch = 1785261463.0   // 2026-07-28T17:57:43Z
        XCTAssertEqual(CompanionStore.parseTimestamp("2026-07-28T17:57:43.500951")?.timeIntervalSince1970 ?? -1, epoch + 0.5, accuracy: 0.001)
        XCTAssertEqual(CompanionStore.parseTimestamp("2026-07-28T17:57:43.500")?.timeIntervalSince1970 ?? -1, epoch + 0.5, accuracy: 0.001)
        XCTAssertEqual(CompanionStore.parseTimestamp("2026-07-28T17:57:43")?.timeIntervalSince1970 ?? -1, epoch, accuracy: 0.001)
    }

    func testParseTimestampExplicitOffsets() {
        let epoch = 1785261463.0
        XCTAssertEqual(CompanionStore.parseTimestamp("2026-07-28T17:57:43Z")?.timeIntervalSince1970 ?? -1, epoch, accuracy: 0.001)
        XCTAssertEqual(CompanionStore.parseTimestamp("2026-07-28T17:57:43+00:00")?.timeIntervalSince1970 ?? -1, epoch, accuracy: 0.001)
        // A non-UTC offset must be honored, not assumed UTC.
        XCTAssertEqual(CompanionStore.parseTimestamp("2026-07-28T17:57:43+02:00")?.timeIntervalSince1970 ?? -1, epoch - 7200, accuracy: 0.001)
    }

    func testParseTimestampRejectsGarbage() {
        XCTAssertNil(CompanionStore.parseTimestamp("not-a-timestamp"))
        XCTAssertNil(CompanionStore.parseTimestamp(""))
    }

    // MARK: - Base URL validation

    private func antigravityRow(_ bucket: String, _ label: String, _ left: Double) -> QuotaRow {
        QuotaRow(provider: "Antigravity",
                 bucket: BucketQuota(bucket: bucket, bucketLabel: label, remainingPercent: left, account: "default"))
    }

    func testAntigravityPoolsCollapseToTwoWorstRows() {
        // One bucket per model floods the popover; collapse to the two dashboard pools,
        // each showing the worst remaining. Pinned to the Windows AntigravityPools cases.
        let pooled = Snapshot.antigravityPools([
            antigravityRow("gemini_3_pro", "Gemini 3 Pro", 62),
            antigravityRow("gemini_3_flash", "Gemini 3 Flash", 41),   // worst gemini
            antigravityRow("claude_sonnet", "Claude Sonnet", 88),
            antigravityRow("gpt_oss", "GPT OSS", 12),                 // worst claude/gpt
        ])

        XCTAssertEqual(pooled.count, 2, "exactly two pooled rows")
        XCTAssertEqual(pooled[0].bucketLabel, "Gemini")
        XCTAssertEqual(pooled[0].left, 41, accuracy: 0.001, "pool shows the worst remaining")
        XCTAssertEqual(pooled[0].bucket, "pool:gemini")
        XCTAssertEqual(pooled[1].bucketLabel, "Claude/GPT")
        XCTAssertEqual(pooled[1].left, 12, accuracy: 0.001)
    }

    func testAntigravityPoolsKeepUnmatchedRows() {
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }
        // A model matching neither pool must not silently vanish; it still gets a window
        // suffix (defaulting to 5-hour when it has no reset time).
        let pooled = Snapshot.antigravityPools([antigravityRow("mystery_model", "Mystery Model", 30)])
        XCTAssertEqual(pooled.count, 1)
        XCTAssertEqual(pooled[0].bucketLabel, "Mystery Model", "falls back to the raw rows")
        XCTAssertEqual(pooled[0].displayBucketLabel, "Mystery Model · 5-hour")
    }

    // MARK: - Antigravity window auto-detection

    func testAntigravityWindowLabelClassifiesByResetTime() {
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }

        // A 5-hour window can never reset more than 5h out; 8h absorbs skew before weekly.
        XCTAssertEqual(QuotaRow.antigravityWindowLabel(forSecondsUntilReset: 3 * 3600), "5-hour")
        XCTAssertEqual(QuotaRow.antigravityWindowLabel(forSecondsUntilReset: 5 * 3600), "5-hour")
        XCTAssertEqual(QuotaRow.antigravityWindowLabel(forSecondsUntilReset: (3 * 24 + 22) * 3600), "Weekly")
        XCTAssertEqual(QuotaRow.antigravityWindowLabel(forSecondsUntilReset: 7 * 24 * 3600), "Weekly")
    }

    func testAntigravityDisplayBucketLabelAppendsAutoDeterminedWindow() {
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }

        let captured = 1_782_907_200
        // Weekly reset (3d22h out from capture) -> "Gemini · Weekly".
        let pooled = Snapshot.antigravityPools([
            QuotaRow(provider: "Antigravity", bucket: BucketQuota(
                bucket: "gemini_3_pro", bucketLabel: "Gemini 3 Pro", remainingPercent: 8,
                resetsAt: captured + (3 * 24 + 22) * 3600, account: "default", capturedAt: captured)),
        ])
        XCTAssertEqual(pooled[0].bucketLabel, "Gemini")
        XCTAssertEqual(pooled[0].displayBucketLabel, "Gemini · Weekly")

        // 5-hour reset -> "Gemini · 5-hour".
        let pooled5h = Snapshot.antigravityPools([
            QuotaRow(provider: "Antigravity", bucket: BucketQuota(
                bucket: "gemini_3_pro", bucketLabel: "Gemini 3 Pro", remainingPercent: 8,
                resetsAt: captured + 3 * 3600, account: "default", capturedAt: captured)),
        ])
        XCTAssertEqual(pooled5h[0].displayBucketLabel, "Gemini · 5-hour")

        // No reset time (idle model) -> defaults to 5-hour, never "Weekly".
        let pooledIdle = Snapshot.antigravityPools([antigravityRow("gemini_3_pro", "Gemini 3 Pro", 8)])
        XCTAssertEqual(pooledIdle[0].displayBucketLabel, "Gemini · 5-hour")
    }

    func testDisplayLabelShortensWindowsAndMeteredFeatures() {
        // Pinned to the Windows DisplayLabel cases.
        XCTAssertEqual(QuotaRow.displayLabel("5-hour window"), "5-hour")
        XCTAssertEqual(QuotaRow.displayLabel("7-day window"), "7-day")
        XCTAssertEqual(QuotaRow.displayLabel("weekly window"), "weekly")
        XCTAssertEqual(QuotaRow.displayLabel("5-hour Window"), "5-hour", "case-insensitive")
        // Labels that never carried the word are untouched.
        XCTAssertEqual(QuotaRow.displayLabel("5-hour"), "5-hour")
        XCTAssertEqual(QuotaRow.displayLabel("Weekly"), "Weekly")
        XCTAssertEqual(QuotaRow.displayLabel("Global 5-hour"), "Global 5-hour")
        XCTAssertEqual(QuotaRow.displayLabel("window"), "window", "would shorten to nothing")
        // Codex metered features collapse to the feature, keeping the window.
        XCTAssertEqual(QuotaRow.displayLabel("GPT-5.3-Codex-Spark · 5-hour"), "Spark · 5-hour")
        XCTAssertEqual(QuotaRow.displayLabel("GPT-5.3-Codex-Spark · 7-day"), "Spark · 7-day")
        XCTAssertEqual(QuotaRow.displayLabel("Video · Weekly"), "Video · Weekly", "plain names untouched")
    }

    func testServerLabelNamesTheConfiguredHost() {
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }

        // Loopback stays "Local"; a remote host uses its first DNS label so a Tailscale
        // URL doesn't claim to be local. Pinned to the Windows ServerLabel cases.
        XCTAssertEqual(CompanionStore.serverLabel(for: "http://127.0.0.1:55423"), "Local")
        XCTAssertEqual(CompanionStore.serverLabel(for: "http://localhost:55423"), "Local")
        XCTAssertEqual(CompanionStore.serverLabel(for: "https://wsl.tail76535.ts.net/tokdash"), "wsl")
        XCTAssertEqual(CompanionStore.serverLabel(for: "  https://WSL.tail76535.ts.net/tokdash  "), "wsl",
                       "trimmed and lowercased")
        XCTAssertEqual(CompanionStore.serverLabel(for: "http://homelab:8080"), "homelab")
        // A bare IP has no name to shorten - showing "192" would be nonsense.
        XCTAssertEqual(CompanionStore.serverLabel(for: "http://192.168.1.50:55423"), "192.168.1.50")
        // Unparseable input must not throw; fall back to the default label.
        XCTAssertEqual(CompanionStore.serverLabel(for: "not a url"), "Local")
    }

    func testIsValidBaseURL() {
        XCTAssertTrue(CompanionStore.isValidBaseURL("http://127.0.0.1:55423"))
        XCTAssertTrue(CompanionStore.isValidBaseURL("https://wsl.tail76535.ts.net/tokdash"))
        XCTAssertTrue(CompanionStore.isValidBaseURL("  http://127.0.0.1:55423  "), "surrounding whitespace is trimmed")
        // Rejecting these at every write path is what stops a blank base URL from
        // recurring on the next launch (init only repairs it on read).
        XCTAssertFalse(CompanionStore.isValidBaseURL(""))
        XCTAssertFalse(CompanionStore.isValidBaseURL("   "))
        XCTAssertFalse(CompanionStore.isValidBaseURL("127.0.0.1:55423"), "no scheme")
        XCTAssertFalse(CompanionStore.isValidBaseURL("/tokdash"), "relative")
        XCTAssertFalse(CompanionStore.isValidBaseURL("ftp://host/tokdash"), "wrong scheme")
        XCTAssertFalse(CompanionStore.isValidBaseURL("http:///tokdash"), "no host")
    }
}
