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
    string dir = costPct.Value <= 0 ? "below" : "above";
    return $"{(int)abs}% {dir} yesterday";
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
    public bool IsLow(QuotaThresholds t) => HasPercent && Left <= t.ThresholdFor(Bucket);

    public string ResetsText
    {
        get
        {
            if (ResetsAt is null) return "";
            var local = ResetsAt.Value.LocalDateTime;
            if (local.Date == DateTime.Today) return $"resets {local:HH:mm}";
            if (local.Date == DateTime.Today.AddDays(1)) return "resets tomorrow";
            bool inWeek = local.Date <= DateTime.Today.AddDays(7);
            return inWeek ? $"resets {local:ddd}" : $"resets {local:MMM d}";
        }
    }
}
