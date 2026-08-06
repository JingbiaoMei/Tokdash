import Foundation
import XCTest
@testable import TokdashCompanion

/// Redirects ``CompanionSettings`` persistence into a per-run temp directory, before any
/// test constructs a ``CompanionStore``.
///
/// Without this, tests share the developer's real settings file: several suites build a
/// store (which loads it), and any path that calls `save()` - the update check, the
/// launch-at-login write, the store's own repair of an invalid base URL - writes it back.
/// That both contaminates the developer's install and makes assertions depend on whatever
/// happens to be on that machine.
///
/// XCTest has no assembly-wide hook without a principal class, so every test class that
/// constructs a store calls ``install()`` from its `class func setUp()`. The install is
/// idempotent and is never uninstalled on purpose: resetting it in a tearDown would reopen
/// the hole for whichever class runs next.
enum TestSettings {
    private static var installed = false

    static func install() {
        guard !installed else { return }
        installed = true
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("TokdashCompanionTests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        CompanionSettings.pathOverride = dir.appendingPathComponent("settings.json")
    }
}

final class TestSettingsIsolationTests: XCTestCase {
    override class func setUp() {
        super.setUp()
        TestSettings.install()
    }

    /// Guards the guard: if the redirect ever stops being installed, this fails here rather
    /// than silently rewriting the developer's settings from some other suite.
    func testSettingsPersistenceIsRedirectedAwayFromTheRealFile() throws {
        let override = try XCTUnwrap(CompanionSettings.pathOverride, "settings redirect is not installed")
        let real = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("TokdashCompanion", isDirectory: true)
            .appendingPathComponent("settings.json")
        XCTAssertNotEqual(override.path, real.path)
        XCTAssertEqual(CompanionSettings.defaultsURL.path, override.path)

        // And a real save() round-trips through the temp path, not the production one.
        var settings = CompanionSettings()
        settings.availableUpdateVersion = "99.0.0"
        settings.save()
        XCTAssertTrue(FileManager.default.fileExists(atPath: override.path))
        XCTAssertEqual(CompanionSettings.load().availableUpdateVersion, "99.0.0")
    }
}
