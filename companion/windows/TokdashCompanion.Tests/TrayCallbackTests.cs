using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Pins the NOTIFYICON_VERSION_4 tray-callback layout: cursor x/y in wParam,
/// mouse message in LOWORD(lParam), icon id in HIWORD(lParam). The previous code
/// read coordinates from lParam (legacy layout) and so placed the flyout at the
/// top-left.
/// </summary>
[TestClass]
public class TrayCallbackTests
{
    private static (IntPtr wParam, IntPtr lParam) Build(int x, int y, uint mouseMsg, uint iconId = 1)
    {
        long w = ((long)(ushort)y << 16) | (ushort)x;
        long l = ((long)(ushort)iconId << 16) | (ushort)mouseMsg;
        return (new IntPtr(w), new IntPtr(l));
    }

    [DataTestMethod]
    [DataRow(100, 200, 0x0202u, 1u, DisplayName = "left-click at (100,200)")]
    [DataRow(1920, 1080, 0x0205u, 1u, DisplayName = "right-click at (1920,1080)")]
    [DataRow(-100, 500, 0x0202u, 1u, DisplayName = "left-click on a left monitor (-100,500)")]
    [DataRow(100, 200, 0x0202u, 99u, DisplayName = "icon id 99 in HIWORD is ignored")]
    public void ParseTrayCallback_Version4_Layout(int x, int y, uint mouseMsg, uint iconId)
    {
        var (wParam, lParam) = Build(x, y, mouseMsg, iconId);
        var (px, py, pmsg) = Program.ParseTrayCallback(wParam, lParam);
        Assert.AreEqual(x, px, "x should come from LOWORD(wParam)");
        Assert.AreEqual(y, py, "y should come from HIWORD(wParam)");
        Assert.AreEqual(mouseMsg, pmsg, "mouse message should come from LOWORD(lParam)");
    }

    [TestMethod]
    public void ParseTrayCallback_LeftClick_IsRecognised()
    {
        var (wParam, lParam) = Build(100, 200, 0x0202u);
        var (_, _, pmsg) = Program.ParseTrayCallback(wParam, lParam);
        Assert.IsTrue(pmsg == NotifyIcon.WM_LBUTTONUP || pmsg == NotifyIcon.NIN_SELECT,
            "WM_LBUTTONUP must map to the toggle path");
    }
}
