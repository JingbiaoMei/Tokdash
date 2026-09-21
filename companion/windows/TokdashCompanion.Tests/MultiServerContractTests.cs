using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

[TestClass]
public class MultiServerContractTests
{
    internal static readonly JsonSerializerOptions JsonOptions = new() { PropertyNameCaseInsensitive = true };

    private static Snapshot MakeSnap(UsageResponse usage, QuotaResponse quota) => new()
    {
        Period = UsagePeriod.Today,
        Usage = usage,
        Quota = quota,
        Thresholds = QuotaThresholds.Defaults,
        Components = new CompanionComponents(),
        Now = new System.DateTimeOffset(2026, 7, 26, 15, 35, 20, System.TimeSpan.Zero),
    };

    [TestMethod]
    public void SharedFixture_Pins_Combine_LowDedup_And_MinimumDelay()
    {
        using var expectedDocument = JsonDocument.Parse(File.ReadAllText(ContractFile("expected", "multi-server.json")));
        var expected = expectedDocument.RootElement.GetProperty("expected");
        var today = Decode<UsageResponse>("usage-today.json");
        var combined = MultiServerTokdashClient.CombineUsage([today, today]);

        Assert.AreEqual(expected.GetProperty("today").GetProperty("tokens_exact").GetString(), combined.TotalTokens.ToString());
        Assert.AreEqual(expected.GetProperty("today").GetProperty("messages").GetString(), combined.TotalMessages.ToString());
        Assert.AreEqual(expected.GetProperty("today").GetProperty("cost").GetString(), Formatter.FormatCost(combined.TotalCost));
        // Full delta row re-derived from the SUMMED current/prev totals (never averaged pcts).
        Assert.AreEqual(-12.0, System.Math.Round(combined.Comparison!.CostPct!.Value));
        Assert.AreEqual(-12.0, System.Math.Round(combined.Comparison.TokensPct!.Value));
        Assert.AreEqual(-12.0, System.Math.Round(combined.Comparison.MessagesPct!.Value));

        var quota = Decode<QuotaResponse>("quota.json");
        var providers = new Dictionary<string, ProviderQuota>();
        foreach (var label in new[] { "Local", "Second" })
            foreach (var provider in quota.Providers!) providers[$"{label} · {provider.Key}"] = provider.Value;
        var snapshot = MakeSnap(combined, new QuotaResponse { Enabled = true, Providers = providers });
        var low = snapshot.LowQuotaRows;
        Assert.IsTrue(expected.GetProperty("quota_low").GetProperty("dedupe_identical_subscriptions").GetBoolean());
        Assert.IsTrue(low.Count <= expected.GetProperty("quota_low").GetProperty("visible_count_max").GetInt32());
        Assert.IsTrue(low.All(row => !row.Provider.Contains(" · ")), "deduped rows must omit the server label");
        var lowerCaseLabelSnapshot = MakeSnap(new UsageResponse(),
            new QuotaResponse { Enabled = true, Providers = new() { ["wsl · codex"] = quota.Providers!["codex"] } });
        Assert.AreEqual("wsl · Codex", lowerCaseLabelSnapshot.AllQuotaGroups[0].Provider);

        Assert.AreEqual("minimum per-server delay", expected.GetProperty("delay_rule").GetString());
        var now = System.DateTimeOffset.UtcNow;
        Assert.AreEqual(System.TimeSpan.FromSeconds(30), CompanionStore.MinimumDelay([
            CompanionStore.ComputeDelay(true, 2, false, null, now),
            CompanionStore.ComputeDelay(true, 3, false, null, now),
        ]));
    }

    [TestMethod]
    public void Combine_Three_Metric_Omits_Metric_When_Any_Server_Omits_Prev()
    {
        // Contract §Full delta row: a metric whose *_prev is omitted by ANY contributing
        // server is omitted from the combined row entirely.
        var both = Decode<UsageResponse>("usage-today.json");
        var noMsgsPrev = Decode<UsageResponse>("usage-today.json");
        noMsgsPrev.Comparison!.MessagesPrev = null;
        var combined = MultiServerTokdashClient.CombineUsage([both, noMsgsPrev]);
        Assert.IsNotNull(combined.Comparison!.CostPct);
        Assert.IsNotNull(combined.Comparison.TokensPct);
        Assert.IsNull(combined.Comparison.MessagesPct, "one server without messages_prev drops the metric");

        // Zero summed prev -> null (never divide by zero).
        var zero = new UsageResponse { Comparison = new Comparison { CostPrev = 0 } };
        Assert.IsNull(MultiServerTokdashClient.CombineUsage([zero, zero]).Comparison!.CostPct);
    }

