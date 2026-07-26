using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using TokdashCompanion.Interop;

namespace TokdashCompanion;

/// <summary>
/// WPF flyout window with Acrylic backdrop, positioned near the tray icon.
/// Light-dismiss on deactivate. Escape closes.
/// </summary>
public partial class FlyoutWindow : Window
{
    private bool _loaded;

    public CompanionStore Store { get; set; } = null!;

    public FlyoutWindow()
    {
        InitializeComponent();
        Loaded += FlyoutWindow_Loaded;
        Store.PropertyChanged += Store_PropertyChanged;
    }

    private void FlyoutWindow_Loaded(object sender, RoutedEventArgs e)
    {
        // Acrylic backdrop: apply via Win32 interop on Windows 11.
        // Falls back to the translucent solid background declared in XAML on older OS.
        try { ApplyAcrylic(); } catch { }
        _loaded = true;
        UpdateView();
    }

    private void ApplyAcrylic()
    {
        var hwnd = new System.Windows.Interop.WindowInteropHelper(this).Handle;
        var accent = new AccentPolicy { AccentState = 4, GradientColor = 0x99000000 }; // ACCENT_ENABLE_ACRYLICBLURBEHIND
        var accentSize = System.Runtime.InteropServices.Marshal.SizeOf(accent);
        var accentPtr = System.Runtime.InteropServices.Marshal.AllocHGlobal(accentSize);
        try
        {
            System.Runtime.InteropServices.Marshal.StructureToPtr(accent, accentPtr, false);
            var data = new WindowCompositionAttributeData
            {
                Attribute = 19, // WCA_ACCENT_POLICY
                Data = accentPtr,
                SizeOfData = accentSize,
            };
            Win32Acrylic.SetWindowCompositionAttribute(hwnd, ref data);
        }
        finally
        {
            System.Runtime.InteropServices.Marshal.FreeHGlobal(accentPtr);
        }
    }

    public void PositionNear(int x, int y)
    {
        Left = x - 360;
        Top = y;
    }

    private void FlyoutWindow_Deactivated(object sender, EventArgs e)
    {
        // Light dismiss when the flyout loses focus.
        if (_loaded) Close();
    }

    private void FlyoutWindow_KeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == System.Windows.Input.Key.Escape) Close();
    }

    private void Store_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e)
    {
        Dispatcher.BeginInvoke(UpdateView);
    }

    private void UpdateView()
    {
        if (Store == null) return;

        ConnDot.Fill = new SolidColorBrush((Color)ColorConverter.ConvertFromString(Store.DotColor));
        ConnText.Text = Store.ConnectionLabel;

        bool showBanner = Store.ShowsBanner;
        Banner.Visibility = showBanner ? Visibility.Visible : Visibility.Collapsed;
        SepBanner.Visibility = showBanner ? Visibility.Visible : Visibility.Collapsed;
        if (showBanner)
        {
            BannerIcon.Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString(
                Store.ConnectionState == ConnectionState.Offline ? "#FF453A" : "#FF9F0A"));
            BannerTitle.Text = Store.BannerTitle;
            BannerBody.Text = Store.BannerBody;
        }

        var snap = Store.Snapshot;
        if (snap is null && Store.ConnectionState == ConnectionState.Connecting)
        {
            TodayCost.Text = "…";
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
        ActivityText.Visibility = activity is null ? Visibility.Collapsed : Visibility.Visible;
        SepActivity.Visibility = activity is null ? Visibility.Collapsed : Visibility.Visible;
        ActivityText.Text = activity ?? "";

        FreshnessText.Text = Store.FreshnessText;

        // Dim last-good data when offline/busy.
        double opacity = (Store.ConnectionState == ConnectionState.Offline || Store.ConnectionState == ConnectionState.Busy) ? 0.45 : 1.0;
        HeroPanel.Opacity = opacity;
        QuotaPanel.Opacity = opacity;
    }

    private void RenderQuota(Snapshot snap)
    {
        QuotaRows.Children.Clear();
        UpdateToggleButtons();

        if (!snap.Quota.Enabled)
        {
            QuotaRows.Children.Add(new TextBlock
            {
                Text = "Subscription tracking is off",
                FontSize = 12.5,
                Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#5F5F5F")),
            });
            var openBtn = new Button { Content = "Open Dashboard", Margin = new Thickness(0, 4, 0, 0) };
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
                    Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#5F5F5F")),
                });
            }
            else
            {
                foreach (var row in low) QuotaRows.Children.Add(MakeQuotaRow(row, showProvider: true));
            }
        }
        else
        {
            var scroll = new ScrollViewer { MaxHeight = 172, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
            var panel = new StackPanel();
            foreach (var group in snap.AllQuotaGroups)
            {
                panel.Children.Add(new TextBlock
                {
                    Text = group.Provider,
                    FontSize = 11,
                    FontWeight = FontWeights.SemiBold,
                    Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#5F5F5F")),
                    Margin = new Thickness(0, 6, 0, 2),
                });
                foreach (var row in group.Rows) panel.Children.Add(MakeQuotaRow(row, showProvider: false));
            }
            scroll.Content = panel;
            QuotaRows.Children.Add(scroll);
        }
    }

    private UIElement MakeQuotaRow(QuotaRow row, bool showProvider)
    {
        var panel = new StackPanel { Margin = new Thickness(0, 0, 0, 8) };
        var top = new StackPanel { Orientation = Orientation.Horizontal };
        top.Children.Add(new TextBlock
        {
            Text = showProvider ? $"{row.Provider} · {row.BucketLabel}" : row.BucketLabel,
            FontSize = 12.5,
            FontWeight = FontWeights.Medium,
        });
        if (row.Estimated)
        {
            var estBorder = new Border
            {
                BorderBrush = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#5F5F5F")),
                BorderThickness = new Thickness(0.5),
                CornerRadius = new CornerRadius(4),
                Padding = new Thickness(4, 0, 4, 0),
                Margin = new Thickness(6, 0, 0, 0),
                Child = new TextBlock { Text = "Estimated", FontSize = 10.5 },
            };
            top.Children.Add(estBorder);
        }
        top.Children.Add(new TextBlock
        {
            Text = $"{(int)row.Left}% left",
            FontSize = 12.5,
            FontWeight = FontWeights.SemiBold,
            Margin = new Thickness(8, 0, 0, 0),
        });
        top.Children.Add(new TextBlock
        {
            Text = row.ResetsText,
            FontSize = 12,
            Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#5F5F5F")),
            Margin = new Thickness(8, 0, 0, 0),
        });
        panel.Children.Add(top);

        var bar = new ProgressBar
        {
            Value = row.Left,
            Maximum = 100,
            Height = 4,
            Margin = new Thickness(0, 5, 0, 0),
        };
        panel.Children.Add(bar);
        return panel;
    }

    private void UpdateToggleButtons()
    {
        LowBtn.FontWeight = Store.QuotaView == QuotaView.Low ? FontWeights.SemiBold : FontWeights.Normal;
        AllBtn.FontWeight = Store.QuotaView == QuotaView.All ? FontWeights.SemiBold : FontWeights.Normal;
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

    private void Gear_Click(object sender, RoutedEventArgs e) { /* Settings window deferred */ }

    private void Low_Click(object sender, RoutedEventArgs e)
    {
        Store.QuotaView = QuotaView.Low;
        if (Store.Snapshot is not null) RenderQuota(Store.Snapshot);
    }

    private void All_Click(object sender, RoutedEventArgs e)
    {
        Store.QuotaView = QuotaView.All;
        if (Store.Snapshot is not null) RenderQuota(Store.Snapshot);
    }
}
