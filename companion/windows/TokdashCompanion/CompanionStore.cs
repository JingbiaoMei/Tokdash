using System.Collections.ObjectModel;
using System.Windows.Threading;

namespace TokdashCompanion;

/// <summary>
/// Companion store / view-model. Holds connection state, the decoded snapshot,
/// and settings. The flyout binds to this. Refresh fetches health, then today,
/// month, and quota concurrently. Mirrors the macOS CompanionStore.
/// </summary>
public sealed class CompanionStore : BindableBase
{
    private ITokdashClient _client;
    private CancellationTokenSource? _cts;
    private DateTimeOffset? _lastFetchAt;
    // Data generation time from the API (Today.timestamp), used for freshness. Falls
    // back to the local fetch time when the API omits a timestamp.
    private DateTimeOffset? _lastDataTime;

    // Last-good per section, retained across refreshes for partial-state rendering.
    private UsageResponse? _lastToday;
    private UsageResponse? _lastMonth;
    private QuotaResponse? _lastQuota;

    // Refresh scheduler: 60s while open, 10min while closed, backoff on failure,
    // 15s short retry while a section is in partial failure.
    private int _failures;
    private bool _partial;
    private bool _open;
    private DispatcherTimer? _timer;
    public Dispatcher? UIDispatcher { private get; set; }

    /// <summary>Begin the resident refresh scheduler. Called once from Program.Main.</summary>
    public void StartScheduler()
    {
        if (_timer is not null) return;
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _timer.Tick += OnTimerTick;
        _timer.Start();
    }

    /// <summary>Notify the scheduler the flyout opened/closed (changes cadence).</summary>
    public void SetOpen(bool open)
    {
        _open = open;
        Reschedule();
    }

    private async void OnTimerTick(object? sender, EventArgs e)
    {
        _timer!.Stop();
        await RefreshAsync();
        Reschedule();
    }

    private void Reschedule()
    {
        if (_timer is null) return;
        _timer.Interval = ComputeDelay(_open, _failures, _partial, _lastFetchAt, DateTimeOffset.Now);
        _timer.Start();
    }

    // Test hooks for scheduler state.
    internal int FailureCount => _failures;
    internal bool PartialPending => _partial;

    /// <summary>
    /// Pure delay computation for the refresh scheduler. Backoff 15/30/60/300s after
    /// consecutive failures; 15s short retry while a section is partially failing;
    /// otherwise 60s while open (immediately if data is stale) and 10min while closed.
    /// </summary>
    internal static TimeSpan ComputeDelay(bool open, int failures, bool partial, DateTimeOffset? lastFetch, DateTimeOffset now)
    {
        if (failures > 0)
        {
            int[] backoff = { 15, 30, 60, 300 };
            return TimeSpan.FromSeconds(backoff[Math.Min(failures - 1, backoff.Length - 1)]);
        }
        if (partial) return TimeSpan.FromSeconds(15);
        if (open)
        {
            if (lastFetch is null) return TimeSpan.Zero;
            var since = now - lastFetch.Value;
            return since >= TimeSpan.FromSeconds(60) ? TimeSpan.Zero : TimeSpan.FromSeconds(60) - since;
        }
        return TimeSpan.FromMinutes(10);
    }

    public CompanionStore() : this(new TokdashClient(CompanionSettings.Load().BaseURL)) { }

    public CompanionStore(ITokdashClient client)
    {
        _client = client;
        Settings = CompanionSettings.Load();
    }

    public CompanionSettings Settings { get; }

    /// <summary>Rebuild the HTTP client with a new base URL. Returns false (and keeps the
    /// previous client) if the URL is not an absolute http/https URL. Cancels any
    /// in-flight refresh before disposing the old client.</summary>
    public bool UpdateBaseURL(string url)
    {
        if (!Uri.TryCreate(url?.Trim(), UriKind.Absolute, out var uri) ||
            (uri.Scheme != "http" && uri.Scheme != "https"))
            return false;
        _cts?.Cancel();
        var old = _client;
        _client = new TokdashClient(uri.ToString());
        old.Dispose();
        return true;
    }

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

    /// <summary>Raised with quota windows that just crossed their threshold (opt-in).</summary>
    public event Action<IReadOnlyList<QuotaRow>>? LowQuotaAlert;
    private readonly HashSet<string> _notifiedKeys = new();
    // Previous remaining % per (provider|account|bucket|resetEpoch), for crossing detection.
    private readonly Dictionary<string, double> _prevQuotaLeft = new();

