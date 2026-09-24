using System.Globalization;
using System.Windows.Threading;

namespace TokdashCompanion;

/// <summary>
/// Companion store / view-model. Holds connection state, the decoded snapshot,
/// and settings. The flyout binds to this. Refresh fetches health, then - for the
/// SELECTED period - usage, active-time, quota and the glance's single source,
/// concurrently. Mirrors the macOS CompanionStore.
/// </summary>
public sealed class CompanionStore : BindableBase
{
    private ITokdashClient _client;
    private CancellationTokenSource? _cts;
    /// <summary>Bumps every SelectPeriod; a pending delayed-skeleton callback checks it so a
    /// superseded switch can never publish after the newer one.</summary>
    private int _usageGen;
    private DateTimeOffset? _lastFetchAt;
    // Data generation time from the API (usage.timestamp), used for freshness. Falls
    // back to the local fetch time when the API omits a timestamp.
    private DateTimeOffset? _lastDataTime;

    // Last-good per section, retained across refreshes for partial-state rendering.
    // v1.1: the usage-side last-goods belong to the SELECTED period; selecting a new
    // segment drops them (rule 2 - the skeleton shows, not a stale other-period number).
    private UsageResponse? _lastUsage;
    private long? _lastActiveMs;
    private InsightsResponse? _lastInsights;
    private StatsResponse? _lastStats;
    private List<PerServerUsage> _lastPerServer = [];
    private QuotaResponse? _lastQuota;

