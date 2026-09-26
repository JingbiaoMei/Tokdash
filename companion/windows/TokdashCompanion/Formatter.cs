namespace TokdashCompanion;

/// <summary>
/// Pure formatting/selection helpers shared between the flyout and tests.
/// Mirrors the macOS Snapshot logic so both platforms stay aligned.
/// </summary>
public static class Formatter
{
    /// <summary>
    /// Token compact notation (contract §Token compact notation): >= 1B -> one decimal with
    /// the trailing ".0" TRIMMED ("1.2B", "75B"); >= 1M -> same rule with "M" ("13M", "18.7M");
    /// >= 1k -> integer "k" ROUNDED (not floored) to the shown precision ("779k", "250k");
    /// below that, the plain integer. The same rule renders hero, top-rank and per-server
    /// tokens; exact values belong to accessibility text, not to this string.
    /// </summary>
    public static string CompactTokens(long tokens)
    {
        if (tokens >= 1_000_000_000)
        {
            string bText = (tokens / 1_000_000_000.0).ToString("F1", System.Globalization.CultureInfo.InvariantCulture);
            if (bText.EndsWith(".0", StringComparison.Ordinal)) bText = bText[..^2];
            return bText + "B";
        }
        if (tokens >= 1_000_000)
        {
            string text = (tokens / 1_000_000.0).ToString("F1", System.Globalization.CultureInfo.InvariantCulture);
            if (text.EndsWith(".0", StringComparison.Ordinal)) text = text[..^2];
            return text + "M";
        }
        if (tokens >= 1_000)
            return ((long)Math.Round(tokens / 1000.0, MidpointRounding.AwayFromZero)).ToString() + "k";
        return tokens.ToString();
    }

    public static string FormatCost(double cost) => cost.ToString("C2", System.Globalization.CultureInfo.GetCultureInfo("en-US"));

    public static string QuotaBarClass(double leftPercent) => leftPercent switch
    {
        < 25 => "low",
        < 50 => "mid",
        _ => "fine",
    };

    /// <summary>
    /// Active-time ladder (contract §Active time), input MILLISECONDS (every duration field
    /// in the payload is ms; every epoch in the quota payload is seconds). Zero and absent
    /// data render NO segment at all (the caller checks) - never "active 0 m". Each part
    /// floors to its own unit.
    /// </summary>
    public static string ActiveText(long activeMs)
    {
        long seconds = activeMs / 1000;
        if (seconds < 60) return L10n.T("active_label", L10n.T("dur_lt1m"));
        if (seconds < 3600) return L10n.T("active_label", L10n.T("dur_m", seconds / 60));
        if (seconds < 86_400) return L10n.T("active_label", L10n.T("dur_hm", seconds / 3600, (seconds % 3600) / 60));
        return L10n.T("active_label", L10n.T("dur_dh", seconds / 86_400, (seconds % 86_400) / 3600));
    }

    /// <summary>Delta-row glyph: ▲ above, ▼ below, ± exactly flat.</summary>
    public static string DeltaGlyph(double pct) => pct > 0 ? "▲" : (pct < 0 ? "▼" : "±");

    /// <summary>Delta-row value: abs(round(pct)) - -11.7 renders 12.</summary>
    public static long DeltaValue(double pct) => (long)Math.Round(Math.Abs(pct), MidpointRounding.AwayFromZero);

