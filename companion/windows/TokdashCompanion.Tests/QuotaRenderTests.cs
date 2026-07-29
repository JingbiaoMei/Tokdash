using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Runtime smoke test for the DataTemplate-based quota row rendering introduced by the
/// visual refactor (QuotaRowVM/QuotaGroupVM + QuotaRowTemplate/QuotaGroupTemplate).
/// FlyoutLaunchTests never populates a Snapshot (Store stays Connecting, so UpdateView
/// returns before RenderQuota runs), so it never exercises this path. This test does, on
/// the real STA/Dispatcher path with a real FlyoutWindow, so a XAML binding problem (e.g.
/// the GridLength Star bindings on ColumnDefinition.Width, or a dangling DynamicResource)
/// surfaces as a real failure here instead of only at manual QA time.
/// </summary>
[TestClass]
public class QuotaRenderTests
{
    // Exercises: a normal percent bucket, a no-percent bucket (failed provider, last-known),
    // and Antigravity pooling - which produces the widest realistic row label the 340px
    // flyout has to fit ("Antigravity · Claude and GPT Models").
    private static QuotaResponse WideQuota() => new()
    {
        Enabled = true,
        Providers = new Dictionary<string, ProviderQuota>
        {
            ["codex"] = new()
            {
                Estimated = true,
                Buckets = new List<BucketQuota>
                {
                    new() { Bucket = "5h", BucketLabel = "5-hour window", RemainingPercent = 8 },
                    new() { Bucket = "weekly", BucketLabel = "Weekly window", RemainingPercent = 63 },
                },
            },
            ["minimax"] = new()
            {
                Status = "error",
                StatusDetail = "stale_token",
                Buckets = new List<BucketQuota>
                {
                    new() { Bucket = "5h", BucketLabel = "5-hour", RemainingPercent = null },
                },
            },
            ["antigravity"] = new()
            {
                Buckets = new List<BucketQuota>
                {
                    new() { Bucket = "claude-model", BucketLabel = "Claude", RemainingPercent = 4 },
                    new() { Bucket = "gpt-model", BucketLabel = "GPT", RemainingPercent = 19 },
                    new() { Bucket = "gemini-model", BucketLabel = "Gemini", RemainingPercent = 40 },
                },
            },
        },
    };

    [TestMethod]
    public void RenderQuota_LowAndAllViews_NoException_OnRealWindow()
    {
        Exception? caught = null;
        int lowCount = -1, allCount = -1;

        var t = new Thread(() =>
        {
            try
            {
                // No System.Windows.Application here deliberately: FlyoutWindow doesn't
                // touch Application.Current, and Application allows only one instance per
                // process - FlyoutLaunchTests already constructs one on its own STA thread,
                // and a second `new App()` throws "Cannot create more than one Application
                // instance in the same AppDomain" when both tests share a process.
                var client = new FakeClient { Quota = WideQuota() };
                var store = new CompanionStore(client);
                store.Settings.Thresholds = new QuotaThresholds(100, 100, 100); // force everything "low"
                store.RefreshAsync().GetAwaiter().GetResult();
                Assert.IsNotNull(store.Snapshot);
                Assert.IsFalse(store.Snapshot!.QuotaFailed, "the quota endpoint itself succeeded");

                var flyout = new FlyoutWindow { Store = store };
                flyout.Show();
                Pump();
                lowCount = flyout.QuotaRows.Items.Count;

                // QuotaView's setter raises PropertyChanged -> Store_PropertyChanged ->
                // Dispatcher.BeginInvoke(UpdateView); pump so RenderQuota actually runs
                // before reading Items again.
                store.QuotaView = QuotaView.All;
                Pump();
                allCount = flyout.QuotaRows.Items.Count;

                flyout.Close();
                Pump();
            }
            catch (Exception ex)
            {
                caught = ex;
            }
            finally
            {
                Dispatcher.CurrentDispatcher.BeginInvokeShutdown(DispatcherPriority.Background);
                Dispatcher.Run();
            }
        });
        t.SetApartmentState(ApartmentState.STA);
        t.Start();
        t.Join();

        Assert.IsNull(caught, $"Quota rendering threw: {caught}");
        Assert.IsTrue(lowCount > 0, "Low view should have rendered at least one row");
        Assert.IsTrue(allCount > 0, "All view should have rendered at least one group");
    }

