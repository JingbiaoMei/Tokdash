import XCTest
@testable import TokdashCompanion

/// Update-check behavior: release selection out of a mixed Python/companion list, numeric
/// SemVer ordering, release-link validation, the 24h throttle, and badge/accessibility
/// rules. Pinned to the same cases as `UpdateCheckerTests.cs` on Windows so the two
/// platforms cannot drift on which release they'd offer.
final class UpdateCheckerTests: XCTestCase {

    /// Redirect settings persistence to a temp file before any store is built - several of
    /// the tests below drive paths that call save().
    override class func setUp() {
        super.setUp()
        TestSettings.install()
    }

    private func release(_ tag: String, draft: Bool = false, prerelease: Bool = true, url: String? = nil) -> GitHubRelease {
        GitHubRelease(tagName: tag, draft: draft, prerelease: prerelease, htmlURL: url)
    }

    // MARK: - Tag parsing

    func testParseTagAcceptsOnlyCompanionTags() {
        XCTAssertEqual(UpdateChecker.parseTag("companion-v0.1.4"), [0, 1, 4])
        XCTAssertEqual(UpdateChecker.parseTag("companion-v0.1.10"), [0, 1, 10])
        XCTAssertEqual(UpdateChecker.parseTag("companion-v12.30.400"), [12, 30, 400])
        // Python releases share the repo and must never be offered as a companion update.
        XCTAssertNil(UpdateChecker.parseTag("v1.5.8"))
        XCTAssertNil(UpdateChecker.parseTag("1.5.8"))
        // Malformed companion tags are skipped, not guessed at.
        XCTAssertNil(UpdateChecker.parseTag("companion-v0.1"))
        XCTAssertNil(UpdateChecker.parseTag("companion-v0.1.4.1"))
        XCTAssertNil(UpdateChecker.parseTag("companion-v0.1.4-rc1"))
        XCTAssertNil(UpdateChecker.parseTag("companion-v0.1.x"))
        XCTAssertNil(UpdateChecker.parseTag("companion-v"))
        XCTAssertNil(UpdateChecker.parseTag(""))
        XCTAssertNil(UpdateChecker.parseTag("companion-0.1.4"), "missing the v")
        // Surrounding whitespace is tolerated.
        XCTAssertEqual(UpdateChecker.parseTag("  companion-v0.2.0  "), [0, 2, 0])
    }

    func testParseVersionIsStrict() {
        XCTAssertEqual(UpdateChecker.parseVersion("0.1.4"), [0, 1, 4])
        XCTAssertNil(UpdateChecker.parseVersion("0.1"))
        XCTAssertNil(UpdateChecker.parseVersion("0..1"))
        XCTAssertNil(UpdateChecker.parseVersion(""))
        XCTAssertNil(UpdateChecker.parseVersion("v0.1.4"))
        XCTAssertNil(UpdateChecker.parseVersion("0.1.4+sha"))
    }

    // MARK: - Numeric ordering

    func testIsNewerComparesNumericallyNotLexically() {
        // The case a string compare gets backwards, and the reason versions are parsed first.
        XCTAssertTrue(UpdateChecker.isNewer([0, 1, 10], than: [0, 1, 9]))
        XCTAssertFalse(UpdateChecker.isNewer([0, 1, 9], than: [0, 1, 10]))
        XCTAssertTrue(UpdateChecker.isNewer([0, 2, 0], than: [0, 1, 99]))
        XCTAssertTrue(UpdateChecker.isNewer([1, 0, 0], than: [0, 99, 99]))
        // Equal is not newer: an up-to-date install must never badge.
        XCTAssertFalse(UpdateChecker.isNewer([0, 1, 4], than: [0, 1, 4]))
        XCTAssertFalse(UpdateChecker.isNewer([0, 1, 3], than: [0, 1, 4]))
    }

    // MARK: - Release selection

    func testNewestCompanionReleaseIgnoresPythonReleasesAndDrafts() {
        let releases = [
            release("v1.5.8", prerelease: false),          // Python release, newer by date
            release("companion-v0.1.3"),
            release("companion-v0.1.9", draft: true),      // draft: tag may not exist yet
            release("not-a-tag"),
            release("companion-v0.1.4"),
            release("v1.5.7", prerelease: false),
        ]
        let newest = UpdateChecker.newestCompanionRelease(in: releases)
        XCTAssertEqual(newest?.version, [0, 1, 4])
        XCTAssertEqual(newest?.release.tagName, "companion-v0.1.4")
    }