    /// <summary>
    /// The shipped single comparison line, cost-only and worded ("12% below yesterday") -
    /// what the hero shows with the fullDeltaRow toggle off (1.0.2 behavior, except the
    /// sentence now follows the selected segment like every other period string).
    /// </summary>
    public static string ComparisonText(double? costPct, UsagePeriod period)
    {
        if (costPct is null) return "";
        // The worded line truncates (1.0.2 behavior); only the delta row's {pct} rounds.
        int abs = (int)Math.Abs(costPct.Value);
        string word = L10n.T(period.WordKey());
        return costPct.Value <= 0 ? L10n.T("comparison_below", abs, word) : L10n.T("comparison_above", abs, word);
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
    bool Failed = false,
    DateTimeOffset? CapturedAt = null)
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
            if (feature.Length > 0) s = $"{feature} · {NormalizeWindow(parts[1])}";
            return s;
        }
        return NormalizeWindow(s);
    }

    /// <summary>
    /// The weekly window is named inconsistently across providers: Codex's 7d bucket label
    /// is "7-day window" while MiniMax/Kimi/Grok already send "Weekly". Normalize the bare
    /// "7-day"/"7 day"/"7d" window token to "Weekly" so every weekly window reads the same
    /// everywhere (contract §Low/All labels). Never touches compound feature labels.
    /// </summary>
    static string NormalizeWindow(string token)
    {
        string t = token.Trim();
        if (t.Equals("7-day", StringComparison.OrdinalIgnoreCase)
            || t.Equals("7 day", StringComparison.OrdinalIgnoreCase)
            || t.Equals("7d", StringComparison.OrdinalIgnoreCase))
            return "Weekly";
        return token;
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
        provider = provider.Split(" · ").Last();
        if (!string.Equals(provider, "claude", StringComparison.OrdinalIgnoreCase)) return bucket;
        string combined = $"{bucket} {label}".ToLowerInvariant();
        if (combined.Contains("session") || combined.Contains("five_hour") || combined.Contains("five hour")
            || combined.Contains("5h") || combined.Contains("5-hour")) return "5h";
        if (combined.Contains("week") || combined.Contains("seven_day") || combined.Contains("seven day")
            || combined.Contains("7-day") || combined.Contains("7d")) return "weekly";
        return bucket;
    }

    /// <summary>
    /// User-facing quota-window label. Claude names its five-hour window "Session" and its
    /// general weekly window "Weekly All"; only those two get the standard 5-hour / Weekly
    /// wording. Everything else passes through with the server's own wording - including a
    /// plain "weekly" bucket, which the contract's expected fixture pins verbatim ("weekly",
    /// not the forced "Weekly"). Model-scoped weekly windows keep their descriptive label
    /// (for example, Fable). Resolve at render time so a language change is live.
    /// </summary>
    public string DisplayBucketLabel
    {
        get
        {
            if (string.Equals(Provider, "antigravity", StringComparison.OrdinalIgnoreCase))
            {
                // Pooled rows carry the (short) pool name as BucketLabel; append the
                // auto-determined window so the bar reads "Gemini · Weekly". Mirrors the web
                // dashboard's pool subtitle + window label in one narrow-flyout label.
                return $"{BucketLabel} · {AntigravityWindowLabel}";
            }
            if (!string.Equals(Provider, "claude", StringComparison.OrdinalIgnoreCase)) return BucketLabel;
            if (Bucket.StartsWith("weekly_scoped", StringComparison.OrdinalIgnoreCase)) return BucketLabel;
            string id = Bucket.ToLowerInvariant();
            if (id.Contains("session") || id.Contains("five hour") || id.Contains("five_hour")
                || id.Contains("5-hour") || id.Contains("5h"))
                return L10n.T("window_5h");
            if ($"{Bucket} {BucketLabel}".ToLowerInvariant().Replace('_', ' ').Contains("weekly all"))
                return L10n.T("window_weekly");
            return BucketLabel;
        }
    }

    /// <summary>
    /// Antigravity's API returns a single window per model - whichever (5-hour or weekly)
    /// currently binds the pool - with no explicit duration field, so the window is inferred
    /// from the reset time. A 5-hour window can never reset more than 5h out, so a reset
    /// beyond the threshold is weekly. The reverse is imperfect: a weekly window in its final
    /// &lt;8h also reads as "5-hour" (self-correcting after the reset; the API exposes no
    /// duration field to disambiguate it). Measured from <see cref="CapturedAt"/> (stable;
    /// matches the web dashboard) with a <c>DateTimeOffset.UtcNow</c> fallback for older
    /// servers. Mirrors antigravityWindowLabel in the web dashboard.
    /// </summary>
    public string AntigravityWindowLabel
    {
        get
        {
            if (ResetsAt is null) return L10n.T("window_5h");
            double remaining = CapturedAt is not null
                ? (ResetsAt.Value - CapturedAt.Value).TotalSeconds
                : (ResetsAt.Value - DateTimeOffset.UtcNow).TotalSeconds;
            return AntigravityWindowLabelForRemaining(remaining);
        }
    }

    // 8h gives skew/rounding margin above the 5h window max before treating a reset as weekly;
    // a weekly window in its final <8h is mislabeled "5-hour" (self-correcting after the reset).
    public static string AntigravityWindowLabelForRemaining(double secondsRemaining) =>
        secondsRemaining > 8 * 3600 ? L10n.T("window_weekly") : L10n.T("window_5h");

    /// <summary>
    /// Mixed reset text (per UI review): a window closing within ~24h gets the relative
    /// countdown ("resets in 3 h" - actionable while it matters), a farther one gets the
    /// absolute local time ("resets Thu 02:00" - "in 4 days" from a stale refresh is just
    /// noise). Mirrors macOS resetsText(for:).
    /// </summary>
    public string ResetsText
    {
        get
        {
            if (ResetsAt is null) return "";
            var remaining = ResetsAt.Value - DateTimeOffset.UtcNow;
            return remaining.TotalSeconds < 86400
                ? ResetsTextForRemaining(remaining)
                : ResetsTextAbsolute(ResetsAt.Value);
        }
    }

    /// <summary>Absolute form for far windows: local weekday + time in the app language
    /// ("resets Thu 02:00" / "将于 周四 02:00 重置").</summary>
    public static string ResetsTextAbsolute(DateTimeOffset resetsAt)
    {
        var local = resetsAt.ToLocalTime();
        return L10n.T("resets_at", local.ToString("ddd HH:mm", L10n.Culture));
    }

    /// <summary>
    /// Relative reset text from remaining time, rounded down to the whole unit (matches the
    /// freshness footer's truncation). &lt;2h -> minutes (&lt;120); &lt;1d -> hours; longer ->
    /// days, so a weekly window reads "resets in 3 days" rather than "resets in 94 hours".
    /// A single unit throughout (no "3d 22h" combinations). A past/stale ResetsAt (window
    /// already rolled over) degrades to "resets soon". Pure so it is unit-testable without a
    /// clock. Mirrors macOS resetsText(forRemaining:) and formatResetCountdownFromSeconds in
    /// the web dashboard — the tier boundaries must stay in lockstep across all three or the
    /// same window reads differently on each surface.
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
        if (seconds < 86400)
        {
            int hours = (int)(seconds / 3600);
            return L10n.T("resets_in_hours", hours, hours == 1 ? "" : L10n.PluralS);
        }
        int days = (int)(seconds / 86400);
        return L10n.T("resets_in_days", days, days == 1 ? "" : L10n.PluralS);
    }
}
