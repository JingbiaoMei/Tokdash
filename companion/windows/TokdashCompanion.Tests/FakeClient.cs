using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;

namespace TokdashCompanion.Tests;

/// <summary>
/// Controllable ITokdashClient (v1.1): one slot per endpoint. A slot may hold
///
///   * null            - the endpoint's default success (empty response; quota empty/off);
///   * a response T    - returned as-is;
///   * "503"           - TokdashException(Busy);
///   * "fail"/"timeout"- TokdashException(Offline) ("fail" also models JSON null:
///                       "the endpoint returns nothing");
///   * "pending"       - a Task that never completes (the loading case; tests must not
///                       await RefreshAsync with pending slots).
///
/// Every call records its request path (the real client's exact query string), which is
/// what the requests_absent / week_request / usage_calls_made expectations read.
/// </summary>
internal sealed class FakeClient : ITokdashClient
{
    private readonly List<string> _requests = [];

    private static Task<T> Pending<T>() => new TaskCompletionSource<T>().Task; // never completes

    public object? Health { get; set; } = new HealthResponse("ok", "tokdash", "1.0");
    public object? Usage { get; set; } = new UsageResponse { TotalTokens = 1000, TotalCost = 1.5, TotalMessages = 10 };
    /// <summary>Null falls back to the <see cref="Usage"/> slot (same payload either way).</summary>
    public object? UsageRange { get; set; }
    public object? ActiveTime { get; set; } = new ActiveTimeResponse();
    public object? ActiveTimeRange { get; set; }
    public object? Insights { get; set; } = new InsightsResponse();
    public object? Stats { get; set; } = new StatsResponse();
    public object? Quota { get; set; }
    public object? Version { get; set; } = new VersionResponse();
    public object? UpdateCheck { get; set; } = new ServerUpdateCheckResponse();

    /// <summary>Exact request paths in call order, for requests_absent assertions.</summary>
    public IReadOnlyList<string> Requests { get { lock (_requests) return _requests.ToList(); } }

    public void Record(string path) { lock (_requests) _requests.Add(path); }

    private Task<T> Resolve<T>(object? slot, string path, T fallback) where T : class
    {
        Record(path);
        return slot switch
        {
            null => Task.FromResult(fallback),
            T value => Task.FromResult(value),
            "503" => Task.FromException<T>(new TokdashException(TokdashError.Busy)),
            "fail" or "timeout" => Task.FromException<T>(new TokdashException(TokdashError.Offline)),
            "pending" => Pending<T>(),
            _ => Task.FromException<T>(new InvalidOperationException($"FakeClient slot for {path}: unexpected {slot.GetType().Name} '{slot}'")),
        };
    }

    public Task<HealthResponse> HealthAsync(CancellationToken ct = default) =>
        Resolve(Health, "/health", new HealthResponse("ok", "tokdash", "1.0"));

    public Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default) =>
        Resolve(Usage, $"/api/usage?period={period}", new UsageResponse());

    public Task<UsageResponse> UsageRangeAsync(string from, string to, CancellationToken ct = default) =>
        Resolve(UsageRange ?? Usage, $"/api/usage?date_from={from}&date_to={to}", new UsageResponse());

    public Task<ActiveTimeResponse> ActiveTimeAsync(string period, CancellationToken ct = default) =>
        Resolve(ActiveTime, $"/api/active-time?period={period}", new ActiveTimeResponse());

    public Task<ActiveTimeResponse> ActiveTimeRangeAsync(string from, string to, CancellationToken ct = default) =>
        Resolve(ActiveTimeRange ?? ActiveTime, $"/api/active-time?date_from={from}&date_to={to}", new ActiveTimeResponse());

    public Task<InsightsResponse> InsightsHourlyTodayAsync(CancellationToken ct = default) =>
        Resolve(Insights, "/api/insights?facets=hourly&period=today", new InsightsResponse());

    public Task<InsightsResponse> InsightsDailyAsync(string from, string to, CancellationToken ct = default) =>
        Resolve(Insights, $"/api/insights?facets=daily&date_from={from}&date_to={to}", new InsightsResponse());

    public Task<StatsResponse> StatsAsync(CancellationToken ct = default) =>
        Resolve(Stats, "/api/stats", new StatsResponse());

    public Task<QuotaResponse> QuotaAsync(CancellationToken ct = default) =>
        Resolve(Quota, "/api/quota", new QuotaResponse());

    public Task<VersionResponse> VersionAsync(CancellationToken ct = default) =>
        Resolve(Version, "/api/version", new VersionResponse());

    public Task<ServerUpdateCheckResponse> ServerUpdateCheckAsync(CancellationToken ct = default) =>
        Resolve(UpdateCheck, "/api/update-check", new ServerUpdateCheckResponse());

    public void Dispose() { }
}