    /// <summary>
    /// Notify only on a crossing from above to at-or-below the threshold, evaluated
    /// over ALL windows (not just the displayed top two). Dedup by
    /// (provider, account, bucket, reset epoch, threshold); a new reset epoch re-arms.
    /// Buckets without a reset time are suppressed (spec §7). Not called for offline/
    /// busy (only on a successful health check) or recovery (only above->below).
    /// </summary>
    private void EvaluateLowQuotaNotifications(Snapshot snap)
    {
        if (!Settings.LowQuotaNotifications || !snap.Quota.Enabled || snap.QuotaFailed) return;

        var rows = snap.AllQuotaGroups.SelectMany(g => g.Rows).ToList();
        var fresh = new List<QuotaRow>();
        var currentKeys = new HashSet<string>();

        foreach (var r in rows)
        {
            if (r.ResetsAt is null) continue; // suppress buckets without a reset time
            long epoch = r.ResetsAt.Value.ToUnixTimeSeconds();
            string stateKey = $"{r.Provider}|{r.Account}|{r.Bucket}|{epoch}";
            currentKeys.Add(stateKey);

            double threshold = Settings.Thresholds.ThresholdFor(r.Bucket);
            bool isLow = r.Left <= threshold;
            if (isLow && _prevQuotaLeft.TryGetValue(stateKey, out double prev) && prev > threshold)
            {
                // Crossing above -> at/below: notify once per (provider, account, bucket, epoch, threshold).
                string notifyKey = $"{stateKey}|{threshold}";
                if (_notifiedKeys.Add(notifyKey)) fresh.Add(r);
            }
            _prevQuotaLeft[stateKey] = r.Left;
        }

        // Re-arm: drop state for windows no longer reported (reset epoch advanced / dropped).
        var stale = _prevQuotaLeft.Keys.Where(k => !currentKeys.Contains(k)).ToList();
        foreach (var k in stale) _prevQuotaLeft.Remove(k);

        if (fresh.Count > 0) LowQuotaAlert?.Invoke(fresh);
    }

    public string FreshnessText
    {
        get
        {
            var baseTime = _lastDataTime ?? _lastFetchAt;
            if (baseTime is null) return ConnectionState == ConnectionState.Connecting ? "" : "No data yet";
            var age = DateTimeOffset.Now - baseTime.Value;
            string text = age.TotalSeconds < 60 ? "Updated just now"
                : age.TotalMinutes < 60 ? $"Updated {(int)age.TotalMinutes} min ago"
                : age.TotalHours < 24 ? $"Updated {(int)age.TotalHours} h ago"
                : $"Updated {(int)age.TotalDays} d ago";
            // Offline/Busy show last-good data - mark it stale.
            if (ConnectionState is ConnectionState.Offline or ConnectionState.Busy) text += " · stale";
            return text;
        }
    }

    public bool ShowsBanner => ConnectionState is ConnectionState.Offline or ConnectionState.Busy or ConnectionState.WrongService;
    public string BannerTitle => ConnectionState switch
    {
        ConnectionState.Offline => "Tokdash is not reachable",
        ConnectionState.Busy => "Tokdash is busy - retrying",
        ConnectionState.WrongService => "This address is not a Tokdash service",
        _ => "",
    };
    public string BannerBody => ConnectionState switch
    {
        ConnectionState.Offline => "Start Tokdash, or check the server address in Settings.",
        ConnectionState.Busy => "Last data shown below. Backing off automatically.",
        ConnectionState.WrongService => "Check that the server address in Settings points at a Tokdash instance.",
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
                // Wrong service: back off so an open flyout doesn't tight-loop the address.
                _failures++;
                _partial = false;
                ConnectionState = ConnectionState.WrongService;
                return;
            }
            ConnectionState = ConnectionState.Connected;

            // Fetch each section independently so one failed request no longer
            // discards the other two. Last-good is retained per section; a failed
            // section keeps its previous data and the UI shows an inline warning.
            var todayTask = _client.UsageAsync("today", ct);
            var monthTask = _client.UsageAsync("month", ct);
            var quotaTask = _client.QuotaAsync(ct);

