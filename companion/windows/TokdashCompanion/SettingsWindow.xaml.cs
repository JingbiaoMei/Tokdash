using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
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
    private readonly List<ServerRow> _serverRows = new();

    private sealed record ServerRow(
        CompanionServerSettings Model,
        CheckBox Enabled,
        TextBox Label,
        TextBox Url,
        TextBlock Result,
        StackPanel Container);

    public CompanionStore Store { get; set; } = null!;

    public SettingsWindow()
    {
        InitializeComponent();
        ApplyTheme();
        ApplyWindowIcon();
        SourceInitialized += (_, _) => ApplyDwmTheme();
        Loaded += SettingsWindow_Loaded;
    }

    private async void SettingsWindow_Loaded(object sender, RoutedEventArgs e)
    {
        var s = Store.Settings;
        // Registry/StartupTask is authoritative. This also removes a stale portable
        // Run entry if the extracted directory was moved.
        s.LaunchAtLogin = await LaunchAtLogin.GetEnabledAsync();
        BaseUrlBox.Text = s.BaseURL;
        RenderServerRows(s.Servers);
        LaunchBox.IsChecked = s.LaunchAtLogin;
        NotifyBox.IsChecked = s.LowQuotaNotifications;
        FiveHourSlider.Value = s.Thresholds.FiveHour;
        WeeklySlider.Value = s.Thresholds.Weekly;
        OtherSlider.Value = s.Thresholds.Other;
        AutoUpdateBox.IsChecked = s.AutomaticUpdateChecks;
        // Store builds swap the whole Updates section for a read-only version line: the
        // Store owns update delivery, so every control in that section is redundant there.
        bool packaged = PackagedApp.IsPackaged;
        UpdatesSection.Visibility = packaged ? Visibility.Collapsed : Visibility.Visible;
        PackagedVersionSection.Visibility = packaged ? Visibility.Visible : Visibility.Collapsed;
        PopulateLanguageCombo();
        ApplySettingsStrings();
        // Keep the Updates section live while the window is open, so "Check now" reports its
        // own result without a reopen.
        Store.PropertyChanged += Store_PropertyChanged;
        Closed += (_, _) => Store.PropertyChanged -= Store_PropertyChanged;
        RenderUpdateSection();
    }

    private void Store_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(CompanionStore.UpdateStatus) or nameof(CompanionStore.ShowsUpdateBadge))
            Dispatcher.BeginInvoke(RenderUpdateSection);
    }

    /// <summary>
    /// Render the Updates status line and its actions. An available version outranks a
    /// Failed/Idle status: a manual check that later fails must not hide an update we
    /// already know about.
    /// </summary>
    private void RenderUpdateSection()
    {
        if (UpdateStatusText is null) return;   // not constructed yet
        string? available = Store.UpdateAvailableVersion;
        var status = Store.UpdateStatus;

        CheckUpdateBtn.IsEnabled = status.Kind != UpdateStatusKind.Checking;
        ViewUpdateBtn.Visibility = available is null ? Visibility.Collapsed : Visibility.Visible;
        SkipUpdateBtn.Visibility = available is null ? Visibility.Collapsed : Visibility.Visible;
        LastCheckedText.Text = Store.LastUpdateCheckText;

        if (status.Kind == UpdateStatusKind.Checking)
        {
            SetUpdateStatus(L10n.T("update_checking"), "SettingsMuted");
        }
        else if (available is not null)
        {
            SetUpdateStatus(L10n.T("update_available", available), "SettingsText");
        }
        else if (Store.Settings.SkippedUpdateVersion is { Length: > 0 } skipped
                 && skipped == Store.Settings.AvailableUpdateVersion)
        {
            SetUpdateStatus(L10n.T("update_skipped"), "SettingsMuted");
        }
        else if (status.Kind == UpdateStatusKind.Failed)
        {
            SetUpdateStatus(status.Message ?? L10n.T("update_failed_generic"), "SettingsError");
        }
        else if (status.Kind == UpdateStatusKind.UpToDate)
        {
            SetUpdateStatus(L10n.T("update_up_to_date"), "SettingsSuccess");
        }
        else
        {
            SetUpdateStatus(L10n.T("update_manual_hint"), "SettingsMuted");
        }
    }

    private void SetUpdateStatus(string text, string brushKey)
    {
        UpdateStatusText.Text = text;
        UpdateStatusText.Foreground = (Brush)FindResource(brushKey);
    }

    private async void CheckUpdate_Click(object sender, RoutedEventArgs e)
    {
        // Persist the opt-in first so a "Check now" from a freshly-ticked box doesn't get
        // undone by Cancel, then check regardless of the 24h throttle.
        Store.SetAutomaticUpdateChecks(AutoUpdateBox.IsChecked == true);
        await Store.CheckForUpdatesAsync(manual: true);
    }

    private void ViewUpdate_Click(object sender, RoutedEventArgs e) => Store.OpenUpdatePage();

    private void SkipUpdate_Click(object sender, RoutedEventArgs e)
    {
        if (Store.UpdateAvailableVersion is { } version) Store.SkipUpdate(version);
        RenderUpdateSection();
    }

    /// <summary>Localize the static XAML literals. The window closes on Save, so a language
    /// change takes effect here on the next open; the flyout updates live via the store.</summary>
    private void ApplySettingsStrings()
    {
        Title = L10n.T("settings_window_title");
        ServerLabel.Text = L10n.T("section_servers");
        AddServerButton.Content = L10n.T("add_server");
        TestButton.Content = L10n.T("test");
        ServerHint.Text = L10n.T("server_hint");
        StartupLabel.Text = L10n.T("section_startup");
        LaunchBox.Content = L10n.T("launch_at_login");
        NotificationsLabel.Text = L10n.T("section_notifications");
        NotifyBox.Content = L10n.T("low_quota_notifications");
        NotifyHint.Text = L10n.T("low_quota_hint");
        ThresholdsLabel.Text = L10n.T("section_thresholds");
        UpdatesLabel.Text = L10n.T("section_updates");
        CurrentVersionText.Text = L10n.T("update_current_version", UpdateChecker.CurrentVersion);
        AutoUpdateBox.Content = L10n.T("update_auto_check");
        AutoUpdateHint.Text = L10n.T("update_auto_check_hint");
        CheckUpdateBtn.Content = L10n.T("update_check_now");
        ViewUpdateBtn.Content = L10n.T("update_view");
        SkipUpdateBtn.Content = L10n.T("update_skip");
        PackagedVersionLabel.Text = L10n.T("section_version");
        PackagedVersionText.Text = L10n.T("update_current_version", UpdateChecker.CurrentVersion);
        PackagedVersionHint.Text = L10n.T("update_managed_by_store");
        LanguageLabel.Text = L10n.T("section_language");
        LanguageHint.Text = L10n.T("language_hint");
        CancelBtn.Content = L10n.T("cancel");
        SaveBtn.Content = L10n.T("save");
        UpdateSliderLabels();
    }

    /// <summary>Fill the language combo in the current language and select the saved setting.
    /// Index order matches the AppLanguage enum (System=0, English=1, ZhHans=2).</summary>
    private void PopulateLanguageCombo()
    {
        var langs = new[] { AppLanguage.System, AppLanguage.English, AppLanguage.ZhHans };
        LanguageCombo.Items.Clear();
        foreach (var lang in langs) LanguageCombo.Items.Add(lang.DisplayName());
        LanguageCombo.SelectedIndex = (int)Store.Settings.Language;
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
        FiveHourLabel.Text = L10n.T("threshold_5h", (int)FiveHourSlider.Value);
        WeeklyLabel.Text = L10n.T("threshold_weekly", (int)WeeklySlider.Value);
        OtherLabel.Text = L10n.T("threshold_other", (int)OtherSlider.Value);
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
            ShowTestResult(L10n.T("test_bad_url"), ok: false);
            return;
        }

        TestButton.IsEnabled = false;
        ShowTestResult(L10n.T("testing"), ok: null);
        try
        {
            using var probe = new TokdashClient(candidate);
            var health = await probe.HealthAsync();
            // A reachable server that isn't Tokdash is a failure, not a success -
            // otherwise a proxy or a wrong port would test green.
            if (health.Service == "tokdash")
                ShowTestResult(L10n.T("test_ok", CompanionStore.ServerLabel(candidate), health.Version), ok: true);
            else
                ShowTestResult(L10n.T("test_not_tokdash"), ok: false);
        }
        catch (Exception ex)
        {
            ShowTestResult(L10n.T("test_reachable_error", (ex as TokdashException)?.Error.ToString() ?? ex.Message), ok: false);
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

    private void RenderServerRows(IEnumerable<CompanionServerSettings> servers)
    {
        _serverRows.Clear();
        ServersPanel.Children.Clear();
        foreach (var server in servers) AddServerRow(server);
        if (_serverRows.Count == 0) AddServerRow(CompanionServerSettings.Create(CompanionSettings.DefaultBaseURL));
    }

    private void AddServerRow(CompanionServerSettings server)
    {
        var enabled = new CheckBox { IsChecked = server.Enabled, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0) };
        var label = new TextBox { Text = server.Label, Width = 72, Margin = new Thickness(0, 0, 6, 0) };
        var url = new TextBox { Text = server.BaseUrl, MinWidth = 190 };
        var test = new Button { Content = L10n.T("test"), Padding = new Thickness(9, 2, 9, 2), Margin = new Thickness(6, 0, 0, 0) };
        var remove = new Button { Content = "−", Padding = new Thickness(8, 2, 8, 2), Margin = new Thickness(4, 0, 0, 0) };
        var line = new StackPanel { Orientation = Orientation.Horizontal };
        line.Children.Add(enabled); line.Children.Add(label); line.Children.Add(url); line.Children.Add(test); line.Children.Add(remove);
        var result = new TextBlock { FontSize = 10, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(22, 3, 0, 0), Visibility = Visibility.Collapsed };
        var container = new StackPanel { Margin = new Thickness(0, 0, 0, 7) };
        container.Children.Add(line); container.Children.Add(result);
        var row = new ServerRow(server, enabled, label, url, result, container);
        _serverRows.Add(row); ServersPanel.Children.Add(container);
        test.Click += async (_, _) => await TestServerRowAsync(row, test);
        remove.Click += (_, _) => {
            if (_serverRows.Count <= 1) return;
            _serverRows.Remove(row); ServersPanel.Children.Remove(container);
        };
    }

    private async Task TestServerRowAsync(ServerRow row, Button button)
    {
        string candidate = row.Url.Text.Trim();
        row.Result.Visibility = Visibility.Visible;
        if (!CompanionStore.IsValidBaseURL(candidate)) { row.Result.Text = L10n.T("test_bad_url"); row.Result.Foreground = (Brush)FindResource("SettingsError"); return; }
        button.IsEnabled = false;
        try
        {
            using var probe = new TokdashClient(candidate);
            var health = await probe.HealthAsync();
            bool ok = health.Service == "tokdash";
            row.Result.Text = ok ? L10n.T("test_ok", CompanionStore.ServerLabel(candidate), health.Version) : L10n.T("test_not_tokdash");
            row.Result.Foreground = (Brush)FindResource(ok ? "SettingsSuccess" : "SettingsError");
        }
        catch (Exception ex) { row.Result.Text = L10n.T("test_reachable_error", ex.Message); row.Result.Foreground = (Brush)FindResource("SettingsError"); }
        finally { button.IsEnabled = true; }
    }

    private void AddServer_Click(object sender, RoutedEventArgs e) =>
        AddServerRow(CompanionServerSettings.Create(CompanionSettings.DefaultBaseURL));

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
        var entries = _serverRows.Select(row => new CompanionServerSettings
        {
            Id = row.Model.Id,
            Label = string.IsNullOrWhiteSpace(row.Label.Text) ? CompanionStore.ServerLabel(row.Url.Text) : row.Label.Text.Trim(),
            BaseUrl = row.Url.Text.Trim(),
            Enabled = row.Enabled.IsChecked == true,
        }).ToList();
        if (entries.Any(entry => !CompanionStore.IsValidBaseURL(entry.BaseUrl)) || !entries.Any(entry => entry.Enabled))
        {
            MessageBox.Show(L10n.T("valid_url"), "Tokdash", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        var s = Store.Settings;
        bool serversChanged = !ServerRegistriesEqual(s.Servers, entries);
        bool requestedLaunch = LaunchBox.IsChecked == true;
        bool launchChanged = s.LaunchAtLogin != requestedLaunch;

        s.Servers = entries;
        s.LowQuotaNotifications = NotifyBox.IsChecked == true;
        s.Thresholds = new QuotaThresholds(
            (int)FiveHourSlider.Value,
            (int)WeeklySlider.Value,
            (int)OtherSlider.Value);
        // Persists and kicks the first check when turned on, so it runs before the blanket
        // save below rather than through it.
        Store.SetAutomaticUpdateChecks(AutoUpdateBox.IsChecked == true);
        var newLang = (AppLanguage)Math.Clamp(LanguageCombo.SelectedIndex, 0, 2);
        bool langChanged = newLang != s.Language;
        if (langChanged) Store.ApplyLanguage(newLang);  // sets s.Language, persists, re-renders flyout
        if (launchChanged)
        {
            bool actualLaunch = await LaunchAtLogin.SetEnabledAsync(requestedLaunch);
            s.LaunchAtLogin = actualLaunch;
            if (requestedLaunch && !actualLaunch)
            {
                MessageBox.Show(
                    L10n.T("launch_failed"),
                    "Tokdash",
                    MessageBoxButton.OK,
                    MessageBoxImage.Information);
            }
        }
        s.Save();
        if (serversChanged)
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

    private static string ServerSignature(CompanionServerSettings server) =>
        $"{server.Id}\u001f{server.Label}\u001f{server.BaseUrl.Trim()}\u001f{server.Enabled}";

    internal static bool ServerRegistriesEqual(
        IEnumerable<CompanionServerSettings> left,
        IEnumerable<CompanionServerSettings> right) =>
        left.Select(ServerSignature).SequenceEqual(right.Select(ServerSignature));

    private void Cancel_Click(object sender, RoutedEventArgs e) => Close();
}
