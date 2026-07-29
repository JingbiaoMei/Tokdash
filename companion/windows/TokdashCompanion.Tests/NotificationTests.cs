using System.Collections.Generic;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Low-quota notification behavior: threshold crossing, deduplication, and
/// suppression of missing-reset and failed-provider rows. Drives the store with a
/// FakeClient and captures the LowQuotaAlert event (mirrors the macOS evaluator).
/// </summary>
[TestClass]
public class NotificationTests
{
    private static QuotaResponse QuotaWith(string status, string? detail, double remaining, int? resetsAt = 1782910800) => new()
    {
        Enabled = true,
        Providers = new Dictionary<string, ProviderQuota>
        {
            ["codex"] = new()
            {
                Status = status, StatusDetail = detail, Estimated = false,
                Buckets = new List<BucketQuota>
                {
                    new() { Bucket = "5h", BucketLabel = "5-hour", RemainingPercent = remaining, ResetsAt = resetsAt, Account = "a" }
                },
            },
        },
    };

    private const int StatusAt = 1785030000;

    // The motivating shape: a provider with several credentials where one is broken. The
    // server reports status "ok" + status_detail "stale_token" for the whole provider (its
    // recovery suppression is ok_at > status_at, and every credential in a cycle shares
    // captured_at, so the detail can't clear while the sibling stays broken). Both buckets
    // report status "ok" - only captured_at vs status_at separates them.
    private static QuotaResponse PartiallyFailedQuota(double okRemaining, double staleRemaining, int? statusAt = StatusAt) => new()
    {
        Enabled = true,
        Providers = new Dictionary<string, ProviderQuota>
        {
            ["minimax"] = new()
            {
                Status = "ok", StatusDetail = "stale_token", StatusAt = statusAt, Estimated = false,
                Buckets = new List<BucketQuota>
                {
                    // Refreshed in the same cycle as the failure -> fresh.
                    new() { Bucket = "global_general_5h", BucketLabel = "Global 5-hour", RemainingPercent = okRemaining, ResetsAt = 1782910800, Account = "global", CapturedAt = StatusAt },
                    // Last observed before the failure -> last-known.
                    new() { Bucket = "cn_general_5h", BucketLabel = "CN 5-hour", RemainingPercent = staleRemaining, ResetsAt = 1782910800, Account = "cn", CapturedAt = StatusAt - 30000 },
                },
            },
        },
    };

    // A fully failed provider: every bucket predates the failure, so no row is eligible.
    private static QuotaResponse FullyFailedQuota(double remaining) => new()
    {
        Enabled = true,
        Providers = new Dictionary<string, ProviderQuota>
        {
            ["codex"] = new()
            {
                Status = "error", StatusDetail = "fetch_error", StatusAt = StatusAt, Estimated = false,
                Buckets = new List<BucketQuota>
                {
                    new() { Bucket = "5h", BucketLabel = "5-hour", RemainingPercent = remaining, ResetsAt = 1782910800, Account = "a", CapturedAt = StatusAt - 30000 },
                },
            },
        },
    };

    private static List<IReadOnlyList<QuotaRow>> CaptureAlerts(CompanionStore store)
    {
        var alerts = new List<IReadOnlyList<QuotaRow>>();
        store.LowQuotaAlert += rows => alerts.Add(rows);
        return alerts;
    }

    [TestMethod]
    public async Task Crossing_BelowThreshold_Notifies_Once_Then_Dedups()
    {
        var client = new FakeClient { Quota = QuotaWith("ok", null, 80) };
        var store = new CompanionStore(client);
        store.Settings.LowQuotaNotifications = true;
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();                 // 80% - above the 20% 5h threshold
        Assert.AreEqual(0, alerts.Count);

        client.Quota = QuotaWith("ok", null, 10);   // crossing below threshold
        await store.RefreshAsync();
        Assert.AreEqual(1, alerts.Count, "an above->below crossing fires once");
        Assert.AreEqual(10, alerts[0][0].Left, 0.001);

        await store.RefreshAsync();                 // still 10% - no duplicate
        Assert.AreEqual(1, alerts.Count, "no repeat at the same level (dedup)");
    }

    [TestMethod]
    public async Task AboveThreshold_Does_Not_Notify()
    {
        var client = new FakeClient { Quota = QuotaWith("ok", null, 80) };
        var store = new CompanionStore(client);
        store.Settings.LowQuotaNotifications = true;
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();
        await store.RefreshAsync();
        Assert.AreEqual(0, alerts.Count, "a window staying above threshold never notifies");
    }

    [TestMethod]
    public async Task MissingReset_Is_Suppressed()
    {
        // A low window without resets_at must never notify, even on a crossing.
        var client = new FakeClient { Quota = QuotaWith("ok", null, 80, resetsAt: null) };
        var store = new CompanionStore(client);
        store.Settings.LowQuotaNotifications = true;
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();
        client.Quota = QuotaWith("ok", null, 10, resetsAt: null);
        await store.RefreshAsync();
        Assert.AreEqual(0, alerts.Count, "buckets without resets_at are suppressed");
    }

