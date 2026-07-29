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
            using var key = Registry.CurrentUser.OpenSubKey(RunKey, writable: true);
            if (key is null) return false;
            if (enabled)
            {
                string? exe = Environment.ProcessPath;
                if (string.IsNullOrEmpty(exe)) return false;
                key.SetValue(ValueName, $"\"{exe}\"");
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
}
