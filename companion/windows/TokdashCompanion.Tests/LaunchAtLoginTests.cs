using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

[TestClass]
public class LaunchAtLoginTests
{
    [TestMethod]
    public void PortableCommand_QuotesPathAndMarksStartupLaunch()
    {
        const string executable = @"C:\Users\Howard Mei\Apps\TokdashCompanion.exe";
        Assert.AreEqual(
            "\"C:\\Users\\Howard Mei\\Apps\\TokdashCompanion.exe\" --startup",
            LaunchAtLogin.PortableCommand(executable));
    }

    [TestMethod]
    public void PortableCommandMatch_DetectsMovedOrStaleRegistration()
    {
        const string current = @"D:\Apps\TokdashCompanion.exe";
        Assert.IsTrue(LaunchAtLogin.IsCurrentPortableCommand(
            "\"D:\\Apps\\TokdashCompanion.exe\" --startup",
            current));
        Assert.IsTrue(LaunchAtLogin.IsCurrentPortableCommand(
            "\"d:\\apps\\tokdashcompanion.exe\" --startup",
            current));
        Assert.IsFalse(LaunchAtLogin.IsCurrentPortableCommand(
            "\"C:\\Old\\TokdashCompanion.exe\" --startup",
            current));
        Assert.IsFalse(LaunchAtLogin.IsCurrentPortableCommand(null, current));
    }
}
