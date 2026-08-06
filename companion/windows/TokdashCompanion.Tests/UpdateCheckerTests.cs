using System.Collections.Generic;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Update-check behavior: release selection out of a mixed Python/companion list, numeric
/// SemVer ordering, release-link validation, the 24h throttle, and badge/accessibility
/// rules. Pinned to the same cases as <c>UpdateCheckerTests.swift</c> on macOS so the two
/// platforms cannot drift on which release they'd offer.
/// </summary>
[TestClass]
public class UpdateCheckerTests
{
    private static GitHubRelease Rel(string tag, bool draft = false, bool prerelease = true, string? url = null) =>
        new() { TagName = tag, Draft = draft, Prerelease = prerelease, HtmlUrl = url };

    /// <summary>Stub releases client: returns a canned list or throws a canned failure,
    /// so every check path is exercised without touching the network.</summary>
    private sealed class FakeUpdateClient : IUpdateClient
    {
        private readonly List<GitHubRelease>? _releases;
        private readonly Exception? _failure;
        public int Calls { get; private set; }

        public FakeUpdateClient(List<GitHubRelease> releases) { _releases = releases; }
        public FakeUpdateClient(Exception failure) { _failure = failure; }

        public Task<List<GitHubRelease>> FetchReleasesAsync(CancellationToken ct = default)
        {
            Calls++;
            if (_failure is not null) return Task.FromException<List<GitHubRelease>>(_failure);
            return Task.FromResult(_releases!);
        }
    }

