using System.Globalization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace TokdashCompanion;

/// <summary>A single-line, clipped notice that scrolls only when it overflows.</summary>
public sealed class MarqueeText : UserControl
{
    public static readonly DependencyProperty TextProperty = DependencyProperty.Register(
        nameof(Text), typeof(string), typeof(MarqueeText),
        new PropertyMetadata("", (d, _) => ((MarqueeText)d).Refresh()));

    public string Text
    {
        get => (string)GetValue(TextProperty);
        set => SetValue(TextProperty, value);
    }

    private readonly TextBlock _label = new() { TextWrapping = TextWrapping.NoWrap };
    private readonly TranslateTransform _position = new();
    private string? _lastText;
    private double _lastOverflow = -1;
    private bool _lastAnimationEnabled;

    public MarqueeText()
    {
        Focusable = false;
        HorizontalContentAlignment = HorizontalAlignment.Stretch;
        _label.HorizontalAlignment = HorizontalAlignment.Left;
        _label.RenderTransform = _position;
        Content = new Border { ClipToBounds = true, Child = _label };
        Loaded += (_, _) => Refresh();
        SizeChanged += (_, _) => Refresh();
        IsVisibleChanged += (_, _) => Refresh();
        Unloaded += (_, _) => Stop();
    }

    protected override void OnPropertyChanged(DependencyPropertyChangedEventArgs e)
    {
        base.OnPropertyChanged(e);
        if (_label is not null && (e.Property == FontSizeProperty || e.Property == FontFamilyProperty
            || e.Property == FontWeightProperty || e.Property == FontStyleProperty
            || e.Property == FontStretchProperty || e.Property == ForegroundProperty)) Refresh();
    }

    private void Stop()
    {
        _position.BeginAnimation(TranslateTransform.XProperty, null);
        _position.X = 0;
        _lastOverflow = -1;
    }

    private void Refresh()
    {
        _label.Text = Text;
        _label.FontFamily = FontFamily;
        _label.FontSize = FontSize;
        _label.FontWeight = FontWeight;
        _label.FontStyle = FontStyle;
        _label.FontStretch = FontStretch;
        _label.Foreground = Foreground;
        ToolTip = Text;
        AutomationProperties.SetName(this, Text);
        if (!IsLoaded || !IsVisible) { Stop(); return; }

        var measured = new FormattedText(Text, CultureInfo.CurrentUICulture, FlowDirection,
            new Typeface(FontFamily, FontStyle, FontWeight, FontStretch), FontSize,
            Foreground, VisualTreeHelper.GetDpi(this).PixelsPerDip);
        _label.Width = Math.Ceiling(measured.WidthIncludingTrailingWhitespace);
        double overflow = Math.Max(0, _label.Width - ActualWidth);
        bool animate = SystemParameters.ClientAreaAnimation;
        if (_lastText == Text && Math.Abs(_lastOverflow - overflow) < 0.1
            && _lastAnimationEnabled == animate) return;
        Stop();
        _lastText = Text;
        _lastOverflow = overflow;
        _lastAnimationEnabled = animate;
        if (overflow == 0 || !animate) return;

        double travel = overflow / 18;
        const double pause = 1.4;
        var animation = new DoubleAnimationUsingKeyFrames { RepeatBehavior = RepeatBehavior.Forever };
        animation.KeyFrames.Add(new LinearDoubleKeyFrame(0, KeyTime.FromTimeSpan(TimeSpan.Zero)));
        animation.KeyFrames.Add(new LinearDoubleKeyFrame(0, KeyTime.FromTimeSpan(TimeSpan.FromSeconds(pause))));
        animation.KeyFrames.Add(new LinearDoubleKeyFrame(-overflow, KeyTime.FromTimeSpan(TimeSpan.FromSeconds(pause + travel))));
        animation.KeyFrames.Add(new LinearDoubleKeyFrame(-overflow, KeyTime.FromTimeSpan(TimeSpan.FromSeconds(2 * pause + travel))));
        animation.KeyFrames.Add(new LinearDoubleKeyFrame(0, KeyTime.FromTimeSpan(TimeSpan.FromSeconds(2 * (pause + travel)))));
        Timeline.SetDesiredFrameRate(animation, 30);
        _position.BeginAnimation(TranslateTransform.XProperty, animation);
    }
}