    [TestMethod]
    public void SharedFixture_Pins_Model_Ranking_Top_Models_From_Combined()
    {
        var today = Decode<UsageResponse>("usage-today.json");

        // combined_models is ranked by tokens, and top_models is its first five.
        var tokens = today.CombinedModels!.Select(m => m.Tokens).ToList();
        CollectionAssert.AreEqual(tokens.OrderByDescending(value => value).ToList(), tokens,
            "combined_models must be ranked by tokens");
        CollectionAssert.AreEqual(
            today.CombinedModels!.Take(5).Select(m => m.Name).ToList(),
            today.TopModels!.Select(m => m.Name).ToList(),
            "top_models must be the first five of combined_models");

        // The costliest model sits outside the token podium. v1.1's model rank strip takes
        // the first three of combined_models - never a by-cost sort - so the trap model
        // must not appear while the token leaders do.
        var costLeader = today.TopModelsByCost![0];
        Assert.AreEqual("openai/o5-deep-research", costLeader.Name);

        var snapshot = MakeSnap(today, new QuotaResponse());
        var modelLabels = snapshot.TopModels.Select(m => m.Label).ToList();
        Assert.AreEqual(3, modelLabels.Count);
        Assert.IsFalse(modelLabels.Any(n => n.Contains("o5-deep-research")),
            "the model strip is token-ranked; a cost sort would name this one");
        Assert.AreEqual("gpt-5.6-sol", modelLabels[0], "provider prefix stripped");
        Assert.AreEqual("12.7M", snapshot.TopModels[0].ValueText);
        Assert.IsTrue(snapshot.TopModels.All(m => m.LogoAsset is null), "model rows carry no logos");
    }

    [TestMethod]
    public void CombineUsage_Ranks_Both_Podiums_From_The_Full_List()
    {
        var today = Decode<UsageResponse>("usage-today.json");
        var combined = MultiServerTokdashClient.CombineUsage([today, today]);

        var tokens = combined.CombinedModels!.Select(m => m.Tokens).ToList();
        CollectionAssert.AreEqual(tokens.OrderByDescending(value => value).ToList(), tokens);
        Assert.AreEqual(5, combined.TopModels!.Count, "top_models is capped at five");
        CollectionAssert.AreEqual(
            combined.CombinedModels!.Take(5).Select(m => m.Name).ToList(),
            combined.TopModels!.Select(m => m.Name).ToList());
        Assert.AreEqual("openai/o5-deep-research", combined.TopModelsByCost![0].Name);
        // Summed across both servers rather than taken from either one's podium.
        Assert.AreEqual(today.CombinedModels![0].Tokens * 2, combined.CombinedModels![0].Tokens);
    }

    [TestMethod]
    public void CombinedActiveMs_Drops_On_Any_Missing_Or_Failed()
    {
        long ms = 11_520_000;
        Assert.AreEqual(2 * ms, CompanionStore.CombinedActiveMs([(ms, false), (ms, false)]));
        Assert.IsNull(CompanionStore.CombinedActiveMs([(ms, false), (null, false)]), "no payload -> drop");
        Assert.IsNull(CompanionStore.CombinedActiveMs([(ms, false), (null, true)]), "failed server -> drop");
        Assert.IsNull(CompanionStore.CombinedActiveMs([]), "no servers at all -> nothing to show");
    }

    [TestMethod]
    public void PerServerRows_Keep_Settings_Order_And_Mark_Unreachable()
    {
        var servers = new List<CompanionServerSettings>
        {
            new() { Id = "local", Label = "Local", Enabled = true },
            new() { Id = "second", Label = "Second", Enabled = true },
        };
        var usages = new UsageResponse?[]
        {
            new() { TotalCost = 3.42, TotalTokens = 18_700_000 },
            null,
        };
        var rows = CompanionStore.PerServerRows(servers, usages, new HashSet<string> { "second" });
        Assert.AreEqual("Local", rows[0].Label);
        Assert.AreEqual("$3.42 · 18.7M", rows[0].ValueText);
        Assert.AreEqual("Second", rows[1].Label);
        Assert.IsFalse(rows[1].Reachable);
        Assert.AreEqual("unreachable", rows[1].ValueText);
    }

