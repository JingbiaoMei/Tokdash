using Microsoft.Win32;

namespace TokdashCompanion;

/// <summary>
/// Opt-in launch at login for the unpackaged Windows app via the per-user
/// Run registry key. Reversible: disabling deletes the value.
/// </summary>
internal static class LaunchAtLogin
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "TokdashCompanion";

    public static void SetEnabled(bool enabled)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey, writable: true);
            if (key is null) return;
            if (enabled)
            {
                string? exe = Environment.ProcessPath;
                if (!string.IsNullOrEmpty(exe)) key.SetValue(ValueName, $"\"{exe}\"");
            }
            else if (key.GetValue(ValueName) is not null)
            {
                key.DeleteValue(ValueName, throwOnMissingValue: false);
            }
        }
        catch { }
    }
}
