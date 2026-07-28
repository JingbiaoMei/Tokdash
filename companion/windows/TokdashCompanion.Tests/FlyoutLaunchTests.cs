using System.Threading;
using System.Windows;
using System.Windows.Threading;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

[TestClass]
public class FlyoutLaunchTests
{
    /// <summary>
    /// Reproduces the repeated-click crash. With ShutdownMode left at its WPF
    /// default (OnLastWindowClose), closing the first flyout begins shutdown, and
    /// the next tray click's new FlyoutWindow() throws
    /// "The Application object is being shut down." Program.Main loads App.xaml via
    /// InitializeComponent() so ShutdownMode=OnExplicitShutdown applies; this test
    /// mirrors that and exercises open -> close -> open -> close on the STA thread.
    /// </summary>
    [TestMethod]
    public void ToggleFlyout_SurvivesRepeatedOpenClose()
    {
        Exception? caught = null;
        App? app = null;

        var t = new Thread(() =>
        {
            try
            {
                // Mirror Program.Main: load App.xaml so ShutdownMode=OnExplicitShutdown.
                app = new App();
                app.InitializeComponent();
                Assert.AreEqual(
                    ShutdownMode.OnExplicitShutdown,
                    app.ShutdownMode,
                    "App.xaml must load OnExplicitShutdown; otherwise closing the flyout shuts the app down.");

                // open -> close -> open -> close, pumping between so Window.Closed fires.
                // The third toggle (re-open after a close) is what crashed.
                app.ToggleFlyout(100, 100);
                Pump();
                app.ToggleFlyout(100, 100); // close
                Pump();
                app.ToggleFlyout(100, 100); // re-open
                Pump();
                app.ToggleFlyout(100, 100); // close
                Pump();
            }
            catch (Exception ex)
            {
                caught = ex;
            }
            finally
            {
                Dispatcher.CurrentDispatcher.BeginInvokeShutdown(DispatcherPriority.Background);
                Dispatcher.Run();
            }
        });
        t.SetApartmentState(ApartmentState.STA);
        t.Start();
        t.Join();

        Assert.IsNull(caught, $"Repeated toggle threw: {caught?.Message}");
        Assert.IsNotNull(app);
    }

    /// <summary>Process queued Dispatcher work (including Window.Closed) until idle.</summary>
    private static void Pump()
    {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }
}