    [TestMethod]
    public async Task FailedProvider_Is_Suppressed()
    {
        // A failed provider's low window is last-known/unreliable - don't alert on it.
        var client = new FakeClient { Quota = QuotaWith("ok", null, 80) };
        var store = new CompanionStore(client);
        store.Settings.LowQuotaNotifications = true;
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();
        client.Quota = QuotaWith("error", "unreachable", 10);  // low AND failed
        await store.RefreshAsync();
        Assert.AreEqual(0, alerts.Count, "failed-provider rows are suppressed from notifications");
    }

    [TestMethod]
    public async Task PartiallyFailedProvider_Alerts_Healthy_Row_And_Suppresses_Stale_Row()
    {
        // Group failed (header warning) must not silence a sibling window that refreshed
        // in the same cycle: only the row whose data predates the failure is suppressed.
        var client = new FakeClient { Quota = PartiallyFailedQuota(80, 80) };
        var store = new CompanionStore(client);
        store.Settings.LowQuotaNotifications = true;
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();
        var group = store.Snapshot!.AllQuotaGroups.Single();
        Assert.IsTrue(group.Failed, "a non-empty status_detail still warns on the provider header");
        Assert.IsFalse(group.Rows.Single(r => r.Bucket == "global_general_5h").Failed, "captured_at == status_at is fresh, not failed");
        Assert.IsTrue(group.Rows.Single(r => r.Bucket == "cn_general_5h").Failed, "captured_at < status_at is last-known, so it keeps the inline warning");

        client.Quota = PartiallyFailedQuota(10, 10);   // both windows cross below threshold
        await store.RefreshAsync();
        Assert.AreEqual(1, alerts.Count, "the healthy sibling still fires on a crossing");
        Assert.AreEqual(1, alerts[0].Count, "the stale row stays suppressed");
        Assert.AreEqual("global_general_5h", alerts[0][0].Bucket);
    }

    [TestMethod]
    public async Task FullyFailedProvider_Suppresses_Its_LastKnown_Rows()
    {
        // Regression guard: every bucket predates the failure, so nothing may alert on
        // stale data even though buckets[].status is "ok".
        var client = new FakeClient { Quota = FullyFailedQuota(80) };
        var store = new CompanionStore(client);
        store.Settings.LowQuotaNotifications = true;
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();
        var group = store.Snapshot!.AllQuotaGroups.Single();
        Assert.IsTrue(group.Failed);
        Assert.IsTrue(group.Rows.All(r => r.Failed), "rows older than status_at are all last-known");

        client.Quota = FullyFailedQuota(10);
        await store.RefreshAsync();
        Assert.AreEqual(0, alerts.Count, "a fully failed provider's last-known rows never alert");
    }

    [TestMethod]
    public async Task Missing_StatusAt_Falls_Back_To_The_Group()
    {
        // Older servers omit status_at; without it the freshness comparison is impossible,
        // so keep today's behavior (group failed -> every row failed) rather than
        // un-suppressing rows that may well be stale.
        var client = new FakeClient { Quota = PartiallyFailedQuota(80, 80, statusAt: null) };
        var store = new CompanionStore(client);
        store.Settings.LowQuotaNotifications = true;
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();
        Assert.IsTrue(store.Snapshot!.AllQuotaGroups.Single().Rows.All(r => r.Failed), "no status_at -> fall back to the group");

        client.Quota = PartiallyFailedQuota(10, 10, statusAt: null);
        await store.RefreshAsync();
        Assert.AreEqual(0, alerts.Count, "the fallback suppresses every row of a failed provider");
    }

    [TestMethod]
    public async Task Notifications_Disabled_ByDefault()
    {
        var client = new FakeClient { Quota = QuotaWith("ok", null, 80) };
        var store = new CompanionStore(client);   // LowQuotaNotifications defaults to false
        var alerts = CaptureAlerts(store);

        await store.RefreshAsync();
        client.Quota = QuotaWith("ok", null, 10);
        await store.RefreshAsync();
        Assert.AreEqual(0, alerts.Count, "opt-in: no alerts unless notifications are enabled");
    }

    [TestMethod]
    public void QuotaView_Is_Observable()
    {
        // A notification activation sets QuotaView.Low; it must raise PropertyChanged so an
        // already-open All view re-renders (EnsureFlyoutOpen is a no-op when already visible).
        var store = new CompanionStore(new FakeClient());
        string? changed = null;
        store.PropertyChanged += (_, e) => changed = e.PropertyName;

        store.QuotaView = QuotaView.All;
        Assert.AreEqual(nameof(CompanionStore.QuotaView), changed, "QuotaView must raise PropertyChanged");

        changed = null;
        store.QuotaView = QuotaView.All;
        Assert.IsNull(changed, "setting the same value does not re-raise");
    }
}
