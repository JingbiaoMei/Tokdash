namespace TokdashCompanion;

/// <summary>Read-only fan-out client used by the resident companion.</summary>
public sealed class MultiServerTokdashClient : ITokdashClient
{
    private readonly List<(CompanionServerSettings Server, TokdashClient Client)> _clients;
    private readonly object _failureLock = new();
    private List<CompanionServerSettings> _failedServers = [];
    public IReadOnlyList<CompanionServerSettings> FailedServers { get { lock (_failureLock) return _failedServers.ToList(); } }
    public IReadOnlyList<string> FailedServerIds => FailedServers.Select(s => s.Id).ToList();
    public IReadOnlyList<string> FailedServerLabels => FailedServers.Select(s => s.Label).ToList();

    public MultiServerTokdashClient(IEnumerable<CompanionServerSettings> servers) =>
        _clients = servers.Where(s => s.Enabled)
            .Select(s => (s, new TokdashClient(s.BaseUrl))).ToList();

    public async Task<HealthResponse> HealthAsync(CancellationToken ct = default)
    {
        lock (_failureLock) _failedServers = [];
        var results = await Settle(c => c.HealthAsync(ct), ct);
        var good = results.Where(r => r.Value?.Service == "tokdash").ToList();
        AddFailures(results.Where(r => r.Value?.Service != "tokdash").Select(r => r.Server));
        if (good.Count == 0) ThrowAggregateFailure(results);
        return good[0].Value!;
    }

    public async Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default)
    {
        var settled = await Settle(c => c.UsageAsync(period, ct), ct);
        var rows = settled.Where(r => r.Value is not null).Select(r => r.Value!).ToList();
        AddFailures(settled.Where(r => r.Value is null).Select(r => r.Server));
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

    private async Task<List<Settled<T>>> Settle<T>(Func<TokdashClient, Task<T>> fetch, CancellationToken ct) where T : class
    {
        var tasks = _clients.Select(async item =>
        {
            try { return new Settled<T>(item.Server, await fetch(item.Client), null); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
            catch (Exception ex) { return new Settled<T>(item.Server, default, ex); }
        });
        return (await Task.WhenAll(tasks)).ToList();
    }

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
        bool hasPrevious = rows.All(r => r.Comparison?.CostPrev is not null);
        double? previous = hasPrevious ? rows.Sum(r => r.Comparison!.CostPrev!.Value) : null;
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
            Comparison = new Comparison { CostPrev = previous, CostPct = previous is > 0 ? (totalCost - previous.Value) / previous.Value * 100 : null },
        };
    }

    public void Dispose() { foreach (var item in _clients) item.Client.Dispose(); }
    private sealed record Settled<T>(CompanionServerSettings Server, T? Value, Exception? Error) where T : class;
}