    // Refresh scheduler: 60s while open, 10min while closed, backoff on failure,
    // 15s short retry while a section is in partial failure.
    private int _failures;
    private readonly Dictionary<string, int> _serverFailureCounts = [];
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
        // Ride the existing refresh cadence instead of adding a second timer. This ticks far
        // more often than daily, but ShouldAutoCheck's 24h throttle is what actually
        // rate-limits the request, and it returns immediately when not due. Deliberately not
        // awaited: a slow GitHub must never delay the next Tokdash refresh.
        _ = CheckForUpdatesAsync(manual: false);
        Reschedule();
    }

    private void Reschedule()
    {
        if (_timer is null) return;
        var now = DateTimeOffset.Now;
        if (_client is MultiServerTokdashClient multi)
        {
            _timer.Interval = MinimumDelay(Settings.Servers.Where(s => s.Enabled)
                .Select(s => ComputeDelay(_open, _serverFailureCounts.GetValueOrDefault(s.Id), false, _lastFetchAt, now)));
        }
        else _timer.Interval = ComputeDelay(_open, _failures, _partial, _lastFetchAt, now);
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

    internal static TimeSpan MinimumDelay(IEnumerable<TimeSpan> delays) =>
        delays.DefaultIfEmpty(TimeSpan.FromMinutes(10)).Min();

    public CompanionStore() : this(CreateDefaultClient()) { }

    // Update checking. UpdateStatus drives only the Settings status line; the gear badge
    // reads UpdateAvailableVersion (persisted) so it survives a relaunch and can't be
    // cleared by a later Checking/Failed state.
    private IUpdateClient? _updateClient;
    private CancellationTokenSource? _updateCts;
    private bool _updateCheckInFlight;
    // Supersedes an older in-flight check rather than letting both write state back.
    private int _updateCheckGeneration;

    private IUpdateClient UpdateClient => _updateClient ??= new GitHubReleasesClient();

    /// <summary>Test seam: inject a fake releases client (mirrors the ITokdashClient seam).</summary>
    internal void SetUpdateClient(IUpdateClient client) => _updateClient = client;

    private UpdateStatus _updateStatus = UpdateStatus.Idle;
    public UpdateStatus UpdateStatus
    {
        get => _updateStatus;
        private set
        {
            if (!SetProperty(ref _updateStatus, value)) return;
            OnPropertyChanged(nameof(ShowsUpdateBadge));
            OnPropertyChanged(nameof(SettingsAccessibilityName));
        }
    }

    /// <summary>
    /// The version the badge is for, or null when there's nothing to show. Derived (not
    /// stored) so the three ways it can go away - installing the update, skipping the
    /// version, a check finding us current - all fall out of one rule. Opening Settings is
    /// deliberately NOT one of them.
    /// </summary>
    public string? UpdateAvailableVersion
    {
        get
        {
            // A settings file carried over from a portable install can hold a pending
            // update from before the switch. The Store build has no action to offer for
            // it, so the badge would be a dead end.
            if (PackagedApp.IsPackaged) return null;
            string? available = Settings.AvailableUpdateVersion;
            if (string.IsNullOrEmpty(available)) return null;
            if (available == Settings.SkippedUpdateVersion) return null;
            int[]? candidate = UpdateChecker.ParseVersion(available);
            int[]? current = UpdateChecker.ParseVersion(UpdateChecker.CurrentVersion);
            if (candidate is null || current is null) return null;
            return UpdateChecker.IsNewer(candidate, current) ? available : null;
        }
    }

    /// <summary>Whether to draw the red dot on the Settings gear. Never true for checking,
    /// offline, malformed-response, or rate-limited states: those aren't news the user can
    /// act on.</summary>
    public bool ShowsUpdateBadge => UpdateAvailableVersion is not null;

    /// <summary>Accessible name for the gear, which changes when an update is pending (the
    /// dot alone carries no meaning to a screen reader).</summary>
    public string SettingsAccessibilityName =>
        ShowsUpdateBadge ? L10n.T("settings_update_available") : L10n.T("settings");

    public string LastUpdateCheckText => UpdateChecker.LastCheckedText(Settings.LastUpdateCheckAt, DateTimeOffset.Now);

    /// <summary>
    /// Run an update check.
    ///
    /// <paramref name="manual"/> (the Settings "Check now" button) bypasses the 24h
    /// throttle, shows a checking state, and reports failures. A scheduled check is
    /// throttled, silent on failure, and - because it runs on its own task off the refresh
    /// path with its own CancellationTokenSource - can never change <see cref="ConnectionState"/>.
    /// </summary>
    public async Task CheckForUpdatesAsync(bool manual)
    {
        // Store builds never check. The Store owns update delivery for packaged apps, and
        // sending a Store user to a GitHub release page to download an unpackaged build
        // both duplicates that channel and reads as distributing code from outside the
        // Store. Manual checks are gated too - the button is hidden when packaged, so
        // reaching here with manual:true would mean the UI and this rule disagreed.
        if (PackagedApp.IsPackaged) return;

        if (!manual)
        {
            if (_updateCheckInFlight) return;
            if (!UpdateChecker.ShouldAutoCheck(Settings.AutomaticUpdateChecks, Settings.LastUpdateCheckAt, DateTimeOffset.Now))
                return;
        }
        _updateCts?.Cancel();
        _updateCts = new CancellationTokenSource();
        var ct = _updateCts.Token;
        int generation = ++_updateCheckGeneration;
        _updateCheckInFlight = true;
        if (manual) UpdateStatus = UpdateStatus.Checking;

        List<GitHubRelease>? releases = null;
        Exception? failure = null;
        try { releases = await UpdateClient.FetchReleasesAsync(ct); }
        catch (Exception ex) { failure = ex; }

        // A superseded check must not write state back over its replacement's.
        if (ct.IsCancellationRequested || generation != _updateCheckGeneration) return;
        _updateCheckInFlight = false;
        if (failure is null) ApplyReleases(releases!, manual);
        else ApplyUpdateFailure(failure, manual);
    }

    internal void ApplyReleases(List<GitHubRelease> releases, bool manual)
    {
        Settings.LastUpdateCheckAt = DateTimeOffset.Now;
        var newest = UpdateChecker.NewestCompanionRelease(releases);
        int[]? current = UpdateChecker.ParseVersion(UpdateChecker.CurrentVersion);
        // A build version that doesn't parse fails CLOSED (no badge): claiming an update we
        // can't compare against would be worse than staying quiet.
        if (newest is null || current is null || !UpdateChecker.IsNewer(newest.Value.Version, current))
        {
            Settings.AvailableUpdateVersion = null;
            Settings.AvailableUpdateUrl = null;
            UpdateStatus = UpdateStatus.UpToDate;
            OnPropertyChanged(nameof(ShowsUpdateBadge));
            Settings.Save();
            return;
        }
        string version = UpdateChecker.VersionString(newest.Value.Version);
        string url = UpdateChecker.ReleaseUrl(newest.Value.Release, newest.Value.Version);
        Settings.AvailableUpdateVersion = version;
        Settings.AvailableUpdateUrl = url;
        UpdateStatus = UpdateStatus.Available(version, url);
        OnPropertyChanged(nameof(ShowsUpdateBadge));
        Settings.Save();
    }

    internal void ApplyUpdateFailure(Exception error, bool manual)
    {
        // Stamp the timestamp on failure too, so "at most once every 24 hours" holds while
        // offline: without it the scheduler would retry GitHub every refresh tick and walk
        // straight into the rate limit.
        Settings.LastUpdateCheckAt = DateTimeOffset.Now;
        Settings.Save();
        // A failed check never clears a known-available update, and a scheduled failure
        // leaves the status line exactly as it was.
        if (!manual) return;
        var kind = (error as UpdateCheckException)?.Error ?? UpdateCheckError.Other;
        UpdateStatus = UpdateStatus.Failed(UpdateChecker.FailureText(kind));
    }

    /// <summary>Persist the automatic-check opt-in. Turning it on checks immediately rather
    /// than waiting up to a day for the first tick.</summary>
    public void SetAutomaticUpdateChecks(bool enabled)
    {
        if (Settings.AutomaticUpdateChecks == enabled) return;
        Settings.AutomaticUpdateChecks = enabled;
        Settings.Save();
        if (enabled) _ = CheckForUpdatesAsync(manual: false);
    }

    /// <summary>Dismiss the badge for this version only. A later release re-arms it.</summary>
    public void SkipUpdate(string version)
    {
        Settings.SkippedUpdateVersion = version;
        Settings.Save();
        OnPropertyChanged(nameof(ShowsUpdateBadge));
        OnPropertyChanged(nameof(SettingsAccessibilityName));
    }

    /// <summary>Open the release page in the default browser. Re-validated at the point of
    /// use so a persisted URL from an older build still can't send the browser elsewhere.</summary>
    public void OpenUpdatePage()
    {
        string? raw = Settings.AvailableUpdateUrl;
        if (!UpdateChecker.IsValidReleaseUrl(raw)) return;
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName = raw,
            UseShellExecute = true,
        });
    }

    /// <summary>Re-publish a previously-found update at launch. The 24h throttle means the
    /// next check can be most of a day away, and the spec requires the badge to persist
    /// until the app is updated or the version is skipped - so it has to come back from
    /// disk, not from the next network round-trip.</summary>
    private void RestorePendingUpdate()
    {
        if (UpdateAvailableVersion is { } version && Settings.AvailableUpdateUrl is { } url)
            UpdateStatus = UpdateStatus.Available(version, url);
    }

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
        return settings.Servers.Count(s => s.Enabled) > 1
            ? new MultiServerTokdashClient(settings.Servers)
            : new TokdashClient(settings.BaseURL);
    }

    public CompanionStore(ITokdashClient client)
    {
        _client = client;
        Settings = CompanionSettings.Load();
        // Resolve the display language before the first view render so launch state is in the
        // right language. ApplyLanguage re-resolves and re-renders on a later change.
        L10n.Current = L10n.Resolve(Settings.Language);
        RestorePendingUpdate();
    }

    public CompanionSettings Settings { get; }

    /// <summary>Apply a new language setting: update the global <see cref="L10n.Current"/>,
    /// persist, and raise a property change so the flyout re-renders its localized strings.</summary>
    public void ApplyLanguage(AppLanguage setting)
    {
        L10n.Current = L10n.Resolve(setting);
        Settings.Language = setting;
        Settings.Save();
        // Any localized property change is enough: Store_PropertyChanged re-renders the flyout.
        OnPropertyChanged(nameof(ConnectionLabel));
        OnPropertyChanged(nameof(FreshnessText));
    }

    /// <summary>Rebuild the HTTP client with a new base URL. Returns false (and keeps the
    /// previous client) if the URL is not an absolute http/https URL. Cancels any
    /// in-flight refresh before disposing the old client.</summary>
    public bool UpdateBaseURL(string url)
    {
        if (!IsValidBaseURL(url)) return false;
        _cts?.Cancel();
        var old = _client;
        _serverFailureCounts.Clear();
        _client = Settings.Servers.Count(s => s.Enabled) > 1
            ? new MultiServerTokdashClient(Settings.Servers)
            : new TokdashClient(url.Trim());
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
    /// claiming to be local. Bare IPs are shown as-is. More than one enabled server reads
    /// "N servers" on its own - a multi-server header claims "Connected" for hosts that
    /// individually might not be. Mirrors the macOS serverLabel.
    /// </summary>
    public static string ServerLabel(string? url)
    {
        if (!Uri.TryCreate(url?.Trim(), UriKind.Absolute, out var uri)) return L10n.T("local");
        string host = uri.Host.ToLowerInvariant();
        if (host.Length == 0 || host == "localhost" || host == "127.0.0.1" || host == "::1") return L10n.T("local");
        // An IPv4/IPv6 literal has no name to shorten; splitting it would be misleading.
        if (host.Contains(':') || host.All(c => char.IsDigit(c) || c == '.')) return host;
        string first = host.Split('.')[0];
        return first.Length == 0 ? host : first;
    }

    public string ServerName => Settings.Servers.Count(s => s.Enabled) > 1
        ? L10n.T("servers_count", Settings.Servers.Count(s => s.Enabled))
        : ServerLabel(Settings.BaseURL);

    // Only the connected state is prefixed with the server label; the failure states are
    // about reachability, not which host. With several servers the label is the count by
    // itself ("2 servers"): the per-server rows below say who answered.
    public string ConnectionLabel => ConnectionState switch
    {
        ConnectionState.Connecting => L10n.T("connecting"),
        ConnectionState.Connected => Settings.Servers.Count(s => s.Enabled) > 1
            ? L10n.T("servers_count", Settings.Servers.Count(s => s.Enabled))
            : L10n.T("server_connected", ServerName),
        ConnectionState.Busy => L10n.T("busy"),
        ConnectionState.Offline => L10n.T("offline"),
        ConnectionState.WrongService => L10n.T("not_tokdash"),
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

    // MARK: - Period segment (E-period)

    /// <summary>
    /// Clock seam for the reset-credits rules ("the clock for 'days remaining' is the
    /// client's own; tests freeze it to the payload timestamp"). Production always null.
    /// Mirrors the CompanionSettings.PathOverride test-seam style.
    /// </summary>
    internal static DateTimeOffset? ClockOverride { get; set; }
    internal static DateTimeOffset Now => ClockOverride ?? DateTimeOffset.Now;

    /// <summary>
    /// The hero segment's selection. Views read this and change it only via
    /// <see cref="SelectPeriod"/> (which persists and refetches).
    /// </summary>
    public UsagePeriod SelectedPeriod => Settings.SelectedPeriod;

    /// <summary>
    /// Select a hero period. The choice persists, and selecting a different segment fires
    /// the whole fetch group for the new window immediately. While that is in flight the
    /// previous period's data stays on screen (anti-flash); only if the fetch is still in
    /// flight after ~150 ms do the hero/delta/rank blocks and the glance drop to their
    /// loading skeleton - the usage-side last-good is then dropped. Quota and connectivity
    /// stay exactly as they were (contract rule 2, delayed-skeleton clause).
    /// </summary>
    public void SelectPeriod(UsagePeriod period)
    {
        if (Settings.SelectedPeriod == period) return;
        Settings.SelectedPeriod = period;
        Settings.Save();
        _cts?.Cancel();
        _lastUsage = null;
        _lastActiveMs = null;
        _lastInsights = null;
        _lastStats = null;
        _lastPerServer = [];
        // Delayed skeleton: the current snapshot keeps the previous period's data while the
        // new period is in flight, so a fast fetch never visibly collapses the sections.
        // The skeleton goes up only if the fetch is STILL in flight after 150 ms.
        // Generation guard: a superseded switch must never touch the newer switch's UI.
        int gen = ++_usageGen;
        _ = Task.Delay(150).ContinueWith(_ => UIDispatcher?.Invoke(() =>
        {
            if (gen != _usageGen) return;
            if (Snapshot is not { } current) return;
            // Once the new period's result has published - success OR failure - the fetch is
            // done as far as the UI is concerned. Only a snapshot still stamped with the
            // PREVIOUS period means the fetch is truly in flight: show the skeleton now.
            if (current.Period == period) return;
            Snapshot = new Snapshot
            {
                Period = period,
                Usage = null,
                ActiveMs = null,
                Insights = null,
                Stats = null,
                Quota = current.Quota,
                Thresholds = Settings.Thresholds,
                Components = Settings.Components.Resolved(),
                RankRows = Settings.RankRows,
                Now = Now,
                UsageFailed = false,
                QuotaFailed = current.QuotaFailed,
                PerServer = [],
                ShowPerServerRows = current.ShowPerServerRows,
            };
        }), TaskScheduler.Default);
        _ = RefreshAsync();
    }

    /// <summary>Monday of the local week containing <paramref name="today"/>. Sunday rolls
    /// back to the Monday six days earlier, so the week never straddles two months.</summary>
    internal static DateOnly StartOfWeekMonday(DateOnly today) =>
        today.DayOfWeek == DayOfWeek.Sunday ? today.AddDays(-6) : today.AddDays(1 - (int)today.DayOfWeek);

    /// <summary><c>date_from</c>/<c>date_to</c> pair: local Monday .. today. Never period=week.</summary>
    internal static (string From, string To) WeekRange(DateOnly today) =>
        (StartOfWeekMonday(today).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
         today.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));

    /// <summary>The usage request path for a period - pure so tests can pin the week's
    /// date-range without a live client (contract §Period windows).</summary>
    internal static string UsageRequestPath(UsagePeriod period, DateOnly today)
    {
        if (period != UsagePeriod.Week) return $"/api/usage?period={period.Token()}";
        var (from, to) = WeekRange(today);
        return $"/api/usage?date_from={from}&date_to={to}";
    }

    private static async Task<UsageResponse> FetchUsageAsync(ITokdashClient client, UsagePeriod period, DateOnly today, CancellationToken ct)
    {
        if (period == UsagePeriod.Week)
        {
            var (from, to) = WeekRange(today);
            return await client.UsageRangeAsync(from, to, ct);
        }
        return await client.UsageAsync(period.Token(), ct);
    }

    /// <summary>Active-time is optional (rule 6): any failure/404 is null, silently.</summary>
    private static async Task<long?> ActiveTimeOptionalAsync(ITokdashClient client, UsagePeriod period, DateOnly today, CancellationToken ct)
    {
        try
        {
            ActiveTimeResponse response = period == UsagePeriod.Week
                ? await client.ActiveTimeRangeAsync(WeekRange(today).From, WeekRange(today).To, ct)
                : await client.ActiveTimeAsync(period.Token(), ct);
            return response.ActiveMs;
        }
        catch { return null; }
    }

    /// <summary>
    /// The glance's single source for the period, or null when no face will render.
    /// Component-off means no request at all (glance-off / histogram-off cycles contain
    /// no /api/insights or /api/stats request). Failure yields null silently (rule 6).
    /// </summary>
    internal enum GlanceSource { None, InsightsHourly, InsightsDaily, Stats }

    internal static GlanceSource GlanceSourceFor(UsagePeriod period, CompanionComponents components)
    {
        if (!components.ActivityGlanceOn) return GlanceSource.None;
        return period switch
        {
            UsagePeriod.Today => components.ActivityHistogramTodayWeekOn ? GlanceSource.InsightsHourly : GlanceSource.None,
            UsagePeriod.Week => components.ActivityHistogramTodayWeekOn ? GlanceSource.InsightsDaily : GlanceSource.None,
            _ => GlanceSource.Stats,
        };
    }

    private static async Task<(InsightsResponse? Insights, StatsResponse? Stats)> FetchGlanceAsync(
        ITokdashClient client, GlanceSource source, DateOnly today, CancellationToken ct)
    {
        try
        {
            switch (source)
            {
                case GlanceSource.InsightsHourly:
                    return (await client.InsightsHourlyTodayAsync(ct), null);
                case GlanceSource.InsightsDaily:
                {
                    var (from, to) = WeekRange(today);
                    return (await client.InsightsDailyAsync(from, to, ct), null);
                }
                case GlanceSource.Stats:
                    return (null, await client.StatsAsync(ct));
                default:
                    return (null, null);
            }
        }
        catch { return (null, null); }
    }

    /// <summary>Rebuild and publish the snapshot from the stored last-goods. Also returns
    /// it so callers evaluate notifications on the exact snapshot just published.</summary>
    private Snapshot RebuildSnapshot(bool usageFailed, bool quotaFailed)
    {
        var snap = new Snapshot
        {
            Period = Settings.SelectedPeriod,
            Usage = _lastUsage,
            ActiveMs = _lastActiveMs,
            Insights = _lastInsights,
            Stats = _lastStats,
            Quota = _lastQuota ?? new QuotaResponse(),
            Thresholds = Settings.Thresholds,
            Components = Settings.Components.Resolved(),
            RankRows = Settings.RankRows,
            Now = Now,
            // Same seam as Now: the week histogram's columns and the week range belong to
            // the frozen clock's week in contract tests, not the wall clock's.
            Today = DateOnly.FromDateTime(Now.DateTime),
            UsageFailed = usageFailed,
            QuotaFailed = quotaFailed,
            PerServer = _lastPerServer,
            ShowPerServerRows = ShowPerServerRows,
        };
        Snapshot = snap;
        return snap;
    }

    /// <summary>Per-server rows render only with the component on and more than one
    /// enabled server (one server would only echo the hero).</summary>
    internal bool ShowPerServerRows =>
        Settings.Components.PerServerRowsOn && Settings.Servers.Count(s => s.Enabled) > 1;

    // MARK: - Server diagnostics (Settings only, E7)

    private string? _serverRuntimeVersion;
    /// <summary>The server's <c>runtime_version</c>, read on Settings open. Null until fetched.</summary>
    public string? ServerRuntimeVersion
    {
        get => _serverRuntimeVersion;
        private set { if (SetProperty(ref _serverRuntimeVersion, value)) OnPropertyChanged(nameof(ServerUpdateBadgeText)); }
    }

    private string? _serverUpdateBadgeVersion;
    /// <summary>The version for the "Server update available: v{latest}" row; null renders nothing.</summary>
    public string? ServerUpdateBadgeVersion
    {
        get => _serverUpdateBadgeVersion;
        private set { if (SetProperty(ref _serverUpdateBadgeVersion, value)) OnPropertyChanged(nameof(ServerUpdateBadgeText)); }
    }

    public string? ServerUpdateBadgeText =>
        ServerUpdateBadgeVersion is { Length: > 0 } latest ? L10n.T("server_update_available", latest) : null;

    private CancellationTokenSource? _serverDiagCts;

    /// <summary>
    /// Settings-open probe: <c>GET /api/version</c>, then - only when the server itself has
    /// update-check enabled - <c>GET /api/update-check</c>. Failures are silent, nothing is
    /// ever POSTed, and this never runs in the flyout or on a schedule.
    /// </summary>
    public async Task FetchServerUpdateInfoAsync()
    {
        _serverDiagCts?.Cancel();
        _serverDiagCts = new CancellationTokenSource();
        var ct = _serverDiagCts.Token;
        try
        {
            var version = await _client.VersionAsync(ct);
            if (ct.IsCancellationRequested) return;
            ServerRuntimeVersion = version.RuntimeVersion;
            ServerUpdateBadgeVersion = null;
            if (version.UpdateCheckEnabled != true) return;
            var check = await _client.ServerUpdateCheckAsync(ct);
            if (ct.IsCancellationRequested) return;
            ServerUpdateBadgeVersion = ServerBadgeVersion(check);
        }
        catch { /* Silent: the badge is a courtesy, not a feature the user can act on here. */ }
    }

    /// <summary>Badge rule: <c>enabled &amp;&amp; update_available &amp;&amp; latest != null</c>.
    /// <c>enabled == false</c> means the server's owner has not given update-check consent:
    /// render nothing and never try to change that. Pure so the gate is unit-testable.</summary>
    internal static string? ServerBadgeVersion(ServerUpdateCheckResponse check) =>
        check.Enabled == true && check.UpdateAvailable == true && !string.IsNullOrEmpty(check.Latest)
            ? check.Latest
            : null;

    // MARK: - Refresh

    /// <summary>
    /// Rebuild the current snapshot from last-good data with the (possibly new)
    /// thresholds, so the Low view re-evaluates immediately without a network refresh.
    /// </summary>
    public void ApplyThresholds()
    {
        if (_lastQuota is not { Enabled: true }) return;
        var snap = RebuildSnapshot(Snapshot?.UsageFailed ?? false, Snapshot?.QuotaFailed ?? false);
        _ = snap;
    }

    /// <summary>
    /// Re-publish the snapshot with the (possibly new) component toggles so the flyout
    /// re-renders without a refetch - the settings window autosaves on toggle, mirroring
    /// macOS applyComponentsChange. A component turned ON mid-cycle shows its data at the
    /// next fetch: the toggle decides whether the source is fetched at all (rule 2), and
    /// last-good data for a component that was off simply does not exist.
    /// </summary>
    public void ApplyComponentsChange()
    {
        if (Snapshot is null) return;
        _ = RebuildSnapshot(Snapshot.UsageFailed, Snapshot.QuotaFailed);
    }

    public async Task RefreshAsync()
    {
        _cts?.Cancel();
        _cts = new CancellationTokenSource();
        var ct = _cts.Token;
        // Seam-aware: the week's date range and the daily-glance window belong to the same
        // clock that drives the credits clause and the snapshot (frozen in contract tests).
        DateOnly today = DateOnly.FromDateTime(Now.DateTime);
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

            // Fetch each section independently so one failed request no longer discards
            // the others, for the SELECTED period (contract rule 2). Active-time and the
            // glance source are optional decorations: their fetch helpers swallow every
            // failure into null and never warn (rule 6). A component whose toggle is off
            // does not fetch its source at all.
            var period = Settings.SelectedPeriod;
            // Multi-server mode has no combined glance (per-server insights would need
            // merging the server doesn't share); mirror the macOS drop for fan-out cycles.
            var glanceSource = _client is MultiServerTokdashClient
                ? GlanceSource.None
                : GlanceSourceFor(period, Settings.Components);
            var usageTask = FetchUsageAsync(_client, period, today, ct);
            var quotaTask = _client.QuotaAsync(ct);
            var activeTask = ActiveTimeOptionalAsync(_client, period, today, ct);
            var glanceTask = FetchGlanceAsync(_client, glanceSource, today, ct);

            bool usageFailed = false, usageBusy = false, quotaFailed = false, quotaBusy = false;
            try { _lastUsage = await usageTask; }
            catch (TokdashException ex) { usageFailed = true; usageBusy = ex.Error == TokdashError.Busy; }
            catch { usageFailed = true; }
            try { _lastQuota = await quotaTask; }
            catch (TokdashException ex) { quotaFailed = true; quotaBusy = ex.Error == TokdashError.Busy; }
            catch { quotaFailed = true; }
            _lastActiveMs = await activeTask;
            var glance = await glanceTask;
            _lastInsights = glance.Insights;
            _lastStats = glance.Stats;
            // Per-server rows from this cycle's fan-out (empty for a single server; this
            // cycle only, no persistence - contract §Per-server rows).
            _lastPerServer = usageFailed || _client is not MultiServerTokdashClient multi
                ? []
                : multi.LastPerServerRows.ToList();

            if (ct.IsCancellationRequested) return;

            var snap = RebuildSnapshot(usageFailed, quotaFailed);

            bool allFailed = usageFailed && quotaFailed;
            if (allFailed)
            {
                // Health was ok but both real endpoints failed. If both were 503, the
                // service is busy: show the Busy banner + dimmed last-good, not Connected.
                if (usageBusy && quotaBusy) ConnectionState = ConnectionState.Busy;
                _failures++;
                _partial = false;
            }
            else
            {
                _lastFetchAt = DateTimeOffset.Now;
                // Data time: prefer the API timestamp (naive UTC -> treated as UTC), else
                // fall back to fetch time minus the cache age, else fetch time. Spec §freshness.
                if (ParseTimestamp(Snapshot!.Usage?.Timestamp) is { } dt)
                    _lastDataTime = dt;
                else if (Snapshot.Usage?.ResponseCache?.AgeSeconds is double age)
                    _lastDataTime = _lastFetchAt - TimeSpan.FromSeconds(age);
                else
                    _lastDataTime = _lastFetchAt;
                _failures = 0;
                _partial = usageFailed || quotaFailed
                    || (_client is MultiServerTokdashClient m && m.FailedServerLabels.Count > 0); // partial -> 15s short retry
                EvaluateLowQuotaNotifications(snap);
                EvaluateResetCreditNotifications(snap);
            }
            UpdateServerFailureCounts();
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // A superseding refresh canceled this one; don't count it as a failure
            // or overwrite the connection state. The newer refresh wins.
            return;
        }
        catch (TokdashException ex)
        {
            UpdateServerFailureCounts();
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

    private void UpdateServerFailureCounts()
    {
        if (_client is not MultiServerTokdashClient multi) return;
        var failedIds = multi.FailedServerIds.ToHashSet();
        foreach (var server in Settings.Servers.Where(s => s.Enabled))
        {
            if (failedIds.Contains(server.Id))
                _serverFailureCounts[server.Id] = _serverFailureCounts.GetValueOrDefault(server.Id) + 1;
            else
                _serverFailureCounts.Remove(server.Id);
        }
        foreach (var id in _serverFailureCounts.Keys.Except(Settings.Servers.Select(s => s.Id)).ToList())
            _serverFailureCounts.Remove(id);
    }

    /// <summary>
    /// Multi-server active-time sum (contract §Active time): sum <c>active_ms</c> across
    /// reachable servers, but if <em>any</em> enabled server lacks active-time data this
    /// cycle (failed endpoint or absent data), drop the segment rather than present a
    /// known-partial sum. Pure so it is unit-testable.
    /// </summary>
    internal static long? CombinedActiveMs(IReadOnlyList<(long? ActiveMs, bool Failed)> perServer)
    {
        if (perServer.Count == 0) return null; // no servers this cycle -> nothing to show
        long total = 0;
        foreach (var entry in perServer)
        {
            if (entry.Failed) return null;
            if (entry.ActiveMs is not { } ms) return null;
            total += ms;
        }
        return total;
    }

    /// <summary>
    /// Per-server rows in settings order; unreachable servers keep their row with the
    /// "unreachable" tag, never dimmed numbers. Pure/testable.
    /// </summary>
    internal static List<PerServerUsage> PerServerRows(
        IReadOnlyList<CompanionServerSettings> servers,
        IReadOnlyList<UsageResponse?> usages,
        IReadOnlySet<string> failedIds)
    {
        var rows = new List<PerServerUsage>();
        for (int i = 0; i < servers.Count; i++)
        {
            var server = servers[i];
            bool failed = failedIds.Contains(server.Id) || i >= usages.Count || usages[i] is null;
            rows.Add(new PerServerUsage(server.Label, failed ? null : usages[i]));
        }
        return rows;
    }

    /// <summary>
    /// Antigravity reports one bucket per model, which floods the list. The web dashboard
    /// collapses them into two pools and shows the worst remaining in each; the companion
    /// matches. Pool labels use the short forms ("Gemini" / "Claude/GPT") so the narrow
    /// flyout can also show the auto-determined window ("Gemini · Weekly"); the web dashboard
    /// keeps the long forms under its own subtitle. Falls back to the raw rows if nothing
    /// matches, so an unrecognised model can never silently vanish. Mirrors macOS antigravityPools.
    /// </summary>
    public static List<QuotaRow> AntigravityPools(List<QuotaRow> rows)
    {
        (string Key, string Label, Func<string, bool> Test)[] pools =
        [
            ("gemini", "Gemini", n => n.Contains("gemini")),
            ("claude", "Claude/GPT", n => n.Contains("claude") || n.Contains("gpt") || n.Contains("oss")),
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

    // MARK: - Tool display names and logos (E3)

    /// <summary>
    /// Display names for the by_tool keys the server actually emits; anything unknown
    /// gets the id capitalized, never a blank row.
    /// </summary>
    internal static string ToolDisplayName(string tool) => tool.ToLowerInvariant() switch
    {
        "codex" => "Codex",
        "claude" => "Claude",
        "kimi" => "Kimi",
        "opencode" => "OpenCode",
        "openclaw" => "OpenClaw",
        "gemini" => "Gemini",
        _ => tool.Length == 0 ? tool : char.ToUpperInvariant(tool[0]) + tool[1..],
    };

    /// <summary>
    /// Packaged logo base name (Assets\Agents\{name}.png) for a tool id, null when no mark
    /// ships for it - a tool without a shipped logo renders text-only (never a placeholder).
    /// </summary>
    internal static string? LogoAssetName(string tool) => tool.ToLowerInvariant() switch
    {
        "codex" => "codex",
        "claude" => "claude",
        "kimi" => "kimi",
        "opencode" => "opencode",
        "gemini" => "gemini",
        "openclaw" => "openclaw",
        _ => null,
    };

    /// <summary>
    /// Packaged logo base name (Assets\Agents\{name}.png) for a quota provider id, shown on
    /// the All-view group header. The asset set mirrors the web dashboard's brand map: Z.ai
    /// ships as the Zcode badge, MiniMax its own pink mark (MiMo is a separate provider -
    /// never borrow its wordmark), opencode_go shares the OpenCode mark. Providers without
    /// a shipped mark (commandcode) render text-only.
    /// Mirrors macOS quotaLogoAssetName(for:).
    /// </summary>
    internal static string? QuotaLogoAssetName(string canonicalProvider) => canonicalProvider.ToLowerInvariant() switch
    {
        "codex" => "codex",
        "claude" => "claude",
        "kimi" => "kimi",
        "grok" => "grok",
        "zai" => "zcode",
        "minimax" => "minimax",
        "opencode" or "opencode_go" => "opencode",
        "antigravity" => "antigravity",
        _ => null,
    };

    /// <summary>"openai/gpt-5.6-sol" -> "gpt-5.6-sol". Model rows strip the provider prefix.</summary>
    internal static string StripProviderPrefix(string model)
    {
        int slash = model.LastIndexOf('/');
        return slash >= 0 ? model[(slash + 1)..] : model;
    }

    // MARK: - Activity glance faces (E9)

    /// <summary>
    /// Pure face selection so the contract tests can pin every period/component combo
    /// without a live server. All-zero faces return null (the component hides itself).
    /// </summary>
    internal static GlanceFace? GlanceFaceFor(UsagePeriod period, InsightsResponse? insights,
        StatsResponse? stats, CompanionComponents components, DateOnly today)
    {
        if (!components.ActivityGlanceOn) return null;
        switch (period)
        {
            case UsagePeriod.Today:
                return components.ActivityHistogramTodayWeekOn && insights is not null ? HourFace(insights) : null;
            case UsagePeriod.Week:
                return components.ActivityHistogramTodayWeekOn && insights?.Daily is { } daily
                    ? DayFace(daily, today) : null;
            default:
                return GridFace(stats, period == UsagePeriod.Month ? 90 : 180);
        }
    }

    /// <summary>24 hourly bars, index = hour; the server's sparse buckets map onto their hour.</summary>
    private static GlanceFace? HourFace(InsightsResponse insights)
    {
        var buckets = insights.Hourly?.Buckets;
        if (buckets is null || buckets.Count == 0) return null;
        var bars = new long[24];
        foreach (var bucket in buckets)
        {
            if (bucket.Hour is { } hour && hour >= 0 && hour <= 23) bars[hour] = bucket.Tokens ?? 0;
        }
        if (bars.All(t => t == 0)) return null;
        return new GlanceFace { Kind = GlanceKind.Hours, Bars = bars, PeakHour = insights.Hourly?.PeakHour };
    }

    /// <summary>
    /// Mon..Sun of the current local week. The daily facet is sparse (no entry = no
    /// usage), so missing days render as empty zero columns, not skipped ones.
    /// </summary>
    private static GlanceFace? DayFace(IReadOnlyList<DailyPoint> daily, DateOnly today)
    {
        var monday = StartOfWeekMonday(today);
        var tokens = new long[7];
        for (int i = 0; i < 7; i++)
        {
            string key = monday.AddDays(i).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            tokens[i] = daily.FirstOrDefault(p => p.Date == key)?.Tokens ?? 0;
        }
        if (tokens.All(t => t == 0)) return null;
        return new GlanceFace { Kind = GlanceKind.Days, DayTokens = tokens };
    }

    /// <summary>
    /// GitHub-style contribution grid: <c>windowDays</c> trailing days ending at the NEWEST
    /// date in the payload (not the wall clock - tests and offline cycles have no clock
    /// truth), column-major weeks starting Monday, out-of-window cells null.
    /// </summary>
    private static GlanceFace? GridFace(StatsResponse? stats, int windowDays)
    {
        var contributions = stats?.Contributions;
        if (contributions is null || contributions.Count == 0) return null;
        var intensityByDay = new Dictionary<string, int>();
        DateOnly? anchor = null;
        foreach (var c in contributions)
        {
            if (c.Date is not { Length: 10 } raw
                || !DateOnly.TryParseExact(raw, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var date))
                continue;
            int intensity = Math.Clamp(c.Intensity ?? 0, 0, 4);
            intensityByDay[raw] = intensityByDay.TryGetValue(raw, out int old) ? old + intensity : intensity;
            if (anchor is null || date > anchor.Value) anchor = date;
        }
        if (anchor is null) return null;
        var windowStart = anchor.Value.AddDays(-(windowDays - 1));
        var gridStart = StartOfWeekMonday(windowStart);
        var columns = new List<int?[]>();
        for (int week = 0; week <= 53; week++)
        {
            var columnStart = gridStart.AddDays(week * 7);
            var column = new int?[7];
            bool anyCell = false;
            for (int dow = 0; dow < 7; dow++)
            {
                var cell = columnStart.AddDays(dow);
                if (cell < windowStart || cell > anchor.Value) continue;
                anyCell = true;
                column[dow] = intensityByDay.TryGetValue(
                    cell.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture), out int i) ? i : 0;
            }
            if (!anyCell) break; // past the anchor: remaining columns are all null
            columns.Add(column);
        }
        int filled = columns.SelectMany(c => c).Count(i => i is > 0);
        if (filled == 0) return null;
        return new GlanceFace
        {
            Kind = GlanceKind.Grid,
            GridColumns = columns.ToArray(),
            FilledCells = filled,
            WindowDays = windowDays,
            FirstColumnWeekday = "Mon",
        };
    }

    // MARK: - Low-quota notifications

    /// <summary>Raised with quota windows that just crossed their threshold (opt-in).</summary>
    public event Action<IReadOnlyList<QuotaRow>>? LowQuotaAlert;
    // Reset-credit expiry alerts (same opt-in, same scheduled read - no extra polling).
    // Internal: CreditAlertItem is an implementation type of this assembly's tray host.
    internal event Action<IReadOnlyList<CreditAlertItem>>? CreditExpiryAlert;
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
            string canonicalProvider = r.Provider.Split(" · ").Last();
            string stateKey = $"{canonicalProvider}|{r.Account}|{r.Bucket}|{epoch}";
            currentKeys.Add(stateKey);

            double threshold = Settings.Thresholds.ThresholdFor(r.CanonicalBucket);
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

    /// <summary>One credit whose last 48 hours has begun (or will, given the frozen clock).</summary>
    internal sealed record CreditAlertItem(string Provider, int Count, string Clause, DateTimeOffset ExpiresAt);

    /// <summary>
    /// Reset credits ride the SAME low-quota opt-in and the same scheduled quota read
    /// (no extra polling, rule 8). Notify once a future credit enters its last 48
    /// hours; dedup by (provider, credit id, expires_at) - a credit carries its own
    /// identity, so no re-arm rule is needed. Suppressed while the Codex provider group
    /// failed: last-known credit data is not a basis for an "expire in" warning.
    /// Requires the <c>resetCredits</c> component (it gates the row AND the notification).
    /// </summary>
    private void EvaluateResetCreditNotifications(Snapshot snap)
    {
        if (!Settings.LowQuotaNotifications || !snap.Components.ResetCreditsOn || !snap.Quota.Enabled) return;
        var fresh = new List<CreditAlertItem>();
        foreach (var group in snap.AllQuotaGroups)
        {
            if (group.Failed) continue;
            // Any provider may carry credits (codex always has; claude since server
            // v2.6.3 limit resets). The dedup key is provider-scoped, so no cross-talk.
            var credits = group.Entry?.ResetCredits;
            if (credits is null || (credits.AvailableCount ?? 0) < 1) continue;
            foreach (var credit in credits.Credits ?? [])
            {
                if (credit.ExpiresAt is not { } raw || ParseTimestamp(raw) is not { } expiry) continue;
                var remaining = expiry - snap.Now;
                if (remaining <= TimeSpan.Zero || remaining > TimeSpan.FromHours(48)) continue;
                string key = $"credit|{group.CanonicalProvider.ToLowerInvariant()}|{credit.Id ?? ""}|{expiry.ToUnixTimeSeconds()}";
                if (_notifiedKeys.Add(key))
                    fresh.Add(new CreditAlertItem(group.Provider, credits.AvailableCount!.Value,
                        CreditsClause(expiry, snap.Now), expiry));
            }
        }
        if (fresh.Count > 0) CreditExpiryAlert?.Invoke(fresh);
    }

    /// <summary>
    /// "Days remaining" clause from the soonest FUTURE expiry, floored to whole days:
    /// &gt;= 2 d -> "in {d} d"; 1..2 d -> "tomorrow"; &lt; 1 d -> "today". Expired entries
    /// are ignored by the caller for both the row clause and the notification.
    /// </summary>
    internal static string CreditsClause(DateTimeOffset expiry, DateTimeOffset now)
    {
        var remaining = expiry - now;
        long days = (long)(remaining.TotalSeconds / 86_400);
        if (days >= 2) return L10n.T("credits_in_days", days);
        if (remaining >= TimeSpan.FromDays(1)) return L10n.T("credits_tomorrow");
        return L10n.T("credits_today");
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
            if (baseTime is null) return ConnectionState == ConnectionState.Connecting ? "" : L10n.T("no_data_yet");
            var age = Now - baseTime.Value;
            string text = age.TotalSeconds < 60 ? L10n.T("updated_just_now")
                : age.TotalMinutes < 60 ? L10n.T("updated_min_ago", (int)age.TotalMinutes)
                : age.TotalHours < 24 ? L10n.T("updated_h_ago", (int)age.TotalHours)
                : L10n.T("updated_d_ago", (int)age.TotalDays);
            // Append "· stale" only when last-good data is older than the refresh window
            // (60s while open, 600s while closed) and the last fetch failed (offline/busy).
            double window = _open ? 60 : 600;
            if ((ConnectionState is ConnectionState.Offline or ConnectionState.Busy) && age.TotalSeconds > window) text += L10n.T("stale_suffix");
            if (_client is MultiServerTokdashClient multi && multi.FailedServerLabels.Count > 0)
                text += " · " + L10n.T("servers_unavailable", string.Join(", ", multi.FailedServerLabels));
            return text;
        }
    }

    public bool ShowsBanner => ConnectionState is ConnectionState.Offline or ConnectionState.Busy or ConnectionState.WrongService;
    public string BannerTitle => ConnectionState switch
    {
        ConnectionState.Offline => L10n.T("banner_offline_title"),
        ConnectionState.Busy => L10n.T("banner_busy_title"),
        ConnectionState.WrongService => L10n.T("banner_wrong_title"),
        _ => "",
    };
    public string BannerBody => ConnectionState switch
    {
        ConnectionState.Offline => L10n.T("banner_offline_body"),
        ConnectionState.Busy => L10n.T("banner_busy_body"),
        ConnectionState.WrongService => L10n.T("banner_wrong_body"),
        _ => "",
    };
}

public enum ConnectionState { Connecting, Connected, Busy, Offline, WrongService }
public enum QuotaView { Low, All }

/// <summary>
/// One server's contribution to this cycle's hero, kept for the per-server rows.
/// <c>Usage == null</c> marks an unreachable server (never dimmed numbers).
/// </summary>
public sealed record PerServerUsage(string Label, UsageResponse? Usage)
{
    public bool Reachable => Usage is not null;
    public string CostText => Formatter.FormatCost(Usage?.TotalCost ?? 0);
    public string TokensCompact => Formatter.CompactTokens(Usage?.TotalTokens ?? 0);
    /// <summary>"$3.42 · 18.7M", or the "unreachable" tag - never a guessed number.</summary>
    public string ValueText => Reachable ? $"{CostText} · {TokensCompact}" : L10n.T("server_unreachable_short");
}

public enum GlanceKind { Hours, Days, Grid }

/// <summary>
/// One face of the Activity glance (contract §Activity glance): 24 hour bars, a 7-day
/// histogram, or a Mon..Sun contribution grid. All-zero faces never exist (the factory
/// returns null instead - the component hides itself).
/// </summary>
public sealed class GlanceFace
{
    public required GlanceKind Kind { get; init; }
    public long[]? Bars { get; init; }              // Hours: 24 entries, hour 0 first
    public int? PeakHour { get; init; }             // Hours only; null renders no caption
    public long[]? DayTokens { get; init; }         // Days: exactly 7 columns, Mon..Sun
    public int?[][]? GridColumns { get; init; }     // Grid: week columns of 7 (Mon..Sun); null = outside window
    public int FilledCells { get; init; }
    public int WindowDays { get; init; }
    public string FirstColumnWeekday { get; init; } = "Mon";
}

/// <summary>
/// The observable outcome of the selected period: the hero (cost, sub-line with the active
/// segment, delta row), the top-ranks strip, the glance face, quota rows, and the
/// this-cycle per-server rows. Pure view data - built once per refresh / period change.
/// </summary>
public sealed class Snapshot
{
    public required UsagePeriod Period { get; init; }
    /// <summary>Null until the selected period has landed once (or failed): the usage-side
    /// sections show their loading skeleton meanwhile.</summary>
    public UsageResponse? Usage { get; init; }
    public long? ActiveMs { get; init; }
    public InsightsResponse? Insights { get; init; }
    public StatsResponse? Stats { get; init; }
    public required QuotaResponse Quota { get; init; }
    public required QuotaThresholds Thresholds { get; init; }
    public required CompanionComponents Components { get; init; }
    /// <summary>Clock this snapshot was built with (test-freezable). Drives the credits clause.</summary>
    public required DateTimeOffset Now { get; init; }
    /// <summary>Local day the glance windows are computed for (week histogram columns).</summary>
    public DateOnly Today { get; init; } = DateOnly.FromDateTime(DateTime.Now);

    // Per-section status from the latest refresh. A failed section keeps its
    // last-good data (held by the store) and the UI shows an inline warning.
    public bool UsageFailed { get; init; }
    public bool QuotaFailed { get; init; }

    // Per-server hero values from this cycle's fan-out (empty for a single server;
    // this cycle only, no persistence - contract §Per-server rows).
    public List<PerServerUsage> PerServer { get; init; } = [];
    public bool ShowPerServerRows { get; init; }

    /// <summary>True while the selected period's data has never landed and no failure has
    /// been reported: the hero/delta/rank blocks and glance show their loading skeleton.</summary>
    public bool UsageLoading => Usage is null && !UsageFailed;
    public bool IsEmptyUsage => Usage?.TotalTokens == 0;

    /// <summary>Hero kicker above the cost number: TODAY / THIS WEEK / THIS MONTH / THIS YEAR.</summary>
    public string KickerText => L10n.T(Period.KickerKey());

    /// <summary>Hero title for the empty / failed states (the hero number is replaced).</summary>
    public string HeroTitle => UsageFailed
        ? L10n.T(Period.UnavailableKey())
        : L10n.T("no_usage_period", L10n.T(Period.SuffixKey()));
    /// <summary>Sub-line under the empty / failed hero title.</summary>
    public string HeroEmptySub => UsageFailed ? L10n.T("will_retry_shortly") : L10n.T("tokdash_running");

    public string CostText => Formatter.FormatCost(Usage?.TotalCost ?? 0);
    public string TokensCompact => Formatter.CompactTokens(Usage?.TotalTokens ?? 0);

    /// <summary>"active 3 h 12 m" - null (absent, not "active 0 m") when zero or missing.</summary>
    public string? ActiveSegmentText => ActiveMs is > 0 ? Formatter.ActiveText(ActiveMs.Value) : null;

    /// <summary>Hero secondary line: "18.7M tokens · 248 messages · active 3 h 12 m".</summary>
    public string SubLine
    {
        get
        {
            string suffix = UsageFailed ? L10n.T("today_retrying_suffix")
                : ActiveSegmentText is { } active ? " · " + active : "";
            return L10n.T("today_tokens_messages", TokensCompact, Usage?.TotalMessages ?? 0, suffix);
        }
    }

    /// <summary>"vs yesterday" / "vs last week" / ... - the delta row's sentence.</summary>
    public string DeltaSentence => L10n.T(Period.VsKey());

    /// <summary>One metric of the delta line; Direction colors the span (-1 down / 0 flat / +1 up).</summary>
    public sealed record DeltaPiece(string Text, int Direction);

    /// <summary>
    /// <c>{glyph} {pct}% cost · {glyph} {pct}% tokens {sentence}</c>. The msgs comparison is
    /// deliberately NOT part of the row: it pushed the line past the flyout width on
    /// week/month (the contract's two-metric row). A null metric is omitted; both null hide
    /// the row entirely (healthy-year).
    /// </summary>
    public List<DeltaPiece>? DeltaPieces
    {
        get
        {
            if (!Components.FullDeltaRowOn || Usage?.Comparison is not { } comparison) return null;
            var pieces = new List<DeltaPiece>();
            AddDeltaPiece(pieces, "delta_cost", comparison.CostPct);
            AddDeltaPiece(pieces, "delta_tokens", comparison.TokensPct);
            return pieces.Count == 0 ? null : pieces;
        }
    }

    private static void AddDeltaPiece(List<DeltaPiece> pieces, string key, double? pct)
    {
        if (pct is not { } value) return;
        pieces.Add(new DeltaPiece(
            L10n.T(key, Formatter.DeltaGlyph(value), Formatter.DeltaValue(value)),
            value > 0 ? 1 : (value < 0 ? -1 : 0)));
    }

    /// <summary>Flat text of the delta line (what the contract tests pin).</summary>
    public string? DeltaRowText =>
        DeltaPieces is { } pieces ? string.Join(" · ", pieces.Select(p => p.Text)) + " " + DeltaSentence : null;

    /// <summary>
    /// The shipped single comparison line, cost-only and worded ("12% below yesterday") -
    /// what the hero shows with <c>fullDeltaRow</c> off (1.0.2 behavior).
    /// </summary>
    public string? ComparisonLine =>
        !Components.FullDeltaRowOn && Usage?.Comparison?.CostPct is { } pct
            ? Formatter.ComparisonText(pct, Period)
            : null;

    /// <summary>Color signal for the cost-only line: -1 below / +1 above (null with no line).</summary>
    public int? ComparisonDirection =>
        ComparisonLine is not null && Usage?.Comparison?.CostPct is { } pct ? (pct < 0 ? -1 : (pct > 0 ? 1 : 0)) : null;

    // MARK: Top ranks (E3)

    /// <summary>Rows per ranks list (settings.rankRows, clamped 3..8, default 3). Shared by
    /// tools and models; the pinned contract fixtures all run at the default 3.</summary>
    public int RankRows { get; init; } = 3;

    /// <summary>
    /// Fraction is the entry's share (0..1) of ALL tokens in its own list, and PctText is the
    /// same number as a rounded percent string - bar and label always agree. Rendered as the
    /// token amount + a percentage bar (E3).
    /// </summary>
    public sealed record RankEntry(string Id, string Label, string ValueText, string? LogoAsset, double Fraction, string PctText);

    /// <summary>Share helper: zero-sum lists render an empty bar and "0%", never NaN.</summary>
    private static (double Fraction, string PctText) ShareOf(long tokens, long total)
    {
        double frac = total > 0 ? (double)tokens / total : 0;
        return (frac, $"{Math.Round(frac * 100, MidpointRounding.AwayFromZero):0}%");
    }

    /// <summary>
    /// by_tool sorted by tokens descending, top RankRows. Labels are display names, values
    /// compact tokens; a tool id with no shipped mark gets NO logo (never a placeholder). The
    /// percentage denominator is the FULL by_tool sum, so the shares need not add to 100.
    /// </summary>
    public List<RankEntry> TopTools
    {
        get
        {
            if (!Components.TopRanksOn || Usage is null) return [];
            var all = Usage.ByTool ?? [];
            long total = all.Sum(kv => kv.Value.Tokens);
            return all
                .OrderByDescending(kv => kv.Value.Tokens)
                .ThenBy(kv => kv.Key, StringComparer.Ordinal)
                .Take(RankRows)
                .Select(kv =>
                {
                    var (frac, pct) = ShareOf(kv.Value.Tokens, total);
                    return new RankEntry(kv.Key, CompanionStore.ToolDisplayName(kv.Key),
                        Formatter.CompactTokens(kv.Value.Tokens), CompanionStore.LogoAssetName(kv.Key), frac, pct);
                })
                .ToList();
        }
    }

    /// <summary>
    /// First RankRows of combined_models (tokens-ranked) - never a cost sort - with the
    /// provider prefix stripped and no logos on model rows. Percentage denominator is the
    /// full combined_models sum.
    /// </summary>
    public List<RankEntry> TopModels
    {
        get
        {
            if (!Components.TopRanksOn || Usage is null) return [];
            var all = Usage.CombinedModels ?? Usage.TopModels ?? [];
            long total = all.Sum(m => m.Tokens);
            return all
                .Take(RankRows)
                .Select(m =>
                {
                    var (frac, pct) = ShareOf(m.Tokens, total);
                    return new RankEntry(m.Name, CompanionStore.StripProviderPrefix(m.Name),
                        Formatter.CompactTokens(m.Tokens), null, frac, pct);
                })
                .ToList();
        }
    }

    public bool HasTopRanks => Components.TopRanksOn && Usage is { TotalTokens: > 0 };
    public string ToolsKickerText => L10n.T("top_tools_kicker", L10n.T(Period.SuffixKey()));
    public string ModelsKickerText => L10n.T("top_models_kicker", L10n.T(Period.SuffixKey()));

    // MARK: Activity glance (E9)

    public GlanceFace? Glance =>
        CompanionStore.GlanceFaceFor(Period, Insights, Stats, Components, Today);

    // MARK: Per-server rows (E10)

    public List<PerServerUsage> PerServerRowsView => ShowPerServerRows ? PerServer : [];
    public string PerServerKickerText => L10n.T("per_server_kicker", L10n.T(Period.SuffixKey()));
    public static string PerServerFootnoteText => L10n.T("per_server_footnote");

    // MARK: Reset credits (E2)

    /// <summary>
    /// The quiet row under the provider group in the All view: rendered when the component
    /// is on, quota tracking is enabled, the group's provider carries credits, and
    /// available_count >= 1. Codex has shipped these since 1.1; Claude Code's limit resets
    /// (server v2.6.3) ride the same field and get the same row - nothing here is codex-specific.
    /// The Low view never shows it (provider context, not a window). A failed group does
    /// NOT hide the row - contract §Reset credits gates rendering on those conditions
    /// only; last-known-data suppression is scoped to the expiry *notification* (see the
    /// notification path), which is where the "expire in" warning actually fires.
    /// </summary>
    public string? CreditsNotice(QuotaGroup group)
    {
        if (!Components.ResetCreditsOn || !Quota.Enabled) return null;
        var credits = group.Entry?.ResetCredits;
        if (credits is null || (credits.AvailableCount ?? 0) < 1) return null;
        // The credits row lives INSIDE its (sectioned) provider group, so the pinned
        // "⚡ {Provider} ..." name is always bare - never "Workstation · Codex" under
        // the Workstation header (contract §All view + §Reset credits).
        return CreditsRowText(group.Provider.Split(" · ")[^1], credits, Now);
    }

    /// <summary>
    /// "⚡ Codex · 2 reset credits · expire in 2 d" from the soonest FUTURE credit; expired
    /// entries are ignored, and with no future expiry the whole clause (the "expire" word
    /// included) drops away.
    /// </summary>
    internal static string CreditsRowText(string provider, ResetCredits reset, DateTimeOffset now)
    {
        int count = reset.AvailableCount ?? 0;
        DateTimeOffset? soonest = null;
        foreach (var credit in reset.Credits ?? [])
        {
            if (credit.ExpiresAt is not { } raw || ParseTimestampStatic(raw) is not { } expiry) continue;
            if (expiry <= now) continue; // expired entries are ignored
            if (soonest is null || expiry < soonest) soonest = expiry;
        }
        return soonest is null
            ? L10n.T("credits_row_plain", provider, count)
            : L10n.T("credits_row", provider, count, CompanionStore.CreditsClause(soonest.Value, now));
    }

    private static DateTimeOffset? ParseTimestampStatic(string s) => CompanionStore.ParseTimestamp(s);

    // MARK: Quota

    /// <summary>Windows below their low-quota threshold, sorted by remaining ascending,
    /// at most two rows. Multi-server duplicates collapse (same window on two servers is
    /// still ONE alert) and the collapsed row sheds the server label.</summary>
    public List<QuotaRow> LowQuotaRows
    {
        get
        {
            if (!Quota.Enabled) return [];
            // Derive from AllQuotaGroups so each row keeps its provider name and the
            // provider-level Estimated flag. The old AllQuotaRows helper built rows
            // with an empty provider and Estimated=false, losing both in the Low view.
            var low = AllQuotaGroups
                .SelectMany(g => g.Rows)
                .Where(r => r.IsLow(Thresholds))
                .OrderBy(r => r.Left)
                .ToList();
            return low.GroupBy(r => new { Provider = r.Provider.Split(" · ").Last(), r.Account, r.Bucket, r.Left, r.ResetsAt })
                .Select(group => group.Count() > 1 ? group.First() with { Provider = group.Key.Provider } : group.First())
                .OrderBy(r => r.Left).Take(2).ToList();
        }
    }

    public List<QuotaGroup> AllQuotaGroups
    {
        get
        {
            if (!Quota.Enabled || Quota.Providers is null) return [];
            return Quota.Providers
                .Where(kv => kv.Value.Buckets is { Count: > 0 })
                .Select(kv =>
                {
                    var keyParts = kv.Key.Split(" · ");
                    string canonicalProvider = keyParts.Last();
                    // Multi-server merge prefixes "Server · " onto the provider key
                    // (MultiServerTokdashClient); keep the server half for All-view
                    // sectioning (contract §All view).
                    string serverLabel = keyParts.Length > 1 ? string.Join(" · ", keyParts[..^1]) : "";
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
                        IsRowFailed(b, kv.Value, failed),
                        b.CapturedAt is null ? null : DateTimeOffset.FromUnixTimeSeconds(b.CapturedAt.Value))).ToList();
                    if (canonicalProvider.Equals("antigravity", StringComparison.OrdinalIgnoreCase))
                        rows = CompanionStore.AntigravityPools(rows);
                    return new QuotaGroup(Capitalize(kv.Key), canonicalProvider, rows, failed, kv.Value) { ServerLabel = serverLabel };
                })
                .ToList();
        }
    }

    /// <summary>All-view server sections (contract §All view): multi-server payloads group
    /// their provider groups under one muted header per server, in first-seen order;
    /// single-server payloads return a single header-less section, so the All view is
    /// unchanged there. Bare provider names are the flyout's job (QuotaGroupVM).</summary>
    public List<QuotaServerSection> AllQuotaServerSections
    {
        get
        {
            var groups = AllQuotaGroups;
            if (groups.All(g => g.ServerLabel.Length == 0))
                return [new QuotaServerSection("", groups)];
            var order = new List<string>();
            foreach (var g in groups)
                if (!order.Contains(g.ServerLabel)) order.Add(g.ServerLabel);
            return order.Select(s => new QuotaServerSection(s, groups.Where(g => g.ServerLabel == s).ToList())).ToList();
        }
    }

    private static string Capitalize(string s)
    {
        var parts = s.Split(" · ");
        string provider = parts[^1];
        string displayProvider = string.IsNullOrEmpty(provider) ? provider : char.ToUpperInvariant(provider[0]) + provider[1..];
        return parts.Length == 1 ? displayProvider : string.Join(" · ", parts[..^1]) + " · " + displayProvider;
    }

    // "ok" or absent (older servers) is healthy; any other value means that quota
    // couldn't be refreshed this cycle. Spec §7.
    private static bool IsProviderOk(string? status) =>
        string.IsNullOrEmpty(status) || status.Equals("ok", StringComparison.OrdinalIgnoreCase);

    // ROW failure drives the inline ⚠ and notification eligibility. buckets[].status is
    // always "ok" (the server only writes failure statuses to the filtered-out "api"
    // bucket), so freshness is the real discriminator: a row is last-known when the
    // failure that APPLIES TO IT is newer than its data. Strict "<" makes same-cycle
    // equality count as fresh, which is what rescues a healthy credential's window when a
    // sibling credential is broken - every credential in a cycle shares captured_at.
    //
    // The applicable failure is the row's OWN account's, whenever the payload attributes
    // them. StatusAt on the provider is the newest error of ANY credential behind the
    // card: a permanently broken sibling advances it every cycle, while a bucket that is
    // not reported every cycle keeps an older captured_at - and those buckets are
    // ordinary, not edge cases (Claude's `limits` carries weekly_scoped_opus only once
    // Opus has been used; MiniMax's per-model buckets come and go with the models called).
    // Judged against the provider, that marks a working install's rows last-known and
    // drops them out of low-quota notification for as long as the sibling stays broken. So
    // a row whose own credential is healthy is fresh, whatever a sibling did.
    //
    // Everything else falls back to the group rather than un-suppressing a row that may
    // well be stale: Accounts absent (single-credential provider, or a pre-Accounts
    // server), a row naming an account with no entry, or a missing timestamp. Spec §7.
    private static bool IsRowFailed(BucketQuota bucket, ProviderQuota prov, bool groupFailed)
    {
        if (!groupFailed) return false;
        var entry = AccountEntry(prov, bucket.Account);
        if (entry is not null)
        {
            if (!IsAccountFailed(entry)) return false;
            int? statusAt = entry.StatusAt ?? prov.StatusAt;
            if (bucket.CapturedAt is null || statusAt is null) return true;
            return bucket.CapturedAt.Value < statusAt.Value;
        }
        if (bucket.CapturedAt is null || prov.StatusAt is null) return true;
        return bucket.CapturedAt.Value < prov.StatusAt.Value;
    }

    private static AccountQuota? AccountEntry(ProviderQuota prov, string? account)
    {
        if (prov.Accounts is null || string.IsNullOrEmpty(account)) return null;
        return prov.Accounts.FirstOrDefault(a => a.Account == account);
    }

    // Same rule as a group: status present and not "ok", OR a non-empty StatusDetail.
    // Status alone is not a verdict - a credential with a live error still reports "ok"
    // once any of its window rows sorts after its "api" row. Spec §7.
    private static bool IsAccountFailed(AccountQuota entry)
    {
        if (!string.IsNullOrWhiteSpace(entry.StatusDetail)
            && !entry.StatusDetail!.Equals("ok", StringComparison.OrdinalIgnoreCase))
            return true;
        return !IsProviderOk(entry.Status);
    }
}

/// <summary>One provider's group in the All view. CanonicalProvider + Entry drive the
/// reset-credits row (Codex-only, "under the Codex group"); Provider is the display
/// label (server-label-prefixed for multi-server cards).</summary>
public sealed record QuotaGroup(
    string Provider,
    string CanonicalProvider,
    List<QuotaRow> Rows,
    bool Failed,
    ProviderQuota? Entry)
{
    /// <summary>Multi-server: the server half of the "Server · provider" payload key
    /// ("Workstation"); empty in single-server setups. The All view sections by it and
    /// then renders bare provider names (contract §All view).</summary>
    public string ServerLabel { get; init; } = "";
}

/// <summary>One server's slice of the All view (contract §All view): a muted server
/// header over its provider groups. Server is "" for single-server payloads, which
/// render no header at all.</summary>
public sealed record QuotaServerSection(string Server, List<QuotaGroup> Groups);
