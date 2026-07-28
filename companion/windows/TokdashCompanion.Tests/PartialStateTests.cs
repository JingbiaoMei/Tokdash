using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// A controllable ITokdashClient for partial-state tests: each endpoint can
/// return data or throw, independently.
/// </summary>
internal sealed class FakeClient : ITokdashClient
{
    public HealthResponse Health { get; set; } = new("ok", "tokdash", "1.0");
    public UsageResponse Today { get; set; } = new() { TotalTokens = 1000, TotalCost = 1.5, TotalMessages = 10 };
    public UsageResponse Month { get; set; } = new() { TotalTokens = 5000, TotalCost = 7.5, TotalMessages = 50 };
    public QuotaResponse? Quota { get; set; }
    public Exception? TodayError { get; set; }
    public Exception? MonthError { get; set; }
    public Exception? QuotaError { get; set; }

    public Task<HealthResponse> HealthAsync(CancellationToken ct = default) => Task.FromResult(Health);
    public Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default)
    {
        if (period == "today" && TodayError is { } te) return Task.FromException<UsageResponse>(te);
        if (period == "month" && MonthError is { } me) return Task.FromException<UsageResponse>(me);
        return Task.FromResult(period == "today" ? Today : Month);
    }
    public Task<QuotaResponse> QuotaAsync(CancellationToken ct = default)
        => QuotaError is { } e
            ? Task.FromException<QuotaResponse>(e)
            : Task.FromResult(Quota ?? new QuotaResponse());
    public void Dispose() { }
}

/// <summary>
/// Partial-state: one failed request no longer discards the other two. The failed
/// section is flagged; successful sections keep fresh data; last-good is retained.
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

    [TestMethod]
    public async Task FailedQuota_KeepsTodayMonth_AndMarksQuotaFailed()
    {
        var client = new FakeClient { Quota = OkQuota, QuotaError = new TokdashException(TokdashError.Offline) };
        var store = new CompanionStore(client);

        await store.RefreshAsync();

        Assert.IsNotNull(store.Snapshot);
        Assert.IsTrue(store.Snapshot!.QuotaFailed, "quota failure must be flagged");
        Assert.IsFalse(store.Snapshot.TodayFailed);
        Assert.IsFalse(store.Snapshot.MonthFailed);
        Assert.AreEqual(1000, store.Snapshot.Today.TotalTokens, "today data survives a quota failure");
        Assert.AreEqual(5000, store.Snapshot.Month.TotalTokens);
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
        client.QuotaError = new TokdashException(TokdashError.Offline);
        await store.RefreshAsync();

        Assert.IsTrue(store.Snapshot!.QuotaFailed);
        Assert.IsTrue(store.Snapshot.Quota.Enabled, "last-good quota is retained across a partial failure");
    }

    [TestMethod]
    public async Task Recovery_Clears_The_FailedFlag()
    {
        var client = new FakeClient { Quota = OkQuota, QuotaError = new TokdashException(TokdashError.Timeout) };
        var store = new CompanionStore(client);

        await store.RefreshAsync();
        Assert.IsTrue(store.Snapshot!.QuotaFailed);

        client.QuotaError = null;
        await store.RefreshAsync();
        Assert.IsFalse(store.Snapshot!.QuotaFailed, "a successful fetch clears the failed flag");
    }

    [TestMethod]
    public async Task WrongService_Backs_Off_And_Does_Not_TightLoop()
    {
        var client = new FakeClient { Health = new("ok", "something-else", "1.0") };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.AreEqual(ConnectionState.WrongService, store.ConnectionState);
        Assert.AreEqual(1, store.FailureCount, "wrong-service must increment failures (back off, not tight-loop)");
        Assert.IsFalse(store.PartialPending);
    }

    [TestMethod]
    public async Task AllSectionsFailed_Backs_Off()
    {
        var client = new FakeClient
        {
            TodayError = new TokdashException(TokdashError.Offline),
            MonthError = new TokdashException(TokdashError.Offline),
            QuotaError = new TokdashException(TokdashError.Offline),
        };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.AreEqual(1, store.FailureCount, "all-sections-failed must back off, not retry in 10min");
        Assert.IsFalse(store.PartialPending);
    }

    [TestMethod]
    public async Task PartialFailure_Schedules_Short_Retry()
    {
        var client = new FakeClient { Quota = OkQuota, QuotaError = new TokdashException(TokdashError.Timeout) };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.AreEqual(0, store.FailureCount, "partial is not a full failure");
        Assert.IsTrue(store.PartialPending, "partial should schedule a 15s short retry");
    }

    [TestMethod]
    public async Task AllSections_503_Sets_Busy_State()
    {
        var client = new FakeClient
        {
            TodayError = new TokdashException(TokdashError.Busy),
            MonthError = new TokdashException(TokdashError.Busy),
            QuotaError = new TokdashException(TokdashError.Busy),
        };
        var store = new CompanionStore(client);
        await store.RefreshAsync();

        Assert.AreEqual(ConnectionState.Busy, store.ConnectionState, "all-503 must show Busy, not Connected");
        Assert.AreEqual(1, store.FailureCount);
    }
}
