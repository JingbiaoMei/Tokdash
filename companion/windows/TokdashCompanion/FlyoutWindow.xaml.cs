using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using Microsoft.Win32;
using TokdashCompanion.Interop;

namespace TokdashCompanion;

/// <summary>
/// WPF flyout window, positioned near the tray icon. Light-dismiss on deactivate.
/// Escape closes. Opaque (not layered) so ClearType subpixel text rendering works -
/// the window silhouette's rounded corners and edge come from DWM (Win32Dwm), not from
/// AllowsTransparency, which forces software rendering and grayscale-only AA.
/// </summary>
public partial class FlyoutWindow : Window
{
    private bool _loaded;
    private bool _closing;
    private bool _highContrast;
    private bool _dark;
    private bool _positionQueued;
    private bool _updateQueued;
    private int _anchorX, _anchorY;

    public CompanionStore Store { get; set; } = null!;

    public FlyoutWindow()
    {
        InitializeComponent();
        Loaded += FlyoutWindow_Loaded;
        Closed += FlyoutWindow_Closed;
        SizeChanged += (_, _) => QueuePosition();
    }

    private void FlyoutWindow_Loaded(object sender, RoutedEventArgs e)
    {
        // Subscribe now - Store is assigned via object initializer after ctor.
        Store.PropertyChanged += Store_PropertyChanged;
        // Apply the system theme (light / dark / high-contrast) before rendering.
        ApplyTheme();
        // Rounded corners + themed border via DWM on Windows 11. Zero-cost and correctly
        // antialiased (unlike the old layered-window acrylic backdrop this replaces),
        // since DWM composites the shape rather than WPF software-blurring it.
        ApplyDwmAttributes();
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
        _closing = true;
        _positionQueued = false;
        _updateQueued = false;
    }

    /// <summary>
    /// Close the light-dismiss flyout at most once. Deactivation can be raised while
    /// another close path is already running, so calling Window.Close directly from
    /// both paths can re-enter WPF's closing lifecycle and throw.
    /// </summary>
    internal void Dismiss()
    {
        if (_closing) return;
        _closing = true;
        try
        {
            Close();
        }
        catch
        {
            _closing = false;
            throw;
        }
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        _closing = true;
        base.OnClosing(e);
        if (e.Cancel) _closing = false;
    }

    private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
    private const int DWMWA_WINDOW_CORNER_PREFERENCE = 33;
    private const int DWMWA_BORDER_COLOR = 34;
    private const int DWMWCP_ROUND = 2;

    /// <summary>
    /// Rounded corners, dark-mode-matched chrome, and a themed edge colour - all via DWM
    /// window attributes, all Win11-only. Each is independent and non-fatal: an older
    /// Windows build (or a failed call) just leaves that one attribute at its default,
    /// never throws into the window lifecycle (spec: FlyoutLaunchTests constructs and
    /// shows this window on an STA thread, so any exception here fails the whole suite).
    /// </summary>
    private void ApplyDwmAttributes()
    {
        var hwnd = new System.Windows.Interop.WindowInteropHelper(this).Handle;

        try
        {
            int corner = DWMWCP_ROUND;
            int hr = Win32Dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ref corner, sizeof(int));
            if (hr < 0) Diag.Log($"DWM corner failed hr=0x{hr:X8}");
        }
        catch (Exception ex) { Diag.Log($"  Loaded: DWM corner FAILED {ex.GetType().Name}: {ex.Message}"); }

