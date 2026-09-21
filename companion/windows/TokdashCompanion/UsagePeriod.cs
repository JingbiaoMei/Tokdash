using System.Text.Json;
using System.Text.Json.Serialization;

namespace TokdashCompanion;

/// <summary>
/// The hero's period segment selection (contract §Period windows). Persisted on its own as
/// <c>selectedPeriod</c>; it is core hero furniture, never a Settings component. Week is a
/// CALENDAR window (<c>date_from</c> local Monday .. today) - <c>period=week</c> is a rolling
/// 7-day window and must never be sent.
/// </summary>
[JsonConverter(typeof(UsagePeriodJsonConverter))]
public enum UsagePeriod
{
    Today,
    Week,
    Month,
    Year,
}

public static class UsagePeriodExtensions
{
    public static string Token(this UsagePeriod period) => period switch
    {
        UsagePeriod.Week => "week",
        UsagePeriod.Month => "month",
        UsagePeriod.Year => "year",
        _ => "today",
    };

    /// <summary>Hero kicker above the cost number: TODAY / THIS WEEK / THIS MONTH / THIS YEAR.</summary>
    public static string KickerKey(this UsagePeriod period) => period switch
    {
        UsagePeriod.Week => "kicker_week",
        UsagePeriod.Month => "kicker_month",
        UsagePeriod.Year => "kicker_year",
        _ => "today",
    };

    /// <summary>Rank-strip suffix: "today" / "this week" / "this month" / "this year".</summary>
    public static string SuffixKey(this UsagePeriod period) => "suffix_" + period.Token();

    /// <summary>Delta-row sentence: "vs yesterday" / "vs last week" / ...</summary>
    public static string VsKey(this UsagePeriod period) => period switch
    {
        UsagePeriod.Week => "vs_last_week",
        UsagePeriod.Month => "vs_last_month",
        UsagePeriod.Year => "vs_last_year",
        _ => "vs_yesterday",
    };

    /// <summary>Cost-only comparison word: "yesterday" / "last week" / ...</summary>
    public static string WordKey(this UsagePeriod period) => period switch
    {
        UsagePeriod.Week => "word_last_week",
        UsagePeriod.Month => "word_last_month",
        UsagePeriod.Year => "word_last_year",
        _ => "word_yesterday",
    };

    /// <summary>Failure hero title: "Today's data unavailable" / "This week's data unavailable" / ...</summary>
    public static string UnavailableKey(this UsagePeriod period) => "unavailable_" + period.Token();

    /// <summary>Segment label: Today / Week / Month / Year.</summary>
    public static string SegmentKey(this UsagePeriod period) => "period_" + period.Token();
}

/// <summary>
/// Reads/writes <c>today|week|month|year</c> (contract spelling). A value written by a
/// FUTURE build (an unknown token) decodes to Today rather than poisoning the whole
/// settings file - same forward-tolerance rule as the component toggles.
/// </summary>
public sealed class UsagePeriodJsonConverter : JsonConverter<UsagePeriod>
{
    public override UsagePeriod Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        if (reader.TokenType == JsonTokenType.String)
        {
            switch (reader.GetString()?.ToLowerInvariant())
            {
                case "week": return UsagePeriod.Week;
                case "month": return UsagePeriod.Month;
                case "year": return UsagePeriod.Year;
                case "today": return UsagePeriod.Today;
            }
        }
        reader.Skip();
        return UsagePeriod.Today;
    }

    public override void Write(Utf8JsonWriter writer, UsagePeriod value, JsonSerializerOptions options) =>
        writer.WriteStringValue(value.Token());
}

/// <summary>
/// Settings schema v3 "Components" toggles (contract §Components and settings). Every key
/// defaults to ON; a whole MISSING components object (any v2 file) means all-on, so
/// upgrading never silently disables a shipped feature. Unknown keys are ignored in both
/// directions. The nullable backing keeps "absent" distinct from "explicitly false" on
/// read while Save always writes resolved, explicit values.
/// </summary>
public sealed class CompanionComponents
{
    [JsonPropertyName("fullDeltaRow")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? FullDeltaRow { get; set; }
    [JsonPropertyName("topRanks")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? TopRanks { get; set; }
    [JsonPropertyName("resetCredits")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ResetCredits { get; set; }
    [JsonPropertyName("activityGlance")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ActivityGlance { get; set; }
    [JsonPropertyName("activityHistogramTodayWeek")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? ActivityHistogramTodayWeek { get; set; }
    [JsonPropertyName("perServerRows")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? PerServerRows { get; set; }

    // Resolved reads: absent == on (the six defaults of the schema).
    public bool FullDeltaRowOn => FullDeltaRow ?? true;
    public bool TopRanksOn => TopRanks ?? true;
    public bool ResetCreditsOn => ResetCredits ?? true;
    public bool ActivityGlanceOn => ActivityGlance ?? true;
    public bool ActivityHistogramTodayWeekOn => ActivityHistogramTodayWeek ?? true;
    public bool PerServerRowsOn => PerServerRows ?? true;

    /// <summary>Snapshot of the current resolved state - the shape Snapshot carries so it
    /// stays renderable after the settings object moves on.</summary>
    public CompanionComponents Resolved() => new()
    {
        FullDeltaRow = FullDeltaRowOn,
        TopRanks = TopRanksOn,
        ResetCredits = ResetCreditsOn,
        ActivityGlance = ActivityGlanceOn,
        ActivityHistogramTodayWeek = ActivityHistogramTodayWeekOn,
        PerServerRows = PerServerRowsOn,
    };
}
