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
    [DataTestMethod]
    [DataRow(0L, "0")]
    [DataRow(999L, "999")]
    [DataRow(249669L, "249k")]
    [DataRow(18_700_000L, "18.7M")]
    [DataRow(281_000_000L, "281.0M")]
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

    [DataTestMethod]
    [DataRow(-12.0, "12% below yesterday")]
    [DataRow(8.0, "8% above yesterday")]
    [DataRow(0.0, "0% below yesterday")]
    [DataRow(null, "")]
    public void ComparisonText_Formats_Correctly(double? costPct, string expected)
    {
        Assert.AreEqual(expected, Formatter.ComparisonText(costPct));
    }
}
