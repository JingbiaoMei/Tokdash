namespace TokdashCompanion;

/// <summary>
/// Pure formatting/selection helpers shared between the flyout and tests.
/// Mirrors the macOS Snapshot logic so both platforms stay aligned.
/// </summary>
public static class Formatter
{
    public static string CompactTokens(long tokens)
    {
        if (tokens >= 1_000_000) return $"{tokens / 1_000_000.0:F1}M";
        if (tokens >= 1_000) return $"{tokens / 1000}k";
        return tokens.ToString();
    }

    public static string FormatCost(double cost) => cost.ToString("C2", System.Globalization.CultureInfo.GetCultureInfo("en-US"));

    public static string QuotaBarClass(double leftPercent) => leftPercent switch
    {
        < 25 => "low",
        < 50 => "mid",
        _ => "fine",
    };

public static string ComparisonText(double? costPct)
{
    if (costPct is null) return "";
    double abs = Math.Abs(costPct.Value);
    return costPct.Value <= 0 ? L10n.T("comparison_below", (int)abs) : L10n.T("comparison_above", (int)abs);
}
}

public sealed record QuotaThresholds(double FiveHour, double Weekly, double Other)
{
    public static QuotaThresholds Defaults { get; } = new(20, 10, 15);

    public double ThresholdFor(string bucket)
    {
        string b = bucket.ToLowerInvariant();
        if (b.Contains("5h") || b.Contains("5-hour") || b == "5h") return FiveHour;
        if (b.Contains("week") || b == "weekly" || b == "7d") return Weekly;
        return Other;
    }
}

public sealed record QuotaRow(
    string Provider,
    string Bucket,
    string BucketLabel,
    double Left,
    DateTimeOffset? ResetsAt,
    bool Estimated,
    string Account,
    bool HasPercent,
    bool Failed = false)
{
    /// <summary>
    /// Drop a trailing "window" from a server bucket label: the flyout is narrow and the
    /// word carries no information ("5-hour window" -> "5-hour", "7-day window" -> "7-day").
    /// Applied at display time so stored labels from older servers shorten too. Labels that
    /// don't end in it (MiniMax "5-hour", Kimi "Weekly") pass through unchanged.
    /// Mirrors the macOS QuotaRow.displayLabel.
    /// </summary>
    public static string DisplayLabel(string raw)
    {
        string s = (raw ?? "").Trim();
        if (s.EndsWith(" window", StringComparison.OrdinalIgnoreCase))
        {
            string shortened = s[..^" window".Length].TrimEnd();
            if (shortened.Length > 0) s = shortened;
        }
        // Codex names metered features "GPT-<ver>-Codex-<feature>", which eats the whole
        // row at flyout width. Keep only the feature and the window: "Spark · 5-hour".
        // Only applies to "<name> · <window>" labels whose name is hyphenated, so plain
        // names (MiniMax "Video · Weekly") and bare windows ("5-hour") are untouched.
        string[] parts = s.Split(" · ");
        if (parts.Length == 2 && parts[0].Contains('-'))
        {
            string feature = parts[0].Split('-')[^1];
            if (feature.Length > 0) s = $"{feature} · {parts[1]}";
        }
        return s;
    }

    public bool IsLow(QuotaThresholds t) => HasPercent && Left <= t.ThresholdFor(CanonicalBucket);

    /// <summary>
    /// Canonical bucket id used for threshold lookup and Claude's normalized display label.
    /// Claude's usage API emits ids like
    /// "session" (the 5-hour window) and "weekly_scoped" / "weekly_scoped_&lt;model&gt;" (weekly),
    /// plus legacy "five_hour" / "seven_day". None match the threshold patterns, so Claude
    /// would otherwise land in the 15% "other" bucket instead of 20% / 10% like Codex. The
    /// notification dedup key keeps the original bucket id. Scoped to the claude provider so
    /// Codex/MiniMax/Kimi/Antigravity classification is untouched. Mirrors macOS.
    /// </summary>
    public string CanonicalBucket => NormalizeBucketForThreshold(Provider, Bucket, BucketLabel);

    public static string NormalizeBucketForThreshold(string provider, string bucket, string label)
    {
        if (!string.Equals(provider, "claude", StringComparison.OrdinalIgnoreCase)) return bucket;
        string combined = $"{bucket} {label}".ToLowerInvariant();
        if (combined.Contains("session") || combined.Contains("five_hour") || combined.Contains("five hour")
            || combined.Contains("5h") || combined.Contains("5-hour")) return "5h";
        if (combined.Contains("week") || combined.Contains("seven_day") || combined.Contains("seven day")
            || combined.Contains("7-day") || combined.Contains("7d")) return "weekly";
        return bucket;
    }

    /// <summary>
    /// User-facing quota-window label. Claude's API calls its five-hour window "Session" and
    /// its general weekly window "Weekly All"; normalize those to the standard 5-hour / Weekly
    /// labels. Model-scoped weekly windows keep their descriptive label (for example, Fable).
    /// Resolve at render time so a language change is live.
    /// </summary>
    public string DisplayBucketLabel
    {
        get
        {
            if (!string.Equals(Provider, "claude", StringComparison.OrdinalIgnoreCase)) return BucketLabel;
            if (Bucket.StartsWith("weekly_scoped", StringComparison.OrdinalIgnoreCase)) return BucketLabel;
            return CanonicalBucket switch
            {
                "5h" => L10n.T("window_5h"),
                "weekly" => L10n.T("window_weekly"),
                _ => BucketLabel,
            };
        }
    }

    public string ResetsText
    {
        get
        {
            if (ResetsAt is null) return "";
            return ResetsTextForRemaining(ResetsAt.Value - DateTimeOffset.UtcNow);
        }
    }

    /// <summary>
    /// Relative reset text from remaining time, rounded down to the whole unit (matches the
    /// freshness footer's truncation). &lt;2h -> minutes (&lt;120); all longer windows -> hours.
    /// A past/stale ResetsAt (window already rolled over) degrades to "resets soon". Pure so
    /// it is unit-testable without a clock. Mirrors macOS resetsText(forRemaining:).
    /// </summary>
    public static string ResetsTextForRemaining(TimeSpan remaining)
    {
        double seconds = remaining.TotalSeconds;
        if (seconds < 60) return L10n.T("resets_soon");
        if (seconds < 7200)
        {
            int mins = (int)(seconds / 60);
            return L10n.T("resets_in_minutes", mins, mins == 1 ? "" : L10n.PluralS);
        }
        int hours = (int)(seconds / 3600);
        return L10n.T("resets_in_hours", hours, hours == 1 ? "" : L10n.PluralS);
    }
}
