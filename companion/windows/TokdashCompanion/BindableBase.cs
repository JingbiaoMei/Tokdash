using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text.Json.Serialization;

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

    [JsonPropertyName("version")]
    public int Version { get; set; } = 2;
    [JsonPropertyName("servers")]
    public List<CompanionServerSettings> Servers { get; set; } =
        [CompanionServerSettings.Create(DefaultBaseURL)];
    [JsonIgnore]
    public string BaseURL
    {
        get => Servers.FirstOrDefault(s => s.Enabled)?.BaseUrl ?? Servers.FirstOrDefault()?.BaseUrl ?? DefaultBaseURL;
        set
        {
            var first = Servers.FirstOrDefault();
            if (first is null) Servers.Add(CompanionServerSettings.Create(value));
            else first.BaseUrl = value;
        }
    }
    [JsonPropertyName("BaseURL")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LegacyBaseURL
    {
        get => null;
        set
        {
            if (!string.IsNullOrWhiteSpace(value))
                Servers = [CompanionServerSettings.Create(value)];
        }
    }
    public bool LaunchAtLogin { get; set; } = false;
    public bool LowQuotaNotifications { get; set; } = false;
    public QuotaThresholds Thresholds { get; set; } = QuotaThresholds.Defaults;
    public AppLanguage Language { get; set; } = AppLanguage.System;

    // Update checking. Every field is optional in the JSON, so a settings file written by
    // v0.1.4 (which predates all of this) decodes with the feature off and every existing
    // preference intact.

    /// <summary>Update checking is opt-in: the companion contacts no third party until asked.</summary>
    public bool AutomaticUpdateChecks { get; set; } = false;
    /// <summary>Last check ATTEMPT (success or failure) - the 24h throttle reads this.</summary>
    public DateTimeOffset? LastUpdateCheckAt { get; set; }
    /// <summary>Last version found newer than this build, and its validated release page.
    /// Persisted so the gear badge survives a relaunch between daily checks.</summary>
    public string? AvailableUpdateVersion { get; set; }
    public string? AvailableUpdateUrl { get; set; }
    /// <summary>A version the user explicitly skipped; suppresses the badge for it only.</summary>
    public string? SkippedUpdateVersion { get; set; }

    /// <summary>
    /// Test seam: when set, settings are read and written here instead of the user's real
    /// file. Null in production. The test assembly installs a temp path in
    /// [AssemblyInitialize], before any store is constructed, so a test can neither read
    /// the developer's own settings (which would make assertions depend on their machine)
    /// nor write to them.
    /// </summary>
    internal static string? PathOverride { get; set; }

    private static string SettingsPath => PathOverride ?? Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "TokdashCompanion", "settings.json");

    public static CompanionSettings Load()
    {
        try
        {
            if (File.Exists(SettingsPath))
            {
                var json = File.ReadAllText(SettingsPath);
                var settings = System.Text.Json.JsonSerializer.Deserialize<CompanionSettings>(json) ?? new();
                using var document = System.Text.Json.JsonDocument.Parse(json);
                var root = document.RootElement;
                if ((!root.TryGetProperty("servers", out var servers) || servers.ValueKind != System.Text.Json.JsonValueKind.Array || servers.GetArrayLength() == 0)
                    && root.TryGetProperty("BaseURL", out var legacy))
                {
                    settings.Servers = [CompanionServerSettings.Create(legacy.GetString() ?? DefaultBaseURL)];
                }
                settings.Version = 2;
                if (settings.Servers.Count == 0) settings.Servers.Add(CompanionServerSettings.Create(DefaultBaseURL));
                return settings;
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

public sealed class CompanionServerSettings
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = Guid.NewGuid().ToString("N");
    [JsonPropertyName("label")]
    public string Label { get; set; } = "Local";
    [JsonPropertyName("baseUrl")]
    public string BaseUrl { get; set; } = CompanionSettings.DefaultBaseURL;
    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; } = true;

    public static CompanionServerSettings Create(string baseUrl) => new()
    {
        BaseUrl = baseUrl,
        Label = DefaultLabel(baseUrl),
    };

    private static string DefaultLabel(string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri)) return "Local";
        var host = uri.Host.ToLowerInvariant();
        if (host is "localhost" or "127.0.0.1" or "::1") return "Local";
        if (host.Contains(':') || host.All(c => char.IsDigit(c) || c == '.')) return host;
        return host.Split('.')[0];
    }
}
