using System.Windows;

namespace TokdashCompanion;

/// <summary>
/// WPF settings window: base URL, launch-at-login, low-quota notifications,
/// and per-bucket alert thresholds. Bound to the shared CompanionSettings.
/// </summary>
public partial class SettingsWindow : Window
{
    public CompanionStore Store { get; set; } = null!;

    public SettingsWindow()
    {
        InitializeComponent();
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
        FiveHourLabel.Text = $"5-hour: {(int)FiveHourSlider.Value}%";
        WeeklyLabel.Text = $"Weekly: {(int)WeeklySlider.Value}%";
        OtherLabel.Text = $"Default: {(int)OtherSlider.Value}%";
    }

    private void Save_Click(object sender, RoutedEventArgs e)
    {
        var url = BaseUrlBox.Text.Trim();
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || (uri.Scheme != "http" && uri.Scheme != "https"))
        {
            MessageBox.Show("Enter a valid http:// or https:// URL.", "Tokdash", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        var s = Store.Settings;
        bool urlChanged = s.BaseURL != url;
        bool launchChanged = s.LaunchAtLogin != (LaunchBox.IsChecked == true);

        s.BaseURL = url;
        s.LaunchAtLogin = LaunchBox.IsChecked == true;
        s.LowQuotaNotifications = NotifyBox.IsChecked == true;
        s.Thresholds = new QuotaThresholds(
            (int)FiveHourSlider.Value,
            (int)WeeklySlider.Value,
            (int)OtherSlider.Value);
        s.Save();

        if (launchChanged) LaunchAtLogin.SetEnabled(s.LaunchAtLogin);
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