    [TestMethod]
    public void RegistryComparisonDetectsAnyServerChange()
    {
        var original = new[] {
            new CompanionServerSettings { Id = "local", Label = "Local", BaseUrl = "http://127.0.0.1:55423", Enabled = true },
            new CompanionServerSettings { Id = "wsl", Label = "wsl", BaseUrl = "https://wsl.example/tokdash", Enabled = false },
        };
        var changed = new[] {
            new CompanionServerSettings { Id = "local", Label = "Local", BaseUrl = "http://127.0.0.1:55423", Enabled = true },
            new CompanionServerSettings { Id = "wsl", Label = "wsl", BaseUrl = "https://wsl.example/tokdash", Enabled = true },
        };

        Assert.IsTrue(SettingsWindow.ServerRegistriesEqual(original, original));
        Assert.IsFalse(SettingsWindow.ServerRegistriesEqual(original, changed));
    }

    /// <summary>
    /// A row is judged against ITS OWN account's failure, not the card's newest one.
    /// <para>
    /// The fixture is a healthy <c>~/.claude</c> beside a permanently broken
    /// <c>~/.claude-academic</c>, generated from the server's own payload
    /// (<c>tests/test_companion_contract_accounts.py</c> regenerates and diffs it), so
    /// decoding drift shows up here rather than in the field. <c>weekly_scoped_opus</c> is
    /// the row that matters: Claude reports it only once Opus has been used, so it carries
    /// an older captured_at than the cycle the sibling's failure landed in. Judged against
    /// <c>providers.claude.status_at</c> it is marked last-known and stops notifying for as
    /// long as the sibling stays broken; judged against its own account it is current.
    /// Spec §7.
    /// </para>
    /// </summary>
    [TestMethod]
    public void MultiAccountFixtureJudgesRowsAgainstTheirOwnAccount()
    {
        var quota = Decode<QuotaResponse>("quota-multi-account.json");
        var prov = quota.Providers!["claude"];

        // The Accounts list has to decode at all - neither client read it before.
        Assert.IsNotNull(prov.Accounts);
        var accounts = prov.Accounts!;
        CollectionAssert.AreEqual(new[] { "default", "academic" },
            accounts.Select(a => a.Account).ToArray());
        // The healthy account carries no failure timestamp: the case the rule must
        // short-circuit before reaching for one, or every row of the working install is
        // marked failed by the missing-timestamp fallback.
        Assert.IsNull(accounts[0].StatusAt);
        Assert.IsNull(accounts[0].StatusDetail);
        Assert.AreEqual("stale_token", accounts[1].StatusDetail);

        var snap = MakeSnap(new UsageResponse(), quota);
        var group = snap.AllQuotaGroups.Single();
        // The card still warns: one broken credential has to keep warning the provider.
        Assert.IsTrue(group.Failed);

        Assert.IsFalse(group.Rows.Single(r => r.Bucket == "session").Failed);
        Assert.IsFalse(group.Rows.Single(r => r.Bucket == "weekly_all").Failed);
        Assert.IsFalse(group.Rows.Single(r => r.Bucket == "weekly_scoped_opus").Failed,
            "the healthy install's own row, older than the SIBLING's failure");
        Assert.IsTrue(group.Rows.Single(r => r.Bucket == "academic_session").Failed,
            "not refreshed since its own sign-in expired");

        // Same payload with Accounts stripped is every pre-Accounts server: the fallback
        // marks the working install's un-refreshed row last-known, which is the behavior
        // the per-account rule exists to replace. Pinned so the two cannot silently merge.
        prov.Accounts = null;
        var legacy = MakeSnap(new UsageResponse(), quota);
        Assert.IsTrue(legacy.AllQuotaGroups.Single().Rows
            .Single(r => r.Bucket == "weekly_scoped_opus").Failed);
    }

    internal static T Decode<T>(string fixture) =>
        JsonSerializer.Deserialize<T>(File.ReadAllText(ContractFile("fixtures", fixture)), JsonOptions)!;

    internal static string ContractFile(string directory, string file, [CallerFilePath] string source = "") =>
        Path.GetFullPath(Path.Combine(Path.GetDirectoryName(source)!, "..", "..", "contract", directory, file));
}