    /// <summary>
    /// The rows must actually contain their text and bar geometry - not merely exist.
    /// A WPF binding failure is SILENT: {Binding Label} against an unreachable property
    /// renders an empty TextBlock and only writes a trace warning, so a test that asserts
    /// Items.Count > 0 passes with every row rendered blank. That is exactly the blank-
    /// flyout symptom this refactor must not reintroduce, so assert on realised visuals:
    /// the label text, the "% left" text, and the star widths the bar fill is sized by
    /// (ColumnDefinition is a FrameworkContentElement, and inheriting DataContext into it
    /// is the single most fragile binding in the new template).
    /// </summary>
    [TestMethod]
    public void RenderQuota_LowView_BindingsResolve_ToRealTextAndBarGeometry()
    {
        Exception? caught = null;
        List<string> texts = new();
        List<(double Fill, double Remainder)> bars = new();
        double longestLabelActualWidth = 0;
        double longestLabelDesiredWidth = 0;
        double flyoutBottom = 0;
        double workAreaBottom = 0;

        var t = new Thread(() =>
        {
            try
            {
                var client = new FakeClient { Quota = WideQuota() };
                var store = new CompanionStore(client);
                store.Settings.Thresholds = new QuotaThresholds(100, 100, 100);
                store.RefreshAsync().GetAwaiter().GetResult();

                var flyout = new FlyoutWindow { Store = store };
                flyout.Show();
                Pump();
                // Templates only expand once the ItemsControl has been measured.
                flyout.UpdateLayout();
                Pump();

                texts = Descendants<TextBlock>(flyout.QuotaRows)
                    .Select(tb => tb.Text)
                    .Where(s => !string.IsNullOrEmpty(s))
                    .ToList();

                bars = Descendants<Grid>(flyout.QuotaRows)
                    .Where(g => g.ColumnDefinitions.Count == 2
                             && g.ColumnDefinitions.All(c => c.Width.IsStar))
                    .Select(g => (g.ColumnDefinitions[0].Width.Value, g.ColumnDefinitions[1].Width.Value))
                    .ToList();

                var longestLabel = Descendants<TextBlock>(flyout.QuotaRows)
                    .Single(tb => tb.Text == "Antigravity · Claude and GPT Models");
                longestLabelActualWidth = longestLabel.ActualWidth;
                // Re-measure without a width constraint to recover the text's natural width.
                // DesiredSize from the normal layout is already capped by the Grid and cannot
                // distinguish a fitted label from one silently ellipsized by TextTrimming.
                longestLabel.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
                longestLabelDesiredWidth = longestLabel.DesiredSize.Width;
                flyoutBottom = flyout.Top + flyout.ActualHeight;
                workAreaBottom = SystemParameters.WorkArea.Bottom;

                flyout.Close();
                Pump();
            }
            catch (Exception ex) { caught = ex; }
            finally
            {
                Dispatcher.CurrentDispatcher.BeginInvokeShutdown(DispatcherPriority.Background);
                Dispatcher.Run();
            }
        });
        t.SetApartmentState(ApartmentState.STA);
        t.Start();
        t.Join();

        Assert.IsNull(caught, $"Quota rendering threw: {caught}");
        string dump = string.Join(" | ", texts);

        // {Binding Label} reached the VM.
        Assert.IsTrue(texts.Any(s => s.Contains("Codex") && s.Contains("5-hour")),
            $"no bound row label rendered - bindings did not resolve. Rendered: [{dump}]");
        // {Binding PercentText} reached the VM.
        Assert.IsTrue(texts.Any(s => s.Contains("% left")),
            $"no bound percentage rendered. Rendered: [{dump}]");
        // The Estimated pill's static text still sits inside the template.
        Assert.IsTrue(texts.Any(s => s == "Estimated"),
            $"Estimated badge missing. Rendered: [{dump}]");

        // {Binding FillStar/RestStar} reached ColumnDefinition.Width. Codex's 8%-left
        // bucket must produce an 8/92 split; a failed binding leaves both at 1*.
        Assert.IsTrue(bars.Count > 0, "no quota bar geometry found");
        Assert.IsTrue(bars.Any(b => System.Math.Abs(b.Fill - 8) < 0.001 && System.Math.Abs(b.Remainder - 92) < 0.001),
            "bar star widths did not bind - the fill would render at the wrong size. Got: "
            + string.Join(", ", bars.Select(b => $"{b.Fill}*/{b.Remainder}*")));
        // A bucket with no remaining_percent renders no bar at all.
        Assert.IsFalse(texts.Any(s => s.Contains("Minimax") && s.Contains("% left")),
            $"a bucket without remaining_percent must not show a percentage. Rendered: [{dump}]");
        Assert.IsTrue(longestLabelActualWidth >= longestLabelDesiredWidth - 0.5,
            $"longest quota label is trimmed: available {longestLabelActualWidth:F1}px, "
            + $"needed {longestLabelDesiredWidth:F1}px");
        Assert.IsTrue(flyoutBottom <= workAreaBottom + 0.5,
            $"flyout extends below the work area after templates render: bottom "
            + $"{flyoutBottom:F1}px, work area {workAreaBottom:F1}px");
    }

    private static IEnumerable<T> Descendants<T>(DependencyObject root) where T : DependencyObject
    {
        int n = VisualTreeHelper.GetChildrenCount(root);
        for (int i = 0; i < n; i++)
        {
            var child = VisualTreeHelper.GetChild(root, i);
            if (child is T hit) yield return hit;
            foreach (var deeper in Descendants<T>(child)) yield return deeper;
        }
    }

    /// <summary>Process queued Dispatcher work until idle (mirrors FlyoutLaunchTests.Pump).</summary>
    private static void Pump()
    {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }
}
