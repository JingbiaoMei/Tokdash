using System.Runtime.InteropServices;

namespace TokdashCompanion.Interop;

/// <summary>
/// Win32 DWM window-attribute interop: rounded corners, dark-mode border, and border
/// colour on Windows 11. Replaces the old SetWindowCompositionAttribute acrylic backdrop
/// (removed - it forced a layered window, which disables ClearType and reads as blurry
/// text next to every other app). These attributes are cheap, GPU-composited, Win11-only,
/// and callers must treat failures as non-fatal.
/// </summary>
internal static class Win32Dwm
{
    [DllImport("dwmapi.dll")]
    internal static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
}
