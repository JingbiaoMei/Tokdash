using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Windows mirror of <c>tests/test_companion_contract_v11.py</c>: the v1.1 display rules
/// transcribed from <c>COMPANION_API.md</c> and run against the shared fixtures, pinning
/// exactly what the <c>expected/</c> files pin. Formatting/period/glance/badge/credits.
/// English locale + the quota payload timestamp as the frozen clock throughout.
/// </summary>
[TestClass]
public sealed class V11RulesTests
{
    // The shared fixtures' own timestamp (quota.json): 2026-07-26T15:35:20Z, a Sunday.
    private static readonly DateTimeOffset Frozen = DateTimeOffset.FromUnixTimeSeconds(1785080120);

    [ClassInitialize]
    public static void PinEnglish(TestContext _) => L10n.Current = AppLanguage.English;

    [ClassCleanup]
    public static void ReleaseClock()
    {
        CompanionStore.ClockOverride = null;
        L10n.Current = AppLanguage.English;
    }

    // MARK: - compact tokens (contract §Token compact notation)

    [DataTestMethod]
    [DataRow(12_982_308L, "13M")]        // trailing ".0" trimmed
    [DataRow(1_243_500_000L, "1243.5M")] // no "B" tier even past 1000M
    [DataRow(779_014L, "779k")]          // k tier rounds, no decimal
    [DataRow(249_669L, "250k")]          // 249.669 -> 250
    public void CompactTokens_Contract_Values(long value, string expected) =>
        Assert.AreEqual(expected, Formatter.CompactTokens(value));

    // MARK: - active-time ladder (contract §Active time)

    [DataTestMethod]
    [DataRow(59_000L, "active <1 m")]
    [DataRow(60_000L, "active 1 m")]
    [DataRow(3_720_000L, "active 1 h 2 m")]
    [DataRow(11_520_000L, "active 3 h 12 m")]
    [DataRow(188_400_000L, "active 2 d 4 h")]
    [DataRow(6_411_600_000L, "active 74 d 5 h")]
    public void ActiveLadder(long activeMs, string expected) =>
        Assert.AreEqual(expected, Formatter.ActiveText(activeMs));

    [TestMethod]
    public void Active_Zero_Is_Absent_Not_Zero()
    {
        Assert.IsNull(MakeSnap(activeMs: 0).ActiveSegmentText);
        Assert.IsNull(MakeSnap(activeMs: null).ActiveSegmentText);
        Assert.AreEqual("active 3 h 12 m", MakeSnap(activeMs: 11_520_000).ActiveSegmentText);
    }

    // MARK: - delta row (contract §Full delta row)

    [DataTestMethod]
    [DataRow(-11.7, "▼", 12L)] // abs(round(pct)), not truncation
    [DataRow(3.2, "▲", 3L)]
    [DataRow(0.0, "±", 0L)]
    public void Delta_Glyph_And_Rounding(double pct, string glyph, long value)
    {
        Assert.AreEqual(glyph, Formatter.DeltaGlyph(pct));
        Assert.AreEqual(value, Formatter.DeltaValue(pct));
    }

    [TestMethod]
    public void Delta_Row_All_Null_Is_Absent()
    {
        // usage-year: the previous year is zero, so every *_pct is null and the whole row hides.
        var usage = Fixture<UsageResponse>("usage-year.json");
        Assert.IsNull(MakeSnap(usage: usage).DeltaRowText);
    }

    [TestMethod]
    public void Delta_Row_Today_Matches_Pinned_String()
    {
        var usage = Fixture<UsageResponse>("usage-today.json");
        Assert.AreEqual("▼ 12% cost · ▼ 12% tokens vs yesterday",
            MakeSnap(usage: usage).DeltaRowText);
    }

    // MARK: - credits clause and row (contract §Reset credits)

    [DataTestMethod]
    [DataRow(2 * 86_400, "in 2 d")]
    [DataRow(5400, "today")]      // < 1 d -> today (1 h 30 m)
    public void Credits_Clause(int expiryOffsetSeconds, string expected) =>
        Assert.AreEqual(expected, CompanionStore.CreditsClause(Frozen.AddSeconds(expiryOffsetSeconds), Frozen));

    [TestMethod]
    public void Credits_Clause_Tomorrow_At_One_And_A_Half_Days()
    {
        // 1..2 days -> "tomorrow" (1.5 d), floored days would say "in 1 d" - it must not.
        Assert.AreEqual("tomorrow", CompanionStore.CreditsClause(Frozen.AddSeconds(36 * 3600), Frozen));
    }