        try
        {
            int dark = _dark ? 1 : 0;
            int hr = Win32Dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ref dark, sizeof(int));
            if (hr < 0) Diag.Log($"DWM dark-mode failed hr=0x{hr:X8}");
        }
        catch (Exception ex) { Diag.Log($"  Loaded: DWM dark-mode FAILED {ex.GetType().Name}: {ex.Message}"); }

        try
        {
            // COLORREF is 0x00BBGGRR. Matches the divider colour so the flyout edge still
            // reads against a similar-coloured desktop even with the system shadow off.
            Color divider = _dark ? ColorFromHex("#3C3E44") : ColorFromHex("#DFE0E3");
            int colorref = divider.R | (divider.G << 8) | (divider.B << 16);
            int hr = Win32Dwm.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ref colorref, sizeof(int));
            if (hr < 0) Diag.Log($"DWM border colour failed hr=0x{hr:X8}");
        }
        catch (Exception ex) { Diag.Log($"  Loaded: DWM border colour FAILED {ex.GetType().Name}: {ex.Message}"); }
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
        if (hMon == IntPtr.Zero) return;
        var mi = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
        if (!GetMonitorInfoW(hMon, ref mi)) return;

        int dpiResult = GetDpiForMonitor(hMon, 0, out uint dpiX, out uint dpiY);
        if (dpiResult != 0 || dpiX == 0 || dpiY == 0) dpiX = dpiY = 96;
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
    /// SizeToContent can grow after Loaded when an ItemsControl realizes its templates.
    /// Re-anchor after the final layout pass so the flyout's bottom remains above the
    /// taskbar instead of extending below the work area.
    /// </summary>
    private void QueuePosition()
    {
        if (!_loaded || _positionQueued) return;
        _positionQueued = true;
        Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.Loaded, () =>
        {
            _positionQueued = false;
            if (_loaded && IsVisible) ApplyPosition();
        });
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
        // NEVER let an exception escape here. This runs as a Win32 window-procedure
        // callback, and an unhandled managed exception crossing that native boundary
        // terminates the process with STATUS_FATAL_USER_CALLBACK_EXCEPTION (0xc000041d)
        // - no dialog, no stack, the tray app just vanishes. Repositioning is cosmetic;
        // a mispositioned flyout is always better than a dead app.
        try
        {
            // Re-position if the effective DPI changes while the flyout is open (mixed-DPI).
            if (msg == 0x02E0 /* WM_DPICHANGED */) ApplyPosition();
        }
        catch (Exception ex)
        {
            Diag.Log($"  WndProcHook msg=0x{msg:X4} THREW {ex.GetType().Name}: {ex.Message}\n{ex.StackTrace}");
        }
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
            SetBrush("WindowBg", SystemColors.WindowBrush);
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
            SetBrush("WindowBg", HexBrush("#26292F"));
            SetBrush("TextBrush", HexBrush("#F3F3F3"));
            SetBrush("MutedBrush", HexBrush("#B4B4B4"));
            SetBrush("FaintBrush", HexBrush("#909090"));
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
            SetBrush("SegText", HexBrush("#B4B4B4"));
            SetBrush("FooterBg", HexBrush("#FFFFFF", 0.04));
            SetBrush("FooterBorder", HexBrush("#FFFFFF", 0.08));
            SetBrush("BarTrack", HexBrush("#FFFFFF", 0.14));
            RootBg.Color = ColorFromHex("#26292F");
            RootBg.Opacity = 1;
        }
        else
        {
            SetBrush("FlyoutBg", HexBrush("#F2F4F7", 0.92));
            SetBrush("WindowBg", HexBrush("#F2F4F7"));
            SetBrush("TextBrush", HexBrush("#1B1B1B"));
            SetBrush("MutedBrush", HexBrush("#616161"));
            SetBrush("FaintBrush", HexBrush("#8A8A8A"));
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
            RootBg.Opacity = 1;
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
    private double FontRes(string key) => (double)FindResource(key);

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
        if (_loaded) Dismiss();
    }

    private void FlyoutWindow_KeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == System.Windows.Input.Key.Escape) Dismiss();
    }

    private void Store_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e)
    {
        if (!_loaded || _updateQueued) return;
        _updateQueued = true;
        Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.DataBind, () =>
        {
            _updateQueued = false;
            if (_loaded) UpdateView();
        });
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
            TodayCost.FontSize = FontRes("FontHeroEmpty");
            TodaySub.Text = snap.TodayFailed ? "Will retry shortly." : "Tokdash is running.";
            TodayCmp.Text = "";
        }
        else
        {
            TodayCost.Text = snap.TodayCostText;
            TodayCost.FontSize = FontRes("FontHero");
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
        QuotaRows.Items.Clear();
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
                FontSize = FontRes("FontSecondary"),
                Foreground = (Brush)FindResource("MutedBrush"),
                VerticalAlignment = VerticalAlignment.Center,
            });
            var retryBtn = new Button { Content = "Retry now", Margin = new Thickness(8, 0, 0, 0), Style = (Style)FindResource("WinBtn") };
            retryBtn.Click += (s, e) => _ = Store.RefreshAsync();
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            row.Children.Add(warn);
            row.Children.Add(retryBtn);
            QuotaRows.Items.Add(row);
            if (!snap.Quota.Enabled) return; // no last-good quota to show
            QuotaRows.Items.Add(new Border { Height = 1, Background = (Brush)FindResource("DividerBrush"), Margin = new Thickness(0, 8, 0, 4) });
            // Fall through: render last-good quota rows below the warning.
        }

        if (!snap.Quota.Enabled)
        {
            QuotaRows.Items.Add(new TextBlock
            {
                Text = "Subscription tracking is off",
                FontSize = FontRes("FontSecondary"),
                Foreground = (Brush)FindResource("MutedBrush"),
            });
            var openBtn = new Button { Content = "Open Dashboard", Margin = new Thickness(0, 4, 0, 0), Style = (Style)FindResource("WinBtn") };
            openBtn.Click += (s, e) => OpenDashboard_Click(s, e);
            QuotaRows.Items.Add(openBtn);
            QuotaHeader.Text = "SUBSCRIPTION";
            return;
        }

        QuotaHeader.Text = Store.QuotaView == QuotaView.Low ? "SUBSCRIPTION" : "ALL SUBSCRIPTIONS";

        if (Store.QuotaView == QuotaView.Low)
        {
            var low = snap.LowQuotaRows;
            if (low.Count == 0)
            {
                QuotaRows.Items.Add(new TextBlock
                {
                    Text = "No subscription window is below its alert threshold.",
                    FontSize = FontRes("FontSecondary"),
                    Foreground = (Brush)FindResource("MutedBrush"),
                });
            }
            else
            {
                foreach (var row in low) QuotaRows.Items.Add(MakeQuotaRowVM(row, showProvider: true));
            }
        }
        else
        {
            var scroll = new ScrollViewer { MaxHeight = 172, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
            var groups = new ItemsControl
            {
                ItemTemplate = (DataTemplate)FindResource("QuotaGroupTemplate"),
                ItemsSource = snap.AllQuotaGroups.Select(MakeQuotaGroupVM).ToList(),
            };
            scroll.Content = groups;
            QuotaRows.Items.Add(scroll);
        }
    }

    /// <summary>
    /// Presentation shape for one quota row, consumed by QuotaRowTemplate. Built here
    /// (not via converters) because the bar colour depends on the resolved theme, which
    /// lives in this class.
    /// </summary>
    private QuotaRowVM MakeQuotaRowVM(QuotaRow row, bool showProvider)
    {
        // A failed provider's rows get a ⚠ prefix so the Low view (cross-provider, no
        // group header) still signals the warning inline.
        string prefix = row.Failed ? "⚠ " : "";
        double pct = Math.Clamp(row.Left, 0, 100);
        return new QuotaRowVM
        {
            Label = prefix + (showProvider ? $"{row.Provider} · {row.BucketLabel}" : row.BucketLabel),
            PercentText = row.HasPercent ? $"{(int)row.Left}% left" : "",
            ResetsText = row.ResetsText,
            EstimatedVisibility = row.Estimated ? Visibility.Visible : Visibility.Collapsed,
            BarVisibility = row.HasPercent ? Visibility.Visible : Visibility.Collapsed,
            BarBrush = row.HasPercent ? new SolidColorBrush(QuotaBarColor(row.Left)) : Brushes.Transparent,
            FillStar = new GridLength(pct, GridUnitType.Star),
            RestStar = new GridLength(100 - pct, GridUnitType.Star),
        };
    }

    /// <summary>Presentation shape for one provider group in the All view (QuotaGroupTemplate).</summary>
    private QuotaGroupVM MakeQuotaGroupVM(QuotaGroup group) => new()
    {
        Provider = group.Provider,
        // GROUP failure drives the provider-header warning (spec §7); rendered inline by
        // QuotaGroupTemplate rather than a separate MakeProviderWarning() element.
        WarningVisibility = group.Failed ? Visibility.Visible : Visibility.Collapsed,
        Rows = group.Rows.Select(r => MakeQuotaRowVM(r, showProvider: false)).ToList(),
    };

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
        if (Application.Current is App app) app.ShowSettings();
    }

    private void Low_Click(object sender, RoutedEventArgs e)
    {
        Store.QuotaView = QuotaView.Low;
    }

    private void All_Click(object sender, RoutedEventArgs e)
    {
        Store.QuotaView = QuotaView.All;
    }
}

/// <summary>
/// Presentation shape for one quota row. Built in code-behind because the bar colour
/// and warning brushes depend on the resolved theme, which lives here — this keeps the
/// DataTemplate pure XAML with no converters.
/// </summary>
internal sealed class QuotaRowVM
{
    public string Label { get; init; } = "";
    public string PercentText { get; init; } = "";   // "" when !HasPercent
    public string ResetsText { get; init; } = "";
    public Visibility EstimatedVisibility { get; init; }
    public Visibility BarVisibility { get; init; }
    public Brush BarBrush { get; init; } = Brushes.Transparent;
    public GridLength FillStar { get; init; }        // GridLength(pct, Star)
    public GridLength RestStar { get; init; }        // GridLength(100-pct, Star)
}

internal sealed class QuotaGroupVM
{
    public string Provider { get; init; } = "";
    public Visibility WarningVisibility { get; init; }
    public List<QuotaRowVM> Rows { get; init; } = new();
}
