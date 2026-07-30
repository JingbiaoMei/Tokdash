using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;

namespace TokdashCompanion;

public abstract class BindableBase : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;

    protected bool SetProperty<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return false;
        field = value;
        OnPropertyChanged(name);
        return true;
    }

    protected void OnPropertyChanged([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

public sealed class CompanionSettings
{
    public const string DefaultBaseURL = "http://127.0.0.1:55423";

    public string BaseURL { get; set; } = DefaultBaseURL;
    public bool LaunchAtLogin { get; set; } = false;
    public bool LowQuotaNotifications { get; set; } = false;
    public QuotaThresholds Thresholds { get; set; } = QuotaThresholds.Defaults;
    public AppLanguage Language { get; set; } = AppLanguage.System;

    private static string SettingsPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "TokdashCompanion", "settings.json");

    public static CompanionSettings Load()
    {
        try
        {
            if (File.Exists(SettingsPath))
            {
                var json = File.ReadAllText(SettingsPath);
                return System.Text.Json.JsonSerializer.Deserialize<CompanionSettings>(json) ?? new();
            }
        }
        catch { }
        return new();
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath)!);
            var json = System.Text.Json.JsonSerializer.Serialize(this);
            File.WriteAllText(SettingsPath, json);
        }
        catch { }
    }
}
