using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TokdashCompanion;

public interface ITokdashClient : IDisposable
{
    Task<HealthResponse> HealthAsync(CancellationToken ct = default);
    Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default);
    Task<UsageResponse> UsageRangeAsync(string from, string to, CancellationToken ct = default);
    Task<ActiveTimeResponse> ActiveTimeAsync(string period, CancellationToken ct = default);
    Task<ActiveTimeResponse> ActiveTimeRangeAsync(string from, string to, CancellationToken ct = default);
    Task<InsightsResponse> InsightsHourlyTodayAsync(CancellationToken ct = default);
    Task<InsightsResponse> InsightsDailyAsync(string from, string to, CancellationToken ct = default);
    Task<StatsResponse> StatsAsync(CancellationToken ct = default);
    Task<QuotaResponse> QuotaAsync(CancellationToken ct = default);
    Task<VersionResponse> VersionAsync(CancellationToken ct = default);
    Task<ServerUpdateCheckResponse> ServerUpdateCheckAsync(CancellationToken ct = default);
}

/// <summary>
/// Tokdash API client. Read-only; never writes, never polls providers.
/// Additive JSON decoding: unknown fields ignored, absent optional fields
/// tolerated. Treats 503 as busy (not offline). Never sends a browser Origin.
/// </summary>
public sealed class TokdashClient : ITokdashClient
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _healthClient;
    private readonly HttpClient _dataClient;
    private readonly HttpClient _versionClient;
    private readonly Uri _baseUri;

    public TokdashClient(string baseUrl)
    {
        _baseUri = NormalizeBase(new Uri(baseUrl));
        _healthClient = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        // Cold month/year scans server-side run tens of seconds (warm docs: year ~15 s,
        // month ~25 s + base). 20 s cut them off mid-parse and the year view showed
        // "unavailable" on every cycle - long windows need a long ceiling.
        _dataClient = new HttpClient { Timeout = TimeSpan.FromSeconds(90) };
        _versionClient = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
    }

    public async Task<HealthResponse> HealthAsync(CancellationToken ct = default)
    {
        return await GetAsync<HealthResponse>(_healthClient, "/health", ct);
    }

    public async Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default)
    {
        return await GetAsync<UsageResponse>(_dataClient, $"/api/usage?period={period}", ct);
    }

    /// Calendar-week window: `date_from`/`date_to` (local Monday .. today). `period=week`
    /// is a rolling 7-day window and must never be used for the segment. Contract §Period windows.
    public async Task<UsageResponse> UsageRangeAsync(string from, string to, CancellationToken ct = default)
    {
        return await GetAsync<UsageResponse>(_dataClient, $"/api/usage?date_from={from}&date_to={to}", ct);
    }

    public async Task<ActiveTimeResponse> ActiveTimeAsync(string period, CancellationToken ct = default)
    {
        return await GetAsync<ActiveTimeResponse>(_dataClient, $"/api/active-time?period={period}", ct);
    }

    public async Task<ActiveTimeResponse> ActiveTimeRangeAsync(string from, string to, CancellationToken ct = default)
    {
        return await GetAsync<ActiveTimeResponse>(_dataClient, $"/api/active-time?date_from={from}&date_to={to}", ct);
    }

    public async Task<InsightsResponse> InsightsHourlyTodayAsync(CancellationToken ct = default)
    {
        return await GetAsync<InsightsResponse>(_dataClient, "/api/insights?facets=hourly&period=today", ct);
    }

    public async Task<InsightsResponse> InsightsDailyAsync(string from, string to, CancellationToken ct = default)
    {
        return await GetAsync<InsightsResponse>(_dataClient, $"/api/insights?facets=daily&date_from={from}&date_to={to}", ct);
    }

    public async Task<StatsResponse> StatsAsync(CancellationToken ct = default)
    {
        return await GetAsync<StatsResponse>(_dataClient, "/api/stats", ct);
    }

    public async Task<QuotaResponse> QuotaAsync(CancellationToken ct = default)
    {
        return await GetAsync<QuotaResponse>(_dataClient, "/api/quota", ct);
    }

    /// Settings-only diagnostics (contract: never the flyout, never on a schedule).
    public async Task<VersionResponse> VersionAsync(CancellationToken ct = default)
    {
        return await GetAsync<VersionResponse>(_versionClient, "/api/version", ct);
    }

    /// Settings-only. Deliberately consent-gated server-side and read-only; the consent
    /// POST stays web-only, the companion never writes.
    public async Task<ServerUpdateCheckResponse> ServerUpdateCheckAsync(CancellationToken ct = default)
    {
        return await GetAsync<ServerUpdateCheckResponse>(_dataClient, "/api/update-check", ct);
    }

    private async Task<T> GetAsync<T>(HttpClient client, string path, CancellationToken ct)
    {
        Uri url = new(_baseUri, path.TrimStart('/'));
        using var req = new HttpRequestMessage(HttpMethod.Get, url);
        // Native client: never send a browser Origin header.
        try
        {
            using var resp = await client.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
            if (resp.StatusCode == System.Net.HttpStatusCode.ServiceUnavailable)
                throw new TokdashException(TokdashError.Busy);
            if (!resp.IsSuccessStatusCode)
                throw new TokdashException(TokdashError.HttpStatus, (int)resp.StatusCode);
            var stream = await resp.Content.ReadAsStreamAsync(ct);
            return await JsonSerializer.DeserializeAsync<T>(stream, JsonOpts, ct)
                ?? throw new TokdashException(TokdashError.Decode);
        }
        catch (TaskCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new TokdashException(TokdashError.Timeout);
        }
        catch (HttpRequestException ex) when (ex.InnerException is System.Net.Sockets.SocketException)
        {
            throw new TokdashException(TokdashError.Offline);
        }
    }

    private static Uri NormalizeBase(Uri raw)
    {
        string s = raw.ToString().TrimEnd('/') + "/";
        return new Uri(s);
    }

    public void Dispose()
    {
        _healthClient.Dispose();
        _dataClient.Dispose();
        _versionClient.Dispose();
    }
}

