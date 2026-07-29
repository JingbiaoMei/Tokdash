using System.Collections.Generic;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Verifies the quota Low-view keeps the provider name and the provider-level
/// Estimated flag (the bug discarded both). Mirrors the macOS snapshot test.
/// </summary>
[TestClass]
public class QuotaViewTests
{
    private static QuotaResponse MakeQuota(bool estimated, params (string Bucket, double Left)[] buckets)
    {
        var prov = new ProviderQuota { Estimated = estimated, Buckets = new List<BucketQuota>() };
        foreach (var (bucket, left) in buckets)
        {
            prov.Buckets.Add(new BucketQuota { Bucket = bucket, BucketLabel = bucket, RemainingPercent = left });
        }
        return new QuotaResponse { Enabled = true, Providers = new Dictionary<string, ProviderQuota> { ["codex"] = prov } };
    }

    private static Snapshot Snap(QuotaResponse quota) => new()
    {
        Today = new UsageResponse(),
        Month = new UsageResponse(),
        Quota = quota,
        Thresholds = QuotaThresholds.Defaults,
    };

    [TestMethod]
    public void LowView_Keeps_Provider_Name_And_Estimated_Flag()
    {
        var snap = Snap(MakeQuota(estimated: true, ("5h", 14), ("weekly", 63)));
        var low = snap.LowQuotaRows;

        Assert.AreEqual(1, low.Count);
        Assert.AreEqual("Codex", low[0].Provider);
        Assert.IsTrue(low[0].Estimated, "Low view must preserve the provider-level Estimated flag");
        Assert.AreEqual(14, low[0].Left);
    }

    [TestMethod]
    public void LowView_Takes_Two_Lowest_Sorted()
    {
        var snap = Snap(MakeQuota(estimated: false, ("5h", 8), ("weekly", 5), ("other", 2)));
        var low = snap.LowQuotaRows;

        Assert.AreEqual(2, low.Count);
        Assert.AreEqual(2, low[0].Left);
        Assert.AreEqual(5, low[1].Left);
    }

    [TestMethod]
    public void AllView_Groups_Keep_Provider_And_Estimated()
    {
        var snap = Snap(MakeQuota(estimated: true, ("5h", 14), ("weekly", 63)));
        var groups = snap.AllQuotaGroups;

        Assert.AreEqual(1, groups.Count);
        Assert.AreEqual("Codex", groups[0].Provider);
        Assert.AreEqual(2, groups[0].Rows.Count);
        Assert.IsTrue(groups[0].Rows.TrueForAll(r => r.Estimated));
    }
}