            bool todayFailed = false, monthFailed = false, quotaFailed = false;
            bool todayBusy = false, monthBusy = false, quotaBusy = false;
            try { _lastToday = await todayTask; }
            catch (TokdashException ex) { todayFailed = true; todayBusy = ex.Error == TokdashError.Busy; }
            catch { todayFailed = true; }
            try { _lastMonth = await monthTask; }
            catch (TokdashException ex) { monthFailed = true; monthBusy = ex.Error == TokdashError.Busy; }
            catch { monthFailed = true; }
            try { _lastQuota = await quotaTask; }
            catch (TokdashException ex) { quotaFailed = true; quotaBusy = ex.Error == TokdashError.Busy; }
            catch { quotaFailed = true; }

            if (ct.IsCancellationRequested) return;

            bool allFailed = todayFailed && monthFailed && quotaFailed;
            Snapshot = new Snapshot
            {
                Today = _lastToday ?? new UsageResponse(),
                Month = _lastMonth ?? new UsageResponse(),
                Quota = _lastQuota ?? new QuotaResponse(),
                Thresholds = Settings.Thresholds,
                TodayFailed = todayFailed,
                MonthFailed = monthFailed,
                QuotaFailed = quotaFailed,
            };

            if (allFailed)
            {
                // Health was ok but every data endpoint failed. If all were 503, the
                // service is busy: show the Busy banner + dimmed last-good, not Connected.
                if (todayBusy && monthBusy && quotaBusy) ConnectionState = ConnectionState.Busy;
                _failures++;
                _partial = false;
            }
            else
            {
                _lastFetchAt = DateTimeOffset.Now;
                _lastDataTime = DateTimeOffset.TryParse(Snapshot.Today.Timestamp, out var dt) ? dt : _lastFetchAt;
                _failures = 0;
                _partial = todayFailed || monthFailed || quotaFailed; // partial -> 15s short retry
                EvaluateLowQuotaNotifications(Snapshot);
            }
        }
        catch (TokdashException ex)
        {
            _failures++;
            _partial = false;
            ConnectionState = ex.Error switch
            {
                TokdashError.Busy => ConnectionState.Busy,
                TokdashError.Offline or TokdashError.Timeout => ConnectionState.Offline,
                _ => ConnectionState.Offline,
            };
        }
        catch
        {
            _failures++;
            _partial = false;
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

    // Per-section status from the latest refresh. A failed section keeps its
    // last-good data (held by the store) and the UI shows an inline warning.
    public bool TodayFailed { get; set; }
    public bool MonthFailed { get; set; }
    public bool QuotaFailed { get; set; }

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
            // Derive from AllQuotaGroups so each row keeps its provider name and the
            // provider-level Estimated flag. The old AllQuotaRows helper built rows
            // with an empty provider and Estimated=false, losing both in the Low view.
            return AllQuotaGroups
                .SelectMany(g => g.Rows)
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
                .Select(kv =>
                {
                    // A failed provider (status != "ok") still shows its last-known
                    // rows, flagged so the UI can render an inline warning per spec §7
                    // rather than a full-surface failure.
                    bool failed = !IsProviderOk(kv.Value.Status);
                    return new QuotaGroup(
                        Capitalize(kv.Key),
                        kv.Value.Buckets!.Select(b => new QuotaRow(
                            Capitalize(kv.Key), b.Bucket, b.BucketLabel ?? b.Bucket,
                            b.RemainingPercent ?? 100,
                            b.ResetsAt is null ? null : DateTimeOffset.FromUnixTimeSeconds(b.ResetsAt.Value),
                            kv.Value.Estimated ?? false,
                            b.Account ?? "",
                            b.RemainingPercent is not null,
                            failed)).ToList(),
                        failed);
                })
                .ToList();
        }
    }

    private static string Capitalize(string s) =>
        string.IsNullOrEmpty(s) ? s : char.ToUpperInvariant(s[0]) + s[1..];

    // A provider is healthy when its status is absent (older servers) or "ok"; any
    // other value means its quota couldn't be refreshed this cycle. Spec §7.
    private static bool IsProviderOk(string? status) =>
        string.IsNullOrEmpty(status) || status.Equals("ok", StringComparison.OrdinalIgnoreCase);
}

public sealed record QuotaGroup(string Provider, List<QuotaRow> Rows, bool Failed);
