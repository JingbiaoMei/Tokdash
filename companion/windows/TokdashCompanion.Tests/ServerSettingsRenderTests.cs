using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls.Primitives;
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
                window.UpdateLayout();
                var scroller = (ScrollViewer)window.FindName("SettingsScrollViewer");
                Assert.IsTrue(scroller.ScrollableHeight > 0);
                var bar = (System.Windows.Controls.Primitives.ScrollBar)scroller.Template.FindName("PART_VerticalScrollBar", scroller);
                bar.ApplyTemplate(); bar.UpdateLayout();
                Assert.AreEqual(12.0, bar.Width);
                var track = (System.Windows.Controls.Primitives.Track)bar.Template.FindName("PART_Track", bar);
                Assert.IsNotNull(track);
                track.Thumb.ApplyTemplate();
                window.UpdateLayout();
                Pump();
                var handle = (Border)track.Thumb.Template.FindName("Handle", track.Thumb);
                Assert.IsTrue(handle.ActualWidth > 0 && handle.ActualWidth <= 6.1,
                    $"Expected a painted 6px thumb; handle={handle.ActualWidth}, thumb={track.Thumb.ActualWidth}, track={track.ActualWidth}, bar={bar.ActualWidth}");
                scroller.ScrollToVerticalOffset(100);
                window.UpdateLayout();
                Assert.IsTrue(scroller.VerticalOffset > 0, "Settings must remain scrollable");
                scroller.ScrollToTop(); window.UpdateLayout();
                var panel = (StackPanel)window.FindName("ServersPanel");
                var editors = Descendants<TextBox>(panel).ToList();
                Assert.AreEqual(5, editors.Count); // two names, three addresses
                foreach (var editor in editors)
                {
                    var right = editor.TranslatePoint(new Point(editor.ActualWidth, 0), panel).X;
                    Assert.IsTrue(right <= panel.ActualWidth + 1, "Address editor overflowed the settings window");
                }
                // The custom templates must preserve native interaction and automation.
                var toggle = (CheckBox)window.FindName("NotifyBox");
                var togglePeer = new CheckBoxAutomationPeer(toggle);
                var toggleProvider = (IToggleProvider)togglePeer.GetPattern(PatternInterface.Toggle);
                toggleProvider.Toggle();
                Assert.AreEqual(true, toggle.IsChecked);
                toggleProvider.Toggle();
                Assert.AreEqual(false, toggle.IsChecked);
                var slider = (Slider)window.FindName("FiveHourSlider");
                var before = slider.Value;
                Slider.IncreaseSmall.Execute(null, slider);
                Assert.IsTrue(slider.Value > before);
                var sliderTrack = (Track)slider.Template.FindName("PART_Track", slider);
                Assert.AreEqual(slider.Value, sliderTrack.Value, "Track must follow the slider's native value binding");
                var routing = Descendants<ComboBox>(panel).First();
                var dropDownButton = (ToggleButton)routing.Template.FindName("DropDownToggle", routing);
                var dropDownPeer = new ToggleButtonAutomationPeer(dropDownButton);
                ((IToggleProvider)dropDownPeer.GetPattern(PatternInterface.Toggle)).Toggle();
                Assert.IsTrue(routing.IsDropDownOpen, "Clicking the dropdown must update the ComboBox");
                Pump(); window.UpdateLayout();
                var popup = (Popup)routing.Template.FindName("PART_Popup", routing);
                Assert.IsTrue(popup.IsOpen && popup.Child.IsVisible, "Route choices must open in the custom dropdown");
                Assert.IsTrue(popup.Child.RenderSize.Width <= routing.ActualWidth + 1, "Long addresses must not widen the dropdown");
                routing.SelectedIndex = 1;
                Assert.IsInstanceOfType<ComboBoxItem>(routing.SelectedItem);
                routing.IsDropDownOpen = false;
                if (Environment.GetEnvironmentVariable("TOKDASH_RENDER_DIR") is { Length: > 0 } renderDir)
                {
                    System.IO.Directory.CreateDirectory(renderDir);
                    var language = (ComboBox)window.FindName("LanguageCombo");
                    language.Items.Add("System language"); language.SelectedIndex = 0;
                    foreach (bool dark in new[] { true, false })
                    {
                        window.ApplyTheme(dark);
                        foreach (bool bottom in new[] { false, true })
                        {
                            if (bottom) scroller.ScrollToBottom(); else scroller.ScrollToTop();
                            Pump(); window.UpdateLayout();
                            var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32);
                            bitmap.Render(window);
                            var encoder = new PngBitmapEncoder();
                            encoder.Frames.Add(BitmapFrame.Create(bitmap));
                            string name = $"windows-settings-{(dark ? "dark" : "light")}-{(bottom ? "bottom" : "top")}.png";
                            using var file = System.IO.File.Create(System.IO.Path.Combine(renderDir, name));
                            encoder.Save(file);
                        }
                    }
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
