using System.Net.Http;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TokdashCompanion;

/// <summary>
/// Companion update checking against GitHub's public releases API.
///
/// The companion ships its own version line (<c>companion/VERSION</c>) and shares the
/// Tokdash repository with the Python package, so <c>/releases/latest</c> is useless here:
/// it resolves to the newest <em>Python</em> release, and companion releases are published
/// with <c>--latest=false</c> so they never take that pointer. The check therefore lists
/// releases and filters by tag.
///
/// No credentials are sent (public endpoint, unauthenticated 60 req/hour/IP), and the check
/// never downloads or installs anything - releases are unsigned, so the only action offered
/// is opening the release page in the browser. Mirrors <c>UpdateChecker.swift</c> on macOS;
/// the pure helpers below are pinned to the same cases in both test suites.
/// </summary>
public static class UpdateChecker
{
    /// <summary>Unauthenticated releases list. per_page=100 covers every release the repo
    /// has published so far in one request, so the newest companion tag can't fall off.</summary>
    public const string ReleasesEndpoint = "https://api.github.com/repos/JingbiaoMei/Tokdash/releases?per_page=100";

    /// <summary>Companion releases are tagged companion-vX.Y.Z; Python releases are vX.Y.Z.
    /// The prefix is what separates the two lines in a shared repository.</summary>
    public const string TagPrefix = "companion-v";

    /// <summary>Only ever open a release page under the Tokdash repo's releases path.</summary>
    public const string ReleasesPathPrefix = "/jingbiaomei/tokdash/releases/";

    /// <summary>At most one automatic check per day (spec: "at most once every 24 hours").</summary>
    public static readonly TimeSpan AutoCheckInterval = TimeSpan.FromHours(24);

    // Selection (pure).

    /// <summary>
    /// Newest published companion release, or null when the list holds none.
    ///
    /// Drafts are dropped (unpublished, their tag may not exist yet); prereleases are
    /// deliberately KEPT. Companion builds are no longer published as prereleases, but the
    /// flag must not be filtered on: releases up to 0.2.0 were prereleases, and excluding
    /// them would hide an upgrade path for anyone still on an older build. Tags that don't parse -
    /// Python releases ("v1.5.8"), partial versions ("companion-v0.1"), suffixed versions
    /// ("companion-v0.1.4-rc1") - are skipped rather than guessed at.
    /// </summary>
    public static (int[] Version, GitHubRelease Release)? NewestCompanionRelease(IEnumerable<GitHubRelease>? releases)
    {
        if (releases is null) return null;
        (int[] Version, GitHubRelease Release)? best = null;
        foreach (var release in releases)
        {
            if (release is null || release.Draft) continue;
            int[]? version = ParseTag(release.TagName);
            if (version is null) continue;
            if (best is null || IsNewer(version, best.Value.Version)) best = (version, release);
        }
        return best;
    }

    /// <summary>Parse a release tag into numeric components: "companion-v0.1.10" -> [0, 1, 10].
    /// Returns null for any tag that isn't exactly companion-v plus three numeric parts.</summary>
    public static int[]? ParseTag(string? tag)
    {
        string trimmed = (tag ?? "").Trim();
        if (!trimmed.StartsWith(TagPrefix, StringComparison.Ordinal)) return null;
        return ParseVersion(trimmed[TagPrefix.Length..]);
    }

    /// <summary>
    /// Parse a bare version string: "0.1.10" -> [0, 1, 10]. Strict - exactly three
    /// components, each a non-empty run of ASCII digits. Anything else (empty parts, "0.1",
    /// "0.1.4-rc1", "v0.1.4", digits that overflow int) returns null, so a malformed value
    /// can never be compared as if it were a version.
    /// </summary>
    public static int[]? ParseVersion(string? version)
    {
        string trimmed = (version ?? "").Trim();
        string[] parts = trimmed.Split('.');
        if (parts.Length != 3) return null;
        var output = new int[3];
        for (int i = 0; i < 3; i++)
        {
            string part = parts[i];
            if (part.Length == 0) return null;
            foreach (char c in part) if (c < '0' || c > '9') return null;
            if (!int.TryParse(part, System.Globalization.NumberStyles.None,
                              System.Globalization.CultureInfo.InvariantCulture, out output[i])) return null;
        }
        return output;
    }

    /// <summary>
    /// Component-wise NUMERIC comparison. This is the whole reason versions are parsed to
    /// int[] first: a string compare puts "0.1.10" <em>below</em> "0.1.9", which would hide
    /// every release after the ninth patch.
    /// </summary>
    public static bool IsNewer(int[] candidate, int[] current)
    {
        int len = Math.Max(candidate.Length, current.Length);
        for (int i = 0; i < len; i++)
        {
            int a = i < candidate.Length ? candidate[i] : 0;
            int b = i < current.Length ? current[i] : 0;
            if (a != b) return a > b;
        }
        return false;
    }

