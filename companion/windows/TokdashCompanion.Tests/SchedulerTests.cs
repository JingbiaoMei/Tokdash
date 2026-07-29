using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Pins the refresh-scheduler delay computation: 15s short retry while a section is
/// partially failing; backoff 15/30/60/300s after consecutive failures; 60s while open
/// (immediately if data is stale); 10min while closed.
/// </summary>
[TestClass]
public class SchedulerTests
{
    private static readonly DateTimeOffset Now = DateTimeOffset.Parse("2026-07-26T12:00:00Z");

    [TestMethod]
    public void Closed_Refreshes_Every_Ten_Minutes()
    {
        var last = Now.AddMinutes(-2);
        Assert.AreEqual(TimeSpan.FromMinutes(10), CompanionStore.ComputeDelay(false, 0, false, last, Now));
    }

    [TestMethod]
    public void Open_Stale_Data_Refreshes_Immediately()
    {
        var last = Now.AddSeconds(-120);
        Assert.AreEqual(TimeSpan.Zero, CompanionStore.ComputeDelay(true, 0, false, last, Now));
    }

    [TestMethod]
    public void Open_Fresh_Data_Waits_The_Remainder_Of_60s()
    {
        var last = Now.AddSeconds(-10);
        Assert.AreEqual(TimeSpan.FromSeconds(50), CompanionStore.ComputeDelay(true, 0, false, last, Now));
    }

    [TestMethod]
    public void Open_No_Prior_Data_Refreshes_Immediately()
    {
        Assert.AreEqual(TimeSpan.Zero, CompanionStore.ComputeDelay(true, 0, false, null, Now));
    }

    [TestMethod]
    public void Partial_Failure_Retries_In_15s_Regardless_Of_Open()
    {
        var last = Now.AddSeconds(-5);
        Assert.AreEqual(TimeSpan.FromSeconds(15), CompanionStore.ComputeDelay(true, 0, true, last, Now));
        Assert.AreEqual(TimeSpan.FromSeconds(15), CompanionStore.ComputeDelay(false, 0, true, last, Now));
    }

    [TestMethod]
    public void Failure_Backs_Off_15_30_60_300()
    {
        Assert.AreEqual(TimeSpan.FromSeconds(15), CompanionStore.ComputeDelay(true, 1, false, Now, Now));
        Assert.AreEqual(TimeSpan.FromSeconds(30), CompanionStore.ComputeDelay(true, 2, false, Now, Now));
        Assert.AreEqual(TimeSpan.FromSeconds(60), CompanionStore.ComputeDelay(true, 3, false, Now, Now));
        Assert.AreEqual(TimeSpan.FromSeconds(300), CompanionStore.ComputeDelay(true, 4, false, Now, Now));
        // caps at 300s
        Assert.AreEqual(TimeSpan.FromSeconds(300), CompanionStore.ComputeDelay(false, 9, false, Now, Now));
    }

    [TestMethod]
    public void Failure_Takes_Precedence_Over_Partial()
    {
        // A full failure backs off even if a partial flag is set.
        Assert.AreEqual(TimeSpan.FromSeconds(15), CompanionStore.ComputeDelay(true, 1, true, Now, Now));
        Assert.AreEqual(TimeSpan.FromSeconds(300), CompanionStore.ComputeDelay(false, 4, true, Now, Now));
    }
}