public enum TokdashError { Busy, Timeout, Offline, HttpStatus, Decode, Other }

public sealed class TokdashException : Exception
{
    public TokdashError Error { get; }
    public int? StatusCode { get; }
    public TokdashException(TokdashError e, int? statusCode = null) : base(e.ToString()) { Error = e; StatusCode = statusCode; }
}

public sealed record HealthResponse(string Status, string Service, string Version);

public sealed class UsageResponse
{
    public string Period { get; set; } = "";
    [JsonPropertyName("total_tokens")] public long TotalTokens { get; set; }
    [JsonPropertyName("total_cost")] public double TotalCost { get; set; }
    [JsonPropertyName("total_messages")] public long TotalMessages { get; set; }
    [JsonPropertyName("by_tool")] public Dictionary<string, ToolAgg>? ByTool { get; set; }
    [JsonPropertyName("top_models")] public List<ModelAgg>? TopModels { get; set; }
    [JsonPropertyName("top_models_by_cost")] public List<ModelAgg>? TopModelsByCost { get; set; }
    [JsonPropertyName("combined_models")] public List<ModelAgg>? CombinedModels { get; set; }
    public Comparison? Comparison { get; set; }
    public string? Timestamp { get; set; }
    [JsonPropertyName("response_cache")] public CacheInfo? ResponseCache { get; set; }
}

public sealed class ToolAgg { public long Tokens { get; set; } public double Cost { get; set; } }
public sealed class ModelAgg { public string Name { get; set; } = ""; public long Tokens { get; set; } public double Cost { get; set; } }
public sealed class Comparison
{
    [JsonPropertyName("tokens_pct")] public double? TokensPct { get; set; }
    [JsonPropertyName("cost_pct")] public double? CostPct { get; set; }
    [JsonPropertyName("messages_pct")] public double? MessagesPct { get; set; }
    // The *_prev fields let a multi-server companion recompute each percentage from
    // summed current and previous totals (contract §Full delta row). A metric whose
    // *_prev is omitted by any contributing server is omitted from the combined row.
    [JsonPropertyName("cost_prev")] public double? CostPrev { get; set; }
    [JsonPropertyName("tokens_prev")] public double? TokensPrev { get; set; }
    [JsonPropertyName("messages_prev")] public double? MessagesPrev { get; set; }
}
public sealed class CacheInfo { [JsonPropertyName("age_seconds")] public double? AgeSeconds { get; set; } }

public sealed class QuotaResponse
{
    public bool Enabled { get; set; }
    public Dictionary<string, ProviderQuota>? Providers { get; set; }
    public int? Timestamp { get; set; }
}

public sealed class ProviderQuota
{
    public bool? Estimated { get; set; }
    // Provider fetch status: "ok" (or absent) is healthy; anything else means the
    // provider's quota couldn't be refreshed and its buckets are last-known. Spec §7.
    public string? Status { get; set; }
    [JsonPropertyName("status_detail")] public string? StatusDetail { get; set; }
    // Epoch seconds the failure status was observed. This is the newest error of ANY
    // account behind the card, so it drives the GROUP warning; rows compare against their
    // own account's StatusAt in Accounts where that is present. Spec §7.
    [JsonPropertyName("status_at")] public int? StatusAt { get; set; }
    // One entry per credential, present only on a card measuring more than one (a
    // ~/.claude install beside a ~/.claude-<profile> sibling, MiniMax global + CN).
    // Absent for single-credential providers and for every pre-Accounts server. Spec §7.
    public List<AccountQuota>? Accounts { get; set; }
    public List<BucketQuota>? Buckets { get; set; }
    // Codex-only reset credits (contract §Reset credits). Absent on every other
    // provider, and usually on Codex too.
    [JsonPropertyName("reset_credits")] public ResetCredits? ResetCredits { get; set; }
}

