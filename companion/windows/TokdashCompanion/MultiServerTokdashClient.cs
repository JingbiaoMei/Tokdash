namespace TokdashCompanion;

/// <summary>Read-only fan-out client used by the resident companion.</summary>
public sealed class MultiServerTokdashClient : ITokdashClient
{
    private readonly List<(CompanionServerSettings Server, ITokdashClient Client)> _clients;
    private readonly object _failureLock = new();
    private List<CompanionServerSettings> _failedServers = [];
    public IReadOnlyList<CompanionServerSettings> FailedServers { get { lock (_failureLock) return _failedServers.ToList(); } }
    public IReadOnlyList<string> FailedServerIds => FailedServers.Select(s => s.Id).ToList();
    public IReadOnlyList<string> FailedServerLabels => FailedServers.Select(s => s.Label).ToList();

    /// <summary>
    /// Per-server hero values from the most recent successful usage fan-out, in settings
    /// order (unreachable servers carry a null usage and render the "unreachable" tag).
    /// Computed at usage time so a later quota/health failure on the same cycle never
    /// re-labels a server whose usage actually answered. Empty before the first cycle.
    /// </summary>
    public IReadOnlyList<PerServerUsage> LastPerServerRows { get; private set; } = [];

    public MultiServerTokdashClient(IEnumerable<CompanionServerSettings> servers) =>
        _clients = servers.Where(s => s.Enabled)
            .Select(s => (s, (ITokdashClient)new TokdashClient(s.BaseUrl))).ToList();

    /// <summary>Test seam: inject per-server clients (a fake per enabled server).</summary>
    internal MultiServerTokdashClient(
        IEnumerable<CompanionServerSettings> servers,
        Func<CompanionServerSettings, ITokdashClient> factory) =>
        _clients = servers.Where(s => s.Enabled)
            .Select(s => (s, factory(s))).ToList();

    public async Task<HealthResponse> HealthAsync(CancellationToken ct = default)
    {
        lock (_failureLock) _failedServers = [];
        var results = await Settle(c => c.HealthAsync(ct), ct);
        var good = results.Where(r => r.Value?.Service == "tokdash").ToList();
        AddFailures(results.Where(r => r.Value?.Service != "tokdash").Select(r => r.Server));
        if (good.Count == 0) ThrowAggregateFailure(results);
        return good[0].Value!;
    }

