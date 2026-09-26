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
    public int Version { get; set; } = 3;
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

    /// <summary>
    /// Schema v3: the six feature components (contract §Components and settings). A v2 file
    /// has no "components" key: every toggle then reads as ON, so upgrading never silently
    /// disables a shipped feature. Unknown keys are ignored in both directions.
    /// </summary>
    [JsonPropertyName("components")]
    public CompanionComponents Components { get; set; } = new();

    /// <summary>Schema v3: the persisted period-segment selection (default today). Stored on
    /// its own - it is a preference, not a component toggle.</summary>
    [JsonPropertyName("selectedPeriod")]
    public UsagePeriod SelectedPeriod { get; set; } = UsagePeriod.Today;

    /// <summary>Schema v3: rows per top-ranks list - tools and models share the count
    /// (contract §Top ranks). Clamped to 3..8 on every write; the flyout grows to fit.</summary>
    [JsonPropertyName("rankRows")]
    public int RankRows
    {
        get => rankRows;
        set => rankRows = Math.Clamp(value, 3, 8);
    }
    private int rankRows = 3;

    // Update checking. Every field is optional in the JSON, so a settings file written by
    // v0.1.4 (which predates all of this) decodes with the feature off and every existing
    // preference intact.

    /// <summary>Update checking is opt-in: the companion contacts no third party until asked.</summary>
    /// <summary>On by default; the Settings checkbox is how you opt OUT.</summary>
    public bool AutomaticUpdateChecks { get; set; } = true;
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
                // v1/v2 files migrate up: absent components = all defaults on (schema v3),
                // and the version stamp is rewritten so the file is self-describing.
                settings.Version = 3;
                settings.Components ??= new CompanionComponents();
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

    [JsonPropertyName("routes")]
    public List<string> Routes { get; set; } = [];
    [JsonPropertyName("preferredRoute")]
    public string? PreferredRoute { get; set; }
    [JsonPropertyName("instanceId")]
    public string? InstanceId { get; set; }
    [JsonIgnore]
    public List<string> Addresses => new[] { BaseUrl }.Concat(Routes)
        .Select(s => s.Trim().TrimEnd('/')).Where(CompanionStore.IsValidBaseURL)
        .Distinct(StringComparer.Ordinal).ToList();

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