    func testNewestCompanionReleaseKeepsPrereleases() {
        // Every companion build is published as a prerelease; excluding them would make
        // the check permanently find nothing.
        let newest = UpdateChecker.newestCompanionRelease(in: [
            release("companion-v0.1.4", prerelease: true),
            release("companion-v0.1.5", prerelease: true),
        ])
        XCTAssertEqual(newest?.version, [0, 1, 5])
    }

    func testNewestCompanionReleasePicksNumericallyHighestNotListOrder() {
        // GitHub lists newest-first by creation date, but a backfilled or re-cut release
        // can break that; the selection must be by version, not position.
        let newest = UpdateChecker.newestCompanionRelease(in: [
            release("companion-v0.1.9"),
            release("companion-v0.1.10"),
            release("companion-v0.1.2"),
        ])
        XCTAssertEqual(newest?.version, [0, 1, 10])
    }

    func testNewestCompanionReleaseReturnsNilWhenNoneMatch() {
        XCTAssertNil(UpdateChecker.newestCompanionRelease(in: []))
        XCTAssertNil(UpdateChecker.newestCompanionRelease(in: [
            release("v1.5.8", prerelease: false),
            release("companion-v0.1.4", draft: true),
        ]))
    }

    // MARK: - Decoding

    func testReleaseDecodeIsAdditiveAndToleratesAbsentFlags() throws {
        let json = Data("""
        [{"tag_name":"companion-v0.1.5","draft":false,"prerelease":true,
          "html_url":"https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5",
          "unknown_future_field":"ignored"},
         {"tag_name":"v1.5.8"}]
        """.utf8)
        let decoded = try JSONDecoder().decode([GitHubRelease].self, from: json)
        XCTAssertEqual(decoded.count, 2)
        XCTAssertEqual(decoded[0].tagName, "companion-v0.1.5")
        XCTAssertTrue(decoded[0].prerelease)
        // Absent draft/prerelease default to false rather than failing the whole list.
        XCTAssertFalse(decoded[1].draft)
        XCTAssertFalse(decoded[1].prerelease)
        XCTAssertNil(decoded[1].htmlURL)
    }