    public Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default) =>
        FanOutUsage(c => c.UsageAsync(period, ct), ct);

    public Task<UsageResponse> UsageRangeAsync(string from, string to, CancellationToken ct = default) =>
        FanOutUsage(c => c.UsageRangeAsync(from, to, ct), ct);

    private async Task<UsageResponse> FanOutUsage(Func<ITokdashClient, Task<UsageResponse>> fetch, CancellationToken ct)
    {
        var settled = await Settle(fetch, ct);
        AddFailures(settled.Where(r => r.Value is null).Select(r => r.Server));
        // Per-server rows for this cycle, computed NOW (see LastPerServerRows).
        LastPerServerRows = CompanionStore.PerServerRows(
            _clients.Select(c => c.Server).ToList(),
            settled.Select(r => r.Value).ToList(),
            FailedServerIds.ToHashSet());
        var rows = settled.Where(r => r.Value is not null).Select(r => r.Value!).ToList();
        if (rows.Count == 0) ThrowAggregateFailure(settled);
        return CombineUsage(rows);
    }

    public async Task<QuotaResponse> QuotaAsync(CancellationToken ct = default)
    {
        var settled = await Settle(c => c.QuotaAsync(ct), ct);
        var good = settled.Where(r => r.Value is not null).ToList();
        AddFailures(settled.Where(r => r.Value is null).Select(r => r.Server));
        if (good.Count == 0) ThrowAggregateFailure(settled);
        var providers = new Dictionary<string, ProviderQuota>();
        foreach (var row in good)
            foreach (var provider in row.Value!.Providers ?? [])
                providers[$"{row.Server.Label} · {provider.Key}"] = provider.Value;
        return new QuotaResponse { Enabled = good.Any(r => r.Value!.Enabled), Providers = providers };
    }

    // MARK: - v1.1 optional endpoints (rule 6: never AddFailures for these)

    /// <summary>
    /// Fan out active-time and SUM <c>active_ms</c> across servers - but if any enabled
    /// server failed the request or its payload omits <c>active_ms</c>, the combined value
    /// is null: a known-partial sum would misreport the period (contract §Active time).
    /// Optional section: per-server failures are silent (no AddFailures, no inline warning).
    /// </summary>
    public Task<ActiveTimeResponse> ActiveTimeAsync(string period, CancellationToken ct = default) =>
        FanOutActiveTime(c => c.ActiveTimeAsync(period, ct), ct);

    public Task<ActiveTimeResponse> ActiveTimeRangeAsync(string from, string to, CancellationToken ct = default) =>
        FanOutActiveTime(c => c.ActiveTimeRangeAsync(from, to, ct), ct);

    private async Task<ActiveTimeResponse> FanOutActiveTime(
        Func<ITokdashClient, Task<ActiveTimeResponse>> fetch, CancellationToken ct)
    {
        var settled = await SettleSilent(fetch, ct);
        long? combined = CompanionStore.CombinedActiveMs(
            settled.Select(r => (r.Value?.ActiveMs, r.Value is null)).ToList());
        // Timestamp: earliest present, mirroring CombineUsage.
        return new ActiveTimeResponse
        {
            ActiveMs = combined,
            Timestamp = settled.Select(r => r.Value?.Timestamp).Where(v => v is not null).Order().FirstOrDefault(),
        };
    }

    /// <summary>
    /// Insights/stats fan-out for completeness. The store never requests the glance in
    /// multi-server mode (per-server hourly/daily series can't be honestly merged into
    /// one shared strip), so these two only exist to keep the interface whole; they merge
    /// by summing tokens per bucket/date. Silent failures like active-time.
    /// </summary>
    public async Task<InsightsResponse> InsightsHourlyTodayAsync(CancellationToken ct = default)
    {
        var settled = await SettleSilent(c => c.InsightsHourlyTodayAsync(ct), ct);
        var bars = new long[24];
        int? peak = null;
        long best = 0;
        foreach (var row in settled.Where(r => r.Value?.Hourly?.Buckets is not null))
            foreach (var b in row.Value!.Hourly!.Buckets!)
                if (b.Hour is { } h && h >= 0 && h <= 23) bars[h] += b.Tokens ?? 0;
        if (settled.Any(r => r.Value?.Hourly is not null))
        {
            for (int i = 0; i < 24; i++) if (bars[i] > best) { best = bars[i]; peak = i; }
        }
        return new InsightsResponse
        {
            Hourly = settled.Any(r => r.Value?.Hourly is not null)
                ? new HourlyFacet
                {
                    Buckets = Enumerable.Range(0, 24).Select(i => new HourBucket { Hour = i, Tokens = bars[i] }).ToList(),
                    PeakHour = best > 0 ? peak : null,
                }
                : null,
        };
    }

    public async Task<InsightsResponse> InsightsDailyAsync(string from, string to, CancellationToken ct = default)
    {
        var settled = await SettleSilent(c => c.InsightsDailyAsync(from, to, ct), ct);
        if (!settled.Any(r => r.Value?.Daily is not null)) return new InsightsResponse();
        var byDate = new Dictionary<string, long>();
        foreach (var row in settled)
            foreach (var p in row.Value?.Daily ?? [])
                if (p.Date is { } d) byDate[d] = byDate.GetValueOrDefault(d) + (p.Tokens ?? 0);
        return new InsightsResponse
        {
            Daily = byDate.OrderBy(kv => kv.Key, StringComparer.Ordinal)
                .Select(kv => new DailyPoint { Date = kv.Key, Tokens = kv.Value }).ToList(),
        };
    }

    public async Task<StatsResponse> StatsAsync(CancellationToken ct = default)
    {
        var settled = await SettleSilent(c => c.StatsAsync(ct), ct);
        if (!settled.Any(r => r.Value?.Contributions is not null)) return new StatsResponse();
        var byDate = new Dictionary<string, (long Tokens, int Intensity)>();
        foreach (var row in settled)
            foreach (var c in row.Value?.Contributions ?? [])
                if (c.Date is { } d)
                {
                    var (tokens, intensity) = byDate.GetValueOrDefault(d);
                    byDate[d] = (tokens + (c.Totals?.Tokens ?? 0), Math.Max(intensity, c.Intensity ?? 0));
                }
        return new StatsResponse
        {
            Contributions = byDate.OrderBy(kv => kv.Key, StringComparer.Ordinal)
                .Select(kv => new Contribution
                {
                    Date = kv.Key,
                    Totals = new ContributionTotals { Tokens = kv.Value.Tokens },
                    Intensity = kv.Value.Intensity,
                }).ToList(),
        };
    }

    /// <summary>Settings-only diagnostics fan out to "the first server that answers":
    /// a single Settings row shows one runtime version, and the update badge lights for
    /// any server behind. Failures never mark a server unreachable (Settings-only).</summary>
    public async Task<VersionResponse> VersionAsync(CancellationToken ct = default)
    {
        var settled = await SettleSilent(c => c.VersionAsync(ct), ct);
        return settled.FirstOrDefault(r => r.Value is not null)?.Value
            ?? ThrowFirst<VersionResponse>(settled);
    }

    public async Task<ServerUpdateCheckResponse> ServerUpdateCheckAsync(CancellationToken ct = default)
    {
        var settled = await SettleSilent(c => c.ServerUpdateCheckAsync(ct), ct);
        return settled.FirstOrDefault(r => r.Value is not null)?.Value
            ?? ThrowFirst<ServerUpdateCheckResponse>(settled);
    }

    private static T ThrowFirst<T>(IReadOnlyCollection<Settled<T>> settled) where T : class
    {
        throw settled.FirstOrDefault(r => r.Error is not null)?.Error
            ?? new TokdashException(TokdashError.Offline);
    }

    private async Task<List<Settled<T>>> Settle<T>(Func<ITokdashClient, Task<T>> fetch, CancellationToken ct) where T : class
    {
        var tasks = _clients.Select(async item =>
        {
            try { return new Settled<T>(item.Server, await fetch(item.Client), null); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
            catch (Exception ex) { return new Settled<T>(item.Server, default, ex); }
        });
        return (await Task.WhenAll(tasks)).ToList();
    }

    /// <summary>Same fan-out, but the section is optional: failures are NOT recorded as
    /// server failures, so they never join the "servers unavailable" footer or the
    /// per-server backoff (contract rule 6).</summary>
    private Task<List<Settled<T>>> SettleSilent<T>(Func<ITokdashClient, Task<T>> fetch, CancellationToken ct) where T : class
        => Settle(fetch, ct);

    private void AddFailures(IEnumerable<CompanionServerSettings> servers)
    {
        lock (_failureLock)
            _failedServers = _failedServers.Concat(servers).DistinctBy(s => s.Id).ToList();
    }

    private static void ThrowAggregateFailure<T>(IReadOnlyCollection<Settled<T>> rows) where T : class
    {
        if (rows.Count > 0 && rows.All(r => r.Error is TokdashException { Error: TokdashError.Busy }))
            throw new TokdashException(TokdashError.Busy);
        throw new TokdashException(TokdashError.Offline);
    }

    internal static UsageResponse CombineUsage(IReadOnlyList<UsageResponse> rows)
    {
        var tools = new Dictionary<string, ToolAgg>();
        var models = new Dictionary<string, ModelAgg>();
        foreach (var row in rows)
        {
            foreach (var item in row.ByTool ?? [])
            {
                if (!tools.TryGetValue(item.Key, out var target)) tools[item.Key] = target = new ToolAgg();
                target.Tokens += item.Value.Tokens; target.Cost += item.Value.Cost;
            }
            foreach (var item in row.CombinedModels ?? row.TopModels ?? [])
            {
                if (!models.TryGetValue(item.Name, out var target)) models[item.Name] = target = new ModelAgg { Name = item.Name };
                target.Tokens += item.Tokens; target.Cost += item.Cost;
            }
        }
        double totalCost = rows.Sum(r => r.TotalCost);
        // Mirror the server's own shape: CombinedModels is the full list ranked by
        // tokens, TopModels its first five, TopModelsByCost the five by cost. This
        // used to hand back one cost-sorted uncapped list under both array names.
        var byTokens = models.Values
            .OrderByDescending(m => m.Tokens).ThenByDescending(m => m.Cost)
            .ThenBy(m => m.Name, StringComparer.Ordinal).ToList();
        var byCost = models.Values
            .OrderByDescending(m => m.Cost).ThenByDescending(m => m.Tokens)
            .ThenBy(m => m.Name, StringComparer.Ordinal).ToList();
        return new UsageResponse
        {
            Period = rows[0].Period,
            TotalTokens = rows.Sum(r => r.TotalTokens), TotalCost = totalCost,
            TotalMessages = rows.Sum(r => r.TotalMessages), ByTool = tools,
            CombinedModels = byTokens,
            TopModels = byTokens.Take(5).ToList(),
            TopModelsByCost = byCost.Take(5).ToList(),
            Timestamp = rows.Select(r => r.Timestamp).Where(v => v is not null).Order().FirstOrDefault(),
            // Full delta row across servers (contract §Full delta row): recompute each
            // percentage from the SUMMED current and previous totals. A metric whose
            // *_prev is omitted by ANY contributing server is omitted from the combined
            // row entirely - never a percentage computed from a known-incomplete sum.
            Comparison = new Comparison
            {
                CostPct = CombinedPct(rows, r => r.TotalCost, c => c.CostPrev),
                TokensPct = CombinedPct(rows, r => (double)r.TotalTokens, c => c.TokensPrev),
                MessagesPct = CombinedPct(rows, r => (double)r.TotalMessages, c => c.MessagesPrev),
            },
        };
    }

    /// <summary>
    /// <c>(sumCurrent - sumPrev) / sumPrev * 100</c>, or null when any server omits the
    /// metric's <c>*_prev</c>, or when the summed previous total is not positive. The
    /// server's own <c>*_pct</c> values are NOT averaged - percentages of different bases
    /// don't average. This also governs a one-survivor fan-out: the contract drops a
    /// metric whose only contributing server omits <c>*_prev</c>; when that survivor
    /// does carry <c>*_prev</c>, the single-row recompute reduces to its own
    /// <c>(current-prev)/prev*100</c> (the Tokdash server's own pct formula, confirmed
    /// against usage-today.json: -11.7 = (248-281)/281), so nothing is lost by not
    /// trusting its precomputed pct.
    /// </summary>
    private static double? CombinedPct(
        IReadOnlyList<UsageResponse> rows,
        Func<UsageResponse, double> current,
        Func<Comparison, double?> prev)
    {
        if (rows.Count == 0) return null;
        var comparisons = rows.Select(r => r.Comparison).ToList();
        if (comparisons.Any(c => c is null)) return null;
        if (comparisons.Any(c => prev(c!) is null)) return null;
        // Non-null by the guard above; the ?? 0 keeps the compiler happy, never taken.
        double sumPrev = rows.Sum(r => prev(r.Comparison!) ?? 0);
        if (sumPrev <= 0) return null;
        double sumCurrent = rows.Sum(current);
        return (sumCurrent - sumPrev) / sumPrev * 100;
    }

    public void Dispose() { foreach (var item in _clients) item.Client.Dispose(); }
    private sealed record Settled<T>(CompanionServerSettings Server, T? Value, Exception? Error) where T : class;
}
