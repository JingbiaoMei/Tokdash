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

    private readonly System.Windows.Threading.DispatcherTimer _routeTimer = new() { Interval = TimeSpan.FromSeconds(30) };
    private readonly CancellationTokenSource _routeLifetime = new();
    private bool _checkingRoutes;
    private sealed class ServerRow(CompanionServerSettings model, CheckBox enabled, TextBox label,
        TextBlock result, StackPanel container, ComboBox routing)
    {
        public CompanionServerSettings Model = model;
        public CheckBox Enabled = enabled;
        public TextBox Label = label;
        public TextBlock Result = result;
        public StackPanel Container = container;
        public ComboBox Routing = routing;
        public List<(TextBox Input, TextBlock Status)> Addresses = [];
        public TextBox Url => Addresses[0].Input;
    }

    public CompanionStore Store { get; set; } = null!;

    public SettingsWindow() : this(true) { }

    internal SettingsWindow(bool initializeNativeSettings)
    {
        InitializeComponent();
        ApplyTheme();
        ApplySettingsStrings();
        ApplyWindowIcon();
        SourceInitialized += (_, _) => ApplyDwmTheme();
        if (initializeNativeSettings) Loaded += SettingsWindow_Loaded;
        _routeTimer.Tick += async (_, _) => await CheckRoutesAsync();
        Closed += (_, _) => { _routeTimer.Stop(); _routeLifetime.Cancel(); };
    }

    private async void SettingsWindow_Loaded(object sender, RoutedEventArgs e)
    {
        MaxHeight = Math.Max(420, SystemParameters.WorkArea.Height - 24);
        Height = Math.Min(850, MaxHeight);
        var s = Store.Settings;
        // Registry/StartupTask is authoritative. This also removes a stale portable
        // Run entry if the extracted directory was moved.
        s.LaunchAtLogin = await LaunchAtLogin.GetEnabledAsync();
        BaseUrlBox.Text = s.BaseURL;
        RenderServerRows(s.Servers);
        _routeTimer.Start();
        _ = CheckRoutesAsync();
        LaunchBox.IsChecked = s.LaunchAtLogin;
        NotifyBox.IsChecked = s.LowQuotaNotifications;
        FiveHourSlider.Value = s.Thresholds.FiveHour;
        WeeklySlider.Value = s.Thresholds.Weekly;
        OtherSlider.Value = s.Thresholds.Other;
        AutoUpdateBox.IsChecked = s.AutomaticUpdateChecks;
        // Components (schema v3): resolved reads, so an absent v2 file renders all-on.
        CompFullDeltaBox.IsChecked = s.Components.FullDeltaRowOn;
        CompTopRanksBox.IsChecked = s.Components.TopRanksOn;
        CompResetCreditsBox.IsChecked = s.Components.ResetCreditsOn;
        CompActivityGlanceBox.IsChecked = s.Components.ActivityGlanceOn;
        CompHistogramBox.IsChecked = s.Components.ActivityHistogramTodayWeekOn;
        CompPerServerBox.IsChecked = s.Components.PerServerRowsOn;
        // Components autosave on toggle (mirrors macOS onChange(of: components) -> save +
        // apply), so a flip survives Cancel and takes effect in the open flyout without a
        // refetch. Attached AFTER the initial IsChecked assignments above: the handler must
        // not fire for state the user never changed.
        foreach (var box in ComponentBoxes())
        {
            box.Checked += CompToggle_Changed;
            box.Unchecked += CompToggle_Changed;
        }
        // Rows per top-ranks list: same autosave live-edit as the toggles. The handler is
        // attached AFTER the initial Value assignment so the load itself never round-trips
        // through Save().
        RankRowsSlider.Value = s.RankRows;
        RankRowsSlider.ValueChanged += RankRowsSlider_Changed;
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
        RenderServerDiagnostics();
        // Settings-open only (contract E7): GET /api/version, then /api/update-check when the
        // server itself has consent. Never a schedule, never a POST, silent on failure.
        _ = Store.FetchServerUpdateInfoAsync();
    }

    private IEnumerable<CheckBox> ComponentBoxes() => new[]
    {
        CompFullDeltaBox, CompTopRanksBox, CompResetCreditsBox,
        CompActivityGlanceBox, CompHistogramBox, CompPerServerBox,
    };

    /// <summary>
    /// A components toggle was flipped: persist immediately (schema v3, explicit resolved
    /// booleans) and re-render the flyout from last-good data without a refetch. Writes all
    /// six together so the file always carries the full explicit set once the user has
    /// touched the section at all.
    /// </summary>
    private void CompToggle_Changed(object sender, RoutedEventArgs e)
    {
        if (Store is null) return;
        Store.Settings.Components = new CompanionComponents
        {
            FullDeltaRow = CompFullDeltaBox.IsChecked == true,
            TopRanks = CompTopRanksBox.IsChecked == true,
            ResetCredits = CompResetCreditsBox.IsChecked == true,
            ActivityGlance = CompActivityGlanceBox.IsChecked == true,
            ActivityHistogramTodayWeek = CompHistogramBox.IsChecked == true,
            PerServerRows = CompPerServerBox.IsChecked == true,
        };
        Store.Settings.Save();
        Store.ApplyComponentsChange();
    }

    /// <summary>The rank-rows slider moved: relabel, persist immediately (same live-edit
    /// contract as the component toggles) and re-render the flyout from last-good data,
    /// which rebuilds both rank lists with the new row count - the window grows to fit.</summary>
    private void RankRowsSlider_Changed(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (Store is null || RankRowsLabel is null) return;
        int rows = (int)RankRowsSlider.Value;   // slider range is 3..8, snap-to-tick
        RankRowsLabel.Text = L10n.T("rank_rows", rows);
        Store.Settings.RankRows = rows;
        Store.Settings.Save();
        Store.ApplyComponentsChange();
    }

    private void RenderServerDiagnostics()
    {
        if (ServerRuntimeText is null) return;
        if (Store.ServerRuntimeVersion is { Length: > 0 } version)
        {
            ServerRuntimeText.Text = $"{L10n.T("server_runtime_row")} · {L10n.T("server_runtime_version", version)}";
            ServerRuntimeText.Visibility = Visibility.Visible;
        }
        else
        {
            ServerRuntimeText.Visibility = Visibility.Collapsed;
        }
        if (Store.ServerUpdateBadgeText is { Length: > 0 } badge)
        {
            ServerUpdateRow.Text = badge;
            ServerUpdateRow.Visibility = Visibility.Visible;
        }
        else
        {
            ServerUpdateRow.Visibility = Visibility.Collapsed;
        }
    }

    private void Store_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(CompanionStore.UpdateStatus) or nameof(CompanionStore.ShowsUpdateBadge))
            Dispatcher.BeginInvoke(RenderUpdateSection);
        if (e.PropertyName is nameof(CompanionStore.ServerRuntimeVersion) or nameof(CompanionStore.ServerUpdateBadgeVersion))
            Dispatcher.BeginInvoke(RenderServerDiagnostics);
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
        ServerHint.Text = L10n.T("route_hint");
        StartupLabel.Text = L10n.T("section_startup");
        LaunchBox.Content = L10n.T("launch_at_login");
        NotificationsLabel.Text = L10n.T("section_notifications");
        NotifyBox.Content = L10n.T("low_quota_notifications");
        NotifyHint.Text = L10n.T("low_quota_hint");
        ComponentsLabel.Text = L10n.T("section_components");
        CompFullDeltaBox.Content = L10n.T("comp_full_delta_row");
        CompFullDeltaDesc.Text = L10n.T("comp_full_delta_row_desc");
        CompTopRanksBox.Content = L10n.T("comp_top_ranks");
        CompTopRanksDesc.Text = L10n.T("comp_top_ranks_desc");
        if (RankRowsLabel is not null)
            RankRowsLabel.Text = L10n.T("rank_rows", (int)RankRowsSlider.Value);
        CompResetCreditsBox.Content = L10n.T("comp_reset_credits");
        CompResetCreditsDesc.Text = L10n.T("comp_reset_credits_desc");
        CompActivityGlanceBox.Content = L10n.T("comp_activity_glance");
        CompActivityGlanceDesc.Text = L10n.T("comp_activity_glance_desc");
        CompHistogramBox.Content = L10n.T("comp_histogram_today_week");
        CompHistogramDesc.Text = L10n.T("comp_histogram_today_week_desc");
        CompPerServerBox.Content = L10n.T("comp_per_server_rows");
        CompPerServerDesc.Text = L10n.T("comp_per_server_rows_desc");
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

    internal void RenderServerRows(IEnumerable<CompanionServerSettings> servers)
    {
        _serverRows.Clear();
        ServersPanel.Children.Clear();
        foreach (var server in servers) AddServerRow(server);
        if (_serverRows.Count == 0) AddServerRow(CompanionServerSettings.Create(CompanionSettings.DefaultBaseURL));
    }

    private void AddServerRow(CompanionServerSettings original)
    {
        // Edits stay local until Save, including route tests and host merging.
        var server = System.Text.Json.JsonSerializer.Deserialize<CompanionServerSettings>(
            System.Text.Json.JsonSerializer.Serialize(original))!;
        var enabled = new CheckBox { IsChecked = server.Enabled, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) };
        var label = new TextBox { Text = server.Label, FontWeight = FontWeights.SemiBold, BorderThickness = new Thickness(0) };
        var add = new Button { Content = "+", ToolTip = L10n.T("route_add"), Padding = new Thickness(8, 2, 8, 2), Margin = new Thickness(0, 0, 8, 0) };
        var header = new DockPanel();
        DockPanel.SetDock(enabled, Dock.Left); DockPanel.SetDock(add, Dock.Left);
        header.Children.Add(enabled); header.Children.Add(add); header.Children.Add(label);
        var result = new TextBlock { FontSize = 11, TextWrapping = TextWrapping.Wrap, Visibility = Visibility.Collapsed };
        var container = new StackPanel { Margin = new Thickness(0, 0, 0, 14) };
        var routing = new ComboBox { Margin = new Thickness(22, 4, 0, 0), FontSize = 11 };
        var row = new ServerRow(server, enabled, label, result, container, routing);
        container.Children.Add(header); container.Children.Add(result);
        _serverRows.Add(row); ServersPanel.Children.Add(container);
        foreach (var address in server.Addresses) AddAddressRow(row, address);
        if (row.Addresses.Count == 0) AddAddressRow(row, server.BaseUrl);
        container.Children.Add(routing);
        RefreshRouteChoices(row);
        add.Click += (_, _) => { AddAddressRow(row, ""); RefreshRouteChoices(row); row.Addresses[^1].Input.Focus(); };
        enabled.Unchecked += (_, _) => {
            if (!_serverRows.Any(r => r.Enabled.IsChecked == true)) enabled.IsChecked = true;
        };
    }

    private void AddAddressRow(ServerRow row, string address)
    {
        var url = new TextBox { Text = address, MinWidth = 140, ToolTip = L10n.T("route_add_hint"), BorderThickness = new Thickness(0) };
        var status = new TextBlock { FontSize = 10, Margin = new Thickness(0, 2, 0, 0), Foreground = (Brush)FindResource("SettingsMuted") };
        var test = new Button { Content = L10n.T("test"), Padding = new Thickness(6, 2, 6, 2), Margin = new Thickness(6, 0, 0, 0) };
        var remove = new Button { Content = "×", ToolTip = L10n.T("route_remove"), Padding = new Thickness(6, 2, 6, 2), Margin = new Thickness(4, 0, 0, 0) };
        var line = new DockPanel();
        DockPanel.SetDock(remove, Dock.Right); DockPanel.SetDock(test, Dock.Right);
        line.Children.Add(remove); line.Children.Add(test); line.Children.Add(url);
        var block = new StackPanel { Margin = new Thickness(22, 6, 0, 0) };
        block.Children.Add(line); block.Children.Add(status);
        row.Container.Children.Insert(Math.Max(2, row.Container.Children.Count - (row.Container.Children.Contains(row.Routing) ? 1 : 0)), block);
        row.Addresses.Add((url, status));
        test.Click += async (_, _) => { test.IsEnabled = false; await CheckRoutesAsync(); test.IsEnabled = true; };
        url.LostKeyboardFocus += (_, _) => RefreshRouteChoices(row);
        remove.Click += (_, _) => {
            if (row.Addresses.Count == 1)
            {
                if (_serverRows.Count == 1) return;
                if (MessageBox.Show(L10n.T("route_remove_last"), "Tokdash", MessageBoxButton.OKCancel) != MessageBoxResult.OK) return;
                _serverRows.Remove(row); ServersPanel.Children.Remove(row.Container);
                if (!_serverRows.Any(r => r.Enabled.IsChecked == true)) _serverRows[0].Enabled.IsChecked = true;
            }
            else { row.Addresses.RemoveAll(r => r.Input == url); row.Container.Children.Remove(block); RefreshRouteChoices(row); }
        };
    }

    private static void RefreshRouteChoices(ServerRow row)
    {
        string selected = (row.Routing.SelectedItem as ComboBoxItem)?.Tag as string ?? row.Model.PreferredRoute ?? "";
        row.Routing.Items.Clear();
        row.Routing.Items.Add(new ComboBoxItem { Content = L10n.T("route_auto"), Tag = "" });
        foreach (var address in row.Addresses.Select(r => r.Input.Text.Trim()).Where(CompanionStore.IsValidBaseURL).Distinct())
            row.Routing.Items.Add(new ComboBoxItem { Content = address, Tag = address });
        row.Routing.SelectedItem = row.Routing.Items.Cast<ComboBoxItem>().FirstOrDefault(i => (string)i.Tag == selected) ?? row.Routing.Items[0];
    }

    private async Task CheckRoutesAsync()
    {
        if (_checkingRoutes || _routeLifetime.IsCancellationRequested) return;
        _checkingRoutes = true;
        try
        {
            var rows = _serverRows.ToList();
            var inputs = rows.SelectMany(row => row.Addresses.Select(a => (Row: row, a.Input, a.Status, Text: a.Input.Text.Trim().TrimEnd('/')))).ToList();
            foreach (var input in inputs) input.Status.Text = L10n.T("testing");
            var results = await Task.WhenAll(inputs.Select(async item => {
                var candidates = item.Text.Contains("://") ? new[] { item.Text } : new[] { "https://" + item.Text, "http://" + item.Text };
                RouteProbe? probe = null;
                foreach (var candidate in candidates.Where(CompanionStore.IsValidBaseURL))
                {
                    probe = await TokdashClient.ProbeAsync(candidate, _routeLifetime.Token);
                    if (probe.Health is not null) break;
                }
                return (Item: item, Probe: probe);
            }));
            if (_routeLifetime.IsCancellationRequested) return;
            foreach (var row in rows.Where(_serverRows.Contains))
            {
                var current = results.Where(r => r.Item.Row == row && row.Addresses.Any(a => a.Input == r.Item.Input) && r.Item.Input.Text.Trim().TrimEnd('/') == r.Item.Text).ToList();
                var identity = row.Model.InstanceId ?? current.FirstOrDefault(r => r.Item.Input == row.Url).Probe?.Health?.InstanceId;
                row.Model.InstanceId = identity;
                var good = current.Where(r => r.Probe?.Health is not null &&
                    (!string.IsNullOrEmpty(identity) ? r.Probe.Health.InstanceId == identity : r.Item.Input == row.Url)).ToList();
                string? preferred = (row.Routing.SelectedItem as ComboBoxItem)?.Tag as string;
                var active = good.OrderBy(r => r.Probe!.Address == preferred ? 0 : 1).ThenBy(r => r.Probe!.Milliseconds).FirstOrDefault().Probe;
                foreach (var result in current)
                {
                    bool accepted = good.Contains(result);
                    result.Item.Status.Text = accepted
                        ? $"{Math.Max(1, (int)result.Probe!.Milliseconds)} ms" + (result.Probe == active ? " · " + L10n.T("route_active") : "")
                        : L10n.T(result.Probe?.Health is null ? "route_offline" : "route_mismatch");
                    result.Item.Status.Foreground = (Brush)FindResource(accepted ? "SettingsSuccess" : "SettingsMuted");
                    if (accepted) result.Item.Input.Text = result.Probe!.Address;
                }
                RefreshRouteChoices(row);
            }
            // Only successful, current identity evidence joins two host cards.
            var known = new Dictionary<string, ServerRow>();
            foreach (var row in rows.Where(_serverRows.Contains))
            {
                if (row.Model.InstanceId is not { Length: > 0 } id || !results.Any(r => r.Item.Row == row && r.Item.Input.Text.Trim() == r.Probe?.Address && r.Probe?.Health?.InstanceId == id)) continue;
                if (!known.TryGetValue(id, out var target)) { known[id] = row; continue; }
                foreach (var address in row.Addresses.Select(a => a.Input.Text.Trim()))
                    if (!target.Addresses.Any(a => a.Input.Text.Trim() == address)) AddAddressRow(target, address);
                target.Enabled.IsChecked = target.Enabled.IsChecked == true || row.Enabled.IsChecked == true;
                _serverRows.Remove(row); ServersPanel.Children.Remove(row.Container); RefreshRouteChoices(target);
            }
        }
        catch (OperationCanceledException) when (_routeLifetime.IsCancellationRequested) { }
        finally { _checkingRoutes = false; }
    }

    private void AddServer_Click(object sender, RoutedEventArgs e) =>
        AddServerRow(new CompanionServerSettings { BaseUrl = "", Label = L10n.T("server_unnamed") });

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
            BaseUrl = row.Url.Text.Trim().TrimEnd('/'),
            Enabled = row.Enabled.IsChecked == true,
            Routes = row.Addresses.Skip(1).Select(a => a.Input.Text.Trim().TrimEnd('/')).ToList(),
            PreferredRoute = ((row.Routing.SelectedItem as ComboBoxItem)?.Tag as string)?.TrimEnd('/'),
            InstanceId = row.Model.InstanceId,
        }).ToList();
        if (entries.Any(entry => new[] { entry.BaseUrl }.Concat(entry.Routes).Any(a => !CompanionStore.IsValidBaseURL(a))) || !entries.Any(entry => entry.Enabled))
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
        // Components: always persisted as explicit resolved booleans (schema v3), so a
        // later file read never has to guess what an absent key meant for this build.
        s.Components = new CompanionComponents
        {
            FullDeltaRow = CompFullDeltaBox.IsChecked == true,
            TopRanks = CompTopRanksBox.IsChecked == true,
            ResetCredits = CompResetCreditsBox.IsChecked == true,
            ActivityGlance = CompActivityGlanceBox.IsChecked == true,
            ActivityHistogramTodayWeek = CompHistogramBox.IsChecked == true,
            PerServerRows = CompPerServerBox.IsChecked == true,
        };
        // Set by the live slider handler too; written again here so Save is always complete.
        s.RankRows = (int)RankRowsSlider.Value;
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
        $"{server.Id}\u001f{server.Label}\u001f{server.BaseUrl.Trim()}\u001f{server.Enabled}\u001f{string.Join("|", server.Routes)}\u001f{server.PreferredRoute}\u001f{server.InstanceId}";

    internal static bool ServerRegistriesEqual(
        IEnumerable<CompanionServerSettings> left,
        IEnumerable<CompanionServerSettings> right) =>
        left.Select(ServerSignature).SequenceEqual(right.Select(ServerSignature));

    private void Cancel_Click(object sender, RoutedEventArgs e) => Close();
}