    func testInvalidJsonFailsDecode() {
        XCTAssertThrowsError(try JSONDecoder().decode([GitHubRelease].self, from: Data("{not json".utf8)))
        // A JSON object where a list is expected is equally a decode failure, not a crash.
        XCTAssertThrowsError(try JSONDecoder().decode([GitHubRelease].self, from: Data(#"{"message":"API rate limit exceeded"}"#.utf8)))
    }

    // MARK: - Release link validation

    func testValidReleaseURLAcceptsOnlyTheTokdashReleasesPath() {
        XCTAssertTrue(UpdateChecker.isValidReleaseURL("https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5"))
        // GitHub owner/repo are case-insensitive; the API's canonical casing need not match ours.
        XCTAssertTrue(UpdateChecker.isValidReleaseURL("https://github.com/jingbiaomei/tokdash/releases/tag/companion-v0.1.5"))
        // http is rejected outright - the link opens in the user's browser.
        XCTAssertFalse(UpdateChecker.isValidReleaseURL("http://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5"))
        // Host spoofs that read as github.com to a human but parse to another host.
        XCTAssertFalse(UpdateChecker.isValidReleaseURL("https://github.com.evil.test/JingbiaoMei/Tokdash/releases/tag/x"))
        XCTAssertFalse(UpdateChecker.isValidReleaseURL("https://github.com@evil.test/JingbiaoMei/Tokdash/releases/tag/x"))
        XCTAssertFalse(UpdateChecker.isValidReleaseURL("https://evil.test/JingbiaoMei/Tokdash/releases/tag/x"))
        // Right host, wrong repo or wrong path.
        XCTAssertFalse(UpdateChecker.isValidReleaseURL("https://github.com/someone/else/releases/tag/x"))
        XCTAssertFalse(UpdateChecker.isValidReleaseURL("https://github.com/JingbiaoMei/Tokdash/issues/1"))
        XCTAssertFalse(UpdateChecker.isValidReleaseURL("javascript:alert(1)"))
        XCTAssertFalse(UpdateChecker.isValidReleaseURL(""))
    }

    func testReleaseURLFallsBackToATagURLBuiltFromTheParsedVersion() {
        // A hostile or missing html_url must not reach the browser; the fallback is built
        // only from parsed integers, so no server-supplied text survives into the URL.
        let hostile = release("companion-v0.1.5", url: "https://evil.test/pwn")
        XCTAssertEqual(UpdateChecker.releaseURL(for: hostile, version: [0, 1, 5]),
                       "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5")
        let missing = release("companion-v0.1.5", url: nil)
        XCTAssertEqual(UpdateChecker.releaseURL(for: missing, version: [0, 1, 5]),
                       "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5")
        // A valid html_url is preserved (it may point at a nicer canonical form).
        let good = "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5"
        XCTAssertEqual(UpdateChecker.releaseURL(for: release("companion-v0.1.5", url: good), version: [0, 1, 5]), good)
        // The fallback is itself a URL the validator accepts.
        XCTAssertTrue(UpdateChecker.isValidReleaseURL(UpdateChecker.releaseURL(for: missing, version: [0, 1, 5])))
    }

    // MARK: - 24h throttle

    func testShouldAutoCheckRespectsOptInAnd24Hours() {
        let now = Date(timeIntervalSince1970: 1_785_261_463)
        // Opt-out means no request is ever made, however long it has been.
        XCTAssertFalse(UpdateChecker.shouldAutoCheck(enabled: false, lastCheck: nil, now: now))
        XCTAssertFalse(UpdateChecker.shouldAutoCheck(enabled: false, lastCheck: now.addingTimeInterval(-90000), now: now))
        // Never checked -> due immediately once opted in.
        XCTAssertTrue(UpdateChecker.shouldAutoCheck(enabled: true, lastCheck: nil, now: now))
        // Inside the window -> not due. This is what keeps the 60s refresh tick from
        // turning into 60s GitHub polling.
        XCTAssertFalse(UpdateChecker.shouldAutoCheck(enabled: true, lastCheck: now.addingTimeInterval(-60), now: now))
        XCTAssertFalse(UpdateChecker.shouldAutoCheck(enabled: true, lastCheck: now.addingTimeInterval(-86399), now: now))
        // Exactly 24h and beyond -> due.
        XCTAssertTrue(UpdateChecker.shouldAutoCheck(enabled: true, lastCheck: now.addingTimeInterval(-86400), now: now))
        XCTAssertTrue(UpdateChecker.shouldAutoCheck(enabled: true, lastCheck: now.addingTimeInterval(-200000), now: now))
        // A future timestamp means the clock moved backwards; treat it as due rather than
        // blocking checks until real time catches up.
        XCTAssertTrue(UpdateChecker.shouldAutoCheck(enabled: true, lastCheck: now.addingTimeInterval(3600), now: now))
    }

    // MARK: - Badge visibility (store rules)

    @MainActor
    func testBadgeShowsOnlyForANewerUnskippedVersion() {
        let store = CompanionStore()
        let current = CompanionStore.currentVersion
        guard let parsed = UpdateChecker.parseVersion(current) else {
            return XCTFail("bundle version \(current) must parse")
        }
        let newer = UpdateChecker.versionString([parsed[0], parsed[1], parsed[2] + 1])
        let older = UpdateChecker.versionString([parsed[0], parsed[1], max(0, parsed[2] - 1)])

        // Nothing known -> no badge.
        store.settings.availableUpdateVersion = nil
        XCTAssertFalse(store.showsUpdateBadge)

        // Newer -> badge.
        store.settings.availableUpdateVersion = newer
        XCTAssertTrue(store.showsUpdateBadge)
        XCTAssertEqual(store.updateAvailableVersion, newer)
        XCTAssertEqual(store.settingsAccessibilityLabel, L10n.t("settings_update_available"))

        // Same version as installed (i.e. the user updated) -> badge clears itself.
        store.settings.availableUpdateVersion = current
        XCTAssertFalse(store.showsUpdateBadge)
        XCTAssertEqual(store.settingsAccessibilityLabel, L10n.t("settings"))

        // Older -> never.
        store.settings.availableUpdateVersion = older
        XCTAssertFalse(store.showsUpdateBadge)

        // Explicitly skipped -> hidden for that version only.
        store.settings.availableUpdateVersion = newer
        store.settings.skippedUpdateVersion = newer
        XCTAssertFalse(store.showsUpdateBadge)
        // ...and a later release re-arms it.
        let evenNewer = UpdateChecker.versionString([parsed[0], parsed[1], parsed[2] + 2])
        store.settings.availableUpdateVersion = evenNewer
        XCTAssertTrue(store.showsUpdateBadge)

        // A malformed persisted version fails closed rather than badging on garbage.
        store.settings.skippedUpdateVersion = nil
        store.settings.availableUpdateVersion = "not-a-version"
        XCTAssertFalse(store.showsUpdateBadge)
    }

    @MainActor
    func testTransientStatesNeverShowTheBadge() {
        // Checking / offline / malformed / rate-limited are not badge-worthy: the dot means
        // "an update is waiting", not "something happened".
        let store = CompanionStore()
        store.settings.availableUpdateVersion = nil
        for status in [UpdateStatus.checking,
                       .failed(L10n.t("update_failed_offline")),
                       .failed(L10n.t("update_failed_rate_limited")),
                       .failed(L10n.t("update_failed_generic")),
                       .upToDate] {
            store.setUpdateStatusForTesting(status)
            XCTAssertFalse(store.showsUpdateBadge, "\(status) must not badge")
        }
    }

    @MainActor
    func testUpdateCheckDoesNotTouchConnectionState() {
        // The whole point of running the check off the refresh path: a GitHub failure is
        // not a Tokdash outage.
        let store = CompanionStore()
        let before = store.connectionState
        store.applyUpdateFailureForTesting(UpdateCheckError.offline, manual: true)
        XCTAssertEqual(store.connectionState, before)
        store.applyUpdateFailureForTesting(UpdateCheckError.rateLimited, manual: false)
        XCTAssertEqual(store.connectionState, before)
        store.applyReleasesForTesting([GitHubRelease(tagName: "companion-v99.0.0")], manual: false)
        XCTAssertEqual(store.connectionState, before)
    }

    @MainActor
    func testScheduledFailureIsSilentAndKeepsAKnownUpdate() {
        let store = CompanionStore()
        store.setUpdateStatusForTesting(.available(version: "99.0.0", url: "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v99.0.0"))
        store.settings.availableUpdateVersion = "99.0.0"

        // A scheduled failure must not overwrite the status line or drop the pending update.
        store.applyUpdateFailureForTesting(UpdateCheckError.offline, manual: false)
        XCTAssertEqual(store.updateStatus, .available(version: "99.0.0", url: "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v99.0.0"))
        XCTAssertEqual(store.settings.availableUpdateVersion, "99.0.0")
        XCTAssertTrue(store.showsUpdateBadge)

        // A manual failure reports the reason but still keeps the badge.
        store.applyUpdateFailureForTesting(UpdateCheckError.rateLimited, manual: true)
        XCTAssertEqual(store.updateStatus, .failed(L10n.t("update_failed_rate_limited")))
        XCTAssertTrue(store.showsUpdateBadge)

        // Either way the attempt is stamped, so "at most once every 24 hours" holds while
        // offline instead of retrying on every refresh tick.
        XCTAssertNotNil(store.settings.lastUpdateCheckAt)
        XCTAssertFalse(UpdateChecker.shouldAutoCheck(enabled: true, lastCheck: store.settings.lastUpdateCheckAt, now: Date()))
    }

    @MainActor
    func testSuccessfulCheckClearsAStaleAvailableVersion() {
        let store = CompanionStore()
        store.settings.availableUpdateVersion = "99.0.0"
        store.settings.availableUpdateURL = "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v99.0.0"
        // The releases list no longer offers anything newer (e.g. the user updated).
        store.applyReleasesForTesting([GitHubRelease(tagName: "companion-v0.0.1")], manual: true)
        XCTAssertEqual(store.updateStatus, .upToDate)
        XCTAssertNil(store.settings.availableUpdateVersion)
        XCTAssertFalse(store.showsUpdateBadge)
    }

    @MainActor
    func testManualCheckRecordsAnAvailableUpdate() {
        let store = CompanionStore()
        store.settings.skippedUpdateVersion = nil
        store.applyReleasesForTesting([
            GitHubRelease(tagName: "v1.5.8"),
            GitHubRelease(tagName: "companion-v99.0.0", prerelease: true),
            GitHubRelease(tagName: "companion-v99.0.1", draft: true),
        ], manual: true)
        XCTAssertEqual(store.updateAvailableVersion, "99.0.0")
        XCTAssertEqual(store.settings.availableUpdateURL,
                       "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v99.0.0")
        XCTAssertTrue(store.showsUpdateBadge)
    }

    // MARK: - Last-check caption

    func testLastCheckedTextTiers() {
        let saved = L10n.current
        L10n.current = .english
        defer { L10n.current = saved }
        let now = Date(timeIntervalSince1970: 1_785_261_463)
        XCTAssertEqual(UpdateChecker.lastCheckedText(nil, now: now), "Not checked yet")
        XCTAssertEqual(UpdateChecker.lastCheckedText(now.addingTimeInterval(-30), now: now), "Last checked just now")
        XCTAssertEqual(UpdateChecker.lastCheckedText(now.addingTimeInterval(-600), now: now), "Last checked 10 min ago")
        XCTAssertEqual(UpdateChecker.lastCheckedText(now.addingTimeInterval(-7200), now: now), "Last checked 2 h ago")
        XCTAssertEqual(UpdateChecker.lastCheckedText(now.addingTimeInterval(-200000), now: now), "Last checked 2 d ago")
    }

    // MARK: - Localization

    func testUpdateStringsExistInBothLanguages() {
        let saved = L10n.current
        defer { L10n.current = saved }
        L10n.current = .zhHans
        XCTAssertEqual(L10n.t("settings_update_available"), "设置，有可用更新")
        XCTAssertEqual(L10n.t("update_available", "0.1.5"), "有新版本 0.1.5 可用")
        XCTAssertEqual(L10n.t("update_up_to_date"), "Tokdash Companion 已是最新版本。")
        L10n.current = .english
        XCTAssertEqual(L10n.t("settings_update_available"), "Settings, update available")
        XCTAssertEqual(L10n.t("update_available", "0.1.5"), "Version 0.1.5 is available")

        // Parity is asserted globally in SnapshotTests; this pins the update keys directly
        // so a new one can't ship English-only.
        let zh = Set(L10n.keys(for: .zhHans))
        for key in L10n.keys(for: .english) where key.hasPrefix("update_") || key == "section_updates" || key == "settings_update_available" {
            XCTAssertTrue(zh.contains(key), "zh-Hans is missing \(key)")
        }
    }

    // MARK: - Settings migration

    func testSettingsFromBeforeUpdateCheckingDecodeWithTheFeatureOff() throws {
        // A v0.1.4 settings file has none of the update fields. Decoding must preserve every
        // existing preference and default update checking to OFF (it is opt-in).
        let data = Data("""
        {
          "baseURL": "https://wsl.example.test/tokdash",
          "launchAtLogin": true,
          "lowQuotaNotifications": true,
          "thresholds": {"fiveHour": 27, "weekly": 13, "other": 19},
          "language": "zhHans"
        }
        """.utf8)
        let settings = try JSONDecoder().decode(CompanionSettings.self, from: data)
        XCTAssertEqual(settings.baseURL, "https://wsl.example.test/tokdash")
        XCTAssertTrue(settings.lowQuotaNotifications)
        XCTAssertEqual(settings.language, .zhHans)
        XCTAssertFalse(settings.automaticUpdateChecks)
        XCTAssertNil(settings.lastUpdateCheckAt)
        XCTAssertNil(settings.availableUpdateVersion)
        XCTAssertNil(settings.skippedUpdateVersion)

        // Round-trips without losing anything.
        let again = try JSONDecoder().decode(CompanionSettings.self, from: JSONEncoder().encode(settings))
        XCTAssertEqual(again.baseURL, settings.baseURL)
        XCTAssertFalse(again.automaticUpdateChecks)
    }
}
