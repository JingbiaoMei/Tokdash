using System.Runtime.InteropServices;

namespace TokdashCompanion;

/// <summary>
/// Win32 Shell_NotifyIconW interop for the notification-area icon.
/// Isolated interop layer - the Windows App SDK has no modern tray abstraction.
/// Uses NOTIFYICON_VERSION_4 so mouse/keyboard activation follow current semantics.
/// </summary>
internal static class NotifyIcon
{
    public const uint NIM_ADD = 0x00000000;
    public const uint NIM_MODIFY = 0x00000001;
    public const uint NIM_DELETE = 0x00000002;
    public const uint NIM_SETVERSION = 0x00000004;

    public const uint NIF_MESSAGE = 0x00000001;
    public const uint NIF_ICON = 0x00000002;
    public const uint NIF_TIP = 0x00000004;
    public const uint NIF_INFO = 0x00000010;
    public const uint NIF_SHOWTIP = 0x00000080;

    public const uint WM_USER = 0x0400;
    public const uint WM_TRAYICON = WM_USER + 20;

    // NOTIFYICON_VERSION_4 callbacks: wParam = icon id, lParam = message + x/y.
    public const uint WM_CONTEXTMENU = 0x007B;
    public const uint WM_LBUTTONUP = 0x0202;
    public const uint WM_LBUTTONDOWN = 0x0201;
    public const uint WM_RBUTTONUP = 0x0205;
    public const uint WM_RBUTTONDOWN = 0x0204;
    public const uint NIN_SELECT = WM_USER + 0;
    public const uint NIN_KEYSELECT = WM_USER + 1;
    public const uint NIN_BALLOONUSERCLICK = WM_USER + 5;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct NOTIFYICONDATA
    {
        public int cbSize;
        public IntPtr hWnd;
        public uint uID;
        public uint uFlags;
        public uint uCallbackMessage;
        public IntPtr hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string szTip;
        public uint dwState;
        public uint dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string szInfo;
        public uint uTimeoutOrVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string szInfoTitle;
        public uint dwInfoFlags;
        // GUID item; left as default for V3 compatibility.
        public Guid guidItem;
        // NOTIFYICONDATA ends with HICON hBalloonIcon (Vista+, the version-4 field).
        // A trailing string here would inflate cbSize and corrupt the layout the shell reads.
        public IntPtr hBalloonIcon;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool Shell_NotifyIconW(uint dwMessage, ref NOTIFYICONDATA lpData);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr LoadIconW(IntPtr hInstance, IntPtr lpIconName);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr LoadImageW(IntPtr hInst, IntPtr lpsz, uint uType, int cxDesired, int cyDesired, uint fuLoad);

    public const uint IMAGE_ICON = 1;
    public const uint LR_LOADFROMFILE = 0x00000010;
    public const uint LR_DEFAULTSIZE = 0x00000040;

    public static NOTIFYICONDATA Create(IntPtr hwnd, uint id, IntPtr hIcon, string tip)
    {
        var data = new NOTIFYICONDATA
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATA>(),
            hWnd = hwnd,
            uID = id,
            uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP,
            uCallbackMessage = WM_TRAYICON,
            hIcon = hIcon,
            szTip = tip.Length > 127 ? tip[..127] : tip,
            uTimeoutOrVersion = 4, // NOTIFYICON_VERSION_4
        };
        return data;
    }
}
