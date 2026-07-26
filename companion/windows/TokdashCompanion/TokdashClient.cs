using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TokdashCompanion;

/// <summary>
/// Tokdash API client. Read-only; never writes, never polls providers.
/// Additive JSON decoding: unknown fields ignored, absent optional fields
/// tolerated. Treats 503 as busy (not offline). Never sends a browser Origin.
/// </summary>
public sealed class TokdashClient : IDisposable
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _healthClient;
    private readonly HttpClient _dataClient;
    private readonly Uri _baseUri;

    public TokdashClient(string baseUrl)
    {
        _baseUri = NormalizeBase(new Uri(baseUrl));
        _healthClient = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        _dataClient = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
    }

    public async Task<HealthResponse> HealthAsync(CancellationToken ct = default)
    {
        return await GetAsync<HealthResponse>(_healthClient, "/health", ct);
    }

    public async Task<UsageResponse> UsageAsync(string period, CancellationToken ct = default)
    {
        return await GetAsync<UsageResponse>(_dataClient, $"/api/usage?period={period}", ct);
    }

    public async Task<QuotaResponse> QuotaAsync(CancellationToken ct = default)
    {
        return await GetAsync<QuotaResponse>(_dataClient, "/api/quota", ct);
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
    public long TotalTokens { get; set; }
    public double TotalCost { get; set; }
    public long TotalMessages { get; set; }
    public Dictionary<string, ToolAgg>? ByTool { get; set; }
    public List<ModelAgg>? TopModels { get; set; }
    public List<ModelAgg>? CombinedModels { get; set; }
    public Comparison? Comparison { get; set; }
    public string? Timestamp { get; set; }
    [JsonPropertyName("response_cache")]
    public CacheInfo? ResponseCache { get; set; }
}

public sealed class ToolAgg { public long Tokens { get; set; } public double Cost { get; set; } }
public sealed class ModelAgg { public string Name { get; set; } = ""; public long Tokens { get; set; } public double Cost { get; set; } }
public sealed class Comparison { public double? TokensPct { get; set; } public double? CostPct { get; set; } public double? MessagesPct { get; set; } }
public sealed class CacheInfo { public int? AgeSeconds { get; set; } }

public sealed class QuotaResponse
{
    public bool Enabled { get; set; }
    public Dictionary<string, ProviderQuota>? Providers { get; set; }
    public int? Timestamp { get; set; }
}

public sealed class ProviderQuota
{
    public bool? Estimated { get; set; }
    public List<BucketQuota>? Buckets { get; set; }
}

public sealed class BucketQuota
{
    public string Bucket { get; set; } = "";
    public string? BucketLabel { get; set; }
    public double? RemainingPercent { get; set; }
    public int? ResetsAt { get; set; }
    public string? Account { get; set; }
}
