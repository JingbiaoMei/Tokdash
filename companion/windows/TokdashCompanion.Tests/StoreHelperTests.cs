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

    private static QuotaRow Row(string bucket, string label, double left) =>
        new("Antigravity", bucket, label, left, null, false, "default", true);

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
        Assert.AreEqual("Gemini Models", pooled[0].BucketLabel);
        Assert.AreEqual(41, pooled[0].Left, 0.001, "pool shows the worst remaining");
        Assert.AreEqual("pool:gemini", pooled[0].Bucket);
        Assert.AreEqual("Claude and GPT Models", pooled[1].BucketLabel);
        Assert.AreEqual(12, pooled[1].Left, 0.001);
    }

    [TestMethod]
    public void AntigravityPools_Keep_Unmatched_Rows_Rather_Than_Hiding_Them()
    {
        // A model matching neither pool must not silently vanish.
        var rows = new List<QuotaRow> { Row("mystery_model", "Mystery Model", 30) };
        var pooled = Snapshot.AntigravityPools(rows);
        Assert.AreEqual(1, pooled.Count);
        Assert.AreEqual("Mystery Model", pooled[0].BucketLabel, "falls back to the raw rows");
    }

    [TestMethod]
    public void ServerLabel_Names_The_Configured_Host()
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
