using System.Threading;
using System.Windows;
using System.Windows.Threading;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

[TestClass]
public class FlyoutLaunchTests
{
    /// <summary>
    /// Reproduces the original crash: FlyoutWindow ctor subscribed to
    /// Store.PropertyChanged before Store was assigned via object initializer.
    /// Clicking the tray icon called ToggleFlyout -> new FlyoutWindow { Store }
    /// -> NullReferenceException -> app died. This test exercises the exact
    /// path on the STA thread WPF requires.
    /// </summary>
    [TestMethod]
    public void ToggleFlyout_CreatesAndAssignsStoreWithoutCrash()
    {
        Exception? caught = null;
        App? app = null;

        var t = new Thread(() =>
        {
            try
            {
                app = new App();
                app.ShutdownMode = ShutdownMode.OnExplicitShutdown;
                app.ToggleFlyout(100, 100);
                app.CloseFlyout();
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

        Assert.IsNull(caught, $"ToggleFlyout threw: {caught?.Message}");
        Assert.IsNotNull(app);
    }
}
