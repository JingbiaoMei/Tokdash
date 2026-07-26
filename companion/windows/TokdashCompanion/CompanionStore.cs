using System.Collections.ObjectModel;

namespace TokdashCompanion;

/// <summary>
/// Companion store / view-model. Holds connection state, the decoded snapshot,
/// and settings. The flyout binds to this. Refresh fetches health, then today,
/// month, and quota concurrently. Mirrors the macOS CompanionStore.
/// </summary>
public sealed class CompanionStore : BindableBase
{
    private readonly TokdashClient _client;
    private CancellationTokenSource? _cts;
    private DateTimeOffset? _lastFetchAt;

    public CompanionStore() : this(new TokdashClient(CompanionSettings.Load().BaseURL)) { }

    public CompanionStore(TokdashClient client)
    {
        _client = client;
        Settings = CompanionSettings.Load();
    }

    public CompanionSettings Settings { get; }

    private ConnectionState _connectionState = ConnectionState.Connecting;
    public ConnectionState ConnectionState
    {
        get => _connectionState;
        set { _connectionState = value; OnPropertyChanged(); OnPropertyChanged(nameof(ConnectionLabel)); OnPropertyChanged(nameof(DotColor)); }
    }

    public string ConnectionLabel => ConnectionState switch
    {
        ConnectionState.Connecting => "Connecting…",
        ConnectionState.Connected => "Local · Connected",
        ConnectionState.Busy => "Busy",
        ConnectionState.Offline => "Offline",
        ConnectionState.WrongService => "Not Tokdash",
        _ => "",
    };

    public string DotColor => ConnectionState switch
    {
        ConnectionState.Connecting => "#FF9F0A",
        ConnectionState.Connected => "#30A74C",
        ConnectionState.Busy => "#FF9F0A",
        ConnectionState.Offline or ConnectionState.WrongService => "#FF453A",
        _ => "#FF453A",
    };

    private Snapshot? _snapshot;
    public Snapshot? Snapshot { get => _snapshot; set { _snapshot = value; OnPropertyChanged(); } }

    public QuotaView QuotaView { get; set; } = QuotaView.Low;

    public string FreshnessText
    {
        get
        {
            if (_lastFetchAt is null) return ConnectionState == ConnectionState.Connecting ? "" : "No data yet";
            var age = DateTimeOffset.Now - _lastFetchAt.Value;
            if (age.TotalSeconds < 60) return "Updated just now";
            if (age.TotalMinutes < 60) return $"Updated {(int)age.TotalMinutes} min ago";
            if (age.TotalHours < 24) return $"Updated {(int)age.TotalHours} h ago";
            return $"Updated {(int)age.TotalDays} d ago";
        }
    }

    public bool ShowsBanner => ConnectionState == ConnectionState.Offline || ConnectionState == ConnectionState.Busy;
    public string BannerTitle => ConnectionState switch
    {
        ConnectionState.Offline => "Tokdash is not reachable",
        ConnectionState.Busy => "Tokdash is busy - retrying",
        _ => "",
    };
    public string BannerBody => ConnectionState switch
    {
        ConnectionState.Offline => "Start Tokdash, or check the server address in Settings.",
        ConnectionState.Busy => "Last data shown below. Backing off automatically.",
        _ => "",
    };

    public async Task RefreshAsync()
    {
        _cts?.Cancel();
        _cts = new CancellationTokenSource();
        var ct = _cts.Token;
        try
        {
            var health = await _client.HealthAsync(ct);
            if (health.Service != "tokdash")
            {
                ConnectionState = ConnectionState.WrongService;
                return;
            }
            ConnectionState = ConnectionState.Connected;

            var todayTask = _client.UsageAsync("today", ct);
            var monthTask = _client.UsageAsync("month", ct);
            var quotaTask = _client.QuotaAsync(ct);
            await Task.WhenAll(todayTask, monthTask, quotaTask);

            Snapshot = new Snapshot
            {
                Today = todayTask.Result,
                Month = monthTask.Result,
                Quota = quotaTask.Result,
                Thresholds = Settings.Thresholds,
            };
            _lastFetchAt = DateTimeOffset.Now;
        }
        catch (TokdashException ex)
        {
            ConnectionState = ex.Error switch
            {
                TokdashError.Busy => ConnectionState.Busy,
                TokdashError.Offline or TokdashError.Timeout => ConnectionState.Offline,
                _ => ConnectionState.Offline,
            };
        }
        catch
        {
            ConnectionState = ConnectionState.Offline;
        }
        OnPropertyChanged(nameof(FreshnessText));
        OnPropertyChanged(nameof(ShowsBanner));
    }
}