/// <summary>
/// <c>providers.codex.reset_credits</c> (contract §Reset credits). <c>ExpiresAt</c> is an
/// ISO 8601 <em>string</em> - unlike the epoch numbers everywhere else in the quota payload.
/// The soonest future <c>expires_at</c> dates the row and arms the expiry notification.
/// </summary>
public sealed class ResetCredits
{
    [JsonPropertyName("available_count")] public int? AvailableCount { get; set; }
    public List<ResetCredit>? Credits { get; set; }
}

public sealed class ResetCredit
{
    public string? Id { get; set; }
    [JsonPropertyName("expires_at")] public string? ExpiresAt { get; set; }
}

/// <summary>
/// One credential behind a provider card, with the failure that belongs to it alone.
/// <para>
/// <c>Status</c> is NOT a verdict on its own: the server takes it from the last stored row
/// it iterated and rows arrive ordered by bucket id, so an install with windows reports
/// <c>"ok"</c> beside a live <c>StatusDetail</c>. Failure is read the same way as for a
/// group — status present and not "ok", OR a non-empty StatusDetail. Spec §7.
/// </para>
/// </summary>
public sealed class AccountQuota
{
    public string? Account { get; set; }
    public string? Plan { get; set; }
    public string? Status { get; set; }
    [JsonPropertyName("status_detail")] public string? StatusDetail { get; set; }
    [JsonPropertyName("status_at")] public int? StatusAt { get; set; }
}

public sealed class BucketQuota
{
    public string Bucket { get; set; } = "";
    [JsonPropertyName("bucket_label")] public string? BucketLabel { get; set; }
    [JsonPropertyName("remaining_percent")] public double? RemainingPercent { get; set; }
    [JsonPropertyName("resets_at")] public int? ResetsAt { get; set; }
    public string? Account { get; set; }
    // Epoch seconds this window was observed. Older than the provider's status_at means
    // the failure is newer than the data, i.e. this row is last-known. Spec §7.
    [JsonPropertyName("captured_at")] public int? CapturedAt { get; set; }
}

// MARK: - v1.1 optional-section payloads (additive; failure/404 hides the section silently)

/// <summary>
/// <c>GET /api/active-time</c> (selected period). <c>ActiveMs</c> is MILLISECONDS - every
/// duration in this payload is; every epoch in the quota payload is seconds. <c>by_tool</c>,
/// <c>comparison</c> and the <c>*_sum</c> fields are not rendered in v1.1 and decode-ignored.
/// </summary>
public sealed class ActiveTimeResponse
{
    [JsonPropertyName("active_ms")] public long? ActiveMs { get; set; }
    public string? Timestamp { get; set; }
}

/// <summary>
/// <c>GET /api/insights?facets=hourly...</c> / <c>?facets=daily...</c>. Exactly one facet per
/// request; only the requested facet is present.
/// </summary>
public sealed class InsightsResponse
{
    public HourlyFacet? Hourly { get; set; }
    public List<DailyPoint>? Daily { get; set; }
}

public sealed class HourlyFacet
{
    public List<HourBucket>? Buckets { get; set; }
    [JsonPropertyName("peak_hour")] public int? PeakHour { get; set; }
}

public sealed class HourBucket
{
    public int? Hour { get; set; }
    public long? Tokens { get; set; }
}

/// <summary>One day of the <c>daily</c> facet. Sparse: a date with no usage has no entry.</summary>
public sealed class DailyPoint
{
    public string? Date { get; set; }
    public long? Tokens { get; set; }
    public int? Intensity { get; set; }
}

/// <summary>
/// <c>GET /api/stats</c> - a rolling 365 days of contributions; v1.1 windows it client-side
/// (trailing 90 days for month, 180 for year). <c>summary.*</c> / <c>stats.*</c> are not rendered.
/// </summary>
public sealed class StatsResponse
{
    public List<Contribution>? Contributions { get; set; }
}

public sealed class Contribution
{
    public string? Date { get; set; }
    public ContributionTotals? Totals { get; set; }
    // int 0..4, ranked quartiles server-side
    public int? Intensity { get; set; }
}

public sealed class ContributionTotals
{
    public long? Tokens { get; set; }
}

/// <summary><c>GET /api/version</c> - Settings only.</summary>
public sealed class VersionResponse
{
    [JsonPropertyName("runtime_version")] public string? RuntimeVersion { get; set; }
    [JsonPropertyName("update_check_enabled")] public bool? UpdateCheckEnabled { get; set; }
}

/// <summary>
/// <c>GET /api/update-check</c> - Settings only, never a POST. <c>Enabled == false</c> means the
/// server has no update-check consent: render nothing, never try to change that.
/// </summary>
public sealed class ServerUpdateCheckResponse
{
    public bool? Enabled { get; set; }
    [JsonPropertyName("update_available")] public bool? UpdateAvailable { get; set; }
    public string? Latest { get; set; }
}
