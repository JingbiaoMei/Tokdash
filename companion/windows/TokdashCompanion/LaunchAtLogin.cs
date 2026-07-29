using Microsoft.Win32;
using Windows.ApplicationModel;

namespace TokdashCompanion;

/// <summary>
/// Opt-in launch at login. Packaged MSIX builds use the manifest-declared
/// StartupTask; portable builds use the per-user Run registry key.
/// </summary>
internal static class LaunchAtLogin
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "TokdashCompanion";
    private const string StartupTaskId = "TokdashCompanionStartup";

    public static async Task<bool> GetEnabledAsync()
    {
        if (IsPackaged())
        {
            try
            {
                StartupTask task = await StartupTask.GetAsync(StartupTaskId);
                return task.State == StartupTaskState.Enabled;
            }
            catch
            {
                return false;
            }
        }

        return ReconcilePortableState();
    }

    public static async Task<bool> SetEnabledAsync(bool enabled)
    {
        if (IsPackaged())
        {
            try
            {
                StartupTask task = await StartupTask.GetAsync(StartupTaskId);
                if (!enabled)
                {
                    task.Disable();
                    return false;
                }

                StartupTaskState state = await task.RequestEnableAsync();
                return state == StartupTaskState.Enabled;
            }
            catch
            {
                return false;
            }
        }

        return SetPortableEnabled(enabled);
    }

    private static bool IsPackaged()
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

    private static bool SetPortableEnabled(bool enabled)
    {
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(RunKey, writable: true);
            if (key is null) return false;
            if (enabled)
            {
                string? exe = Environment.ProcessPath;
                if (string.IsNullOrEmpty(exe)) return false;
                key.SetValue(ValueName, PortableCommand(exe));
            }
            else if (key.GetValue(ValueName) is not null)
            {
                key.DeleteValue(ValueName, throwOnMissingValue: false);
            }
            return enabled;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Read the registry rather than trusting settings.json. If the portable app was
    /// moved, remove the stale value so Windows does not keep launching a dead path.
    /// The user can then re-enable startup from the app's new location.
    /// </summary>
    private static bool ReconcilePortableState()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey, writable: true);
            string? command = key?.GetValue(ValueName) as string;
            if (command is null) return false;

            string? exe = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(exe) && IsCurrentPortableCommand(command, exe))
                return true;

            key!.DeleteValue(ValueName, throwOnMissingValue: false);
            return false;
        }
        catch
        {
            return false;
        }
    }

    internal static string PortableCommand(string executablePath) =>
        $"\"{executablePath}\" --startup";

    internal static bool IsCurrentPortableCommand(string? command, string executablePath) =>
        string.Equals(
            command?.Trim(),
            PortableCommand(executablePath),
            StringComparison.OrdinalIgnoreCase);
}