    /// <summary>Render parsed components back to a display string ("0.1.10").</summary>
    public static string VersionString(int[] parts) => string.Join(".", parts);

    // Link safety (pure).

    /// <summary>
    /// The link the "View update" button opens. Uses the API's html_url only when it
    /// validates; otherwise falls back to a URL built entirely from the PARSED numeric
    /// version, which has no injection surface at all (no server-supplied text reaches it).
    /// </summary>
    public static string ReleaseUrl(GitHubRelease release, int[] version)
    {
        if (release.HtmlUrl is { } raw && IsValidReleaseUrl(raw)) return raw;
        return $"https://github.com/JingbiaoMei/Tokdash/releases/tag/{TagPrefix}{VersionString(version)}";
    }

    /// <summary>
    /// True for an HTTPS URL whose host is exactly github.com and whose path sits under the
    /// Tokdash repo's releases. Host is compared after URL parsing, so the usual spoofs
    /// (https://github.com@evil.test/..., https://github.com.evil.test/...) resolve to a
    /// different host and fail. Owner/repo are compared case-insensitively: GitHub treats
    /// them that way and the API's canonical casing need not match ours.
    /// </summary>
    public static bool IsValidReleaseUrl(string? raw)
    {
        if (!Uri.TryCreate((raw ?? "").Trim(), UriKind.Absolute, out var uri)) return false;
        if (uri.Scheme != Uri.UriSchemeHttps) return false;
        if (!string.Equals(uri.Host, "github.com", StringComparison.OrdinalIgnoreCase)) return false;
        return uri.AbsolutePath.ToLowerInvariant().StartsWith(ReleasesPathPrefix, StringComparison.Ordinal);
    }

    // Scheduling (pure).

    /// <summary>
    /// True when an automatic check is due: the opt-in is on and the last attempt was at
    /// least 24h ago. A timestamp in the future means the clock moved backwards; treat it as
    /// due rather than blocking checks until real time catches up.
    /// </summary>
    public static bool ShouldAutoCheck(bool enabled, DateTimeOffset? lastCheck, DateTimeOffset now)
    {
        if (!enabled) return false;
        if (lastCheck is null) return true;
        if (lastCheck.Value > now) return true;
        return now - lastCheck.Value >= AutoCheckInterval;
    }

    /// <summary>Relative "last checked" caption, using the same tiers as the freshness footer.</summary>
    public static string LastCheckedText(DateTimeOffset? last, DateTimeOffset now)
    {
        if (last is null) return L10n.T("update_never_checked");
        var age = now - last.Value;
        if (age < TimeSpan.Zero) age = TimeSpan.Zero;
        if (age.TotalSeconds < 60) return L10n.T("update_last_checked_just_now");
        if (age.TotalMinutes < 60) return L10n.T("update_last_checked_min", (int)age.TotalMinutes);
        if (age.TotalHours < 24) return L10n.T("update_last_checked_h", (int)age.TotalHours);
        return L10n.T("update_last_checked_d", (int)age.TotalDays);
    }

    /// <summary>User-facing reason for a FAILED manual check. Scheduled checks never surface
    /// these (spec: silent), so this is only read from the Settings status line.</summary>
    public static string FailureText(UpdateCheckError error) => error switch
    {
        UpdateCheckError.Offline or UpdateCheckError.Timeout => L10n.T("update_failed_offline"),
        UpdateCheckError.RateLimited => L10n.T("update_failed_rate_limited"),
        _ => L10n.T("update_failed_generic"),
    };

    /// <summary>
    /// The running app's version ("0.1.4"), read from the assembly so it can never drift
    /// from what was shipped. Directory.Build.props feeds this from companion/VERSION and
    /// may append "+&lt;sha&gt;", which is trimmed off.
    /// </summary>
    public static string CurrentVersion
    {
        get
        {
            string? raw = typeof(UpdateChecker).Assembly
                .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
            if (string.IsNullOrWhiteSpace(raw)) raw = typeof(UpdateChecker).Assembly.GetName().Version?.ToString();
            if (string.IsNullOrWhiteSpace(raw)) return "0.0.0";
            int plus = raw.IndexOf('+');
            if (plus >= 0) raw = raw[..plus];
            // AssemblyVersion is four-part ("0.1.4.0"); the update comparison wants three.
            string[] parts = raw.Split('.');
            return parts.Length >= 3 ? $"{parts[0]}.{parts[1]}.{parts[2]}" : raw;
        }
    }
}

