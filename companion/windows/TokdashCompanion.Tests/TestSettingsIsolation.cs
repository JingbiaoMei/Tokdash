using System.IO;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Redirects <see cref="CompanionSettings"/> persistence into a per-run temp directory for
/// the whole assembly, before any test constructs a <see cref="CompanionStore"/>.
///
/// Without this, tests share the developer's real settings file: several suites build a
/// store (which loads it), and any path that calls Save - the update check, the
/// launch-at-login write, the store's own repair of an invalid base URL - writes it back.
/// That both contaminates the developer's install and makes assertions depend on whatever
/// happens to be on that machine.
///
/// Assembly-wide and never uninstalled on purpose: a per-class hook would leave the hole
/// open for the next test class someone adds.
/// </summary>
[TestClass]
public class TestSettingsIsolation
{
    private static string? _dir;

    [AssemblyInitialize]
    public static void RedirectSettings(TestContext _)
    {
        _dir = Path.Combine(Path.GetTempPath(), "TokdashCompanionTests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_dir);
        CompanionSettings.PathOverride = Path.Combine(_dir, "settings.json");
    }

    [AssemblyCleanup]
    public static void RemoveTempSettings()
    {
        try { if (_dir is not null) Directory.Delete(_dir, recursive: true); }
        catch { /* a leftover temp directory is not worth failing a test run over */ }
    }

    [TestMethod]
    public void Settings_Persistence_Is_Redirected_Away_From_The_Real_File()
    {
        // Guards the guard: if the redirect ever stops being installed, this fails here
        // rather than silently rewriting the developer's settings from some other suite.
        Assert.IsNotNull(CompanionSettings.PathOverride, "settings redirect is not installed");
        string real = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TokdashCompanion", "settings.json");
        Assert.AreNotEqual(real, CompanionSettings.PathOverride);

        // And a real Save round-trips through the temp path, not the production one.
        var settings = new CompanionSettings { BaseURL = "http://127.0.0.1:9999", AvailableUpdateVersion = "99.0.0" };
        settings.Save();
        Assert.IsTrue(File.Exists(CompanionSettings.PathOverride));
        Assert.AreEqual("99.0.0", CompanionSettings.Load().AvailableUpdateVersion);
    }
}
