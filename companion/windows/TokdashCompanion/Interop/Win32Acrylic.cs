using System.Runtime.InteropServices;

namespace TokdashCompanion.Interop;

/// <summary>
/// Win32 SetWindowCompositionAttribute interop for Acrylic backdrop on Windows 11.
/// Isolated; falls back to the XAML translucent solid background if this fails.
/// </summary>
internal static class Win32Acrylic
{
    [DllImport("user32.dll")]
    internal static extern int SetWindowCompositionAttribute(IntPtr hwnd, ref WindowCompositionAttributeData data);
}

[StructLayout(LayoutKind.Sequential)]
internal struct WindowCompositionAttributeData
{
    public int Attribute;
    public IntPtr Data;
    public int SizeOfData;
}

[StructLayout(LayoutKind.Sequential)]
internal struct AccentPolicy
{
    public int AccentState;
    public int AccentFlags;
    public uint GradientColor;
    public int AnimationId;
}
