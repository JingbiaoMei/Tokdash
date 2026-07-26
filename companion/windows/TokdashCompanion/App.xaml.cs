using Microsoft.UI.Xaml;

namespace TokdashCompanion;

/// <summary>
/// WinUI 3 application. The tray host (Program.cs) owns the process lifetime
/// and message loop; this App provides the flyout content. Single-instance:
/// Program checks for an existing instance before launching WinUI.
/// </summary>
public partial class App : Application
{
    private FlyoutWindow? _flyout;

    public App()
    {
        InitializeComponent();
    }

    public CompanionStore Store { get; } = new();

    public void ToggleFlyout(int x, int y)
    {
        if (_flyout is null)
        {
            _flyout = new FlyoutWindow { Store = Store };
            _flyout.Closed += (_, _) => _flyout = null;
            _flyout.PositionNear(x, y);
            _flyout.Activate();
            _ = Store.RefreshAsync();
        }
        else
        {
            _flyout.Close();
            _flyout = null;
        }
    }

    public void CloseFlyout()
    {
        _flyout?.Close();
        _flyout = null;
    }
}
