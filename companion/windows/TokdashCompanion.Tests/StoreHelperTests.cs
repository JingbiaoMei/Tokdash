using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Pure CompanionStore helpers: API timestamp parsing and base-URL validation. The
/// timestamp cases are pinned to the same instants as the macOS parseTimestamp tests so
/// the two platforms cannot drift on freshness.
/// </summary>
[TestClass]
public class StoreHelperTests
{
    // 2026-07-28T17:57:43Z
    private const long Epoch = 1785261463L;

    private static long Ms(string s) =>
        CompanionStore.ParseTimestamp(s)!.Value.ToUnixTimeMilliseconds();

    [TestMethod]
    public void ParseTimestamp_Reads_Naive_Forms_As_Utc()
    {
        // The server emits a naive UTC datetime with six fractional digits.
        Assert.AreEqual(Epoch * 1000 + 500, Ms("2026-07-28T17:57:43.500951"));
        Assert.AreEqual(Epoch * 1000 + 500, Ms("2026-07-28T17:57:43.500"));
        Assert.AreEqual(Epoch * 1000, Ms("2026-07-28T17:57:43"));
    }

    [TestMethod]
    public void ParseTimestamp_Reads_Explicit_Offsets()
    {
        Assert.AreEqual(Epoch * 1000, Ms("2026-07-28T17:57:43Z"));
        Assert.AreEqual(Epoch * 1000, Ms("2026-07-28T17:57:43+00:00"));
        // A non-UTC offset must be honored, not assumed UTC.
        Assert.AreEqual((Epoch - 7200) * 1000, Ms("2026-07-28T17:57:43+02:00"));
    }

    [TestMethod]
    public void ParseTimestamp_Rejects_Garbage()
    {
        Assert.IsNull(CompanionStore.ParseTimestamp("not-a-timestamp"));
        Assert.IsNull(CompanionStore.ParseTimestamp(null));
    }

    [TestMethod]
    public void DisplayLabel_Drops_The_Trailing_Window()
    {
        // Pinned to the macOS displayLabel cases.
        Assert.AreEqual("5-hour", QuotaRow.DisplayLabel("5-hour window"));
        Assert.AreEqual("7-day", QuotaRow.DisplayLabel("7-day window"));
        Assert.AreEqual("weekly", QuotaRow.DisplayLabel("weekly window"));
        Assert.AreEqual("5-hour", QuotaRow.DisplayLabel("5-hour Window"), "case-insensitive");
        // Labels that never carried the word are untouched.
        Assert.AreEqual("5-hour", QuotaRow.DisplayLabel("5-hour"));
        Assert.AreEqual("Weekly", QuotaRow.DisplayLabel("Weekly"));
        Assert.AreEqual("Global 5-hour", QuotaRow.DisplayLabel("Global 5-hour"));
        // "window" as the whole label would shorten to nothing - keep it rather than
        // render an empty row.
        Assert.AreEqual("window", QuotaRow.DisplayLabel("window"));
        Assert.AreEqual("", QuotaRow.DisplayLabel(null!));
    }

    [TestMethod]
    public void DisplayLabel_Shortens_Codex_Metered_Feature_Names()
    {
        // "GPT-5.3-Codex-Spark · 5-hour" is far too wide for the flyout; the window must
        // survive the shortening. Pinned to the macOS displayLabel cases.
        Assert.AreEqual("Spark · 5-hour", QuotaRow.DisplayLabel("GPT-5.3-Codex-Spark · 5-hour"));
        Assert.AreEqual("Spark · 7-day", QuotaRow.DisplayLabel("GPT-5.3-Codex-Spark · 7-day"));
        // A non-hyphenated name is left alone - only Codex's model naming is verbose.
        Assert.AreEqual("Video · Weekly", QuotaRow.DisplayLabel("Video · Weekly"));
        // A bare window contains a hyphen but no " · " separator; it must not be split.
        Assert.AreEqual("5-hour", QuotaRow.DisplayLabel("5-hour"));
        Assert.AreEqual("Global 5-hour", QuotaRow.DisplayLabel("Global 5-hour"));
    }

