using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

[TestClass]
public class ServerSettingsRenderTests
{
    [TestMethod]
    public void ServerAddressesFitSettingsAndAddAddressWorks()
    {
        Exception? caught = null;
        var thread = new Thread(() => {
            SettingsWindow? window = null;
            try
            {
                window = new SettingsWindow(false) { Store = new CompanionStore(new FakeClient()) };
                window.RenderServerRows([
                    new() { Label = "Workstation", BaseUrl = "http://192.168.1.42:55423", Routes = ["https://workstation.tail-example.ts.net/tokdash"] },
                    new() { Label = "MacBook", BaseUrl = "http://127.0.0.1:55424" }
                ]);
                window.Show();
                Pump();
                var panel = (StackPanel)window.FindName("ServersPanel");
                var editors = Descendants<TextBox>(panel).ToList();
                Assert.AreEqual(5, editors.Count); // two names, three addresses
                foreach (var editor in editors)
                {
                    var right = editor.TranslatePoint(new Point(editor.ActualWidth, 0), panel).X;
                    Assert.IsTrue(right <= panel.ActualWidth + 1, "Address editor overflowed the settings window");
                }
                if (Environment.GetEnvironmentVariable("TOKDASH_RENDER_DIR") is { Length: > 0 } renderDir)
                {
                    System.IO.Directory.CreateDirectory(renderDir);
                    var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32);
                    bitmap.Render(window);
                    var encoder = new PngBitmapEncoder();
                    encoder.Frames.Add(BitmapFrame.Create(bitmap));
                    using var file = System.IO.File.Create(System.IO.Path.Combine(renderDir, "windows-settings.png"));
                    encoder.Save(file);
                }
                Descendants<Button>(panel).First(b => b.Content as string == "+").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                Pump();
                Assert.AreEqual(6, Descendants<TextBox>(panel).Count());
            }
            catch (Exception ex) { caught = ex; }
            finally { window?.Close(); Dispatcher.CurrentDispatcher.BeginInvokeShutdown(DispatcherPriority.Background); Dispatcher.Run(); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start(); thread.Join();
        Assert.IsNull(caught, caught?.ToString());
    }
    private static void Pump() { var frame = new DispatcherFrame(); Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false)); Dispatcher.PushFrame(frame); }
    private static IEnumerable<T> Descendants<T>(DependencyObject root) where T : DependencyObject
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++)
        {
            var child = VisualTreeHelper.GetChild(root, i);
            if (child is T found) yield return found;
            foreach (var nested in Descendants<T>(child)) yield return nested;
        }
    }
}
