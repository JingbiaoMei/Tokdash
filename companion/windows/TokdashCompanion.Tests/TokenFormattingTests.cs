using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Unit tests for the shared formatting/selection logic. The Win32 interop
/// itself is exercised by the manual spike; these tests pin the behavior of
/// the pure functions the flyout will bind to.
/// </summary>
[TestClass]
public class TokenFormattingTests
{
    // Contract §Token compact notation: the M tier trims a trailing ".0" (281M, not
    // 281.0M - v1.1 change) with no B tier even past 1000M; the k tier ROUNDS.
    [DataTestMethod]
    [DataRow(0L, "0")]
    [DataRow(999L, "999")]
    [DataRow(1_000L, "1k")]
    [DataRow(249_669L, "250k")]
    [DataRow(778_900L, "779k")]
    [DataRow(12_982_308L, "13M")]
    [DataRow(18_700_000L, "18.7M")]
    [DataRow(281_000_000L, "281M")]
    [DataRow(1_243_500_000L, "1243.5M")]
    public void CompactTokens_Formats_Correctly(long tokens, string expected)
    {
        Assert.AreEqual(expected, Formatter.CompactTokens(tokens));
    }

    [DataTestMethod]
    [DataRow(3.42, "$3.42")]
    [DataRow(0.06, "$0.06")]
    [DataRow(0.0, "$0.00")]
    [DataRow(149.23, "$149.23")]
    public void CostFormats_Two_Decimals(double cost, string expected)
    {
        Assert.AreEqual(expected, Formatter.FormatCost(cost));
    }

    // Contract §Active time - input milliseconds (fixture values -> shipped strings).
    [DataTestMethod]
    [DataRow(1_152_000L, "active 19 m")]
    [DataRow(45_000L, "active <1 m")]
    [DataRow(11_520_000L, "active 3 h 12 m")]
    [DataRow(188_400_000L, "active 2 d 4 h")]
    [DataRow(850_800_000L, "active 9 d 20 h")]
    [DataRow(6_411_600_000L, "active 74 d 5 h")]
    public void ActiveText_Ladder(long ms, string expected)
    {
        Assert.AreEqual(expected, Formatter.ActiveText(ms));
    }

    [TestMethod]
    public void ActiveText_Zero_Is_Absent_Not_Zero()
    {
        // The CALLER drops the segment for zero/null; the ladder itself never sees 0 in
        // the shipped pipeline, but it must degrade to the "<1 m" wording, not "0 m".
        Assert.AreEqual("active <1 m", Formatter.ActiveText(0));
    }

    [DataTestMethod]
    [DataRow(14.0, "low")]
    [DataRow(24.0, "low")]
    [DataRow(25.0, "mid")]
    [DataRow(49.0, "mid")]
    [DataRow(50.0, "fine")]
    [DataRow(71.0, "fine")]
    public void QuotaBarClass_Tiers_Remaining(double left, string expected)
    {
        Assert.AreEqual(expected, Formatter.QuotaBarClass(left));
    }

    // The worded comparison follows the selected segment now (word arg).
    [DataTestMethod]
    [DataRow(-12.0, "12% below yesterday")]
    [DataRow(8.0, "8% above yesterday")]
    [DataRow(0.0, "0% below yesterday")]
    [DataRow(null, "")]
    public void ComparisonText_Formats_Correctly(double? costPct, string expected)
    {
        Assert.AreEqual(expected, Formatter.ComparisonText(costPct, UsagePeriod.Today));
    }

    [TestMethod]
    public void ComparisonText_Follows_The_Period()
    {
        Assert.AreEqual("12% below last week", Formatter.ComparisonText(-12.4, UsagePeriod.Week));
        Assert.AreEqual("10% above last month", Formatter.ComparisonText(10.4, UsagePeriod.Month));
    }

    [DataTestMethod]
    [DataRow(-11.7, "▼", 12L)]
    [DataRow(-12.0, "▼", 12L)]
    [DataRow(-10.1, "▼", 10L)]
    [DataRow(-7.8, "▼", 8L)]
    [DataRow(-5.4, "▼", 5L)]
    [DataRow(3.2, "▲", 3L)]
    [DataRow(0.0, "±", 0L)]
    public void Delta_Glyph_And_Value(double pct, string glyph, long value)
    {
        Assert.AreEqual(glyph, Formatter.DeltaGlyph(pct));
        Assert.AreEqual(value, Formatter.DeltaValue(pct));
    }
}
