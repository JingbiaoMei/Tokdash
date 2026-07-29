using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Microsoft.Win32;
using TokdashCompanion.Interop;

namespace TokdashCompanion;

/// <summary>
/// WPF settings window: base URL, launch-at-login, low-quota notifications,
/// and per-bucket alert thresholds. Bound to the shared CompanionSettings.
/// </summary>
public partial class SettingsWindow : Window
{
    private bool _dark;
    private bool _highContrast;

    public CompanionStore Store { get; set; } = null!;

    public SettingsWindow()
    {
        InitializeComponent();
        ApplyTheme();
        ApplyWindowIcon();
        SourceInitialized += (_, _) => ApplyDwmTheme();
        Loaded += SettingsWindow_Loaded;
    }

    private void SettingsWindow_Loaded(object sender, RoutedEventArgs e)
    {
        var s = Store.Settings;
        BaseUrlBox.Text = s.BaseURL;
        LaunchBox.IsChecked = s.LaunchAtLogin;
        NotifyBox.IsChecked = s.LowQuotaNotifications;
        FiveHourSlider.Value = s.Thresholds.FiveHour;
        WeeklySlider.Value = s.Thresholds.Weekly;
        OtherSlider.Value = s.Thresholds.Other;
        UpdateSliderLabels();
    }

    private void Slider_Changed(object sender, RoutedPropertyChangedEventArgs<double> e) => UpdateSliderLabels();

    private void UpdateSliderLabels()
    {
        // ValueChanged fires DURING InitializeComponent: setting Minimum on the first
        // slider coerces its Value, and the labels declared later in the XAML don't exist
        // yet. An unhandled NRE here unwinds through the dispatcher's native callback and
        // kills the whole app, so bail until the fields are assigned - SettingsWindow_Loaded
        // calls this again once everything is constructed.
        if (FiveHourLabel is null || WeeklyLabel is null || OtherLabel is null) return;
        FiveHourLabel.Text = $"5-hour: {(int)FiveHourSlider.Value}%";
        WeeklyLabel.Text = $"Weekly: {(int)WeeklySlider.Value}%";
        OtherLabel.Text = $"Default: {(int)OtherSlider.Value}%";
    }

    /// <summary>
    /// Probe the URL currently in the box with its own short-lived client, so testing
    /// never disturbs the live connection or persists an address that turns out to be bad.
    /// Mirrors the macOS runConnectionTest.
    /// </summary>
    private async void Test_Click(object sender, RoutedEventArgs e)
    {
        string candidate = BaseUrlBox.Text.Trim();
        TestResult.Visibility = Visibility.Visible;
        if (!CompanionStore.IsValidBaseURL(candidate))
        {
            ShowTestResult("Enter an absolute http:// or https:// URL.", ok: false);
            return;
        }

        TestButton.IsEnabled = false;
        ShowTestResult("Testing…", ok: null);
        try
        {
            using var probe = new TokdashClient(candidate);
            var health = await probe.HealthAsync();
            // A reachable server that isn't Tokdash is a failure, not a success -
            // otherwise a proxy or a wrong port would test green.
            if (health.Service == "tokdash")
                ShowTestResult($"Connected to {CompanionStore.ServerLabel(candidate)} · Tokdash {health.Version}", ok: true);
            else
                ShowTestResult("Reachable, but not a Tokdash server.", ok: false);
        }
        catch (Exception ex)
        {
            ShowTestResult($"Couldn't reach it: {(ex as TokdashException)?.Error.ToString() ?? ex.Message}", ok: false);
        }
        finally
        {
            TestButton.IsEnabled = true;
        }
    }

    private void ShowTestResult(string text, bool? ok)
    {
        TestResult.Text = text;
        TestResult.Foreground = (Brush)FindResource(
            ok is null ? "SettingsMuted" : ok.Value ? "SettingsSuccess" : "SettingsError");
    }

