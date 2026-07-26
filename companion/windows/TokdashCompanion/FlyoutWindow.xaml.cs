using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.Graphics;

namespace TokdashCompanion;

/// <summary>
/// WinUI 3 flyout window with Acrylic backdrop, positioned near the tray icon.
/// Light-dismiss on deactivate. Escape closes.
/// </summary>
public sealed partial class FlyoutWindow : Window
{
    private bool _loaded;

    public required CompanionStore Store { get; set; }

    public FlyoutWindow()
    {
        InitializeComponent();
        Title = "Tokdash";
        // No taskbar button, no system caption, stays-on-top flyout.
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindowHandle(hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);
        appWindow.SetPresenter(AppWindowPresenterKind.Overlapped);
        if (appWindow.Presenter is OverlappedPresenter p)
        {
            p.IsAlwaysOnTop = true;
            p.SetBorderAndTitleBar(true, false);
            p.IsMinimizable = false;
            p.IsMaximizable = false;
            p.IsResizable = false;
        }
        Activated += FlyoutWindow_Activated;
        Store.PropertyChanged += Store_PropertyChanged;
    }

    public void PositionNear(int x, int y)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindowHandle(hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);
        appWindow.MoveAndResize(new RectInt32(x - 360, y, 360, 560));
    }

    private void FlyoutWindow_Activated(object sender, WindowActivatedEventArgs e)
    {
        if (e.WindowActivationState == WindowActivationState.Deactivated && _loaded)
        {
            // Light dismiss when the flyout loses focus.
            Close();
        }
        _loaded = true;
    }

    private void FlyoutWindow_Closed(object sender, WindowEventArgs e) { }

    private void Store_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e)
    {
        DispatcherQueue.TryEnqueue(UpdateView);
    }

    private void UpdateView()
    {
        ConnDot.Fill = new SolidColorBrush(Microsoft.UI.Xaml.MediaHelper.ColorFromString(Store.DotColor));

        bool showBanner = Store.ShowsBanner;
        Banner.Visibility = showBanner ? Visibility.Visible : Visibility.Collapsed;
        if (showBanner)
        {
            BannerIcon.Foreground = new SolidColorBrush(Microsoft.UI.Xaml.MediaHelper.ColorFromString(
                Store.ConnectionState == ConnectionState.Offline ? "#FF453A" : "#FF9F0A"));
        }

        var snap = Store.Snapshot;
        if (snap is null && Store.ConnectionState == ConnectionState.Connecting)
        {
            TodayCost.Text = "…";
            TodaySub.Text = "";
            TodayCmp.Text = "";
            MonthLabel.Text = "";
            MonthCost.Text = "";
            MonthTokens.Text = "";
            return;
        }

        if (snap is null) return;

        if (snap.Today.TotalTokens == 0)
        {
            TodayCost.Text = "No usage recorded today";
            TodayCost.FontSize = 14;
            TodaySub.Text = "Tokdash is running. Today's totals will appear as tools report usage.";
            TodayCmp.Text = "";
        }
        else
        {
            TodayCost.Text = snap.TodayCostText;
            TodayCost.FontSize = 30;
            TodaySub.Text = $"{snap.TodayTokensCompact} tokens · {snap.Today.TotalMessages} messages";
            TodayCmp.Text = snap.ComparisonText ?? "";
        }

        MonthLabel.Text = snap.MonthLabel;
        MonthCost.Text = snap.MonthCostText;
        MonthTokens.Text = $"{snap.MonthTokensCompact} tokens";

        RenderQuota(snap);

        var activity = snap.ActivityText;
        ActivityBorder.Visibility = activity is null ? Visibility.Collapsed : Visibility.Visible;
        ActivityText.Text = activity ?? "";

        FreshnessText.Text = Store.FreshnessText;
    }

    private void RenderQuota(Snapshot snap)
    {
        QuotaRows.Children.Clear();
        if (!snap.Quota.Enabled)
        {
            QuotaRows.Children.Add(new TextBlock
            {
                Text = "Subscription tracking is off",
                FontSize = 12.5,
                Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"],
            });
            var openBtn = new HyperlinkButton { Content = "Open Dashboard" };
            openBtn.Click += (s, e) => OpenDashboard_Click(s, e);
            QuotaRows.Children.Add(openBtn);
            QuotaHeader.Text = "SUBSCRIPTION";
            return;
        }

        QuotaHeader.Text = Store.QuotaView == QuotaView.Low ? "SUBSCRIPTION" : "ALL SUBSCRIPTIONS";

        if (Store.QuotaView == QuotaView.Low)
        {
            var low = snap.LowQuotaRows;
            if (low.Count == 0)
            {
                QuotaRows.Children.Add(new TextBlock
                {
                    Text = "No subscription window is below its alert threshold.",
                    FontSize = 12.5,
                    Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"],
                });
            }
            else
            {
                foreach (var row in low) QuotaRows.Children.Add(MakeQuotaRow(row, showProvider: true));
            }
        }
        else
        {
            var scroll = new ScrollViewer
            {
                MaxHeight = 172,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            };
            var panel = new StackPanel { Spacing = 8 };
            foreach (var group in snap.AllQuotaGroups)
            {
                panel.Children.Add(new TextBlock
                {
                    Text = group.Provider,
                    FontSize = 11,
                    FontWeight = Microsoft.UI.Text.FontWeight.SemiBold,
                    Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"],
                });
                foreach (var row in group.Rows) panel.Children.Add(MakeQuotaRow(row, showProvider: false));
            }
            scroll.Content = panel;
            QuotaRows.Children.Add(scroll);
        }
    }

    private UIElement MakeQuotaRow(QuotaRow row, bool showProvider)
    {
        var panel = new StackPanel { Spacing = 5 };
        var top = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        top.Children.Add(new TextBlock
        {
            Text = showProvider ? $"{row.Provider} · {row.BucketLabel}" : row.BucketLabel,
            FontSize = 12.5,
            FontWeight = Microsoft.UI.Text.FontWeight.Medium,
        });
        if (row.Estimated)
        {
            top.Children.Add(new Border
            {
                BorderBrush = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"],
                BorderThickness = new Thickness(0.5),
                CornerRadius = new CornerRadius(4),
                Padding = new Thickness(4, 0, 4, 0),
                Child = new TextBlock { Text = "Estimated", FontSize = 10.5 },
            });
        }
        top.Children.Add(new TextBlock
        {
            Text = $"{(int)row.Left}% left",
            FontSize = 12.5,
            FontWeight = Microsoft.UI.Text.FontWeight.SemiBold,
        });
        top.Children.Add(new TextBlock
        {
            Text = row.ResetsText,
            FontSize = 12,
            Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"],
        });
        panel.Children.Add(top);

        var bar = new ProgressBar
        {
            Value = row.Left,
            Maximum = 100,
            Height = 4,
            CornerRadius = new CornerRadius(2),
        };
        panel.Children.Add(bar);
        return panel;
    }

    private void OpenDashboard_Click(object sender, RoutedEventArgs e)
    {
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName = "http://127.0.0.1:55423/",
            UseShellExecute = true,
        });
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e) => await Store.RefreshAsync();

    private void Gear_Click(object sender, RoutedEventArgs e)
    {
        // Settings window deferred; for now toggle Quit via context menu.
    }

    private void QuotaToggle_Toggled(object sender, RoutedEventArgs e)
    {
        Store.QuotaView = QuotaToggle.IsOn ? QuotaView.All : QuotaView.Low;
        if (Store.Snapshot is not null) RenderQuota(Store.Snapshot);
    }
}
