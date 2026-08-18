using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Store (MSIX) build behavior. The packaged and portable builds ship from the same
/// source, so the Store-only branches are only reachable from the test suite through
/// <see cref="PackagedApp.OverrideForTests"/> - the test host itself is unpackaged.
///
/// What is pinned here is a certification rule, not a preference: a Store build must not
/// check GitHub for updates or offer the user a download from outside the Store. The
/// Store owns update delivery for packaged apps.
/// </summary>
[TestClass]
public class PackagedBuildTests
{
    private sealed class CountingUpdateClient : IUpdateClient
    {
        public int Calls { get; private set; }
        public Task<List<GitHubRelease>> FetchReleasesAsync(CancellationToken ct = default)
        {
            Calls++;
            return Task.FromResult(new List<GitHubRelease>
            {
                new() { TagName = "companion-v99.0.0", Draft = false, Prerelease = true },
            });
        }
    }

    private sealed class StubTokdashClient : ITokdashClient
    {
        public Task<HealthResponse> HealthAsync(CancellationToken ct = default) =>
            Task.FromResult(new HealthResponse("ok", "tokdash", "1.5.8"));
        public Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default) =>
            Task.FromResult(new UsageResponse());
        public Task<QuotaResponse> QuotaAsync(CancellationToken ct = default) =>
            Task.FromResult(new QuotaResponse());
        public void Dispose() { }
    }

    /// <summary>The override is process-wide state; leaving it set would silently change
    /// every later test's view of the world.</summary>
    [TestCleanup]
    public void ResetPackagedOverride() => PackagedApp.OverrideForTests(null);

    [TestMethod]
    public void Unpackaged_Is_The_Default_For_An_Ordinary_Process()
    {
        PackagedApp.OverrideForTests(null);
        Assert.IsFalse(PackagedApp.IsPackaged, "the test host is not an MSIX package");
    }

    [TestMethod]
    public async Task Packaged_Build_Never_Contacts_GitHub()
    {
        PackagedApp.OverrideForTests(true);
        var fake = new CountingUpdateClient();
        var store = new CompanionStore(new StubTokdashClient());
        store.SetUpdateClient(fake);
        // Every condition that would normally force a check: opted in, long overdue, and
        // an explicit manual request.
        store.Settings.AutomaticUpdateChecks = true;
        store.Settings.LastUpdateCheckAt = DateTimeOffset.Now.AddDays(-30);

        await store.CheckForUpdatesAsync(manual: false);
        await store.CheckForUpdatesAsync(manual: true);

        Assert.AreEqual(0, fake.Calls, "a Store build must not reach GitHub's releases API");
    }

    [TestMethod]
    public async Task Unpackaged_Build_Still_Checks()
    {
        PackagedApp.OverrideForTests(false);
        var fake = new CountingUpdateClient();
        var store = new CompanionStore(new StubTokdashClient());
        store.SetUpdateClient(fake);

        await store.CheckForUpdatesAsync(manual: true);

        Assert.AreEqual(1, fake.Calls, "the portable build owns its own update checking");
    }

    [TestMethod]
    public void Packaged_Build_Shows_No_Update_Badge()
    {
        var store = new CompanionStore(new StubTokdashClient());
        int[] parsed = UpdateChecker.ParseVersion(UpdateChecker.CurrentVersion)!;
        string newer = UpdateChecker.VersionString([parsed[0], parsed[1], parsed[2] + 1]);

        // A settings file carried over from a portable install can hold a pending update.
        store.Settings.AvailableUpdateVersion = newer;
        store.Settings.AvailableUpdateUrl = "https://github.com/JingbiaoMei/Tokdash/releases/tag/companion-v" + newer;
        store.Settings.SkippedUpdateVersion = null;

        PackagedApp.OverrideForTests(false);
        Assert.AreEqual(newer, store.UpdateAvailableVersion, "portable build offers it");
        Assert.IsTrue(store.ShowsUpdateBadge);

        PackagedApp.OverrideForTests(true);
        Assert.IsNull(store.UpdateAvailableVersion, "the Store build has no action to offer");
        Assert.IsFalse(store.ShowsUpdateBadge, "a badge that leads nowhere is worse than none");
    }
}