    private void ApplyTheme()
    {
        _highContrast = SystemParameters.HighContrast;
        _dark = !_highContrast && IsDarkMode();

        if (_highContrast)
        {
            SetBrush("SettingsBg", SystemColors.WindowBrush);
            SetBrush("SettingsText", SystemColors.WindowTextBrush);
            SetBrush("SettingsMuted", SystemColors.GrayTextBrush);
            SetBrush("SettingsControlBg", SystemColors.ControlBrush);
            SetBrush("SettingsControlBorder", SystemColors.ControlTextBrush);
            SetBrush("SettingsSuccess", SystemColors.HighlightBrush);
            SetBrush("SettingsError", SystemColors.WindowTextBrush);
        }
        else if (_dark)
        {
            SetBrush("SettingsBg", HexBrush("#202124"));
            SetBrush("SettingsText", HexBrush("#F3F3F3"));
            SetBrush("SettingsMuted", HexBrush("#B4B4B4"));
            SetBrush("SettingsControlBg", HexBrush("#2B2D31"));
            SetBrush("SettingsControlBorder", HexBrush("#686A70"));
            SetBrush("SettingsSuccess", HexBrush("#6CCB5F"));
            SetBrush("SettingsError", HexBrush("#FF99A4"));
        }
        else
        {
            SetBrush("SettingsBg", HexBrush("#F2F4F7"));
            SetBrush("SettingsText", HexBrush("#1B1B1B"));
            SetBrush("SettingsMuted", HexBrush("#616161"));
            SetBrush("SettingsControlBg", HexBrush("#FFFFFF"));
            SetBrush("SettingsControlBorder", HexBrush("#8A8A8A"));
            SetBrush("SettingsSuccess", HexBrush("#187A32"));
            SetBrush("SettingsError", HexBrush("#C42B1C"));
        }
    }

    private void ApplyWindowIcon()
    {
        try
        {
            string path = Path.Combine(AppContext.BaseDirectory, "Assets", "tray.ico");
            if (File.Exists(path))
                Icon = BitmapFrame.Create(new Uri(path), BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnLoad);
        }
        catch (Exception ex) { Diag.Log($"Settings icon failed: {ex.Message}"); }
    }

    private void ApplyDwmTheme()
    {
        try
        {
            const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
            int dark = _dark ? 1 : 0;
            IntPtr hwnd = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            int hr = Win32Dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ref dark, sizeof(int));
            if (hr < 0) Diag.Log($"Settings DWM theme failed hr=0x{hr:X8}");
        }
        catch (Exception ex) { Diag.Log($"Settings DWM theme failed: {ex.Message}"); }
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

    private void SetBrush(string key, Brush brush) => Resources[key] = brush;
    private static SolidColorBrush HexBrush(string hex) =>
        new((Color)ColorConverter.ConvertFromString(hex));

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        var url = BaseUrlBox.Text.Trim();
        if (!CompanionStore.IsValidBaseURL(url))
        {
            MessageBox.Show("Enter a valid http:// or https:// URL.", "Tokdash", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        var s = Store.Settings;
        bool urlChanged = s.BaseURL != url;
        bool requestedLaunch = LaunchBox.IsChecked == true;
        bool launchChanged = s.LaunchAtLogin != requestedLaunch;

        s.BaseURL = url;
        s.LowQuotaNotifications = NotifyBox.IsChecked == true;
        s.Thresholds = new QuotaThresholds(
            (int)FiveHourSlider.Value,
            (int)WeeklySlider.Value,
            (int)OtherSlider.Value);
        if (launchChanged)
        {
            bool actualLaunch = await LaunchAtLogin.SetEnabledAsync(requestedLaunch);
            s.LaunchAtLogin = actualLaunch;
            if (requestedLaunch && !actualLaunch)
            {
                MessageBox.Show(
                    "Windows did not enable launch at login. Check Settings > Apps > Startup.",
                    "Tokdash",
                    MessageBoxButton.OK,
                    MessageBoxImage.Information);
            }
        }
        s.Save();
        if (urlChanged)
        {
            Store.UpdateBaseURL(s.BaseURL);
            _ = Store.RefreshAsync();
        }
        else
        {
            // Thresholds affect Low-quota selection; re-render with the new values.
            _ = Store.RefreshAsync();
        }
        Close();
    }

    private void Cancel_Click(object sender, RoutedEventArgs e) => Close();
}