    [TestMethod]
    public void Credits_Row_From_Fixture_At_Frozen_Clock()
    {
        var codex = Fixture<QuotaResponse>("quota-reset-credits.json").Providers!["codex"];
        var reset = codex.ResetCredits!;
        // The pinned credits.json row (expected/credits.json).
        Assert.AreEqual("⚡ Codex · 2 reset credits · expire in 2 d",
            Snapshot.CreditsRowText("Codex", reset, Frozen));
        // Three days out: credit-a is gone, credit-b remains, nothing notifies (the clause).
        Assert.AreEqual("⚡ Codex · 2 reset credits · expire in 38 d",
            Snapshot.CreditsRowText("Codex", reset, Frozen.AddDays(3)));
        // Past both: the count still renders, the whole clause (the word "expire" too) drops.
        Assert.AreEqual("⚡ Codex · 2 reset credits",
            Snapshot.CreditsRowText("Codex", reset, Frozen.AddDays(60)));
    }

    [TestMethod]
    public async Task Credits_Notification_Fires_At_The_Exact_48h_Edge()
    {
        // "notify once a future credit enters its last 48 hours" - the boundary itself counts.
        var storeExact = await NotifyWithCreditExpiryAsync(Frozen.AddHours(48));
        Assert.AreEqual(1, storeExact.Count, "a credit exactly 48 h out is inside the window");
        var storeLate = await NotifyWithCreditExpiryAsync(Frozen.AddHours(48).AddSeconds(1));
        Assert.AreEqual(0, storeLate.Count, "48 h + 1 s is still outside the window");
    }

    [TestMethod]
    public async Task Credits_Notification_Suppressed_While_Group_Failed()
    {
        // Contract §Low-quota notifications: "Suppressed while the Codex provider group
        // failed (last-known credit data is not a basis for an 'expire in' warning)."
        // The suppression is notification-scoped - the ROW still renders (see
        // ContractDecodeTests.Credits_Row_Persists_When_Group_Failed).
        var alerts = await NotifyWithCreditExpiryAsync(Frozen.AddHours(24), groupFailed: true);
        Assert.AreEqual(0, alerts.Count, "failed group must not arm the expiry warning");
    }

    [TestMethod]
    public async Task Credits_Notification_Dedups_By_Credit_Id()
    {
        // Same credit, two refresh cycles -> one alert. Dedup key is
        // (provider, credits[].id, expires_at); a second read must not re-alert.
        CompanionStore.ClockOverride = Frozen;
        var client = new FakeClient { Quota = JsonSerializer.Deserialize<QuotaResponse>("""
        {"enabled":true,"providers":{"codex":{"buckets":[
          {"account":"a","bucket":"5h","bucket_label":"5-hour window","remaining_percent":50.0,
           "resets_at":1785090000,"captured_at":1785080120}],
          "reset_credits":{"available_count":1,"credits":[
            {"id":"credit-dup","expires_at":"2026-07-27T15:35:20Z"}]}}}}
        """, Opts)! };
        var store = new CompanionStore(client);
        L10n.Current = AppLanguage.English;
        store.Settings.LowQuotaNotifications = true;
        var alerts = new List<CompanionStore.CreditAlertItem>();
        store.CreditExpiryAlert += items => alerts.AddRange(items);
        await store.RefreshAsync();
        await store.RefreshAsync();
        CompanionStore.ClockOverride = null;
        Assert.AreEqual(1, alerts.Count, "the same credit id never re-arms");
    }

