using System.Collections.Generic;
using System.Threading.Tasks;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Partial-state: one failed request no longer discards the others. The failed section
/// is flagged; successful sections keep fresh data; last-good is retained. v1.1 sections
/// per the fetch group: usage + quota are real sections; active-time and the glance
/// source are optional decorations (rule 6 - they fail silently).
/// </summary>
[TestClass]
public class PartialStateTests
{
    private static QuotaResponse OkQuota => new()
    {
        Enabled = true,
        Providers = new Dictionary<string, ProviderQuota>
        {
            ["codex"] = new() { Estimated = true, Buckets = new List<BucketQuota> { new() { Bucket = "5h", RemainingPercent = 80 } } },
        },
    };

    private static UsageResponse Usage(long tokens) => new() { TotalTokens = tokens, TotalCost = 1.5, TotalMessages = 10 };

    [TestMethod]
    public async Task FailedQuota_KeepsUsage_AndMarksQuotaFailed()
    {
        var client = new FakeClient { Usage = Usage(1000), Quota = "fail" };
        var store = new CompanionStore(client);

        await store.RefreshAsync();

        Assert.IsNotNull(store.Snapshot);
        Assert.IsTrue(store.Snapshot!.QuotaFailed, "quota failure must be flagged");
        Assert.IsFalse(store.Snapshot.UsageFailed);
        Assert.AreEqual(1000, store.Snapshot.Usage!.TotalTokens, "usage data survives a quota failure");
    }

    [TestMethod]
    public async Task FailedQuota_Retains_LastGood_OnNextRefresh()
    {
        var client = new FakeClient { Quota = OkQuota };
        var store = new CompanionStore(client);

        await store.RefreshAsync();
        Assert.IsFalse(store.Snapshot!.QuotaFailed);
        Assert.IsTrue(store.Snapshot.Quota.Enabled);

        // Quota now fails; last-good quota should be retained.
        client.Quota = "fail";
        await store.RefreshAsync();

        Assert.IsTrue(store.Snapshot!.QuotaFailed);
        Assert.IsTrue(store.Snapshot.Quota.Enabled, "last-good quota is retained across a partial failure");
    }

    [TestMethod]
    public async Task Recovery_Clears_The_FailedFlag()
    {
        var client = new FakeClient { Quota = "fail" };
        var store = new CompanionStore(client);

        await store.RefreshAsync();
        Assert.IsTrue(store.Snapshot!.QuotaFailed);

        client.Quota = OkQuota;
        await store.RefreshAsync();
        Assert.IsFalse(store.Snapshot!.QuotaFailed, "a successful fetch clears the failed flag");
    }

    [TestMethod]
    public async Task WrongService_Backs_Off_And_Does_Not_TightLoop()
    {
        var client = new FakeClient { Health = new HealthResponse("ok", "something-else", "1.0") };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.AreEqual(ConnectionState.WrongService, store.ConnectionState);
        Assert.AreEqual(1, store.FailureCount, "wrong-service must increment failures (back off, not tight-loop)");
        Assert.IsFalse(store.PartialPending);
    }

    [TestMethod]
    public async Task AllRealSectionsFailed_Backs_Off()
    {
        var client = new FakeClient { Usage = "fail", Quota = "fail" };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.AreEqual(1, store.FailureCount, "all-real-sections-failed must back off, not retry in 10min");
        Assert.IsFalse(store.PartialPending);
    }

    [TestMethod]
    public async Task OptionalSections_Fail_Silently()
    {
        // Rule 6: active-time and insights failing while usage+quota succeed is NOT a
        // partial failure (no warning, no short-retry state): they are decorations.
        var client = new FakeClient { Usage = Usage(1000), Quota = OkQuota, ActiveTime = "503", Insights = "503" };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.IsFalse(store.Snapshot!.UsageFailed);
        Assert.IsFalse(store.Snapshot.QuotaFailed);
        Assert.IsNull(store.Snapshot.ActiveMs, "the failed optional section contributes null");
        Assert.IsFalse(store.PartialPending, "optional failures never flip the partial state");
    }

    [TestMethod]
    public async Task PartialFailure_Schedules_Short_Retry()
    {
        var client = new FakeClient { Usage = Usage(1000), Quota = "503" };
        var store = new CompanionStore(client);
        // usage succeeds, quota 503s -> partial
        await store.RefreshAsync();

        Assert.AreEqual(0, store.FailureCount, "partial is not a full failure");
        Assert.IsTrue(store.PartialPending, "partial should schedule a 15s short retry");
    }

    [TestMethod]
    public async Task AllRealSections_503_Sets_Busy_State()
    {
        var client = new FakeClient { Usage = "503", ActiveTime = "503", Insights = "503", Quota = "503" };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.AreEqual(ConnectionState.Busy, store.ConnectionState, "all-503 must show Busy, not Connected");
        Assert.AreEqual(1, store.FailureCount);
    }
}
