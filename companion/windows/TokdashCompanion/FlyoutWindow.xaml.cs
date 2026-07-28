using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using Microsoft.Win32;
using TokdashCompanion.Interop;

namespace TokdashCompanion;

/// <summary>
/// WPF flyout window with Acrylic backdrop, positioned near the tray icon.
/// Light-dismiss on deactivate. Escape closes.
/// </summary>
public partial class FlyoutWindow : Window
{
    private bool _loaded;
    private bool _highContrast;
    private bool _dark;
    private int _anchorX, _anchorY;

    public CompanionStore Store { get; set; } = null!;

    public FlyoutWindow()
    {
        InitializeComponent();
        Loaded += FlyoutWindow_Loaded;
        Closed += FlyoutWindow_Closed;
    }

    private void FlyoutWindow_Loaded(object sender, RoutedEventArgs e)
    {
        // Subscribe now - Store is assigned via object initializer after ctor.
        Store.PropertyChanged += Store_PropertyChanged;
        // Apply the system theme (light / dark / high-contrast) before rendering.
        ApplyTheme();
        // Acrylic backdrop via Win32 interop on Windows 11. Skipped for high-contrast
        // (solid fallback) where translucency hurts readability.
        if (!_highContrast) { try { ApplyAcrylic(_dark ? ColorFromHex("#26292F") : ColorFromHex("#F2F4F7")); } catch { } }
        _loaded = true;
        UpdateView();
        // Final placement needs the measured ActualHeight; refine now.
        ApplyPosition();
        // Re-position on DPI changes while open (mixed-DPI).
        var src = System.Windows.Interop.HwndSource.FromHwnd(new System.Windows.Interop.WindowInteropHelper(this).Handle);
        src?.AddHook(WndProcHook);
    }

    private void FlyoutWindow_Closed(object? sender, EventArgs e)
    {
        if (_loaded) Store.PropertyChanged -= Store_PropertyChanged;
        _loaded = false;
    }

    private void ApplyAcrylic(Color tint)
    {
        var hwnd = new System.Windows.Interop.WindowInteropHelper(this).Handle;
        // GradientColor is 0xAABBGGRR; tint the blur to match the chosen background.
        uint abgr = (uint)((0x99 << 24) | (tint.B << 16) | (tint.G << 8) | tint.R);
        var accent = new AccentPolicy { AccentState = 4, GradientColor = abgr }; // ACCENT_ENABLE_ACRYLICBLURBEHIND
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
        _anchorX = x;
        _anchorY = y;
        // Rough placement before Loaded measures the height (avoids a (0,0) flash);
        // ApplyPosition() refines against the work area, taskbar edge, and DPI.
        Left = x - Width;
        Top = y - 60;
    }

    /// <summary>
    /// Anchor the flyout to the notification-area work area using the target monitor's
    /// DPI (not the window's, which can be wrong before the window is positioned there).
    /// Handles top/bottom/left/right taskbars, clamps to the work area, and works in
    /// WPF device-independent pixels.
    /// </summary>
    private void ApplyPosition()
    {
        IntPtr hMon = MonitorFromPoint(new POINT { X = _anchorX, Y = _anchorY }, MONITOR_DEFAULTTONEAREST);
        var mi = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
        GetMonitorInfoW(hMon, ref mi);

        GetDpiForMonitor(hMon, 0, out uint dpiX, out uint dpiY);
        double scaleX = dpiX / 96.0;
        double scaleY = dpiY / 96.0;

        var (leftDip, topDip) = ComputeFlyoutPosition(
            _anchorX, _anchorY,
            mi.rcMonitor.Left, mi.rcMonitor.Top, mi.rcMonitor.Right,
            mi.rcWork.Left, mi.rcWork.Top, mi.rcWork.Right, mi.rcWork.Bottom,
            scaleX, scaleY,
            ActualWidth, ActualHeight);
        Left = leftDip;
        Top = topDip;
    }

