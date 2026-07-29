using Microsoft.VisualStudio.TestTools.UnitTesting;
using TokdashCompanion;

namespace TokdashCompanion.Tests;

/// <summary>
/// Behavioral tests for flyout placement near the tray: taskbar-edge detection,
/// work-area clamping, and per-monitor DPI scaling. The math lives in the pure
/// FlyoutWindow.ComputeFlyoutPosition helper (extracted from ApplyPosition) so it
/// can be exercised without a real window or monitor.
/// </summary>
[TestClass]
public class PlacementTests
{
    // Flyout size + margin in WPF device-independent pixels.
    private const double W = 384, H = 520, Margin = 8;

    private static (double Left, double Top) Place(
        int anchorX, int anchorY,
        int fullLeft, int fullTop, int fullRight,
        int workLeft, int workTop, int workRight, int workBottom,
        double scaleX = 1, double scaleY = 1)
        => FlyoutWindow.ComputeFlyoutPosition(
            anchorX, anchorY, fullLeft, fullTop, fullRight,
            workLeft, workTop, workRight, workBottom,
            scaleX, scaleY, W, H, Margin);

    [TestMethod]
    public void BottomTaskbar_AnchorsAboveClick_OpensLeft()
    {
        // 1920x1080, 40px taskbar at the bottom -> work area bottom = 1040.
        var (left, top) = Place(1900, 1060, 0, 0, 1920, 0, 0, 1920, 1040);
        Assert.AreEqual(1900 - W - Margin, left, 0.001);   // opens to the left of the click
        Assert.AreEqual(1040 - H - Margin, top, 0.001);    // sits above the work-area bottom
    }

    [TestMethod]
    public void TopTaskbar_PushesFlyoutDown()
    {
        // 40px taskbar at the top -> work area top = 40.
        var (left, top) = Place(1900, 10, 0, 0, 1920, 0, 40, 1920, 1080);
        Assert.AreEqual(1900 - W - Margin, left, 0.001);
        Assert.AreEqual(40 + Margin, top, 0.001);          // pushed below the top taskbar
    }

    [TestMethod]
    public void LeftTaskbar_AnchorsBesideBar()
    {
        // 40px taskbar at the left -> work area left = 40.
        var (left, top) = Place(10, 1060, 0, 0, 1920, 40, 0, 1920, 1080);
        Assert.AreEqual(40 + Margin, left, 0.001);         // just right of the left bar
        Assert.AreEqual(1080 - H - Margin, top, 0.001);
    }

    [TestMethod]
    public void RightTaskbar_AnchorsBesideBar()
    {
        // 40px taskbar at the right -> work area right = 1880.
        var (left, top) = Place(1900, 1060, 0, 0, 1920, 0, 0, 1880, 1080);
        Assert.AreEqual(1880 - W - Margin, left, 0.001);
        Assert.AreEqual(1080 - H - Margin, top, 0.001);
    }

    [TestMethod]
    public void BottomTaskbar_ClampsToLeftEdge()
    {
        // Click near the left edge: the left-anchored position would go negative.
        var (left, top) = Place(10, 1060, 0, 0, 1920, 0, 0, 1920, 1040);
        Assert.AreEqual(0, left, 0.001);                   // clamped into the work area
        Assert.AreEqual(1040 - H - Margin, top, 0.001);
    }

    [TestMethod]
    public void BottomTaskbar_ClampsToRightEdge()
    {
        // Click at the far right: the left-anchored position would overflow the work area.
        var (left, top) = Place(1930, 1060, 0, 0, 1920, 0, 0, 1920, 1040);
        Assert.AreEqual(1920 - W, left, 0.001);            // clamped to workRight - width
        Assert.AreEqual(1040 - H - Margin, top, 0.001);
    }

    [TestMethod]
    public void HighDpi_ProducesSameDipPlacement_AsStandardDpi()
    {
        // 2880x1620 @ 150% (scale 1.5), 60px taskbar at the bottom. The DIP geometry is
        // identical to the 1920x1080 @100% bottom-taskbar case, so the DIP placement must
        // match exactly - placement is DPI-independent in device-independent pixels.
        var (left, top) = Place(2850, 1590, 0, 0, 2880, 0, 0, 2880, 1560, scaleX: 1.5, scaleY: 1.5);
        Assert.AreEqual(1900 - W - Margin, left, 0.001);   // 2850/1.5 = 1900 DIP
        Assert.AreEqual(1040 - H - Margin, top, 0.001);    // 1560/1.5 = 1040 DIP
    }
}
