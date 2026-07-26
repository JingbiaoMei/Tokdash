using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace TokdashCompanion;

/// <summary>Color parsing helper for runtime hex strings (#RRGGBB / #AARRGGBB).</summary>
internal static class MediaHelper
{
    public static Color ColorFromString(string hex)
    {
        string h = hex.TrimStart('#');
        return h.Length switch
        {
            6 => Color.FromArgb(255, Convert.ToByte(h[0..2], 16), Convert.ToByte(h[2..4], 16), Convert.ToByte(h[4..6], 16)),
            8 => Color.FromArgb(Convert.ToByte(h[0..2], 16), Convert.ToByte(h[2..4], 16), Convert.ToByte(h[4..6], 16), Convert.ToByte(h[6..8], 16)),
            _ => Colors.Transparent,
        };
    }
}
