using System.Windows;

namespace TokdashCompanion;

/// <summary>
/// WPF application. Program.cs owns the tray host + Win32 message loop;
/// this App provides the WPF flyout. Single-instance: Program checks for
/// an existing instance before launching.
/// </summary>
public partial class App : Application
{
    private FlyoutWindow? _flyout;

    public CompanionStore Store { get; } = new();

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        // Initial refresh so the tooltip and flyout have data.
        _ = Store.RefreshAsync();
    }

    public void ToggleFlyout(int x, int y)
    {
        if (_flyout is null || !_flyout.IsVisible)
        {
            _flyout = new FlyoutWindow { Store = Store };
            _flyout.Closed += (_, _) => { _flyout = null; Store.SetOpen(false); };
            _flyout.PositionNear(x, y);
            _flyout.Show();
            Store.SetOpen(true);
            // The scheduler refreshes immediately if the data is stale (>60s); don't
            // force a second refresh on every open.
        }
        else
        {
            _flyout.Close();
            _flyout = null;
            Store.SetOpen(false);
        }
    }

    public void CloseFlyout()
    {
        _flyout?.Close();
        _flyout = null;
        Store.SetOpen(false);
    }

    /// <summary>Open the flyout if it isn't already visible (used by notification
    /// activation). Does not close an already-open flyout.</summary>
    public void EnsureFlyoutOpen(int x, int y)
    {
        if (_flyout is null || !_flyout.IsVisible) ToggleFlyout(x, y);
    }

    public void Quit() => Shutdown();
}
