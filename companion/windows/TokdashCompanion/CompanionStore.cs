using System.Collections.ObjectModel;
using System.Globalization;
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
        // The open/closed refresh window also decides FreshnessText's "· stale" suffix;
        // republish it or the transition never re-renders the footer.
        OnPropertyChanged(nameof(FreshnessText));
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

    public CompanionStore() : this(CreateDefaultClient()) { }

    /// <summary>
    /// True when a base URL is usable: an absolute http/https URL with a host. Every
    /// write path (settings window, UpdateBaseURL) validates with this, so the startup
    /// migration below stops being a recurring patch. Mirrors the macOS isValidBaseURL.
    /// </summary>
    public static bool IsValidBaseURL(string? url) =>
        Uri.TryCreate(url?.Trim(), UriKind.Absolute, out var uri) &&
        (uri.Scheme == "http" || uri.Scheme == "https") &&
        !string.IsNullOrEmpty(uri.Host);

    // Migrate a blank/malformed base URL saved by an earlier build so it can't crash
    // startup (new Uri throws) or point the client at nothing. Resets to the default
    // and persists the fix.
    private static ITokdashClient CreateDefaultClient()
    {
        var settings = CompanionSettings.Load();
        if (!IsValidBaseURL(settings.BaseURL))
        {
            settings.BaseURL = CompanionSettings.DefaultBaseURL;
            settings.Save();
        }
        return new TokdashClient(settings.BaseURL);
    }

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
        if (!IsValidBaseURL(url)) return false;
        _cts?.Cancel();
        var old = _client;
        _client = new TokdashClient(url.Trim());
        old.Dispose();
        // ConnectionLabel embeds the server name, so it has to re-render on a URL change.
        OnPropertyChanged(nameof(ServerName));
        OnPropertyChanged(nameof(ConnectionLabel));
        return true;
    }

    private ConnectionState _connectionState = ConnectionState.Connecting;
    public ConnectionState ConnectionState
    {
        get => _connectionState;
        set
        {
            if (!SetProperty(ref _connectionState, value)) return;
            OnPropertyChanged(nameof(ConnectionLabel));
            OnPropertyChanged(nameof(DotColor));
        }
    }

    /// <summary>
    /// Short name for the configured server, shown beside the connection state.
    /// Loopback reads "Local"; anything else uses the host's first DNS label, so a
    /// Tailscale URL like https://wsl.tail76535.ts.net/tokdash reads "wsl" rather than
    /// claiming to be local. Bare IPs are shown as-is. Mirrors the macOS serverLabel.
    /// </summary>
    public static string ServerLabel(string? url)
    {
        if (!Uri.TryCreate(url?.Trim(), UriKind.Absolute, out var uri)) return "Local";
        string host = uri.Host.ToLowerInvariant();
        if (host.Length == 0 || host == "localhost" || host == "127.0.0.1" || host == "::1") return "Local";
        // An IPv4/IPv6 literal has no name to shorten; splitting it would be misleading.
        if (host.Contains(':') || host.All(c => char.IsDigit(c) || c == '.')) return host;
        string first = host.Split('.')[0];
        return first.Length == 0 ? host : first;
    }

    public string ServerName => ServerLabel(Settings.BaseURL);

    // Only the connected state is prefixed with the server label; the failure states are
    // about reachability, not which host.
    public string ConnectionLabel => ConnectionState switch
    {
        ConnectionState.Connecting => "Connecting…",
        ConnectionState.Connected => $"{ServerName} · Connected",
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
    public Snapshot? Snapshot { get => _snapshot; set => SetProperty(ref _snapshot, value); }

    private QuotaView _quotaView = QuotaView.Low;
    // Observable so a notification-activation assignment (QuotaView.Low) re-renders an
    // already-open flyout via Store_PropertyChanged -> UpdateView, not just a closed one.
    public QuotaView QuotaView { get => _quotaView; set => SetProperty(ref _quotaView, value); }

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
            if (r.Failed) continue; // suppress rows whose own bucket failed (last-known, unreliable for alerts)
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

    /// <summary>
    /// Parse the API `timestamp`: a full ISO 8601 string with offset/Z, or a naive UTC
    /// datetime with arbitrary fractional-second digits (e.g. "2026-07-28T17:57:43.500951").
    /// Naive forms are read as UTC. Mirrors the macOS parseTimestamp. Spec §freshness.
    /// </summary>
    internal static DateTimeOffset? ParseTimestamp(string? s) =>
        DateTimeOffset.TryParse(s, CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var dt)
            ? dt
            : null;

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
            // Append "· stale" only when last-good data is older than the refresh window
            // (60s while open, 600s while closed) and the last fetch failed (offline/busy).
            double window = _open ? 60 : 600;
            if ((ConnectionState is ConnectionState.Offline or ConnectionState.Busy) && age.TotalSeconds > window) text += " · stale";
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
                // Data time: prefer the API timestamp (naive UTC -> treated as UTC), else
                // fall back to fetch time minus the cache age, else fetch time. Spec §freshness.
                if (ParseTimestamp(Snapshot.Today.Timestamp) is { } dt)
                    _lastDataTime = dt;
                else if (Snapshot.Today.ResponseCache?.AgeSeconds is double age)
                    _lastDataTime = _lastFetchAt - TimeSpan.FromSeconds(age);
                else
                    _lastDataTime = _lastFetchAt;
                _failures = 0;
                _partial = todayFailed || monthFailed || quotaFailed; // partial -> 15s short retry
                EvaluateLowQuotaNotifications(Snapshot);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // A superseding refresh canceled this one; don't count it as a failure
            // or overwrite the connection state. The newer refresh wins.
            return;
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
                    // GROUP failure drives the provider-header warning: status != "ok" OR a
                    // non-empty status_detail (e.g. stale_token, even when status is "ok").
                    // A provider with several credentials reports the detail for the whole
                    // provider, so this stays broad. Spec §7.
                    bool failed = !IsProviderOk(kv.Value.Status) || !string.IsNullOrWhiteSpace(kv.Value.StatusDetail);
                    var rows = kv.Value.Buckets!.Select(b => new QuotaRow(
                        Capitalize(kv.Key), b.Bucket, QuotaRow.DisplayLabel(b.BucketLabel ?? b.Bucket),
                        b.RemainingPercent ?? 100,
                        b.ResetsAt is null ? null : DateTimeOffset.FromUnixTimeSeconds(b.ResetsAt.Value),
                        kv.Value.Estimated ?? false,
                        b.Account ?? "",
                        b.RemainingPercent is not null,
                        IsRowFailed(b.CapturedAt, kv.Value.StatusAt, failed))).ToList();
                    if (kv.Key.Equals("antigravity", StringComparison.OrdinalIgnoreCase))
                        rows = AntigravityPools(rows);
                    return new QuotaGroup(Capitalize(kv.Key), rows, failed);
                })
                .ToList();
        }
    }

    private static string Capitalize(string s) =>
        string.IsNullOrEmpty(s) ? s : char.ToUpperInvariant(s[0]) + s[1..];

    // "ok" or absent (older servers) is healthy; any other value means that quota
    // couldn't be refreshed this cycle. Spec §7.
    /// <summary>
    /// Antigravity reports one bucket per model, which floods the list. The web dashboard
    /// collapses them into two pools and shows the worst remaining in each; the companion
    /// matches so the two surfaces agree. Falls back to the raw rows if nothing matches,
    /// so an unrecognised model can never silently vanish. Mirrors macOS antigravityPools.
    /// </summary>
    public static List<QuotaRow> AntigravityPools(List<QuotaRow> rows)
    {
        (string Key, string Label, Func<string, bool> Test)[] pools =
        [
            ("gemini", "Gemini Models", n => n.Contains("gemini")),
            ("claude", "Claude and GPT Models", n => n.Contains("claude") || n.Contains("gpt") || n.Contains("oss")),
        ];
        var pooled = new List<QuotaRow>();
        foreach (var pool in pools)
        {
            QuotaRow? worst = rows
                .Where(r => r.HasPercent && pool.Test($"{r.BucketLabel} {r.Bucket}".ToLowerInvariant()))
                .OrderBy(r => r.Left)
                .FirstOrDefault();
            if (worst is not null)
                pooled.Add(worst with { Bucket = $"pool:{pool.Key}", BucketLabel = pool.Label });
        }
        return pooled.Count == 0 ? rows : pooled;
    }

    private static bool IsProviderOk(string? status) =>
        string.IsNullOrEmpty(status) || status.Equals("ok", StringComparison.OrdinalIgnoreCase);

    // ROW failure drives the inline ⚠ and notification eligibility. buckets[].status is
    // always "ok" (the server only writes failure statuses to the filtered-out "api"
    // bucket), so freshness is the real discriminator: a row is last-known when the
    // provider's failure is NEWER than the row's data. Strict "<" makes same-cycle
    // equality count as fresh, which is what rescues a healthy credential's window when a
    // sibling credential is broken - every credential in a cycle shares captured_at.
    // Missing timestamps (older servers) fall back to the group rather than silently
    // un-suppressing. Spec §7.
    private static bool IsRowFailed(int? capturedAt, int? statusAt, bool groupFailed)
    {
        if (!groupFailed) return false;
        if (capturedAt is null || statusAt is null) return true;
        return capturedAt.Value < statusAt.Value;
    }
}

public sealed record QuotaGroup(string Provider, List<QuotaRow> Rows, bool Failed);