    [TestMethod]
    public void ResetsTextForRemaining_Is_Relative()
    {
        // Pin English: ResetsTextForRemaining routes through L10n, so assertions are locale-stable.
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            // Pure form: seconds-remaining -> text, no clock dependency. Pinned to the macOS cases.
            Assert.AreEqual("resets soon", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(-10)));  // past/stale
            Assert.AreEqual("resets soon", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(0)));
            Assert.AreEqual("resets soon", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(59)));   // sub-minute
            Assert.AreEqual("resets in 1 minute", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(60)));
            Assert.AreEqual("resets in 1 minute", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(119)));
            Assert.AreEqual("resets in 2 minutes", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(120)));
            Assert.AreEqual("resets in 90 minutes", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(5400)));
            Assert.AreEqual("resets in 119 minutes", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(7199)), "max minute value stays under 120");
            Assert.AreEqual("resets in 2 hours", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(7200)));
            Assert.AreEqual("resets in 5 hours", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(18000)));
            Assert.AreEqual("resets in 23 hours", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(86399)), "max hour value stays under 24");
            Assert.AreEqual("resets in 1 day", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(86400)));
            Assert.AreEqual("resets in 1 day", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(129600)), "1.5d floors to the whole unit");
            Assert.AreEqual("resets in 3 days", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(259200)));
            // The antigravity weekly case that motivated the days tier: 3d22h reads as days here
            // and as "resets in 3 days" on the web dashboard, not "resets in 94 hours".
            Assert.AreEqual("resets in 3 days", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds((3 * 24 + 22) * 3600)));
            Assert.AreEqual("resets in 7 days", QuotaRow.ResetsTextForRemaining(TimeSpan.FromSeconds(7 * 24 * 3600)));
        }
        finally { L10n.Current = saved; }

        // A nil resets_at renders nothing (bucket without a reset time).
        Assert.AreEqual("", Row("5h", "5-hour", 10).ResetsText);
    }

    [TestMethod]
    public void L10n_Chinese_Translations_And_Parity()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.ZhHans;
        try
        {
            Assert.AreEqual("订阅跟踪已关闭", L10n.T("tracking_off"));
            Assert.AreEqual("剩余 14%", L10n.T("percent_left", 14));
            Assert.AreEqual("wsl · 已连接", L10n.T("server_connected", "wsl"));
            Assert.AreEqual("本地", CompanionStore.ServerLabel("http://127.0.0.1:55423"));
            Assert.AreEqual("低于昨日 12%", L10n.T("comparison_below", 12));
            Assert.AreEqual("5 小时后重置", L10n.T("resets_in_hours", 5, ""));
            Assert.AreEqual("3 天后重置", L10n.T("resets_in_days", 3, ""));
            Assert.AreEqual("5 小时", ClaudeRow("session", "Session", 14).DisplayBucketLabel);
            Assert.AreEqual("每周", ClaudeRow("weekly_all", "Weekly All", 8).DisplayBucketLabel);
            Assert.AreEqual("Fable", ClaudeRow("weekly_scoped_fable", "Fable", 8).DisplayBucketLabel);

            L10n.Current = AppLanguage.English;
            Assert.AreEqual("14% left", L10n.T("percent_left", 14));
        }
        finally { L10n.Current = saved; }

        // Every English key has a Chinese translation (no silent fallback to English).
        var enKeys = L10n.KeysFor(AppLanguage.English);
        var zhKeys = L10n.KeysFor(AppLanguage.ZhHans);
        CollectionAssert.AreEquivalent(enKeys, zhKeys, "zh-Hans is missing keys present in English");
    }

    private static QuotaRow Row(string bucket, string label, double left) =>
        new("Antigravity", bucket, label, left, null, false, "default", true);

    private static QuotaRow ClaudeRow(string bucket, string label, double left) =>
        new("Claude", bucket, label, left, null, false, "default", true);

    [TestMethod]
    public void CanonicalBucket_Maps_Claude_Session_And_Weekly_To_Threshold_Windows()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            var t = QuotaThresholds.Defaults;  // 5h=20, weekly=10, other=15

            // Claude's real bucket ids share Codex's thresholds instead of the 15% "other" bucket.
            // Generic windows use the standard names; model-scoped weekly windows keep the model.
            var session = ClaudeRow("session", "Session", 14);
            Assert.AreEqual("5h", session.CanonicalBucket);
            Assert.AreEqual("5-hour", session.DisplayBucketLabel);
            Assert.IsTrue(session.IsLow(t), "session -> 5h (20%); 14% is low");
            Assert.IsFalse(ClaudeRow("session", "Session", 25).IsLow(t));

            var weeklyScoped = ClaudeRow("weekly_scoped_opus", "Opus", 8);
            Assert.AreEqual("weekly", weeklyScoped.CanonicalBucket);
            Assert.AreEqual("Opus", weeklyScoped.DisplayBucketLabel);
            Assert.IsTrue(weeklyScoped.IsLow(t), "weekly_scoped -> weekly (10%); 8% is low");
            Assert.AreEqual("Fable", ClaudeRow("weekly_scoped_fable", "Fable", 8).DisplayBucketLabel);
            Assert.AreEqual("Weekly", ClaudeRow("weekly_all", "Weekly All", 8).DisplayBucketLabel);

            // Legacy fallback bucket ids from the older API shape.
            Assert.AreEqual("5h", ClaudeRow("five_hour", "5-hour", 10).CanonicalBucket);
            Assert.AreEqual("weekly", ClaudeRow("seven_day", "7-day", 10).CanonicalBucket);

            // An unrecognised Claude bucket falls through to "other" - we don't guess its window.
            var unknown = ClaudeRow("usage_claude_sonnet_4", "Claude Sonnet 4", 10);
            Assert.AreEqual("usage_claude_sonnet_4", unknown.CanonicalBucket);
            Assert.IsTrue(unknown.IsLow(t), "unknown -> other (15%); 10% is low");

            // Non-Claude providers are untouched: a "session" bucket on another provider stays itself.
            Assert.AreEqual("session", Row("session", "Session", 10).CanonicalBucket);
        }
        finally { L10n.Current = saved; }
    }

    [TestMethod]
    public void AntigravityPools_Collapse_To_Two_Worst_Rows()
    {
        // One bucket per model floods the flyout; collapse to the two dashboard pools,
        // each showing the worst remaining. Pinned to the macOS antigravityPools cases.
        var pooled = Snapshot.AntigravityPools(
        [
            Row("gemini_3_pro", "Gemini 3 Pro", 62),
            Row("gemini_3_flash", "Gemini 3 Flash", 41),   // worst gemini
            Row("claude_sonnet", "Claude Sonnet", 88),
            Row("gpt_oss", "GPT OSS", 12),                 // worst claude/gpt
        ]);

        Assert.AreEqual(2, pooled.Count, "exactly two pooled rows");
        Assert.AreEqual("Gemini", pooled[0].BucketLabel);
        Assert.AreEqual(41, pooled[0].Left, 0.001, "pool shows the worst remaining");
        Assert.AreEqual("pool:gemini", pooled[0].Bucket);
        Assert.AreEqual("Claude/GPT", pooled[1].BucketLabel);
        Assert.AreEqual(12, pooled[1].Left, 0.001);
    }

    [TestMethod]
    public void AntigravityPools_Keep_Unmatched_Rows_Rather_Than_Hiding_Them()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            // A model matching neither pool must not silently vanish; it still gets a window
            // suffix (defaulting to 5-hour when it has no reset time).
            var rows = new List<QuotaRow> { Row("mystery_model", "Mystery Model", 30) };
            var pooled = Snapshot.AntigravityPools(rows);
            Assert.AreEqual(1, pooled.Count);
            Assert.AreEqual("Mystery Model", pooled[0].BucketLabel, "falls back to the raw rows");
            Assert.AreEqual("Mystery Model · 5-hour", pooled[0].DisplayBucketLabel);
        }
        finally { L10n.Current = saved; }
    }

    [TestMethod]
    public void AntigravityWindowLabel_Auto_Determined_From_Reset_Time()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            // A 5-hour window can never reset more than 5h out; 8h absorbs skew before weekly.
            Assert.AreEqual("5-hour", QuotaRow.AntigravityWindowLabelForRemaining(3 * 3600));
            Assert.AreEqual("5-hour", QuotaRow.AntigravityWindowLabelForRemaining(5 * 3600));
            Assert.AreEqual("Weekly", QuotaRow.AntigravityWindowLabelForRemaining((3 * 24 + 22) * 3600));
            Assert.AreEqual("Weekly", QuotaRow.AntigravityWindowLabelForRemaining(7 * 24 * 3600));

            // Pooled row appends the auto-determined window. Weekly reset (3d22h out from
            // capture) -> "Gemini · Weekly"; 5-hour reset -> "Gemini · 5-hour".
            long captured = 1_782_907_200L;
            var weekly = new QuotaRow("Antigravity", "gemini_3_pro", "Gemini 3 Pro", 8,
                DateTimeOffset.FromUnixTimeSeconds(captured + (3 * 24 + 22) * 3600L),
                false, "default", true, false,
                DateTimeOffset.FromUnixTimeSeconds(captured));
            Assert.AreEqual("Gemini · Weekly", Snapshot.AntigravityPools(new List<QuotaRow> { weekly })[0].DisplayBucketLabel);

            var fiveHour = new QuotaRow("Antigravity", "gemini_3_pro", "Gemini 3 Pro", 8,
                DateTimeOffset.FromUnixTimeSeconds(captured + 3 * 3600L),
                false, "default", true, false,
                DateTimeOffset.FromUnixTimeSeconds(captured));
            Assert.AreEqual("Gemini · 5-hour", Snapshot.AntigravityPools(new List<QuotaRow> { fiveHour })[0].DisplayBucketLabel);

            // No reset time (idle model) -> defaults to 5-hour, never "Weekly".
            Assert.AreEqual("Gemini · 5-hour", Snapshot.AntigravityPools(new List<QuotaRow> { Row("gemini_3_pro", "Gemini 3 Pro", 8) })[0].DisplayBucketLabel);
        }
        finally { L10n.Current = saved; }
    }

    [TestMethod]
    public void ServerLabel_Names_The_Configured_Host()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            // Loopback stays "Local"; a remote host uses its first DNS label so a Tailscale
            // URL doesn't claim to be local. Pinned to the macOS serverLabel cases.
            Assert.AreEqual("Local", CompanionStore.ServerLabel("http://127.0.0.1:55423"));
            Assert.AreEqual("Local", CompanionStore.ServerLabel("http://localhost:55423"));
            Assert.AreEqual("wsl", CompanionStore.ServerLabel("https://wsl.tail76535.ts.net/tokdash"));
            Assert.AreEqual("wsl", CompanionStore.ServerLabel("  https://WSL.tail76535.ts.net/tokdash  "), "trimmed and lowercased");
            Assert.AreEqual("homelab", CompanionStore.ServerLabel("http://homelab:8080"));
            // A bare IP has no name to shorten - showing "192" would be nonsense.
            Assert.AreEqual("192.168.1.50", CompanionStore.ServerLabel("http://192.168.1.50:55423"));
            // Unparseable input must not throw; fall back to the default label.
            Assert.AreEqual("Local", CompanionStore.ServerLabel(null));
            Assert.AreEqual("Local", CompanionStore.ServerLabel("not a url"));
        }
        finally { L10n.Current = saved; }
    }

    [TestMethod]
    public void IsValidBaseURL_Accepts_Absolute_Http_Urls()
    {
        Assert.IsTrue(CompanionStore.IsValidBaseURL("http://127.0.0.1:55423"));
        Assert.IsTrue(CompanionStore.IsValidBaseURL("https://wsl.tail76535.ts.net/tokdash"));
        Assert.IsTrue(CompanionStore.IsValidBaseURL("  http://127.0.0.1:55423  "), "surrounding whitespace is trimmed");
    }

    [TestMethod]
    public void IsValidBaseURL_Rejects_Blank_Relative_And_Wrong_Scheme()
    {
        // Rejecting these at every write path is what stops the blank-URL state from
        // recurring on the next launch (CreateDefaultClient only repairs it on read).
        Assert.IsFalse(CompanionStore.IsValidBaseURL(null));
        Assert.IsFalse(CompanionStore.IsValidBaseURL(""));
        Assert.IsFalse(CompanionStore.IsValidBaseURL("   "));
        Assert.IsFalse(CompanionStore.IsValidBaseURL("127.0.0.1:55423"), "no scheme");
        Assert.IsFalse(CompanionStore.IsValidBaseURL("/tokdash"), "relative");
        Assert.IsFalse(CompanionStore.IsValidBaseURL("ftp://host/tokdash"), "wrong scheme");
        Assert.IsFalse(CompanionStore.IsValidBaseURL("http:///tokdash"), "no host");
    }
}