/// <summary>Outcome of the most recent check, used only for the Settings status line. The
/// Settings-gear badge is deliberately NOT derived from this: it reads the persisted
/// available version instead, so a later Checking/Failed state can't clear a pending update
/// and the dot survives a relaunch. See <see cref="CompanionStore.UpdateAvailableVersion"/>.</summary>
public enum UpdateStatusKind { Idle, Checking, UpToDate, Available, Failed }

public sealed record UpdateStatus(UpdateStatusKind Kind, string? Version = null, string? Url = null, string? Message = null)
{
    public static readonly UpdateStatus Idle = new(UpdateStatusKind.Idle);
    public static readonly UpdateStatus Checking = new(UpdateStatusKind.Checking);
    public static readonly UpdateStatus UpToDate = new(UpdateStatusKind.UpToDate);
    public static UpdateStatus Available(string version, string url) => new(UpdateStatusKind.Available, version, url);
    public static UpdateStatus Failed(string message) => new(UpdateStatusKind.Failed, Message: message);
}

public enum UpdateCheckError
{
    Offline,
    Timeout,
    /// <summary>403 (unauthenticated hourly limit) or 429. Distinct from a plain HTTP
    /// failure because the remedy is "wait", not "check your network".</summary>
    RateLimited,
    HttpStatus,
    Decode,
    Other,
}

public sealed class UpdateCheckException : Exception
{
    public UpdateCheckError Error { get; }
    public int? StatusCode { get; }
    public UpdateCheckException(UpdateCheckError error, int? statusCode = null) : base(error.ToString())
    {
        Error = error;
        StatusCode = statusCode;
    }
}

/// <summary>One entry from the releases list. Additive decoding: unknown fields are ignored
/// and absent optional fields are tolerated, so a GitHub API addition can't break the check.</summary>
public sealed class GitHubRelease
{
    [JsonPropertyName("tag_name")] public string TagName { get; set; } = "";
    [JsonPropertyName("draft")] public bool Draft { get; set; }
    [JsonPropertyName("prerelease")] public bool Prerelease { get; set; }
    [JsonPropertyName("html_url")] public string? HtmlUrl { get; set; }
}

public interface IUpdateClient
{
    Task<List<GitHubRelease>> FetchReleasesAsync(CancellationToken ct = default);
}

/// <summary>
/// Fetches the releases list. Separate from <see cref="TokdashClient"/> on purpose: this
/// talks to a third party, and a GitHub failure must never touch Tokdash connection state.
/// </summary>
public sealed class GitHubReleasesClient : IUpdateClient, IDisposable
{
    private static readonly JsonSerializerOptions JsonOpts = new() { PropertyNameCaseInsensitive = true };

    private readonly HttpClient _http;
    private readonly string _endpoint;

    public GitHubReleasesClient(string endpoint = UpdateChecker.ReleasesEndpoint)
    {
        _endpoint = endpoint;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        // GitHub rejects requests with no User-Agent (403). No token is sent: this is a
        // public endpoint and the companion holds no credentials.
        _http.DefaultRequestHeaders.Add("User-Agent", $"TokdashCompanion/{UpdateChecker.CurrentVersion}");
        _http.DefaultRequestHeaders.Add("Accept", "application/vnd.github+json");
        _http.DefaultRequestHeaders.Add("X-GitHub-Api-Version", "2022-11-28");
    }

    public async Task<List<GitHubRelease>> FetchReleasesAsync(CancellationToken ct = default)
    {
        using var req = new HttpRequestMessage(HttpMethod.Get, _endpoint);
        try
        {
            using var resp = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
            // 403 is how the unauthenticated hourly limit is reported; 429 is the newer form.
            if (resp.StatusCode == System.Net.HttpStatusCode.Forbidden ||
                resp.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
                throw new UpdateCheckException(UpdateCheckError.RateLimited, (int)resp.StatusCode);
            if (!resp.IsSuccessStatusCode)
                throw new UpdateCheckException(UpdateCheckError.HttpStatus, (int)resp.StatusCode);
            var stream = await resp.Content.ReadAsStreamAsync(ct);
            try
            {
                return await JsonSerializer.DeserializeAsync<List<GitHubRelease>>(stream, JsonOpts, ct) ?? new();
            }
            catch (JsonException)
            {
                throw new UpdateCheckException(UpdateCheckError.Decode);
            }
        }
        catch (UpdateCheckException)
        {
            throw;
        }
        catch (TaskCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new UpdateCheckException(UpdateCheckError.Timeout);
        }
        catch (HttpRequestException)
        {
            throw new UpdateCheckException(UpdateCheckError.Offline);
        }
    }

    public void Dispose() => _http.Dispose();
}