    /// <summary>
    /// Pure flyout placement in WPF device-independent pixels. Detects the taskbar edge
    /// from the gap between the monitor's full rect and its work area, anchors beside a
    /// side taskbar (otherwise opens to the left of the click), pushes below a top
    /// taskbar (otherwise above the bottom edge), and clamps into the work area. Takes
    /// raw pixel rects + DPI scale so the per-monitor DPI math is exercised by tests.
    /// </summary>
    internal static (double LeftDip, double TopDip) ComputeFlyoutPosition(
        int anchorX, int anchorY,
        int fullLeft, int fullTop, int fullRight,
        int workLeft, int workTop, int workRight, int workBottom,
        double scaleX, double scaleY,
        double widthDip, double heightDip, double margin = 8)
    {
        double sx = scaleX == 0 ? 1 : scaleX;
        double sy = scaleY == 0 ? 1 : scaleY;
        double workLeftDip = workLeft / sx, workRightDip = workRight / sx;
        double workTopDip = workTop / sy, workBottomDip = workBottom / sy;
        double fullLeftDip = fullLeft / sx, fullRightDip = fullRight / sx;
        double fullTopDip = fullTop / sy;

        bool topBar = workTopDip > fullTopDip + 0.5;
        bool leftBar = workLeftDip > fullLeftDip + 0.5;
        bool rightBar = workRightDip < fullRightDip - 0.5;

        // Horizontal: anchor beside a side taskbar; otherwise open to the left of the click.
        double leftDip;
        if (leftBar) leftDip = workLeftDip + margin;
        else if (rightBar) leftDip = workRightDip - widthDip - margin;
        else leftDip = (anchorX / sx) - widthDip - margin;
        leftDip = Math.Max(leftDip, workLeftDip);
        leftDip = Math.Min(leftDip, workRightDip - widthDip);

        // Vertical: against the taskbar edge (top taskbar pushes down, otherwise up).
        double topDip = topBar ? workTopDip + margin : workBottomDip - heightDip - margin;
        return (leftDip, topDip);
    }

    private IntPtr WndProcHook(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        // Re-position if the effective DPI changes while the flyout is open (mixed-DPI).
        if (msg == 0x02E0 /* WM_DPICHANGED */) ApplyPosition();
        return IntPtr.Zero;
    }

    [DllImport("shcore.dll")]
    private static extern int GetDpiForMonitor(IntPtr hMonitor, int dpiType, out uint dpiX, out uint dpiY);

    /// <summary>Detect light / dark / high-contrast and swap the theme brushes.</summary>
    private void ApplyTheme()
    {
        _highContrast = SystemParameters.HighContrast;
        _dark = !_highContrast && IsDarkMode();

        if (_highContrast)
        {
            SetBrush("FlyoutBg", SystemColors.WindowBrush);
            SetBrush("TextBrush", SystemColors.WindowTextBrush);
            SetBrush("MutedBrush", SystemColors.WindowTextBrush);
            SetBrush("FaintBrush", SystemColors.GrayTextBrush);
            SetBrush("DividerBrush", SystemColors.ControlDarkBrush);
            SetBrush("BtnBg", SystemColors.ControlBrush);
            SetBrush("BtnBorder", SystemColors.ControlDarkBrush);
            SetBrush("BtnText", SystemColors.WindowTextBrush);
            SetBrush("PrimaryBg", SystemColors.ControlTextBrush);
            SetBrush("PrimaryText", SystemColors.WindowBrush);
            SetBrush("IconBtnHover", SystemColors.HighlightBrush);
            SetBrush("SegBg", SystemColors.ControlLightBrush);
            SetBrush("SegIdleBg", Brushes.Transparent);
            SetBrush("SegSelBg", SystemColors.ControlBrush);
            SetBrush("SegSelText", SystemColors.WindowTextBrush);
            SetBrush("SegText", SystemColors.WindowTextBrush);
            SetBrush("FooterBg", SystemColors.ControlLightBrush);
            SetBrush("FooterBorder", SystemColors.ControlDarkBrush);
            SetBrush("BarTrack", SystemColors.ControlDarkBrush);
            RootBg.Color = SystemColors.WindowColor;
            RootBg.Opacity = 1;
        }
        else if (_dark)
        {
            SetBrush("FlyoutBg", HexBrush("#26292F", 0.92));
            SetBrush("TextBrush", HexBrush("#F3F3F3"));
            SetBrush("MutedBrush", HexBrush("#B0B0B0"));
            SetBrush("FaintBrush", HexBrush("#9A9A9A"));
            SetBrush("DividerBrush", HexBrush("#FFFFFF", 0.10));
            SetBrush("BtnBg", HexBrush("#FFFFFF", 0.09));
            SetBrush("BtnBorder", HexBrush("#FFFFFF", 0.10));
            SetBrush("BtnText", HexBrush("#F3F3F3"));
            SetBrush("PrimaryBg", HexBrush("#4CC2FF"));
            SetBrush("PrimaryText", HexBrush("#0F1A24"));
            SetBrush("IconBtnHover", HexBrush("#FFFFFF", 0.09));
            SetBrush("SegBg", HexBrush("#FFFFFF", 0.10));
            SetBrush("SegIdleBg", Brushes.Transparent);
            SetBrush("SegSelBg", HexBrush("#FFFFFF", 0.18));
            SetBrush("SegSelText", HexBrush("#F3F3F3"));
            SetBrush("SegText", HexBrush("#B0B0B0"));
            SetBrush("FooterBg", HexBrush("#FFFFFF", 0.04));
            SetBrush("FooterBorder", HexBrush("#FFFFFF", 0.08));
            SetBrush("BarTrack", HexBrush("#FFFFFF", 0.14));
            RootBg.Color = ColorFromHex("#26292F");
            RootBg.Opacity = 0.88;
        }
        else
        {
            SetBrush("FlyoutBg", HexBrush("#F2F4F7", 0.92));
            SetBrush("TextBrush", HexBrush("#1B1B1B"));
            SetBrush("MutedBrush", HexBrush("#5F5F5F"));
            SetBrush("FaintBrush", HexBrush("#767676"));
            SetBrush("DividerBrush", HexBrush("#000000", 0.08));
            SetBrush("BtnBg", HexBrush("#FFFFFF", 0.70));
            SetBrush("BtnBorder", HexBrush("#000000", 0.14));
            SetBrush("BtnText", HexBrush("#1B1B1B"));
            SetBrush("PrimaryBg", HexBrush("#0067C0"));
            SetBrush("PrimaryText", HexBrush("#FFFFFF"));
            SetBrush("IconBtnHover", HexBrush("#000000", 0.06));
            SetBrush("SegBg", HexBrush("#000000", 0.06));
            SetBrush("SegIdleBg", Brushes.Transparent);
            SetBrush("SegSelBg", HexBrush("#FFFFFF"));
            SetBrush("SegSelText", HexBrush("#1B1B1B"));
            SetBrush("SegText", HexBrush("#5F5F5F"));
            SetBrush("FooterBg", HexBrush("#000000", 0.03));
            SetBrush("FooterBorder", HexBrush("#000000", 0.06));
            SetBrush("BarTrack", HexBrush("#000000", 0.10));
            RootBg.Color = ColorFromHex("#F2F4F7");
            RootBg.Opacity = 0.92;
        }
    }

