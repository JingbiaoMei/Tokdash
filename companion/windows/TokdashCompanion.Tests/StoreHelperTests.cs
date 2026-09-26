using System.IO;
using System.Runtime.CompilerServices;
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
        // A bare 7-day window normalizes to "Weekly" (contract §Row anatomy): Codex
        // sends "7-day window", MiniMax/Kimi/Grok send "Weekly" - one reading now.
        Assert.AreEqual("Weekly", QuotaRow.DisplayLabel("7-day window"));
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
        // The feature name survives the shortening; the window token normalizes like
        // a bare one ("7-day" -> "Weekly").
        Assert.AreEqual("Spark · Weekly", QuotaRow.DisplayLabel("GPT-5.3-Codex-Spark · 7-day"));
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
            Assert.AreEqual("低于昨日 12%", L10n.T("comparison_below", 12, L10n.T("word_yesterday")));
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
    public void Claude_DisplayLabel_Passes_Server_Wording_Through()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            // v1.1 rule: only Claude's own two names get the standard wording; everything
            // else keeps the server's wording. The expected fixture pins the plain "weekly"
            // bucket lower-case ("weekly", not a forced "Weekly").
            Assert.AreEqual("weekly", ClaudeRow("weekly", "weekly", 8).DisplayBucketLabel);
            Assert.AreEqual("5-hour", ClaudeRow("5h", "5-hour", 71).DisplayBucketLabel);
            // Claude's own names normalize; the weekly_all id reaches the same via "weekly all".
            Assert.AreEqual("5-hour", ClaudeRow("session", "Session", 14).DisplayBucketLabel);
            Assert.AreEqual("Weekly", ClaudeRow("weekly_all", "Weekly All", 8).DisplayBucketLabel);
            // Model-scoped windows keep the model name.
            Assert.AreEqual("Opus", ClaudeRow("weekly_scoped_opus", "Opus", 8).DisplayBucketLabel);
        }
        finally { L10n.Current = saved; }
    }

    [TestMethod]
    public void Rank_Helpers_Name_Display_And_Logo()
    {
        Assert.AreEqual("Codex", CompanionStore.ToolDisplayName("codex"));
        Assert.AreEqual("OpenCode", CompanionStore.ToolDisplayName("opencode"));
        Assert.AreEqual("OpenClaw", CompanionStore.ToolDisplayName("openclaw"));
        Assert.AreEqual("Zed", CompanionStore.ToolDisplayName("zed"), "every scanner id has a name now");
        Assert.AreEqual("Mystery_tool", CompanionStore.ToolDisplayName("mystery_tool"), "unknown ids capitalize, never blank");
        Assert.AreEqual("Pi", CompanionStore.ToolDisplayName("pi_agent"));
        Assert.AreEqual("DeepSeek Harness", CompanionStore.ToolDisplayName("dsh"));

        Assert.AreEqual("codex", CompanionStore.LogoAssetName("codex"));
        Assert.AreEqual("opencode", CompanionStore.LogoAssetName("opencode"));
        Assert.AreEqual("openclaw", CompanionStore.LogoAssetName("openclaw"), "OpenClaw ships a mark now");
        Assert.AreEqual("zed", CompanionStore.LogoAssetName("zed"), "zed ships a mark now");
        Assert.AreEqual("pi", CompanionStore.LogoAssetName("pi_agent"));
        Assert.AreEqual("omp", CompanionStore.LogoAssetName("omp"), "pi/omp used to be text-only");

        Assert.AreEqual("gpt-5.6-sol", CompanionStore.StripProviderPrefix("openai/gpt-5.6-sol"));
        Assert.AreEqual("claude-opus-4-7", CompanionStore.StripProviderPrefix("claude-opus-4-7"), "no slash -> unchanged");
    }

    [TestMethod]
    public void QuotaLogo_Mark_Map_Matches_The_Web_Brand_Map()
    {
        // The minimax regression: MiniMax wears its OWN pink mark. MiMo is a separate
        // provider - its wordmark must never stand in for MiniMax, and the server never
        // emits a "mimo" quota provider, so mimo stays unmapped (text-only header).
        Assert.AreEqual("minimax", CompanionStore.QuotaLogoAssetName("minimax"));
        Assert.IsNull(CompanionStore.QuotaLogoAssetName("mimo"), "no mimo quota provider exists; never borrow its wordmark");

        Assert.AreEqual("codex", CompanionStore.QuotaLogoAssetName("codex"));
        Assert.AreEqual("claude", CompanionStore.QuotaLogoAssetName("claude"));
        Assert.AreEqual("kimi", CompanionStore.QuotaLogoAssetName("kimi"));
        Assert.AreEqual("grok", CompanionStore.QuotaLogoAssetName("grok"));
        Assert.AreEqual("zcode", CompanionStore.QuotaLogoAssetName("zai"), "Z.ai rows wear the Zcode badge");
        Assert.AreEqual("opencode", CompanionStore.QuotaLogoAssetName("opencode_go"), "OpenCode Go shares the OpenCode mark");
        Assert.AreEqual("antigravity", CompanionStore.QuotaLogoAssetName("antigravity"));
        Assert.IsNull(CompanionStore.QuotaLogoAssetName("commandcode"), "no shipped mark -> text-only header");

        // Every mark the map returns must exist under Assets\Agents\ (the csproj glob
        // packages them) - the map and the packaged asset set must not drift apart.
        var agents = AgentsDir();
        foreach (var name in new[] { "codex", "claude", "kimi", "grok", "zcode", "minimax", "opencode", "antigravity" })
            Assert.IsTrue(File.Exists(Path.Combine(agents, $"{name}.png")), $"Assets/Agents/{name}.png missing");
    }

    [TestMethod]
    public void ToolLogo_Map_Covers_Every_Scanner_Tool_And_Pins_Assets()
    {
        // Every source_name the scanner can emit (src/tokdash/sources/coding_tools.py) maps
        // to a packaged mark - except the two documented text-only ids: mimo (wordmark-only
        // brand, illegible at row height) and devin (no brand art shipped anywhere).
        var expected = new Dictionary<string, string?>
        {
            ["opencode"] = "opencode", ["kilocode"] = "kilocode", ["cline"] = "cline",
            ["codex"] = "codex", ["claude"] = "claude", ["gemini_cli"] = "gemini",
            ["antigravity_cli"] = "antigravity", ["amp"] = "amp", ["kimi"] = "kimi",
            ["grok"] = "grok", ["pi_agent"] = "pi", ["omp"] = "omp",
            ["copilot_cli"] = "copilot", ["hermes"] = "hermes", ["mimo"] = null,
            ["zcode"] = "zcode", ["qoder"] = "qoder", ["qoder_cli"] = "qoder",
            ["dsh"] = "dsh", ["reasonix"] = "reasonix", ["workbuddy"] = "workbuddy",
            ["zed"] = "zed", ["qwen_code"] = "qwen_code", ["muse"] = "muse",
            ["crush"] = "crush", ["minimax"] = "minimax", ["devin"] = null,
            // Aliases the web brand map normalizes onto the same marks.
            ["claude_code"] = "claude", ["gemini"] = "gemini", ["pi"] = "pi",
            ["copilot"] = "copilot", ["github_copilot_cli"] = "copilot", ["antigravity"] = "antigravity",
            ["cursor"] = "cursor",
        };
        var agents = AgentsDir();
        var packaged = new HashSet<string>();
        foreach (var (tool, asset) in expected)
        {
            Assert.AreEqual(asset, CompanionStore.LogoAssetName(tool), $"tool id {tool}");
            if (asset is not null) packaged.Add(asset);
        }

        // The map and the packaged asset set must not drift apart: every mapped mark exists
        // as a PNG, and every darkInvert mark also ships its pre-inverted {name}-dark copy.
        foreach (var asset in packaged)
            Assert.IsTrue(File.Exists(Path.Combine(agents, $"{asset}.png")), $"Assets/Agents/{asset}.png missing");
        foreach (var asset in new[] { "codex", "grok", "zcode", "cline", "hermes", "omp", "zed", "cursor" })
            Assert.IsTrue(File.Exists(Path.Combine(agents, $"{asset}-dark.png")), $"Assets/Agents/{asset}-dark.png missing");
        Assert.IsFalse(File.Exists(Path.Combine(agents, "mimo.png")),
            "the MiMo wordmark must not sit unreferenced where it could be mistaken for MiniMax's mark");
    }

    private static string AgentsDir([CallerFilePath] string source = "") =>
        Path.GetFullPath(Path.Combine(Path.GetDirectoryName(source)!, "..", "TokdashCompanion", "Assets", "Agents"));

    [TestMethod]
    public void Rank_Shares_Are_Percent_Of_Full_List_And_Never_NaN()
    {
        // Denominators are the FULL by_tool sum / the FULL combined_models list, so the
        // top-3 shares need not add to 100 and the bar always matches the printed percent.
        var usage = new UsageResponse
        {
            TotalTokens = 1000,
            ByTool = new()
            {
                ["codex"] = new ToolAgg { Tokens = 550 },
                ["claude"] = new ToolAgg { Tokens = 250 },
                ["kimi"] = new ToolAgg { Tokens = 100 },
                ["zed"] = new ToolAgg { Tokens = 100 },
            },
            CombinedModels = new()
            {
                new ModelAgg { Name = "openai/gpt-5.6", Tokens = 700 },
                new ModelAgg { Name = "anthropic/claude-x", Tokens = 200 },
                new ModelAgg { Name = "x/y", Tokens = 50 },
                new ModelAgg { Name = "z/w", Tokens = 50 },
            },
        };
        var snap = new Snapshot
        {
            Period = UsagePeriod.Today, Usage = usage, Quota = new QuotaResponse(),
            Thresholds = QuotaThresholds.Defaults, Components = new CompanionComponents(),
            Now = DateTimeOffset.FromUnixTimeSeconds(1785080120),
        };
        var tools = snap.TopTools;
        Assert.AreEqual(3, tools.Count);
        CollectionAssert.AreEqual(new[] { "55%", "25%", "10%" }, tools.Select(t => t.PctText).ToList(),
            "shares of the full 1000-token by_tool sum; the omitted zed row keeps the top-3 under 100%");
        CollectionAssert.AreEqual(new[] { 0.55, 0.25, 0.10 }, tools.Select(t => t.Fraction).ToList());
        var models = snap.TopModels;
        CollectionAssert.AreEqual(new[] { "70%", "20%", "5%" }, models.Select(m => m.PctText).ToList());

        // Zero-sum lists: empty bar and "0%", never NaN.
        var zero = new Snapshot
        {
            Period = UsagePeriod.Today,
            Usage = new UsageResponse { TotalTokens = 0, ByTool = new(), CombinedModels = new() },
            Quota = new QuotaResponse(), Thresholds = QuotaThresholds.Defaults,
            Components = new CompanionComponents(),
            Now = DateTimeOffset.FromUnixTimeSeconds(1785080120),
        };
        CollectionAssert.AreEqual(new string[0], zero.TopTools.Select(t => t.PctText).ToList());
        CollectionAssert.AreEqual(new string[0], zero.TopModels.Select(m => m.PctText).ToList());
    }

    [TestMethod]
    public void AntigravityPools_Collapse_To_Two_Worst_Rows()
    {
        // One bucket per model floods the flyout; collapse to the two dashboard pools,
        // each showing the worst remaining. Pinned to the macOS antigravityPools cases.
        var pooled = CompanionStore.AntigravityPools(
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
            var pooled = CompanionStore.AntigravityPools(rows);
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
            Assert.AreEqual("Gemini · Weekly", CompanionStore.AntigravityPools(new List<QuotaRow> { weekly })[0].DisplayBucketLabel);

            var fiveHour = new QuotaRow("Antigravity", "gemini_3_pro", "Gemini 3 Pro", 8,
                DateTimeOffset.FromUnixTimeSeconds(captured + 3 * 3600L),
                false, "default", true, false,
                DateTimeOffset.FromUnixTimeSeconds(captured));
            Assert.AreEqual("Gemini · 5-hour", CompanionStore.AntigravityPools(new List<QuotaRow> { fiveHour })[0].DisplayBucketLabel);

            // No reset time (idle model) -> defaults to 5-hour, never "Weekly".
            Assert.AreEqual("Gemini · 5-hour", CompanionStore.AntigravityPools(new List<QuotaRow> { Row("gemini_3_pro", "Gemini 3 Pro", 8) })[0].DisplayBucketLabel);
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

    // MARK: - E12 instance stepper (contract §Instance stepper)

    /// <summary>Thursday 2026-09-24; its local week starts Monday 2026-09-21.</summary>
    private static readonly DateOnly Thu = new(2026, 9, 24);

    private static void SteppedEq((DateOnly From, DateOnly To)? actual, string from, string to)
    {
        Assert.IsNotNull(actual, "stepped instance has no window?");
        Assert.AreEqual(DateOnly.Parse(from), actual.Value.From);
        Assert.AreEqual(DateOnly.Parse(to), actual.Value.To);
    }

    [TestMethod]
    public void EarlierLimit_Pins_The_Walkback_Limits()
    {
        Assert.AreEqual(13, CompanionStore.EarlierLimit(UsagePeriod.Today));
        Assert.AreEqual(8, CompanionStore.EarlierLimit(UsagePeriod.Week));
        Assert.AreEqual(11, CompanionStore.EarlierLimit(UsagePeriod.Month));
        Assert.AreEqual(2, CompanionStore.EarlierLimit(UsagePeriod.Year));
    }

    [TestMethod]
    public void SteppedDates_Are_Full_Elapsed_Calendar_Windows()
    {
        Assert.IsNull(CompanionStore.SteppedDates(UsagePeriod.Today, 0, Thu), "present is not stepped");

        // Day: the single calendar day N back.
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Today, 1, Thu), "2026-09-23", "2026-09-23");
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Today, 13, Thu), "2026-09-11", "2026-09-11");

        // Week: full Mon..Sun weeks (never a partial one), stepped from THIS week's Monday.
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Week, 1, Thu), "2026-09-14", "2026-09-20");
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Week, 2, Thu), "2026-09-07", "2026-09-13");

        // A Sunday rolls back to its own week's Monday BEFORE stepping (no straddling).
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Week, 1, new DateOnly(2026, 9, 27)), "2026-09-14", "2026-09-20");

        // Weeks may cross years: Jan 7 2026 (Wed) -> its Monday is Jan 5; one back starts Dec 29.
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Week, 1, new DateOnly(2026, 1, 7)), "2025-12-29", "2026-01-04");

        // Month: full 1st..last-day calendar months, rolling across the year line both ways.
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Month, 1, Thu), "2026-08-01", "2026-08-31");
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Month, 11, Thu), "2025-10-01", "2025-10-31");
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Month, 1, new DateOnly(2026, 1, 15)), "2025-12-01", "2025-12-31");

        // Year: full Jan1..Dec31 calendar years.
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Year, 1, Thu), "2025-01-01", "2025-12-31");
        SteppedEq(CompanionStore.SteppedDates(UsagePeriod.Year, 2, Thu), "2024-01-01", "2024-12-31");
    }

    [TestMethod]
    public void UsageRequestPath_Present_Keeps_Wire_Forms_Stepped_Always_Uses_Dates()
    {
        // Present instances: exactly the shipped §Period windows wire forms.
        Assert.AreEqual("/api/usage?period=today", CompanionStore.UsageRequestPath(UsagePeriod.Today, Thu));
        Assert.AreEqual("/api/usage?date_from=2026-09-21&date_to=2026-09-24", CompanionStore.UsageRequestPath(UsagePeriod.Week, Thu));
        Assert.AreEqual("/api/usage?period=month", CompanionStore.UsageRequestPath(UsagePeriod.Month, Thu));
        Assert.AreEqual("/api/usage?period=year", CompanionStore.UsageRequestPath(UsagePeriod.Year, Thu));

        // Stepped: explicit full elapsed window, never period=.
        Assert.AreEqual("/api/usage?date_from=2026-09-23&date_to=2026-09-23", CompanionStore.UsageRequestPath(UsagePeriod.Today, Thu, 1));
        Assert.AreEqual("/api/usage?date_from=2026-09-07&date_to=2026-09-13", CompanionStore.UsageRequestPath(UsagePeriod.Week, Thu, 2));
        Assert.AreEqual("/api/usage?date_from=2026-08-01&date_to=2026-08-31", CompanionStore.UsageRequestPath(UsagePeriod.Month, Thu, 1));
        Assert.AreEqual("/api/usage?date_from=2024-01-01&date_to=2024-12-31", CompanionStore.UsageRequestPath(UsagePeriod.Year, Thu, 2));
    }

    [TestMethod]
    public void InstanceKicker_Words_One_Back_Dates_Further_Back()
    {
        // Present kickers stay byte-identical.
        Assert.AreEqual("TODAY", CompanionStore.InstanceKicker(UsagePeriod.Today, 0, Thu));
        Assert.AreEqual("THIS WEEK", CompanionStore.InstanceKicker(UsagePeriod.Week, 0, Thu));
        Assert.AreEqual("THIS MONTH", CompanionStore.InstanceKicker(UsagePeriod.Month, 0, Thu));
        Assert.AreEqual("THIS YEAR", CompanionStore.InstanceKicker(UsagePeriod.Year, 0, Thu));

        // One back: the localized words, uppercased to kicker style in Latin scripts.
        Assert.AreEqual("YESTERDAY", CompanionStore.InstanceKicker(UsagePeriod.Today, 1, Thu));
        Assert.AreEqual("LAST WEEK", CompanionStore.InstanceKicker(UsagePeriod.Week, 1, Thu));
        Assert.AreEqual("LAST MONTH", CompanionStore.InstanceKicker(UsagePeriod.Month, 1, Thu));
        Assert.AreEqual("LAST YEAR", CompanionStore.InstanceKicker(UsagePeriod.Year, 1, Thu));

        // Further back: calendar labels. Same-month weeks drop the repeated month in English.
        Assert.AreEqual("SEP 21", CompanionStore.InstanceKicker(UsagePeriod.Today, 3, Thu));
        Assert.AreEqual("SEP 7 – 13", CompanionStore.InstanceKicker(UsagePeriod.Week, 2, Thu));
        // Cross-month week (two back from Oct 12's week = Sep 28..Oct 4) and cross-year
        // week (two back from Jan 14's week = Dec 29..Jan 4). offset 1 is the word form.
        Assert.AreEqual("SEP 28 – OCT 4", CompanionStore.InstanceKicker(UsagePeriod.Week, 2, new DateOnly(2026, 10, 12)));
        Assert.AreEqual("DEC 29 – JAN 4, 2026", CompanionStore.InstanceKicker(UsagePeriod.Week, 2, new DateOnly(2026, 1, 14)));
        Assert.AreEqual("JUL 2026", CompanionStore.InstanceKicker(UsagePeriod.Month, 2, Thu));
        Assert.AreEqual("OCT 2025", CompanionStore.InstanceKicker(UsagePeriod.Month, 11, Thu));
        Assert.AreEqual("2024", CompanionStore.InstanceKicker(UsagePeriod.Year, 2, Thu));
    }

    [TestMethod]
    public void InstanceKicker_Chinese_Words_And_Date_Forms()
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.ZhHans;
        try
        {
            Assert.AreEqual("今日", CompanionStore.InstanceKicker(UsagePeriod.Today, 0, Thu));
            Assert.AreEqual("昨日", CompanionStore.InstanceKicker(UsagePeriod.Today, 1, Thu));
            Assert.AreEqual("上周", CompanionStore.InstanceKicker(UsagePeriod.Week, 1, Thu));
            Assert.AreEqual("9月21日", CompanionStore.InstanceKicker(UsagePeriod.Today, 3, Thu));
            // CJK forms keep both operands of a range (the "Sep 7 – 13" ellipsis is English-only).
            Assert.AreEqual("9月7日 – 9月13日", CompanionStore.InstanceKicker(UsagePeriod.Week, 2, Thu));
            Assert.AreEqual("2026年7月", CompanionStore.InstanceKicker(UsagePeriod.Month, 2, Thu));
            // Cross-year CJK week ranges carry the year on both operands.
            Assert.AreEqual("2025年12月29日 – 2026年1月4日",
                CompanionStore.InstanceKicker(UsagePeriod.Week, 2, new DateOnly(2026, 1, 14)));
            Assert.AreEqual("2024", CompanionStore.InstanceKicker(UsagePeriod.Year, 2, Thu));
        }
        finally { L10n.Current = saved; }
    }

    [TestMethod]
    public void Glance_Stepped_Week_Histogram_Windows_On_Instances_Week()
    {
        var insights = new InsightsResponse
        {
            Daily =
            [
                new DailyPoint { Date = "2026-09-07", Tokens = 5 },
                new DailyPoint { Date = "2026-09-13", Tokens = 7 },
                // A day from the CURRENT week must not leak into the stepped instance.
                new DailyPoint { Date = "2026-09-21", Tokens = 99 },
            ],
        };
        var face = CompanionStore.GlanceFaceFor(UsagePeriod.Week, insights, null,
            new CompanionComponents(), Thu, 2)!;
        Assert.AreEqual(GlanceKind.Days, face.Kind);
        Assert.AreEqual(5, face.DayTokens![0], "Mon of the stepped week");
        Assert.AreEqual(7, face.DayTokens![6], "Sun of the stepped week");
        Assert.IsFalse(face.DayTokens!.Contains(99), "current-week data does not leak");
    }

    [TestMethod]
    public void Glance_Stepped_Month_Grid_Is_Calendar_Bounded()
    {
        var stats = StatsRollingTo("2026-09-22");
        var face = CompanionStore.GlanceFaceFor(UsagePeriod.Month, null, stats,
            new CompanionComponents(), Thu, 1)!;
        Assert.AreEqual(GlanceKind.Grid, face.Kind);
        Assert.AreEqual(31, face.WindowDays, "exactly Aug 1..31");
        Assert.AreEqual(2, face.FilledCells, "Aug 15 + Aug 31 only");
        // Jul 27 column is clamped (Aug 1-2 cells), then 5 whole weeks: Jul27, Aug3, 10, 17, 24, 31.
        Assert.AreEqual(6, face.GridColumns!.Length, "Mon-start columns clamped to the month");
    }

    [TestMethod]
    public void Glance_Stepped_Year_Hides_When_Rolling_Series_Does_Not_Reach()
    {
        var stats = StatsRollingTo("2026-09-22");
        // Any stepped year starts outside a rolling 365-day series (contract §Instance stepper).
        Assert.IsNull(CompanionStore.GlanceFaceFor(UsagePeriod.Year, null, stats, new CompanionComponents(), Thu, 1));
        Assert.IsNull(CompanionStore.GlanceFaceFor(UsagePeriod.Year, null, stats, new CompanionComponents(), Thu, 2));
        // Present year view is unaffected: the trailing 180-day grid still renders.
        Assert.IsNotNull(CompanionStore.GlanceFaceFor(UsagePeriod.Year, null, stats, new CompanionComponents(), Thu));
    }

    private static StatsResponse StatsRollingTo(string newest)
    {
        // A sparse rolling series: newest day + one day at the 364-day edge + Aug days.
        return new StatsResponse
        {
            Contributions =
            [
                new Contribution { Date = "2025-09-23", Intensity = 1 },
                new Contribution { Date = "2026-08-15", Intensity = 3 },
                new Contribution { Date = "2026-08-31", Intensity = 2 },
                new Contribution { Date = newest, Intensity = 1 },
            ],
        };
    }

    [TestMethod]
    public async Task Stepper_Walks_Clamps_Reanchors_And_Fetches_Instance_Windows()
    {
        CompanionStore.ClockOverride = new DateTimeOffset(2026, 9, 24, 12, 0, 0, TimeSpan.Zero);
        try
        {
            var client = new FakeClient();
            var store = new CompanionStore(client);

            // Present: › is inert, ‹ is available.
            Assert.AreEqual(0, store.PeriodOffset);
            Assert.IsFalse(store.CanStepLater);
            Assert.IsTrue(store.CanStepEarlier);

            // Walk the day past its 13-step limit and back.
            for (int i = 0; i < 20; i++) store.StepPeriod(1);
            Assert.AreEqual(13, store.PeriodOffset, "clamped at the walk-back limit");
            Assert.IsFalse(store.CanStepEarlier, "‹ inert at the limit");
            store.StepPeriod(-1);
            Assert.AreEqual(12, store.PeriodOffset);

            // Selecting a segment re-anchors to the present, even the SAME segment.
            store.SelectPeriod(UsagePeriod.Today);
            Assert.AreEqual(0, store.PeriodOffset);
            Assert.IsFalse(store.CanStepLater);

            // Stepped day: fetch group used ranged hourly + explicit usage window.
            store.StepPeriod(1);
            await store.RefreshAsync();
            CollectionAssert.Contains(client.Requests.ToList(), "/api/usage?date_from=2026-09-23&date_to=2026-09-23");
            CollectionAssert.Contains(client.Requests.ToList(), "/api/insights?facets=hourly&date_from=2026-09-23&date_to=2026-09-23");
            var snap = store.Snapshot!;
            Assert.AreEqual(1, snap.InstanceOffset);
            Assert.AreEqual("YESTERDAY", snap.KickerText);

            // Stepped year: snapshot stamp, kicker, explicit window on both endpoints.
            store.SelectPeriod(UsagePeriod.Year);
            store.StepPeriod(2);
            await store.RefreshAsync();
            snap = store.Snapshot!;
            Assert.AreEqual(UsagePeriod.Year, snap.Period);
            Assert.AreEqual(2, snap.InstanceOffset);
            Assert.AreEqual("2024", snap.KickerText);
            CollectionAssert.Contains(client.Requests.ToList(), "/api/usage?date_from=2024-01-01&date_to=2024-12-31");
            CollectionAssert.Contains(client.Requests.ToList(), "/api/active-time?date_from=2024-01-01&date_to=2024-12-31");
        }
        finally { CompanionStore.ClockOverride = null; }
    }
}
