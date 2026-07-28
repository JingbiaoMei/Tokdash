import XCTest
@testable import TokdashCompanion

final class SnapshotTests: XCTestCase {

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

    private func makeRow(bucket: String, left: Double) -> QuotaRow {
        QuotaRow(provider: "test", bucket: BucketQuota(
            bucket: bucket,
            bucketLabel: bucket,
            remainingPercent: left,
            resetsAt: nil,
            account: "default"
        ))
    }

    // MARK: - Provider-level quota failures (spec §7)

    func testProviderFailureFlagsGroupAndRows() {
        // codex failed to refresh (status "error"); claude is healthy (status absent).
        // The failed provider's last-known rows stay visible but are flagged for an
        // inline warning - not a full-surface failure.
        let quota = QuotaResponse(
            enabled: true,
            providers: [
                "codex": ProviderQuota(estimated: false, buckets: [
                    BucketQuota(bucket: "5h", bucketLabel: "5h", remainingPercent: 14, resetsAt: nil, account: "a")
                ], status: "error", statusDetail: "unreachable"),
                "claude": ProviderQuota(estimated: true, buckets: [
                    BucketQuota(bucket: "5h", bucketLabel: "5h", remainingPercent: 71, resetsAt: nil, account: "b")
                ])
            ],
            timestamp: nil
        )
        let snap = Snapshot(today: .empty, month: .empty, quota: quota, thresholds: .defaults)
        let groups = snap.allQuotaGroups
        let codex = groups.first(where: { $0.provider == "Codex" })!
        let claude = groups.first(where: { $0.provider == "Claude" })!
        XCTAssertTrue(codex.failed, "error-status provider must be flagged failed")
        XCTAssertFalse(claude.failed, "absent-status provider must not be failed")
        XCTAssertTrue(codex.rows.allSatisfy { $0.failed }, "failed flag propagates to the provider's rows")
        XCTAssertFalse(claude.rows.allSatisfy { $0.failed })

        // Low view: the failed provider's low window carries the flag for an inline ⚠.
        let low = snap.lowQuotaRows
        XCTAssertEqual(low.count, 1)
        XCTAssertTrue(low[0].failed)
        XCTAssertEqual(low[0].provider, "Codex")
    }
}
