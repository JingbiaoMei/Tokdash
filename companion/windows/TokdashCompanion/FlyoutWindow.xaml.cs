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
        ApplyStrings();

        ConnDot.Fill = new SolidColorBrush((Color)ColorConverter.ConvertFromString(Store.DotColor));
        ConnText.Text = Store.ConnectionLabel;

        bool showBanner = Store.ShowsBanner;
        Banner.Visibility = showBanner ? Visibility.Visible : Visibility.Collapsed;
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

        UpdatePeriodButtons();

        var snap = Store.Snapshot;
        // Hero kicker follows the selected instance (E12: present or stepped) even before
        // the first snapshot exists.
        TodayHeader.Text = snap?.KickerText ?? Store.CurrentKickerText;

        // One skeleton for the whole usage side while the selected period's data has not
        // landed (first load or a period switch). Quota lives in its own section (rule 2).
        bool heroLoading = snap is null || snap.UsageLoading;
        HeroSkeleton.Visibility = heroLoading ? Visibility.Visible : Visibility.Collapsed;
        var components = Store.Settings.Components;
        RanksSkeleton.Visibility = components.TopRanksOn ? Visibility.Visible : Visibility.Collapsed;
        GlanceSkeleton.Visibility = components.ActivityGlanceOn &&
            (Store.SelectedPeriod is UsagePeriod.Month or UsagePeriod.Year || components.ActivityHistogramTodayWeekOn)
            ? Visibility.Visible : Visibility.Collapsed;
        TodayCost.Visibility = heroLoading ? Visibility.Collapsed : Visibility.Visible;
        TodaySub.Visibility = heroLoading ? Visibility.Collapsed : Visibility.Visible;
        TodayCmp.Visibility = Visibility.Collapsed;
        DeltaRow.Visibility = Visibility.Collapsed;
        TodayCost.Text = "";
        TodaySub.Text = "";

        // Quota skeleton: only when no snapshot has ever been built (all endpoints pending).
        QuotaSkeleton.Visibility = snap is null ? Visibility.Visible : Visibility.Collapsed;

        if (snap is not null && !snap.UsageLoading)
        {
            if (snap.Usage is null || snap.IsEmptyUsage)
            {
                TodayCost.Text = snap.HeroTitle;
                TodayCost.FontSize = FontRes("FontHeroEmpty");
                TodaySub.Text = snap.HeroEmptySub;
            }
            else
            {
                TodayCost.Text = snap.CostText;
                TodayCost.FontSize = FontRes("FontHero");
                TodaySub.Text = snap.SubLine;
                if (snap.DeltaPieces is { } pieces)
                {
                    DeltaRow.Inlines.Clear();
                    for (int i = 0; i < pieces.Count; i++)
                    {
                        if (i > 0)
                            DeltaRow.Inlines.Add(new Run(" · ") { Foreground = (Brush)FindResource("FaintBrush") });
                        DeltaRow.Inlines.Add(new Run(pieces[i].Text) { Foreground = PieceBrush(pieces[i].Direction) });
                    }
                    DeltaRow.Inlines.Add(new Run(" " + snap.DeltaSentence) { Foreground = (Brush)FindResource("MutedBrush") });
                    DeltaRow.Visibility = Visibility.Visible;
                }
                else if (snap.ComparisonLine is { Length: > 0 } line && snap.ComparisonDirection is { } dir)
                {
                    TodayCmp.Text = line;
                    TodayCmp.Foreground = PieceBrush(dir);
                    TodayCmp.Visibility = Visibility.Visible;
                }
            }
        }

        RenderRanks(snap);
        RenderGlance(snap);
        RenderPerServer(snap);
        RenderQuota(snap);

        FreshnessText.Text = Store.FreshnessText;

        // Dim last-good data when offline/busy.
        double opacity = (Store.ConnectionState == ConnectionState.Offline || Store.ConnectionState == ConnectionState.Busy) ? 0.45 : 1.0;
        HeroPanel.Opacity = opacity;
        QuotaPanel.Opacity = opacity;
        RanksPanel.Opacity = opacity;
        GlancePanel.Opacity = opacity;
        PerServerPanel.Opacity = opacity;
    }

    private void UpdatePeriodButtons()
    {
        var buttons = new[] { Period0Btn, Period1Btn, Period2Btn, Period3Btn };
        string selected = Store.SelectedPeriod.Token();
        foreach (var b in buttons)
        {
            bool on = (string)b.Tag == selected;
            b.SetResourceReference(BackgroundProperty, on ? "SegSelBg" : "SegIdleBg");
            b.SetResourceReference(TextElement.ForegroundProperty, on ? "SegSelText" : "SegText");
            b.FontWeight = on ? FontWeights.SemiBold : FontWeights.Normal;
        }
        // E12 kicker stepper: ‹ walks instances into the past, › walks back toward the
        // present; ‹ is inert at the walk-back limit, › at the present (contract §Instance
        // stepper). Names refresh here so a language change re-labels them.
        StepEarlierBtn.IsEnabled = Store.CanStepEarlier;
        StepLaterBtn.IsEnabled = Store.CanStepLater;
        string earlierName = L10n.T("step_earlier"), laterName = L10n.T("step_later");
        System.Windows.Automation.AutomationProperties.SetName(StepEarlierBtn, earlierName);
        System.Windows.Automation.AutomationProperties.SetName(StepLaterBtn, laterName);
        StepEarlierBtn.ToolTip = earlierName;
        StepLaterBtn.ToolTip = laterName;
    }

    private void Period_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string token } btn) return;
        UsagePeriod period = token switch
        {
            "week" => UsagePeriod.Week,
            "month" => UsagePeriod.Month,
            "year" => UsagePeriod.Year,
            _ => UsagePeriod.Today,
        };
        // Selecting a segment always re-anchors to the present instance (E12): clicking the
        // already-selected segment while stepped steps back to the present, so no early
        // return here - the store's own guard no-ops only a true no-op.
        Store.SelectPeriod(period);
        UpdatePeriodButtons();
    }

    private void PeriodStep_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string tag }) return;
        if (!int.TryParse(tag, out int delta)) return;
        Store.StepPeriod(delta);
        // Refresh the inert states immediately: a step that lands ON a limit changes button
        // enablement even if the fetch behind it is still in flight.
        UpdatePeriodButtons();
    }

    /// <summary>Delta line colors (mirrors macOS pieceColor): down green / up red / flat grey.</summary>
    private Brush PieceBrush(int direction)
    {
        if (direction < 0) return new SolidColorBrush(_dark ? ColorFromHex("#6CCB5F") : ColorFromHex("#0F7B0F"));
        if (direction > 0) return new SolidColorBrush(_dark ? ColorFromHex("#FF99A4") : ColorFromHex("#C42B1C"));
        return (Brush)FindResource("MutedBrush");
    }

    // MARK: Top ranks

    private void RenderRanks(Snapshot? snap)
    {
        var tools = snap?.TopTools ?? [];
        var models = snap?.TopModels ?? [];
        bool show = snap is not null && !snap.UsageLoading && snap.HasTopRanks && (tools.Count > 0 || models.Count > 0);
        RanksPanel.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        if (!show) return;
        ToolsKicker.Text = snap!.ToolsKickerText;
        ModelsKicker.Text = snap.ModelsKickerText;
        ToolsStrip.ItemsSource = tools.Select(MakeRankVM).ToList();
        // Model rows carry no marks at all: the mark sits inside the name cell, so its
        // absence collapses and the names sit flush with the kicker (RankTemplate).
        ModelsStrip.ItemsSource = models.Select(MakeRankVM).ToList();
    }

    private RankVM MakeRankVM(Snapshot.RankEntry entry)
    {
        var logo = LogoFor(entry.LogoAsset);
        return new RankVM
        {
            Logo = logo,
            LogoVisibility = logo is null ? Visibility.Collapsed : Visibility.Visible,
            Label = entry.Label,
            Value = entry.ValueText,
            FillStar = new GridLength(entry.Fraction * 100, GridUnitType.Star),
            RestStar = new GridLength(100 - entry.Fraction * 100, GridUnitType.Star),
            Pct = entry.PctText,
        };
    }

    private static readonly Dictionary<string, ImageSource?> LogoCache = new();

    /// <summary>Marks that ship as dark ink: the dark theme swaps in a pre-inverted
    /// {name}-dark copy, mirroring the web dashboard's darkInvert rule (the same set the
    /// macOS asset catalog carries as dark-appearance imageset variants).</summary>
    private static readonly HashSet<string> DarkInvertAssets =
        new() { "codex", "grok", "zcode", "cline", "hermes", "omp", "zed", "cursor" };

    /// <summary>
    /// Load a harness mark from the packaged Assets\Agents resources. A missing asset
    /// degrades to text-only (never a broken-image box). Dark-ink marks (codex, grok, zcode)
    /// swap to their pre-inverted copies when the flyout renders dark.
    /// </summary>
    private ImageSource? LogoFor(string? asset)
    {
        if (string.IsNullOrEmpty(asset)) return null;
        string name = _dark && DarkInvertAssets.Contains(asset) ? asset + "-dark" : asset;
        if (LogoCache.TryGetValue(name, out var cached)) return cached;
        ImageSource? img = null;
        try
        {
            var bmp = new System.Windows.Media.Imaging.BitmapImage();
            bmp.BeginInit();
            bmp.UriSource = new Uri($"pack://application:,,,/Assets/Agents/{name}.png");
            bmp.DecodePixelWidth = 32;
            bmp.CacheOption = System.Windows.Media.Imaging.BitmapCacheOption.OnLoad;
            bmp.EndInit();
            bmp.Freeze();
            img = bmp;
        }
        catch (Exception ex) { Diag.Log($"logo {name}: {ex.Message}"); }
        LogoCache[name] = img;
        return img;
    }

    // MARK: Activity glance

    private void RenderGlance(Snapshot? snap)
    {
        var face = snap?.Glance;
        bool show = snap is not null && !snap.UsageLoading && snap.Usage is { TotalTokens: > 0 } && snap.Components.ActivityGlanceOn && face is not null;
        GlancePanel.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        GlanceHost.Content = null;
        GlanceCaption.Text = "";
        if (face is null || !show) return;

        GlanceKicker.Text = face.Kind switch
        {
            GlanceKind.Hours => L10n.T("glance_kicker_hours"),
            GlanceKind.Days => L10n.T("glance_kicker_days"),
            _ => L10n.T(face.WindowDays == 90 ? "glance_kicker_90" : "glance_kicker_180"),
        };

        switch (face.Kind)
        {
            case GlanceKind.Hours:
            {
                var panel = new StackPanel();
                panel.Children.Add(BarsVisual(face.Bars!, 24));
                // Hour axis: labeling all 24 columns can't fit at flyout width, so label
                // every 6th hour plus the last column - the time is readable, the axis
                // is anchored at both ends. Bare digits, no L10n needed.
                panel.Children.Add(LabelAxis(24, i => i % 6 == 0 || i == 23 ? i.ToString() : null));
                GlanceHost.Content = panel;
                if (face.PeakHour is { } peak) GlanceCaption.Text = L10n.T("peak_caption", peak);
                break;
            }
            case GlanceKind.Days:
            {
                var panel = new StackPanel();
                panel.Children.Add(BarsVisual(face.DayTokens!, 7, spacing: 6));
                string[] keys = ["wd_mon", "wd_tue", "wd_wed", "wd_thu", "wd_fri", "wd_sat", "wd_sun"];
                panel.Children.Add(LabelAxis(7, i => L10n.T(keys[i])));
                GlanceHost.Content = panel;
                break;
            }
            case GlanceKind.Grid:
                GlanceHost.Content = GridViewVisual(face);
                break;
        }
    }

    /// <summary>
    /// Axis labels under a BarsVisual: one star column per bar so each label sits under
    /// its own column; a null label leaves that column blank (sparse hour axis).
    /// </summary>
    private Grid LabelAxis(int columns, Func<int, string?> label)
    {
        var grid = new Grid { Margin = new Thickness(0, 3, 0, 0) };
        for (int i = 0; i < columns; i++)
        {
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            if (label(i) is not { } text) continue;
            var t = new TextBlock
            {
                Text = text,
                FontSize = FontRes("FontMicro"),
                Foreground = (Brush)FindResource("FaintBrush"),
                HorizontalAlignment = HorizontalAlignment.Center,
            };
            Grid.SetColumn(t, i);
            grid.Children.Add(t);
        }
        return grid;
    }

    /// <summary>
    /// Histogram bars: proportional to the max, min 2px for non-zero, zero bars stay as
    /// invisible stubs so gaps keep their position (mirrors macOS barsView).
    /// </summary>
    private FrameworkElement BarsVisual(long[] values, int columns, double spacing = 2)
    {
        double maxVal = Math.Max(values.Length == 0 ? 1 : values.Max(), 1);
        var grid = new Grid { Height = 26 };
        for (int i = 0; i < values.Length; i++)
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        for (int i = 0; i < values.Length; i++)
        {
            var bar = new Border
            {
                CornerRadius = new CornerRadius(1),
                VerticalAlignment = VerticalAlignment.Bottom,
                Height = values[i] > 0 ? Math.Max(2, 26.0 * values[i] / maxVal) : 2,
                Background = values[i] > 0
                    ? (Brush)FindResource("PrimaryBg")
                    : System.Windows.Media.Brushes.Transparent,
                Margin = new Thickness(i == 0 ? 0 : spacing / 2, 0, i == values.Length - 1 ? 0 : spacing / 2, 0),
            };
            Grid.SetColumn(bar, i);
            grid.Children.Add(bar);
        }
        return grid;
    }

    private FrameworkElement GridViewVisual(GlanceFace face)
    {
        int windowDays = face.WindowDays;
        double cell = windowDays == 90 ? 9 : 5;
        double gap = windowDays == 90 ? 3 : 2;
        double radius = windowDays == 90 ? 2 : 1;
        var row = new StackPanel { Orientation = Orientation.Horizontal };
        foreach (var column in face.GridColumns!)
        {
            var col = new StackPanel { Orientation = Orientation.Vertical };
            for (int dow = 0; dow < 7; dow++)
            {
                int? v = column[dow];
                col.Children.Add(new Border
                {
                    Width = cell,
                    Height = cell,
                    CornerRadius = new CornerRadius(radius),
                    Margin = new Thickness(0, dow == 0 ? 0 : gap, 0, 0),
                    Background = v is null
                        ? System.Windows.Media.Brushes.Transparent
                        : GridCellBrush(v.Value),
                });
            }
            col.Margin = new Thickness(0, 0, gap, 0);
            row.Children.Add(col);
        }
        return row;
    }

    // Grid intensity ramp from the approved mock (identical steps to macOS GlancePalette;
    // intensity 4 clamps to the darkest shipped step).
    private Brush GridCellBrush(int intensity)
    {
        int i = Math.Clamp(intensity, 0, 3);
        Color c = _dark
            ? i switch { 0 => ColorFromHex("#EBEBF5", 0.10), 1 => ColorFromHex("#4CAE68"), 2 => ColorFromHex("#30A74C"), _ => ColorFromHex("#32D74B") }
            : i switch { 0 => ColorFromHex("#3C3C43", 0.08), 1 => ColorFromHex("#A6D8B0"), 2 => ColorFromHex("#5BB977"), _ => ColorFromHex("#166F37") };
        return new SolidColorBrush(c);
    }

    private static Color ColorFromHex(string hex, double opacity)
    {
        var c = ColorFromHex(hex);
        c.A = (byte)Math.Round(opacity * 255);
        return c;
    }

    // MARK: Per-server rows

    private void RenderPerServer(Snapshot? snap)
    {
        var rows = snap?.PerServerRowsView ?? [];
        bool show = snap is not null && snap.ShowPerServerRows && rows.Count > 0 && !snap.UsageLoading;
        PerServerPanel.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        PerServerRowsCtl.Items.Clear();
        if (!show) return;

        PerServerKicker.Text = snap!.PerServerKickerText;
        PerServerFootnote.Text = Snapshot.PerServerFootnoteText;
        foreach (var r in rows)
        {
            var grid = new Grid { Margin = new Thickness(0, 1, 0, 1) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var label = new TextBlock
            {
                Text = r.Label,
                FontSize = FontRes("FontSecondary"),
                TextTrimming = TextTrimming.CharacterEllipsis,
                Foreground = (Brush)FindResource("TextBrush"),
                VerticalAlignment = VerticalAlignment.Center,
            };
            Grid.SetColumn(label, 0);
            var value = new TextBlock
            {
                Text = r.ValueText,
                FontSize = FontRes("FontSecondary"),
                Foreground = (Brush)FindResource("MutedBrush"),
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(8, 0, 0, 0),
            };
            Grid.SetColumn(value, 1);
            grid.Children.Add(label);
            grid.Children.Add(value);
            PerServerRowsCtl.Items.Add(grid);
        }
    }

    /// <summary>Apply localized text to the static XAML literals. Called from UpdateView so a
    /// language change (which raises a store property change) re-renders them live, without
    /// reopening the flyout. DataTemplate literals (Estimated / Couldn't refresh) are localized
    /// via the QuotaRowVM / QuotaGroupVM they're bound to, rebuilt in RenderQuota.</summary>
    private void ApplyStrings()
    {
        // The kicker (TodayHeader) follows the snapshot's period; UpdateView sets it.
        // The segment buttons are static labels but still language-dependent.
        Period0Btn.Content = L10n.T("period_today");
        Period1Btn.Content = L10n.T("period_week");
        Period2Btn.Content = L10n.T("period_month");
        Period3Btn.Content = L10n.T("period_year");
        LowBtn.Content = L10n.T("low");
        AllBtn.Content = L10n.T("all");
        OpenDashboardBtn.Content = L10n.T("open_dashboard");
        TrayHint.Text = L10n.T("tray_hint");
        BannerRetry.Content = L10n.T("retry");
        BannerSettings.Content = L10n.T("settings");
        RefreshBtn.ToolTip = L10n.T("refresh");
        // The dot means nothing to a screen reader, so the gear's name and tooltip carry
        // the update state instead.
        UpdateBadge.Visibility = Store.ShowsUpdateBadge ? Visibility.Visible : Visibility.Collapsed;
        GearBtn.ToolTip = Store.SettingsAccessibilityName;
        System.Windows.Automation.AutomationProperties.SetName(GearBtn, Store.SettingsAccessibilityName);
    }

    private void RenderQuota(Snapshot? snap)
    {
        QuotaRows.Items.Clear();
        UpdateToggleButtons();
        // The All/High view can open before the first snapshot exists (loading case): the
        // skeleton bars stand in, there is nothing to render here yet.
        if (snap is null) { QuotaHeader.Text = L10n.T("subscription"); return; }

        if (snap.QuotaFailed)
        {
            QuotaHeader.Text = L10n.T("subscription");
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
                Text = L10n.T("quota_unavailable"),
                FontSize = FontRes("FontSecondary"),
                Foreground = (Brush)FindResource("MutedBrush"),
                VerticalAlignment = VerticalAlignment.Center,
            });
            var retryBtn = new Button
            {
                Content = L10n.T("retry_now"),
                Margin = new Thickness(8, 0, 0, 0),
                Style = (Style)FindResource("WinBtn"),
                // A plain horizontal StackPanel stretched the button to the row's height
                // and it read "out of line" with the warning text. Grid + center keeps
                // both on the same line, the button at the trailing edge.
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Center,
            };
            retryBtn.Click += (s, e) => _ = Store.RefreshAsync();
            var row = new Grid();
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            Grid.SetColumn(warn, 0);
            warn.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(retryBtn, 1);
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
                Text = L10n.T("tracking_off"),
                FontSize = FontRes("FontSecondary"),
                Foreground = (Brush)FindResource("MutedBrush"),
            });
            var openBtn = new Button { Content = L10n.T("open_dashboard"), Margin = new Thickness(0, 4, 0, 0), Style = (Style)FindResource("WinBtn") };
            openBtn.Click += (s, e) => OpenDashboard_Click(s, e);
            QuotaRows.Items.Add(openBtn);
            QuotaHeader.Text = L10n.T("subscription");
            return;
        }

        QuotaHeader.Text = L10n.T(Store.QuotaView == QuotaView.Low ? "subscription" : "all_subscriptions");

        if (Store.QuotaView == QuotaView.Low)
        {
            var low = snap.LowQuotaRows;
            if (low.Count == 0)
            {
                QuotaRows.Items.Add(new TextBlock
                {
                    Text = L10n.T("no_low_windows"),
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
            // Height budget follows the section gaps (SectionPad halved in a prior round):
            // 212 -> 280 per review so the All view shows more subscription rows before it
            // scrolls (macOS mirror: CompanionLayout.quotaMaxHeight 340).
            var scroll = new ScrollViewer { MaxHeight = 280, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
            scroll.Resources[typeof(System.Windows.Controls.Primitives.ScrollBar)] = FindResource("QuotaScrollBar");
            // Multi-server payloads sectionize the All view: one muted server header over
            // its provider groups (contract §All view). Single-server yields one
            // header-less section - the All view looks exactly as before.
            var stack = new StackPanel();
            foreach (var section in snap.AllQuotaServerSections)
            {
                if (section.Server.Length > 0)
                    stack.Children.Add(new TextBlock
                    {
                        Text = section.Server.ToUpperInvariant(),
                        FontSize = FontRes("FontCaption"),
                        FontWeight = FontWeights.SemiBold,
                        Foreground = (Brush)FindResource("MutedBrush"),
                        Margin = new Thickness(0, 8, 0, 0),
                    });
                stack.Children.Add(new ItemsControl
                {
                    ItemTemplate = (DataTemplate)FindResource("QuotaGroupTemplate"),
                    ItemsSource = section.Groups.Select(g => MakeQuotaGroupVM(snap, g)).ToList(),
                });
            }
            scroll.Content = stack;
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
            Label = prefix + (showProvider ? $"{row.Provider} · {row.DisplayBucketLabel}" : row.DisplayBucketLabel),
            PercentText = row.HasPercent ? L10n.T("percent_left", (int)row.Left) : "",
            ResetsText = row.ResetsText,
            EstimatedText = L10n.T("estimated"),
            EstimatedVisibility = row.Estimated ? Visibility.Visible : Visibility.Collapsed,
            BarVisibility = row.HasPercent ? Visibility.Visible : Visibility.Collapsed,
            BarBrush = row.HasPercent ? new SolidColorBrush(QuotaBarColor(row.Left)) : Brushes.Transparent,
            FillStar = new GridLength(pct, GridUnitType.Star),
            RestStar = new GridLength(100 - pct, GridUnitType.Star),
        };
    }

    /// <summary>Presentation shape for one provider group in the All view (QuotaGroupTemplate).</summary>
    private QuotaGroupVM MakeQuotaGroupVM(Snapshot snap, QuotaGroup group) => new()
    {
        // Under a server section the provider reads bare - "Codex", never the
        // "Workstation · Codex" compound (contract §All view). Single-server groups
        // carry no prefix to strip.
        Provider = group.ServerLabel.Length > 0 ? group.Provider.Split(" · ")[^1] : group.Provider,
        Logo = LogoFor(CompanionStore.QuotaLogoAssetName(group.CanonicalProvider)),
        WarningText = L10n.T("couldnt_refresh"),
        // GROUP failure drives the provider-header warning (spec §7); rendered inline by
        // QuotaGroupTemplate rather than a separate MakeProviderWarning() element.
        WarningVisibility = group.Failed ? Visibility.Visible : Visibility.Collapsed,
        Rows = group.Rows.Select(r => MakeQuotaRowVM(r, showProvider: false)).ToList(),
        // Reset-credits row: last inside its provider group (Codex, Claude Code), All
        // view only. Null (hidden) for providers without credits, on a failed group, or
        // component off.
        CreditsText = snap.CreditsNotice(group) ?? "",
        CreditsVisibility = snap.CreditsNotice(group) is null ? Visibility.Collapsed : Visibility.Visible,
        // Static right-aligned decoration on the same row (mock design): outside the
        // pinned row string, so CreditsNotice stays byte-identical.
        CreditsUseOrLoseText = L10n.T("credits_use_or_lose"),
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
        var url = Store?.DashboardBaseUrl ?? CompanionSettings.DefaultBaseURL;
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
    public string EstimatedText { get; init; } = "";
    public Visibility EstimatedVisibility { get; init; }
    public Visibility BarVisibility { get; init; }
    public Brush BarBrush { get; init; } = Brushes.Transparent;
    public GridLength FillStar { get; init; }        // GridLength(pct, Star)
    public GridLength RestStar { get; init; }        // GridLength(100-pct, Star)
}

internal sealed class QuotaGroupVM
{
    public string Provider { get; init; } = "";
    /// <summary>Provider mark for the group header (mock design's 14px logo); null renders
    /// text-only - never a placeholder (see CompanionStore.QuotaLogoAssetName).</summary>
    public ImageSource? Logo { get; init; }
    public string WarningText { get; init; } = "";
    public Visibility WarningVisibility { get; init; }
    public List<QuotaRowVM> Rows { get; init; } = new();
    /// <summary>Reset-credits row (⚡ …), Any provider that ships reset_credits
    /// (Codex, Claude Code), All view only.</summary>
    public string CreditsText { get; init; } = "";
    public Visibility CreditsVisibility { get; init; } = Visibility.Collapsed;
    /// <summary>Muted "use or lose" hint right-aligned on the credits row (static decoration).</summary>
    public string CreditsUseOrLoseText { get; init; } = "";
}

/// <summary>
/// Presentation shape for one top-rank row: logo + name + share bar + token amount + percent.
/// The logo is a pre-loaded ImageSource (null = no mark shipped / failed to load -> text-only,
/// never a broken-image placeholder). FillStar/RestStar are the quota bar's star-column pair
/// summing to 100*, so the fill width is exactly the printed percent (Pct) of all tokens in
/// the list - bar and label always agree.
/// </summary>
internal sealed class RankVM
{
    public ImageSource? Logo { get; init; }
    public Visibility LogoVisibility { get; init; } = Visibility.Collapsed;
    public string Label { get; init; } = "";
    public string Value { get; init; } = "";
    public GridLength FillStar { get; init; }
    public GridLength RestStar { get; init; }
    public string Pct { get; init; } = "";
}
