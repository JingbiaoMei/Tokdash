using System.IO;
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

    private static T Decode<T>(string fixture) =>
        JsonSerializer.Deserialize<T>(File.ReadAllText(ContractFile("fixtures", fixture)), JsonOptions)!;

    private static string ContractFile(string directory, string file, [CallerFilePath] string source = "") =>
        Path.GetFullPath(Path.Combine(Path.GetDirectoryName(source)!, "..", "..", "contract", directory, file));
}
