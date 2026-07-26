using System.Runtime.InteropServices;

namespace TokdashCompanion;

/// <summary>
/// Technical spike: proves Shell_NotifyIconW activation, a hidden message-only
/// window, a native context menu, and clean exit. This is the Win32 interop
/// layer that the WinUI 3 flyout will sit on top of once the VS WinUI workload
/// is installed.
/// </summary>
internal static class Program
{
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern ushort RegisterClassW(ref WNDCLASS lpWndClass);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr CreateWindowExW(
        uint dwExStyle, IntPtr lpClassName, IntPtr lpWindowName,
        uint dwStyle, int x, int y, int nWidth, int nHeight,
        IntPtr hWndParent, IntPtr hMenu, IntPtr hInstance, IntPtr lpParam);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern int GetMessage(out MSG msg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage(ref MSG msg);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessage(ref MSG msg);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr LoadIconW(IntPtr hInstance, IntPtr lpIconName);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostQuitMessage(int nExitCode);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern uint TrackPopupMenuEx(IntPtr hMenu, uint uFlags, int x, int y, IntPtr hwnd, IntPtr lptpm);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr CreatePopupMenu();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyMenu(IntPtr hMenu);

    [DllImport("user32.dll")]
    private static extern bool AppendMenuW(IntPtr hMenu, uint uFlags, uint uIDNewItem, IntPtr lpNewItem);

    [DllImport("user32.dll")]
    private static extern bool AppendMenuW(IntPtr hMenu, uint uFlags, uint uIDNewItem, [MarshalAs(UnmanagedType.LPWStr)] string lpNewItem);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);

    private const uint MW_HANDLERS = 0x00000000;
    private const int IDI_APPLICATION = 32512;
    private const uint MF_STRING = 0x00000000;
    private const uint MF_SEPARATOR = 0x00000800;
    private const uint TPM_RIGHTBUTTON = 0x0002;
    private const uint TPM_BOTTOMALIGN = 0x0020;
    private const uint TPM_RETURNCMD = 0x0100;
    private const int IDM_OPEN = 40001;
    private const int IDM_REFRESH = 40002;
    private const int IDM_SETTINGS = 40003;
    private const int IDM_EXIT = 40004;

    private static IntPtr _hwnd;
    private static IntPtr _hIcon;
    private static NotifyIcon.NOTIFYICONDATA _nid;
    private static bool _added;

    [STAThread]
    private static void Main()
    {
        var hInstance = GetModuleHandleW(null);
        var wc = new WNDCLASS
        {
            lpfnWndProc = WndProc,
            hInstance = hInstance,
            lpszClassName = "TokdashCompanionHidden",
        };
        ushort atom = RegisterClassW(ref wc);
        if (atom == 0)
        {
            Console.Error.WriteLine($"RegisterClass failed: {Marshal.GetLastWin32Error()}");
            return;
        }

        // Message-only window (HWND_MESSAGE parent) - no taskbar button, no focus.
        _hwnd = CreateWindowExW(0, (IntPtr)atom, IntPtr.Zero, 0, 0, 0, 0, 0,
            new IntPtr(-3) /* HWND_MESSAGE */, IntPtr.Zero, hInstance, IntPtr.Zero);
        if (_hwnd == IntPtr.Zero)
        {
            Console.Error.WriteLine($"CreateWindow failed: {Marshal.GetLastWin32Error()}");
            return;
        }

        // Load a standard system icon for the spike (production uses the Tokdash logo).
        _hIcon = LoadIconW(IntPtr.Zero, new IntPtr(IDI_APPLICATION));
        _nid = NotifyIcon.Create(_hwnd, 1, _hIcon, "Tokdash - spike");
        _added = NotifyIcon.Shell_NotifyIconW(NotifyIcon.NIM_ADD, ref _nid);

        Console.WriteLine($"Tray icon added: {_added}");
        if (!_added)
        {
            Console.Error.WriteLine($"Shell_NotifyIconW NIM_ADD failed: {Marshal.GetLastWin32Error()}");
        }

        // Message loop.
        while (GetMessage(out MSG msg, IntPtr.Zero, 0, 0) > 0)
        {
            TranslateMessage(ref msg);
            DispatchMessage(ref msg);
        }

        if (_added)
        {
            NotifyIcon.Shell_NotifyIconW(NotifyIcon.NIM_DELETE, ref _nid);
        }
        if (_hIcon != IntPtr.Zero) DestroyIcon(_hIcon);
        DestroyWindow(_hwnd);
    }

    private static IntPtr WndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == NotifyIcon.WM_TRAYICON)
        {
            // NOTIFYICON_VERSION_4: wParam = icon id, lParam = message + screen coords.
            int x = SignedLOWORD(lParam);
            int y = SignedHIWORD(lParam);
            uint mouseMsg = (uint)(lParam.ToInt64() & 0xFFFF);
            if (mouseMsg == NotifyIcon.WM_LBUTTONUP || mouseMsg == NotifyIcon.NIN_SELECT)
            {
                Console.WriteLine($"Left-click at ({x},{y}) - would open flyout");
            }
            else if (mouseMsg == NotifyIcon.WM_RBUTTONUP || mouseMsg == NotifyIcon.WM_CONTEXTMENU)
            {
                Console.WriteLine($"Right-click at ({x},{y}) - opening context menu");
                ShowContextMenu(x, y);
            }
            return IntPtr.Zero;
        }

        if (msg == 0x0002 /* WM_DESTROY */)
        {
            PostQuitMessage(0);
            return IntPtr.Zero;
        }

        return DefWindowProcW(hWnd, msg, wParam, lParam);
    }

    private static void ShowContextMenu(int x, int y)
    {
        var hMenu = CreatePopupMenu();
        if (hMenu == IntPtr.Zero) return;
        AppendMenuW(hMenu, MF_STRING, IDM_OPEN, "Open Tokdash");
        AppendMenuW(hMenu, MF_STRING, IDM_REFRESH, "Refresh");
        AppendMenuW(hMenu, MF_STRING, IDM_SETTINGS, "Settings");
        AppendMenuW(hMenu, MF_SEPARATOR, 0, IntPtr.Zero);
        AppendMenuW(hMenu, MF_STRING, IDM_EXIT, "Exit");

        // Set foreground window (required for tray menu to dismiss on outside click).
        SetForegroundWindow(_hwnd);
        uint cmd = TrackPopupMenuEx(hMenu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_RETURNCMD, x, y, _hwnd, IntPtr.Zero);
        DestroyMenu(hMenu);

        if (cmd == IDM_EXIT)
        {
            PostQuitMessage(0);
        }
        else if (cmd == IDM_OPEN)
        {
            Console.WriteLine("Open Tokdash clicked");
        }
        else if (cmd == IDM_REFRESH)
        {
            Console.WriteLine("Refresh clicked");
        }
        else if (cmd == IDM_SETTINGS)
        {
            Console.WriteLine("Settings clicked");
        }
    }

    private static int SignedLOWORD(IntPtr p) => unchecked((short)(int)(long)p);
    private static int SignedHIWORD(IntPtr p) => unchecked((short)(((int)(long)p) >> 16));

    [StructLayout(LayoutKind.Sequential)]
    private struct WNDCLASS
    {
        public uint style;
        public WndProcDelegate lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        public string? lpszMenuName;
        public string? lpszClassName;
    }

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public int pt_x;
        public int pt_y;
    }

    [DllImport("user32.dll")]
    private static extern IntPtr DefWindowProcW(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);
}