public enum ConnectionState { Connecting, Connected, Busy, Offline, WrongService }
public enum QuotaView { Low, All }

public sealed class Snapshot
{
    public required UsageResponse Today { get; set; }
    public required UsageResponse Month { get; set; }
    public required QuotaResponse Quota { get; set; }
    public required QuotaThresholds Thresholds { get; set; }

    public string TodayCostText => Formatter.FormatCost(Today.TotalCost);
    public string MonthCostText => Formatter.FormatCost(Month.TotalCost);
    public string TodayTokensCompact => Formatter.CompactTokens(Today.TotalTokens);
    public string MonthTokensCompact => Formatter.CompactTokens(Month.TotalTokens);
    public string MonthLabel => DateTimeOffset.Now.ToString("MMMM").ToUpperInvariant();

    public string? ComparisonText
    {
        get
        {
            var pct = Today.Comparison?.CostPct;
            if (pct is null) return null;
            return Formatter.ComparisonText(pct);
        }
    }

    public string? ActivityText
    {
        get
        {
            var leadTool = Today.ByTool?.OrderByDescending(kv => kv.Value.Cost).Select(kv => (KeyValuePair<string, ToolAgg>?)kv).FirstOrDefault();
            var leadModel = (Today.CombinedModels ?? Today.TopModels ?? [])
                .OrderByDescending(m => m.Cost).FirstOrDefault();
            if (leadTool is null || leadModel is null) return null;
            string modelName = leadModel.Name.Split('/').LastOrDefault() ?? leadModel.Name;
            return $"Most used today  {leadTool.Value.Key} · {modelName}";
        }
    }

    public List<QuotaRow> LowQuotaRows
    {
        get
        {
            if (!Quota.Enabled) return new();
            return AllQuotaRows
                .Where(r => r.IsLow(Thresholds))
                .OrderBy(r => r.Left)
                .Take(2)
                .ToList();
        }
    }

    public List<QuotaGroup> AllQuotaGroups
    {
        get
        {
            if (!Quota.Enabled || Quota.Providers is null) return new();
            return Quota.Providers
                .Where(kv => kv.Value.Buckets is { Count: > 0 })
                .Select(kv => new QuotaGroup(
                    Capitalize(kv.Key),
                    kv.Value.Buckets!.Select(b => new QuotaRow(
                        Capitalize(kv.Key), b.Bucket, b.BucketLabel ?? b.Bucket,
                        b.RemainingPercent ?? 100,
                        b.ResetsAt is null ? null : DateTimeOffset.FromUnixTimeSeconds(b.ResetsAt.Value),
                        kv.Value.Estimated ?? false)).ToList()))
                .ToList();
        }
    }

    private List<QuotaRow> AllQuotaRows
    {
        get
        {
            if (Quota.Providers is null) return new();
            return Quota.Providers.Values
                .Where(p => p.Buckets is not null)
                .SelectMany(p => p.Buckets!)
                .Select(b => new QuotaRow("", b.Bucket, b.BucketLabel ?? b.Bucket,
                    b.RemainingPercent ?? 100,
                    b.ResetsAt is null ? null : DateTimeOffset.FromUnixTimeSeconds(b.ResetsAt.Value),
                    false))
                .ToList();
        }
    }

    private static string Capitalize(string s) =>
        string.IsNullOrEmpty(s) ? s : char.ToUpperInvariant(s[0]) + s[1..];
}

public sealed record QuotaGroup(string Provider, List<QuotaRow> Rows);