    private static async Task<List<CompanionStore.CreditAlertItem>> NotifyWithCreditExpiryAsync(
        DateTimeOffset expiry, bool groupFailed = false)
    {
        string expiryText = expiry.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);
        string status = groupFailed ? "\"status\":\"error\",\"status_detail\":\"boom\",\"status_at\":1785080000," : "";
        var json = """
        {"enabled":true,"providers":{"codex":{__STATUS__"buckets":[
          {"account":"a","bucket":"5h","bucket_label":"5-hour window","remaining_percent":50.0,
           "resets_at":1785090000,"captured_at":1785080120}],
          "reset_credits":{"available_count":2,"credits":[
            {"id":"credit-edge","expires_at":"__EXPIRY__"}]}}}}
        """.Replace("__STATUS__", status).Replace("__EXPIRY__", expiryText);
        CompanionStore.ClockOverride = Frozen;
        var client = new FakeClient { Quota = JsonSerializer.Deserialize<QuotaResponse>(json, Opts)! };
        var store = new CompanionStore(client);
        L10n.Current = AppLanguage.English;
        store.Settings.LowQuotaNotifications = true;
        var alerts = new List<CompanionStore.CreditAlertItem>();
        store.CreditExpiryAlert += items => alerts.AddRange(items);
        await store.RefreshAsync();
        CompanionStore.ClockOverride = null;
        return alerts;
    }

    // MARK: - calendar-week window (contract §Period windows)

    [TestMethod]
    public void Week_Window_Is_Calendar_Monday_To_Today()
    {
        var sunday = new DateOnly(2026, 7, 26);
        Assert.AreEqual(new DateOnly(2026, 7, 20), CompanionStore.StartOfWeekMonday(sunday),
            "Sunday rolls back to the Monday six days earlier");
        var monday = new DateOnly(2026, 7, 27);
        Assert.AreEqual(monday, CompanionStore.StartOfWeekMonday(monday),
            "a Monday request is a one-day window; weekday() is what moves it");

        var (from, to) = CompanionStore.WeekRange(sunday);
        var range = Fixture<JsonElement>("usage-week.json").GetProperty("range");
        Assert.AreEqual(range.GetProperty("from").GetString(), from);
        Assert.AreEqual(range.GetProperty("to").GetString(), to);
        // The echo is the raw query param, NOT the window - never branch on it.
        Assert.AreEqual("today", range.GetProperty("period_requested").GetString());
        Assert.AreEqual("custom", range.GetProperty("period_resolved").GetString());
    }

    [DataTestMethod]
    [DataRow(UsagePeriod.Today, "/api/usage?period=today")]
    [DataRow(UsagePeriod.Month, "/api/usage?period=month")]
    [DataRow(UsagePeriod.Year, "/api/usage?period=year")]
    public void Request_Urls_For_Token_Periods(UsagePeriod period, string expected) =>
        Assert.AreEqual(expected,
            CompanionStore.UsageRequestPath(period, new DateOnly(2026, 7, 26)));

    [TestMethod]
    public void Week_Request_Is_A_Date_Range_Never_Period_Week()
    {
        var path = CompanionStore.UsageRequestPath(UsagePeriod.Week, new DateOnly(2026, 7, 26));
        Assert.AreEqual("/api/usage?date_from=2026-07-20&date_to=2026-07-26", path);
        Assert.IsFalse(path.Contains("period=week"), "period=week is a rolling window - never for the segment");
    }

    // MARK: - glance source selection (rule 2: a component that is off never fetches)

    // (The DataRow params are strings: CompanionStore.GlanceSource is internal, and a
    // public test method must not take an internal-typed parameter - CS0051.)
    [DataTestMethod]
    [DataRow("today", true, true, "InsightsHourly")]
    [DataRow("week", true, true, "InsightsDaily")]
    [DataRow("month", true, true, "Stats")]
    [DataRow("year", true, true, "Stats")]
    [DataRow("today", false, true, "None")]
    [DataRow("month", false, true, "None")]
    [DataRow("today", true, false, "None")]
    [DataRow("week", true, false, "None")]
    [DataRow("month", true, false, "Stats")] // grids unaffected
    public void Glance_Source_Per_Period_And_Toggles(
        string periodToken, bool glance, bool histogram, string expected)
    {
        var period = periodToken switch
        {
            "week" => UsagePeriod.Week,
            "month" => UsagePeriod.Month,
            "year" => UsagePeriod.Year,
            _ => UsagePeriod.Today,
        };
        var c = new CompanionComponents { ActivityGlance = glance, ActivityHistogramTodayWeek = histogram };
        Assert.AreEqual(expected, CompanionStore.GlanceSourceFor(period, c).ToString());
    }

    // MARK: - glance windows (contract §Activity glance)

    [DataTestMethod]
    [DataRow(90, 68)]
    [DataRow(180, 143)]
    public void Grid_Window_Trailing_Days_And_Monday_Alignment(int days, int filled)
    {
        var stats = Fixture<StatsResponse>("stats-contributions.json");
        var period = days == 90 ? UsagePeriod.Month : UsagePeriod.Year;
        var face = CompanionStore.GlanceFaceFor(period, null, stats,
            new CompanionComponents(), new DateOnly(2026, 7, 26))!;
        Assert.AreEqual(filled, face.FilledCells, "filled cells in the trailing window");
        Assert.AreEqual(days, face.WindowDays);
        Assert.AreEqual("Mon", face.FirstColumnWeekday);
        Assert.IsTrue(face.GridColumns!.All(col => col.Length == 7), "strict 7-cell Mon..Sun columns");
    }

    [TestMethod]
    public void Day_Face_Maps_Sparse_Daily_Onto_Mon_Sun()
    {
        var ins = Fixture<InsightsResponse>("insights-week.json");
        var face = CompanionStore.GlanceFaceFor(UsagePeriod.Week, ins, null,
            new CompanionComponents(), new DateOnly(2026, 7, 26))!;
        Assert.AreEqual(7, face.DayTokens!.Length);
        Assert.AreEqual(0, face.DayTokens[1], "the fixture has no Tuesday: empty column, not a skipped one");
        Assert.IsTrue(face.DayTokens[0] > 0 && face.DayTokens[6] > 0);
    }

    [TestMethod]
    public void DayFace_AnchorsToTheClocksWeek_NotThePayload()
    {
        // The face is Mon..TODAY (contract §Activity glance). A daily facet that lags
        // into last week must not be presented as the current week's activity: the
        // current week reads all-zero and the strip hides itself instead.
        var ins = Fixture<InsightsResponse>("insights-week.json"); // ends Sun 2026-07-26
        var face = CompanionStore.GlanceFaceFor(UsagePeriod.Week, ins, null,
            new CompanionComponents(), new DateOnly(2026, 8, 1)); // next week
        Assert.IsNull(face, "a stale payload must never be shown as this week's columns");
    }

    [TestMethod]
    public void Glance_Hides_On_Zero_Sources()
    {
        var empty = Fixture<InsightsResponse>("insights-empty.json");
        Assert.IsNull(CompanionStore.GlanceFaceFor(UsagePeriod.Today, empty, null,
            new CompanionComponents(), new DateOnly(2026, 7, 26)), "all-zero day (peak_hour null) hides the strip");
        Assert.IsNull(CompanionStore.GlanceFaceFor(UsagePeriod.Month, null, new StatsResponse(),
            new CompanionComponents(), new DateOnly(2026, 7, 26)), "no contributions hide the grid");
    }

    // MARK: - server update badge (contract §Server update badge)

    [TestMethod]
    public void Update_Badge_Three_Fixtures_Three_Verdicts()
    {
        var available = Fixture<ServerUpdateCheckResponse>("update-check-available.json");
        var off = Fixture<ServerUpdateCheckResponse>("update-check-off.json");
        Assert.AreEqual("2.6.0", CompanionStore.ServerBadgeVersion(available));
        Assert.IsNull(CompanionStore.ServerBadgeVersion(off), "no consent: render nothing");
        Assert.IsNull(CompanionStore.ServerBadgeVersion(
            new ServerUpdateCheckResponse { Enabled = true, UpdateAvailable = true, Latest = null }),
            "update_available without latest is not a badge");
        var version = Fixture<VersionResponse>("version.json");
        Assert.AreEqual("2.5.7", version.RuntimeVersion);
        Assert.AreEqual(true, version.UpdateCheckEnabled);
    }

    [TestMethod]
    public async Task Update_Badge_Is_Gated_By_Update_Check_Enabled()
    {
        // version.update_check_enabled false: /api/update-check is never called.
        var gated = new FakeClient
        {
            Version = new VersionResponse { RuntimeVersion = "2.5.7", UpdateCheckEnabled = false },
            UpdateCheck = Fixture<ServerUpdateCheckResponse>("update-check-available.json"),
        };
        var store = new CompanionStore(gated);
        L10n.Current = AppLanguage.English;
        await store.FetchServerUpdateInfoAsync();
        Assert.AreEqual("2.5.7", store.ServerRuntimeVersion);
        Assert.IsNull(store.ServerUpdateBadgeVersion);
        Assert.AreEqual(0, gated.Requests.Count(r => r.StartsWith("/api/update-check")),
            "the gate is not just a render rule - the endpoint is not called");

        var shown = new FakeClient
        {
            Version = new VersionResponse { RuntimeVersion = "2.5.7", UpdateCheckEnabled = true },
            UpdateCheck = Fixture<ServerUpdateCheckResponse>("update-check-available.json"),
        };
        var store2 = new CompanionStore(shown);
        L10n.Current = AppLanguage.English;
        await store2.FetchServerUpdateInfoAsync();
        Assert.AreEqual("2.6.0", store2.ServerUpdateBadgeVersion);
        Assert.AreEqual("Server update available: v2.6.0", store2.ServerUpdateBadgeText);

        // Silent failure: no version at all, and nothing thrown.
        var dead = new FakeClient { Version = "timeout" };
        var store3 = new CompanionStore(dead);
        await store3.FetchServerUpdateInfoAsync();
        Assert.IsNull(store3.ServerUpdateBadgeText ?? null);
    }

    // MARK: - helpers

    private static Snapshot MakeSnap(UsageResponse? usage = null, long? activeMs = 0) => new()
    {
        Period = UsagePeriod.Today,
        Usage = usage ?? new UsageResponse { TotalTokens = 1 },
        ActiveMs = activeMs,
        Quota = new QuotaResponse(),
        Thresholds = QuotaThresholds.Defaults,
        Components = new CompanionComponents(),
        Now = Frozen,
    };

    private static readonly JsonSerializerOptions Opts = new() { PropertyNameCaseInsensitive = true };

    private static T Fixture<T>(string name) =>
        JsonSerializer.Deserialize<T>(File.ReadAllText(ContractFile("fixtures", name)), Opts)!;

    private static string ContractFile(string leaf, string file, [CallerFilePath] string source = "") =>
        Path.GetFullPath(Path.Combine(Path.GetDirectoryName(source)!, "..", "..", "contract", leaf, file));
}
