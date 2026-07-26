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

    private func makeRow(bucket: String, left: Double) -> QuotaRow {
        QuotaRow(provider: "test", bucket: BucketQuota(
            bucket: bucket,
            bucketLabel: bucket,
            remainingPercent: left,
            resetsAt: nil,
            account: "default"
        ))
    }
}
