using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

[TestClass]
public class MultiServerContractTests
{
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNameCaseInsensitive = true };

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
        Assert.AreEqual(-12.0, Math.Round(combined.Comparison!.CostPct!.Value));

        var quota = Decode<QuotaResponse>("quota.json");
        var providers = new Dictionary<string, ProviderQuota>();
        foreach (var label in new[] { "Local", "Second" })
            foreach (var provider in quota.Providers!) providers[$"{label} · {provider.Key}"] = provider.Value;
        var snapshot = new Snapshot
        {
            Today = combined,
            Month = new UsageResponse(),
            Quota = new QuotaResponse { Enabled = true, Providers = providers },
            Thresholds = QuotaThresholds.Defaults,
        };
        var low = snapshot.LowQuotaRows;
        Assert.IsTrue(expected.GetProperty("quota_low").GetProperty("dedupe_identical_subscriptions").GetBoolean());
        Assert.IsTrue(low.Count <= expected.GetProperty("quota_low").GetProperty("visible_count_max").GetInt32());
        Assert.IsTrue(low.All(row => !row.Provider.Contains(" · ")), "deduped rows must omit the server label");
        var lowerCaseLabelSnapshot = new Snapshot
        {
            Today = new UsageResponse(), Month = new UsageResponse(), Thresholds = QuotaThresholds.Defaults,
            Quota = new QuotaResponse { Enabled = true, Providers = new() { ["wsl · codex"] = quota.Providers!["codex"] } },
        };
        Assert.AreEqual("wsl · Codex", lowerCaseLabelSnapshot.AllQuotaGroups[0].Provider);

        Assert.AreEqual("minimum per-server delay", expected.GetProperty("delay_rule").GetString());
        var now = DateTimeOffset.UtcNow;
        Assert.AreEqual(TimeSpan.FromSeconds(30), CompanionStore.MinimumDelay([
            CompanionStore.ComputeDelay(true, 2, false, null, now),
            CompanionStore.ComputeDelay(true, 3, false, null, now),
        ]));
    }

    [TestMethod]
    public void SharedFixture_Pins_Model_Ranking_And_Cost_Podium()
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

        // The costliest model sits outside the token podium, so a maximum by cost
        // over top_models names the wrong one. If this ever stops holding, the
        // fixture has stopped being able to fail a client that does that.
        var costLeader = today.TopModelsByCost![0];
        var trap = today.TopModels!.OrderByDescending(m => m.Cost).First();
        Assert.AreNotEqual(costLeader.Name, trap.Name,
            "fixture no longer expresses the truncate-then-sort trap");
        Assert.AreEqual("openai/o5-deep-research", costLeader.Name);
        Assert.IsFalse(today.TopModels!.Any(m => m.Name == costLeader.Name),
            "the costliest model must sit outside the token podium");

        var snapshot = new Snapshot
        {
            Today = today,
            Month = new UsageResponse(),
            Quota = new QuotaResponse(),
            Thresholds = QuotaThresholds.Defaults,
        };
        var activity = snapshot.ActivityText!;
        StringAssert.Contains(activity, "o5-deep-research");
        Assert.IsFalse(activity.Contains("gpt-5.6-sol"),
            "a maximum by cost over top_models would have named this model");
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

        var snap = new Snapshot
        {
            Today = new UsageResponse(),
            Month = new UsageResponse(),
            Quota = quota,
            Thresholds = QuotaThresholds.Defaults,
        };
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
        var legacy = new Snapshot
        {
            Today = new UsageResponse(),
            Month = new UsageResponse(),
            Quota = quota,
            Thresholds = QuotaThresholds.Defaults,
        };
        Assert.IsTrue(legacy.AllQuotaGroups.Single().Rows
            .Single(r => r.Bucket == "weekly_scoped_opus").Failed);
    }

    private static T Decode<T>(string fixture) =>
        JsonSerializer.Deserialize<T>(File.ReadAllText(ContractFile("fixtures", fixture)), JsonOptions)!;

    private static string ContractFile(string directory, string file, [CallerFilePath] string source = "") =>
        Path.GetFullPath(Path.Combine(Path.GetDirectoryName(source)!, "..", "..", "contract", directory, file));
}
