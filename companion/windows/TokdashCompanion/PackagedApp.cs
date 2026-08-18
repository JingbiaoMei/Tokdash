using Windows.ApplicationModel;

namespace TokdashCompanion;

/// <summary>
/// Whether this process is running from an MSIX package (Microsoft Store build) rather
/// than the portable ZIP. The two builds ship from the same source and differ only in
/// how Windows delivers and updates them, so every behavior that depends on the delivery
/// channel reads this one flag.
///
/// Two behaviors currently branch on it:
/// <list type="bullet">
///   <item>launch at login - manifest StartupTask vs the per-user Run key
///     (see <see cref="LaunchAtLogin"/>);</item>
///   <item>update checking - suppressed entirely when packaged, because the Store owns
///     updates and pointing a Store user at a GitHub download is a certification
///     hazard (see <see cref="CompanionStore.CheckForUpdatesAsync"/>).</item>
/// </list>
/// </summary>
internal static class PackagedApp
{
    // Package.Current throws for unpackaged processes, which is the documented way to ask
    // this question. It cannot change over a process lifetime, so the answer is cached -
    // the throwing path is expensive and the update scheduler consults this on every tick.
    private static bool? _isPackaged;

    public static bool IsPackaged => _isPackaged ??= Detect();

    private static bool Detect()
    {
        try
        {
            _ = Package.Current.Id;
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>Test seam: force the packaged/unpackaged answer. Tests run unpackaged, so
    /// the Store-build branches would otherwise be unreachable from the test suite.</summary>
    internal static void OverrideForTests(bool? packaged) => _isPackaged = packaged;
}