    private sealed class StubTokdashClient : ITokdashClient
    {
        public Task<HealthResponse> HealthAsync(CancellationToken ct = default) =>
            Task.FromResult(new HealthResponse("ok", "tokdash", "1.5.8"));
        public Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default) =>
            Task.FromResult(new UsageResponse());
        public Task<QuotaResponse> QuotaAsync(CancellationToken ct = default) =>
            Task.FromResult(new QuotaResponse());
        public void Dispose() { }
    }

    // Tag parsing.

    [TestMethod]
    public void ParseTag_Accepts_Only_Companion_Tags()
    {
        CollectionAssert.AreEqual(new[] { 0, 1, 4 }, UpdateChecker.ParseTag("companion-v0.1.4"));
        CollectionAssert.AreEqual(new[] { 0, 1, 10 }, UpdateChecker.ParseTag("companion-v0.1.10"));
        CollectionAssert.AreEqual(new[] { 12, 30, 400 }, UpdateChecker.ParseTag("companion-v12.30.400"));
        // Python releases share the repo and must never be offered as a companion update.
        Assert.IsNull(UpdateChecker.ParseTag("v1.5.8"));
        Assert.IsNull(UpdateChecker.ParseTag("1.5.8"));
        // Malformed companion tags are skipped, not guessed at.
        Assert.IsNull(UpdateChecker.ParseTag("companion-v0.1"));
        Assert.IsNull(UpdateChecker.ParseTag("companion-v0.1.4.1"));
        Assert.IsNull(UpdateChecker.ParseTag("companion-v0.1.4-rc1"));
        Assert.IsNull(UpdateChecker.ParseTag("companion-v0.1.x"));
        Assert.IsNull(UpdateChecker.ParseTag("companion-v"));
        Assert.IsNull(UpdateChecker.ParseTag(""));
        Assert.IsNull(UpdateChecker.ParseTag(null));
        Assert.IsNull(UpdateChecker.ParseTag("companion-0.1.4"), "missing the v");
        CollectionAssert.AreEqual(new[] { 0, 2, 0 }, UpdateChecker.ParseTag("  companion-v0.2.0  "));
    }

    [TestMethod]
    public void ParseVersion_Is_Strict()
    {
        CollectionAssert.AreEqual(new[] { 0, 1, 4 }, UpdateChecker.ParseVersion("0.1.4"));
        Assert.IsNull(UpdateChecker.ParseVersion("0.1"));
        Assert.IsNull(UpdateChecker.ParseVersion("0..1"));
        Assert.IsNull(UpdateChecker.ParseVersion(""));
        Assert.IsNull(UpdateChecker.ParseVersion("v0.1.4"));
        Assert.IsNull(UpdateChecker.ParseVersion("0.1.4+sha"));
        Assert.IsNull(UpdateChecker.ParseVersion("-1.0.0"), "the sign is not a digit");
    }

    // Numeric ordering.

    [TestMethod]
    public void IsNewer_Compares_Numerically_Not_Lexically()
    {
        // The case a string compare gets backwards, and the reason versions are parsed first.
        Assert.IsTrue(UpdateChecker.IsNewer([0, 1, 10], [0, 1, 9]));
        Assert.IsFalse(UpdateChecker.IsNewer([0, 1, 9], [0, 1, 10]));
        Assert.IsTrue(UpdateChecker.IsNewer([0, 2, 0], [0, 1, 99]));
        Assert.IsTrue(UpdateChecker.IsNewer([1, 0, 0], [0, 99, 99]));
        // Equal is not newer: an up-to-date install must never badge.
        Assert.IsFalse(UpdateChecker.IsNewer([0, 1, 4], [0, 1, 4]));
        Assert.IsFalse(UpdateChecker.IsNewer([0, 1, 3], [0, 1, 4]));
    }

    // Release selection.

    [TestMethod]
    public void NewestCompanionRelease_Ignores_Python_Releases_And_Drafts()
    {
        var newest = UpdateChecker.NewestCompanionRelease(
        [
            Rel("v1.5.8", prerelease: false),          // Python release, newer by date
            Rel("companion-v0.1.3"),
            Rel("companion-v0.1.9", draft: true),      // draft: tag may not exist yet
            Rel("not-a-tag"),
            Rel("companion-v0.1.4"),
            Rel("v1.5.7", prerelease: false),
        ]);
        Assert.IsNotNull(newest);
        CollectionAssert.AreEqual(new[] { 0, 1, 4 }, newest.Value.Version);
        Assert.AreEqual("companion-v0.1.4", newest.Value.Release.TagName);
    }

    [TestMethod]
    public void NewestCompanionRelease_Keeps_Prereleases()
    {
        // Every companion build is published as a prerelease; excluding them would make the
        // check permanently find nothing.
        var newest = UpdateChecker.NewestCompanionRelease(
        [
            Rel("companion-v0.1.4", prerelease: true),
            Rel("companion-v0.1.5", prerelease: true),
        ]);
        CollectionAssert.AreEqual(new[] { 0, 1, 5 }, newest!.Value.Version);
    }

    [TestMethod]
    public void NewestCompanionRelease_Picks_Numerically_Highest_Not_List_Order()
    {
        // GitHub lists newest-first by creation date, but a backfilled or re-cut release can
        // break that; the selection must be by version, not position.
        var newest = UpdateChecker.NewestCompanionRelease(
        [
            Rel("companion-v0.1.9"),
            Rel("companion-v0.1.10"),
            Rel("companion-v0.1.2"),
        ]);
        CollectionAssert.AreEqual(new[] { 0, 1, 10 }, newest!.Value.Version);
    }

    [TestMethod]
    public void NewestCompanionRelease_Returns_Null_When_None_Match()
    {
        Assert.IsNull(UpdateChecker.NewestCompanionRelease([]));
        Assert.IsNull(UpdateChecker.NewestCompanionRelease(null));
        Assert.IsNull(UpdateChecker.NewestCompanionRelease(
        [
            Rel("v1.5.8", prerelease: false),
            Rel("companion-v0.1.4", draft: true),
        ]));
    }

    // Decoding.

    [TestMethod]
    public void Release_Decode_Is_Additive_And_Tolerates_Absent_Flags()
    {
        const string json = """
        [{"tag_name":"companion-v0.1.5","draft":false,"prerelease":true,
          "html_url":"https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5",
          "unknown_future_field":"ignored"},
         {"tag_name":"v1.5.8"}]
        """;
        var decoded = JsonSerializer.Deserialize<List<GitHubRelease>>(json,
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true })!;
        Assert.AreEqual(2, decoded.Count);
        Assert.AreEqual("companion-v0.1.5", decoded[0].TagName);
        Assert.IsTrue(decoded[0].Prerelease);
        // Absent draft/prerelease default to false rather than failing the whole list.
        Assert.IsFalse(decoded[1].Draft);
        Assert.IsFalse(decoded[1].Prerelease);
        Assert.IsNull(decoded[1].HtmlUrl);
    }

    [TestMethod]
    public void Invalid_Json_Fails_Decode()
    {
        Assert.ThrowsException<JsonException>(() =>
            JsonSerializer.Deserialize<List<GitHubRelease>>("{not json"));
        // GitHub's rate-limit body is a JSON object where a list is expected: a decode
        // failure, not a crash.
        Assert.ThrowsException<JsonException>(() =>
            JsonSerializer.Deserialize<List<GitHubRelease>>("""{"message":"API rate limit exceeded"}"""));
    }

    // Release link validation.

    [TestMethod]
    public void IsValidReleaseUrl_Accepts_Only_The_Tokdash_Releases_Path()
    {
        Assert.IsTrue(UpdateChecker.IsValidReleaseUrl("https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5"));
        // GitHub owner/repo are case-insensitive; the API's canonical casing need not match ours.
        Assert.IsTrue(UpdateChecker.IsValidReleaseUrl("https://github.com/jingbiaomei/tokdash/releases/tag/companion-v0.1.5"));
        // http is rejected outright - the link opens in the user's browser.
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl("http://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5"));
        // Host spoofs that read as github.com to a human but parse to another host.
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl("https://github.com.evil.test/JingbiaoMei/Tokdash/releases/tag/x"));
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl("https://github.com@evil.test/JingbiaoMei/Tokdash/releases/tag/x"));
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl("https://evil.test/JingbiaoMei/Tokdash/releases/tag/x"));
        // Right host, wrong repo or wrong path.
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl("https://github.com/someone/else/releases/tag/x"));
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl("https://github.com/JingbiaoMei/Tokdash/issues/1"));
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl("javascript:alert(1)"));
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl(""));
        Assert.IsFalse(UpdateChecker.IsValidReleaseUrl(null));
    }

    [TestMethod]
    public void ReleaseUrl_Falls_Back_To_A_Tag_Url_Built_From_The_Parsed_Version()
    {
        const string canonical = "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v0.1.5";
        // A hostile or missing html_url must not reach the browser; the fallback is built
        // only from parsed integers, so no server-supplied text survives into the URL.
        Assert.AreEqual(canonical, UpdateChecker.ReleaseUrl(Rel("companion-v0.1.5", url: "https://evil.test/pwn"), [0, 1, 5]));
        Assert.AreEqual(canonical, UpdateChecker.ReleaseUrl(Rel("companion-v0.1.5", url: null), [0, 1, 5]));
        // A valid html_url is preserved (it may point at a nicer canonical form).
        Assert.AreEqual(canonical, UpdateChecker.ReleaseUrl(Rel("companion-v0.1.5", url: canonical), [0, 1, 5]));
        // The fallback is itself a URL the validator accepts.
        Assert.IsTrue(UpdateChecker.IsValidReleaseUrl(UpdateChecker.ReleaseUrl(Rel("companion-v0.1.5"), [0, 1, 5])));
    }

    // 24h throttle.

    [TestMethod]
    public void ShouldAutoCheck_Respects_Opt_In_And_24_Hours()
    {
        var now = DateTimeOffset.FromUnixTimeSeconds(1785261463);
        // Opt-out means no request is ever made, however long it has been.
        Assert.IsFalse(UpdateChecker.ShouldAutoCheck(false, null, now));
        Assert.IsFalse(UpdateChecker.ShouldAutoCheck(false, now.AddSeconds(-90000), now));
        // Never checked -> due immediately once opted in.
        Assert.IsTrue(UpdateChecker.ShouldAutoCheck(true, null, now));
        // Inside the window -> not due. This is what keeps the 60s refresh tick from turning
        // into 60s GitHub polling.
        Assert.IsFalse(UpdateChecker.ShouldAutoCheck(true, now.AddSeconds(-60), now));
        Assert.IsFalse(UpdateChecker.ShouldAutoCheck(true, now.AddSeconds(-86399), now));
        // Exactly 24h and beyond -> due.
        Assert.IsTrue(UpdateChecker.ShouldAutoCheck(true, now.AddSeconds(-86400), now));
        Assert.IsTrue(UpdateChecker.ShouldAutoCheck(true, now.AddSeconds(-200000), now));
        // A future timestamp means the clock moved backwards; treat it as due rather than
        // blocking checks until real time catches up.
        Assert.IsTrue(UpdateChecker.ShouldAutoCheck(true, now.AddSeconds(3600), now));
    }

    [TestMethod]
    public async Task Scheduled_Check_Is_Skipped_While_Inside_The_24h_Window()
    {
        var fake = new FakeUpdateClient(new List<GitHubRelease> { Rel("companion-v99.0.0") });
        var store = new CompanionStore(new StubTokdashClient());
        store.SetUpdateClient(fake);
        store.Settings.AutomaticUpdateChecks = true;
        store.Settings.LastUpdateCheckAt = DateTimeOffset.Now.AddMinutes(-5);

        await store.CheckForUpdatesAsync(manual: false);
        Assert.AreEqual(0, fake.Calls, "throttled: no request at all");

        // Manual always checks, throttle or not.
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual(1, fake.Calls);

        // Opted out -> scheduled checks never fire even when overdue.
        store.Settings.AutomaticUpdateChecks = false;
        store.Settings.LastUpdateCheckAt = DateTimeOffset.Now.AddDays(-3);
        await store.CheckForUpdatesAsync(manual: false);
        Assert.AreEqual(1, fake.Calls);
    }

    // Badge visibility (store rules).

    [TestMethod]
    public void Badge_Shows_Only_For_A_Newer_Unskipped_Version()
    {
        var store = new CompanionStore(new StubTokdashClient());
        string current = UpdateChecker.CurrentVersion;
        int[] parsed = UpdateChecker.ParseVersion(current)!;
        Assert.IsNotNull(parsed, $"build version {current} must parse");
        string newer = UpdateChecker.VersionString([parsed[0], parsed[1], parsed[2] + 1]);
        string evenNewer = UpdateChecker.VersionString([parsed[0], parsed[1], parsed[2] + 2]);
        string older = UpdateChecker.VersionString([parsed[0], parsed[1], Math.Max(0, parsed[2] - 1)]);

        // Nothing known -> no badge.
        store.Settings.AvailableUpdateVersion = null;
        Assert.IsFalse(store.ShowsUpdateBadge);

        // Newer -> badge, and the accessible name says so.
        store.Settings.AvailableUpdateVersion = newer;
        Assert.IsTrue(store.ShowsUpdateBadge);
        Assert.AreEqual(newer, store.UpdateAvailableVersion);
        Assert.AreEqual(L10n.T("settings_update_available"), store.SettingsAccessibilityName);

        // Same version as installed (i.e. the user updated) -> badge clears itself.
        store.Settings.AvailableUpdateVersion = current;
        Assert.IsFalse(store.ShowsUpdateBadge);
        Assert.AreEqual(L10n.T("settings"), store.SettingsAccessibilityName);

        // Older -> never.
        store.Settings.AvailableUpdateVersion = older;
        Assert.IsFalse(store.ShowsUpdateBadge);

        // Explicitly skipped -> hidden for that version only...
        store.Settings.AvailableUpdateVersion = newer;
        store.Settings.SkippedUpdateVersion = newer;
        Assert.IsFalse(store.ShowsUpdateBadge);
        // ...and a later release re-arms it.
        store.Settings.AvailableUpdateVersion = evenNewer;
        Assert.IsTrue(store.ShowsUpdateBadge);

        // A malformed persisted version fails closed rather than badging on garbage.
        store.Settings.SkippedUpdateVersion = null;
        store.Settings.AvailableUpdateVersion = "not-a-version";
        Assert.IsFalse(store.ShowsUpdateBadge);
    }

    [TestMethod]
    public async Task Transient_States_Never_Show_The_Badge()
    {
        // Checking / offline / malformed / rate-limited are not badge-worthy: the dot means
        // "an update is waiting", not "something happened".
        foreach (var failure in new Exception[]
        {
            new UpdateCheckException(UpdateCheckError.Offline),
            new UpdateCheckException(UpdateCheckError.Timeout),
            new UpdateCheckException(UpdateCheckError.RateLimited, 403),
            new UpdateCheckException(UpdateCheckError.RateLimited, 429),
            new UpdateCheckException(UpdateCheckError.Decode),
            new UpdateCheckException(UpdateCheckError.HttpStatus, 500),
        })
        {
            var store = new CompanionStore(new StubTokdashClient());
            store.SetUpdateClient(new FakeUpdateClient(failure));
            store.Settings.AvailableUpdateVersion = null;
            await store.CheckForUpdatesAsync(manual: true);
            Assert.IsFalse(store.ShowsUpdateBadge, $"{failure.Message} must not badge");
            Assert.AreEqual(UpdateStatusKind.Failed, store.UpdateStatus.Kind);
            Assert.AreEqual(L10n.T("settings"), store.SettingsAccessibilityName);
        }
    }

    [TestMethod]
    public async Task Update_Check_Never_Alters_The_Tokdash_Connection_State()
    {
        // The whole point of running the check off the refresh path: a GitHub failure is not
        // a Tokdash outage.
        var store = new CompanionStore(new StubTokdashClient());
        await store.RefreshAsync();
        var connected = store.ConnectionState;
        Assert.AreEqual(ConnectionState.Connected, connected);

        store.SetUpdateClient(new FakeUpdateClient(new UpdateCheckException(UpdateCheckError.Offline)));
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual(connected, store.ConnectionState);

        store.SetUpdateClient(new FakeUpdateClient(new UpdateCheckException(UpdateCheckError.RateLimited, 429)));
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual(connected, store.ConnectionState);

        store.SetUpdateClient(new FakeUpdateClient(new List<GitHubRelease> { Rel("companion-v99.0.0") }));
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual(connected, store.ConnectionState);
        // ...and the failure count driving refresh backoff is untouched too.
        Assert.AreEqual(0, store.FailureCount);
    }

    [TestMethod]
    public async Task Scheduled_Failure_Is_Silent_And_Keeps_A_Known_Update()
    {
        const string url = "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v99.0.0";
        var store = new CompanionStore(new StubTokdashClient());
        store.SetUpdateClient(new FakeUpdateClient(new List<GitHubRelease> { Rel("companion-v99.0.0") }));
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual(UpdateStatusKind.Available, store.UpdateStatus.Kind);
        Assert.AreEqual("99.0.0", store.UpdateAvailableVersion);
        Assert.AreEqual(url, store.Settings.AvailableUpdateUrl);

        // A scheduled failure must not overwrite the status line or drop the pending update.
        store.SetUpdateClient(new FakeUpdateClient(new UpdateCheckException(UpdateCheckError.Offline)));
        store.Settings.AutomaticUpdateChecks = true;
        store.Settings.LastUpdateCheckAt = DateTimeOffset.Now.AddDays(-2);   // make it due
        await store.CheckForUpdatesAsync(manual: false);
        Assert.AreEqual(UpdateStatusKind.Available, store.UpdateStatus.Kind, "scheduled failure stays silent");
        Assert.IsTrue(store.ShowsUpdateBadge);

        // Either way the attempt is stamped, so "at most once every 24 hours" holds while
        // offline instead of retrying on every refresh tick.
        Assert.IsNotNull(store.Settings.LastUpdateCheckAt);
        Assert.IsFalse(UpdateChecker.ShouldAutoCheck(true, store.Settings.LastUpdateCheckAt, DateTimeOffset.Now));

        // A manual failure reports the reason but still keeps the badge.
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual(UpdateStatusKind.Failed, store.UpdateStatus.Kind);
        Assert.AreEqual(L10n.T("update_failed_offline"), store.UpdateStatus.Message);
        Assert.IsTrue(store.ShowsUpdateBadge, "a failed check never clears a known update");
    }

    [TestMethod]
    public async Task Successful_Check_Clears_A_Stale_Available_Version()
    {
        var store = new CompanionStore(new StubTokdashClient());
        store.Settings.AvailableUpdateVersion = "99.0.0";
        store.Settings.AvailableUpdateUrl = "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v99.0.0";
        // The releases list no longer offers anything newer (e.g. the user updated).
        store.SetUpdateClient(new FakeUpdateClient(new List<GitHubRelease> { Rel("companion-v0.0.1") }));
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual(UpdateStatusKind.UpToDate, store.UpdateStatus.Kind);
        Assert.IsNull(store.Settings.AvailableUpdateVersion);
        Assert.IsFalse(store.ShowsUpdateBadge);
    }

    [TestMethod]
    public async Task Mixed_Release_List_Yields_The_Newest_Companion_Prerelease()
    {
        var store = new CompanionStore(new StubTokdashClient());
        store.Settings.SkippedUpdateVersion = null;
        store.SetUpdateClient(new FakeUpdateClient(
        [
            Rel("v1.5.8", prerelease: false),
            Rel("companion-v99.0.0", prerelease: true),
            Rel("companion-v99.0.1", draft: true),
            Rel("companion-v99.0.0-rc1"),
        ]));
        await store.CheckForUpdatesAsync(manual: true);
        Assert.AreEqual("99.0.0", store.UpdateAvailableVersion);
        Assert.IsTrue(store.ShowsUpdateBadge);
    }

    // Last-check caption.

    [TestMethod]
    public void LastCheckedText_Tiers()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            var now = DateTimeOffset.FromUnixTimeSeconds(1785261463);
            Assert.AreEqual("Not checked yet", UpdateChecker.LastCheckedText(null, now));
            Assert.AreEqual("Last checked just now", UpdateChecker.LastCheckedText(now.AddSeconds(-30), now));
            Assert.AreEqual("Last checked 10 min ago", UpdateChecker.LastCheckedText(now.AddSeconds(-600), now));
            Assert.AreEqual("Last checked 2 h ago", UpdateChecker.LastCheckedText(now.AddSeconds(-7200), now));
            Assert.AreEqual("Last checked 2 d ago", UpdateChecker.LastCheckedText(now.AddSeconds(-200000), now));
        }
        finally { L10n.Current = saved; }
    }

    // Localization.

    [TestMethod]
    public void Update_Strings_Exist_In_Both_Languages()
    {
        var saved = L10n.Current;
        try
        {
            L10n.Current = AppLanguage.ZhHans;
            Assert.AreEqual("设置，有可用更新", L10n.T("settings_update_available"));
            Assert.AreEqual("有新版本 0.1.5 可用", L10n.T("update_available", "0.1.5"));
            Assert.AreEqual("Tokdash Companion 已是最新版本。", L10n.T("update_up_to_date"));
            L10n.Current = AppLanguage.English;
            Assert.AreEqual("Settings, update available", L10n.T("settings_update_available"));
            Assert.AreEqual("Version 0.1.5 is available", L10n.T("update_available", "0.1.5"));
        }
        finally { L10n.Current = saved; }

        // Parity is asserted globally in StoreHelperTests; this pins the update keys directly
        // so a new one can't ship English-only.
        var zh = new HashSet<string>(L10n.KeysFor(AppLanguage.ZhHans));
        foreach (var key in L10n.KeysFor(AppLanguage.English))
        {
            if (key.StartsWith("update_") || key == "section_updates" || key == "settings_update_available")
                Assert.IsTrue(zh.Contains(key), $"zh-Hans is missing {key}");
        }
    }

    // Settings migration.

    [TestMethod]
    public void Settings_From_Before_Update_Checking_Decode_With_The_Feature_Off()
    {
        // A v0.1.4 settings file has none of the update fields. Decoding must preserve every
        // existing preference and default update checking to OFF (it is opt-in).
        const string json = """
        {
          "BaseURL": "https://wsl.example.test/tokdash",
          "LaunchAtLogin": true,
          "LowQuotaNotifications": true,
          "Thresholds": {"FiveHour": 27, "Weekly": 13, "Other": 19},
          "Language": 2
        }
        """;
        var settings = JsonSerializer.Deserialize<CompanionSettings>(json)!;
        Assert.AreEqual("https://wsl.example.test/tokdash", settings.BaseURL);
        Assert.IsTrue(settings.LowQuotaNotifications);
        Assert.AreEqual(AppLanguage.ZhHans, settings.Language);
        Assert.IsFalse(settings.AutomaticUpdateChecks);
        Assert.IsNull(settings.LastUpdateCheckAt);
        Assert.IsNull(settings.AvailableUpdateVersion);
        Assert.IsNull(settings.SkippedUpdateVersion);

        // Round-trips without losing anything.
        var again = JsonSerializer.Deserialize<CompanionSettings>(JsonSerializer.Serialize(settings))!;
        Assert.AreEqual(settings.BaseURL, again.BaseURL);
        Assert.IsFalse(again.AutomaticUpdateChecks);
    }

    [TestMethod]
    public void CurrentVersion_Is_A_Parseable_Three_Part_Version()
    {
        // Fed from companion/VERSION via Directory.Build.props. If this ever stops parsing,
        // the badge silently fails closed - so pin it.
        string current = UpdateChecker.CurrentVersion;
        Assert.IsNotNull(UpdateChecker.ParseVersion(current), $"CurrentVersion '{current}' must parse");
    }
}