    private static bool IsDarkMode()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            if (key?.GetValue("AppsUseLightTheme") is int v) return v == 0;
        }
        catch { }
        return false;
    }

    private void SetBrush(string key, Brush b) => Resources[key] = b;
    private static SolidColorBrush HexBrush(string hex, double opacity = 1.0)
        => new((Color)ColorConverter.ConvertFromString(hex)) { Opacity = opacity };
    private static Color ColorFromHex(string hex) => (Color)ColorConverter.ConvertFromString(hex);

    private Color QuotaBarColor(double left) => left switch
    {
        < 25 => _dark ? ColorFromHex("#FF99A4") : ColorFromHex("#C42B1C"),
        < 50 => _dark ? ColorFromHex("#F7630C") : ColorFromHex("#CA5010"),
        _ => _dark ? ColorFromHex("#6CCB5F") : ColorFromHex("#0F7B0F"),
    };

    private Brush ComparisonBrush(double? costPct)
    {
        bool above = costPct is > 0;
        Color c = above
            ? (_dark ? ColorFromHex("#FF99A4") : ColorFromHex("#C42B1C"))
            : (_dark ? ColorFromHex("#6CCB5F") : ColorFromHex("#0F7B0F"));
        return new SolidColorBrush(c);
    }

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromPoint(POINT pt, uint dwFlags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetMonitorInfoW(IntPtr hMonitor, ref MONITORINFO lpmi);

    private const uint MONITOR_DEFAULTTONEAREST = 0x00000002;

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct MONITORINFO
    {
        public int cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public uint dwFlags;
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
                Store.ConnectionState is ConnectionState.Offline or ConnectionState.WrongService ? "#FF453A" : "#FF9F0A"));
            BannerTitle.Text = Store.BannerTitle;
            BannerBody.Text = Store.BannerBody;
            // Retry + Settings actions per spec §3 (busy auto-retries, no buttons).
            BannerRetry.Visibility = Store.ConnectionState == ConnectionState.Offline ? Visibility.Visible : Visibility.Collapsed;
            BannerSettings.Visibility = (Store.ConnectionState == ConnectionState.Offline || Store.ConnectionState == ConnectionState.WrongService)
                ? Visibility.Visible : Visibility.Collapsed;
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
            TodayCost.Text = snap.TodayFailed ? "Today's data unavailable" : "No usage recorded today";
            TodayCost.FontSize = 14;
            TodaySub.Text = snap.TodayFailed ? "Will retry shortly." : "Tokdash is running. Today's totals will appear as tools report usage.";
            TodayCmp.Text = "";
        }
        else
        {
            TodayCost.Text = snap.TodayCostText;
            TodayCost.FontSize = 28;
            TodaySub.Text = $"{snap.TodayTokensCompact} tokens · {snap.Today.TotalMessages} messages" + (snap.TodayFailed ? " · retrying" : "");
            TodayCmp.Text = snap.ComparisonText ?? "";
            TodayCmp.Foreground = ComparisonBrush(snap.Today.Comparison?.CostPct);
        }

        MonthLabel.Text = snap.MonthLabel;
        if (snap.MonthFailed && snap.Month.TotalTokens > 0)
        {
            // Keep last-good month visible with a retrying note (don't hide it as "-").
            MonthCost.Text = snap.MonthCostText;
            MonthTokens.Text = $"{snap.MonthTokensCompact} tokens · retrying";
        }
        else if (snap.MonthFailed)
        {
            MonthCost.Text = "–";
            MonthTokens.Text = "retrying";
        }
        else
        {
            MonthCost.Text = snap.MonthCostText;
            MonthTokens.Text = $"{snap.MonthTokensCompact} tokens";
        }

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

        if (snap.QuotaFailed)
        {
            QuotaHeader.Text = "SUBSCRIPTION";
            var warn = new StackPanel { Orientation = Orientation.Horizontal };
            warn.Children.Add(new TextBlock
            {
                Text = "⚠",
                FontSize = 14,
                Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#FF9F0A")),
                Margin = new Thickness(0, 0, 8, 0),
                VerticalAlignment = VerticalAlignment.Center,
            });
            warn.Children.Add(new TextBlock
            {
                Text = "Quota data unavailable - will retry shortly.",
                FontSize = 12.5,
                Foreground = (Brush)FindResource("MutedBrush"),
                VerticalAlignment = VerticalAlignment.Center,
            });
            var retryBtn = new Button { Content = "Retry now", Margin = new Thickness(8, 0, 0, 0), Style = (Style)FindResource("WinBtn") };
            retryBtn.Click += (s, e) => _ = Store.RefreshAsync();
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            row.Children.Add(warn);
            row.Children.Add(retryBtn);
            QuotaRows.Children.Add(row);
            if (!snap.Quota.Enabled) return; // no last-good quota to show
            QuotaRows.Children.Add(new Separator { Opacity = 0.3, Margin = new Thickness(0, 10, 0, 4) });
            // Fall through: render last-good quota rows below the warning.
        }

        if (!snap.Quota.Enabled)
        {
            QuotaRows.Children.Add(new TextBlock
            {
                Text = "Subscription tracking is off",
                FontSize = 12.5,
                Foreground = (Brush)FindResource("MutedBrush"),
            });
            var openBtn = new Button { Content = "Open Dashboard", Margin = new Thickness(0, 4, 0, 0), Style = (Style)FindResource("WinBtn") };
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
                    Foreground = (Brush)FindResource("MutedBrush"),
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
                    Foreground = (Brush)FindResource("MutedBrush"),
                    Margin = new Thickness(0, 6, 0, 2),
                });
                // A failed provider shows an inline warning above its last-known rows,
                // not a full-surface failure (spec §7).
                if (group.Failed) panel.Children.Add(MakeProviderWarning());
                foreach (var row in group.Rows) panel.Children.Add(MakeQuotaRow(row, showProvider: false));
            }
            scroll.Content = panel;
            QuotaRows.Children.Add(scroll);
        }
    }

    private UIElement MakeQuotaRow(QuotaRow row, bool showProvider)
    {
        var textBrush = (Brush)FindResource("TextBrush");
        var muted = (Brush)FindResource("MutedBrush");

        var panel = new StackPanel { Margin = new Thickness(0, 0, 0, 12) };
        var top = new DockPanel { LastChildFill = false };

        // A failed provider's rows get a ⚠ prefix so the Low view (cross-provider,
        // no group header) still signals the warning inline.
        string prefix = row.Failed ? "⚠ " : "";
        var name = new TextBlock
        {
            Text = prefix + (showProvider ? $"{row.Provider} · {row.BucketLabel}" : row.BucketLabel),
            FontSize = 12.5,
            FontWeight = FontWeights.Medium,
            Foreground = textBrush,
        };
        DockPanel.SetDock(name, Dock.Left);
        top.Children.Add(name);

        if (row.Estimated)
        {
            var estBorder = new Border
            {
                BorderBrush = muted,
                BorderThickness = new Thickness(0.5),
                CornerRadius = new CornerRadius(4),
                Padding = new Thickness(4, 0, 4, 0),
                Margin = new Thickness(6, 0, 0, 0),
                Child = new TextBlock { Text = "Estimated", FontSize = 10.5, Foreground = muted },
            };
            DockPanel.SetDock(estBorder, Dock.Left);
            top.Children.Add(estBorder);
        }

        var resets = new TextBlock
        {
            Text = row.ResetsText,
            FontSize = 12,
            Foreground = muted,
        };
        DockPanel.SetDock(resets, Dock.Right);
        top.Children.Add(resets);
        if (row.HasPercent)
        {
            var left = new TextBlock
            {
                Text = $"{(int)row.Left}% left",
                FontSize = 12.5,
                FontWeight = FontWeights.SemiBold,
                Foreground = textBrush,
                Margin = new Thickness(8, 0, 0, 0),
            };
            DockPanel.SetDock(left, Dock.Right);
            top.Children.Add(left);
        }
        panel.Children.Add(top);

        // Coloured quota bar: light track with a coloured fill sized to the % left.
        // Buckets without a remaining_percent render without a bar.
        if (row.HasPercent)
        {
            double pct = Math.Clamp(row.Left, 0, 100);
            var track = new Border
            {
                CornerRadius = new CornerRadius(2),
                Background = (Brush)FindResource("BarTrack"),
                Height = 4,
                Margin = new Thickness(0, 6, 0, 0),
            };
            var inner = new Grid();
            inner.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(pct, GridUnitType.Star) });
            inner.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(100 - pct, GridUnitType.Star) });
            var fill = new Border
            {
                CornerRadius = new CornerRadius(2),
                Background = new SolidColorBrush(QuotaBarColor(row.Left)),
            };
            Grid.SetColumn(fill, 0);
            inner.Children.Add(fill);
            track.Child = inner;
            panel.Children.Add(track);
        }
        return panel;
    }

    /// <summary>Inline warning rendered above a failed provider's last-known rows.</summary>
    private UIElement MakeProviderWarning()
    {
        var warn = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 4) };
        warn.Children.Add(new TextBlock
        {
            Text = "⚠",
            FontSize = 12,
            Foreground = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#FF9F0A")),
            Margin = new Thickness(0, 0, 6, 0),
            VerticalAlignment = VerticalAlignment.Center,
        });
        warn.Children.Add(new TextBlock
        {
            Text = "Couldn't refresh - showing last known",
            FontSize = 11.5,
            Foreground = (Brush)FindResource("MutedBrush"),
            VerticalAlignment = VerticalAlignment.Center,
        });
        return warn;
    }

    private void UpdateToggleButtons()
    {
        bool low = Store.QuotaView == QuotaView.Low;
        LowBtn.SetResourceReference(BackgroundProperty, low ? "SegSelBg" : "SegIdleBg");
        LowBtn.SetResourceReference(TextElement.ForegroundProperty, low ? "SegSelText" : "SegText");
        LowBtn.FontWeight = low ? FontWeights.SemiBold : FontWeights.Normal;
        AllBtn.SetResourceReference(BackgroundProperty, low ? "SegIdleBg" : "SegSelBg");
        AllBtn.SetResourceReference(TextElement.ForegroundProperty, low ? "SegText" : "SegSelText");
        AllBtn.FontWeight = low ? FontWeights.Normal : FontWeights.SemiBold;
    }

    private void OpenDashboard_Click(object sender, RoutedEventArgs e)
    {
        var url = string.IsNullOrWhiteSpace(Store?.Settings?.BaseURL)
            ? "http://127.0.0.1:55423/"
            : Store.Settings.BaseURL;
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName = url,
            UseShellExecute = true,
        });
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e) => await Store.RefreshAsync();

    private void Gear_Click(object sender, RoutedEventArgs e)
    {
        new SettingsWindow { Store = Store }.Show();
    }

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
