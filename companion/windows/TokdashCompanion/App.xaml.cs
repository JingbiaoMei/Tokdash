using System.IO;
using System.Windows;
using System.Windows.Threading;

namespace TokdashCompanion;

/// Error diagnostics for a windowless tray process. The file is capped so a persistent
/// failure cannot grow it without bound.
internal static class Diag
{
    private const long MaxBytes = 256 * 1024;
    private static readonly object Gate = new();
    private static readonly string Path =
        System.IO.Path.Combine(System.IO.Path.GetTempPath(), "tokdash-flyout.log");

    public static void Log(string message)
    {
        try
        {
            lock (Gate)
            {
                if (File.Exists(Path) && new FileInfo(Path).Length >= MaxBytes)
                    File.WriteAllText(Path, "");
                File.AppendAllText(Path, $"{DateTime.Now:HH:mm:ss.fff}  {message}{Environment.NewLine}");
            }
        }
        catch { }
    }
}

/// <summary>
/// WPF application. Program.cs owns the tray host + Win32 message loop;
/// this App provides the WPF flyout. Single-instance: Program checks for
/// an existing instance before launching.
/// </summary>
public partial class App : Application
{
    private FlyoutWindow? _flyout;
    private SettingsWindow? _settings;

    public CompanionStore Store { get; } = new();

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        // A tray app has no window to show a crash in, so an unhandled exception just
        // makes it vanish. Record it before the process goes down.
        DispatcherUnhandledException += (_, args) =>
            Diag.Log($"FATAL dispatcher {args.Exception.GetType().Name}: {args.Exception.Message}\n{args.Exception.StackTrace}");
        AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            Diag.Log($"FATAL domain {(args.ExceptionObject as Exception)?.GetType().Name}: " +
                     $"{(args.ExceptionObject as Exception)?.Message}\n{(args.ExceptionObject as Exception)?.StackTrace}");
        // No first refresh here: the scheduler's 2s timer drives the first fetch, so
        // kicking one here would just be canceled and restarted by that tick. OnStartup
        // does fire now that Program.Main calls app.Run() to pump the Dispatcher.
    }

    public void ToggleFlyout(int x, int y)
    {
        if (_flyout is null || !_flyout.IsVisible)
        {
            // Hold a local reference: the Closed handler nulls _flyout, and a window that
            // closes during Show() would otherwise leave _flyout null for anything below.
            var flyout = new FlyoutWindow { Store = Store };
            _flyout = flyout;
            flyout.Closed += (_, _) => { _flyout = null; Store.SetOpen(false); };
            flyout.PositionNear(x, y);
            // Mark open BEFORE showing: Show() runs Loaded -> UpdateView, which reads
            // FreshnessText, and the closed state would render the 600s "· stale" window.
            Store.SetOpen(true);
            flyout.Show();
            // The scheduler refreshes immediately if the data is stale (>60s); don't
            // force a second refresh on every open.
        }
        else
        {
            _flyout.Dismiss();
            _flyout = null;
            Store.SetOpen(false);
        }
    }

    public void CloseFlyout()
    {
        _flyout?.Dismiss();
        _flyout = null;
        Store.SetOpen(false);
    }

    /// <summary>Open the flyout if it isn't already visible (used by notification
    /// activation). Does not close an already-open flyout.</summary>
    public void EnsureFlyoutOpen(int x, int y)
    {
        if (_flyout is null || !_flyout.IsVisible) ToggleFlyout(x, y);
    }

    /// <summary>Show one Settings window and force its first activation to the foreground.</summary>
    public void ShowSettings()
    {
        CloseFlyout();

        if (_settings is null)
        {
            var settings = new SettingsWindow { Store = Store };
            settings.Closed += (_, _) => _settings = null;
            _settings = settings;
            settings.Show();
        }

        if (_settings.WindowState == WindowState.Minimized)
            _settings.WindowState = WindowState.Normal;

        // A tray process has no foreground main window. Briefly making Settings topmost
        // lets Windows activate it reliably; drop topmost immediately so it behaves like
        // a normal settings window afterwards.
        _settings.Topmost = true;
        _settings.Activate();
        _settings.Focus();
        var shown = _settings;
        shown.Dispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, () =>
        {
            if (shown.IsVisible) shown.Topmost = false;
        });
    }

    public void Quit() => Shutdown();
}
