using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32;

namespace TokdashCompanion;

/// <summary>
/// Win32 tray host: owns the Shell_NotifyIconW lifecycle and the message loop.
/// Left-click toggles the WinUI 3 flyout (managed by App); right-click opens
/// the native context menu. This is the resident process - it never shows a
/// taskbar button or main window.
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

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr LoadImageW(IntPtr hInst, string lpszName, uint uType, int cxDesired, int cyDesired, uint fuLoad);

    private const uint IMAGE_ICON = 1;
    private const uint LR_LOADFROMFILE = 0x00000010;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostQuitMessage(int nExitCode);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern uint TrackPopupMenuEx(IntPtr hMenu, uint uFlags, int x, int y, IntPtr hwnd, IntPtr lptpm);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr CreatePopupMenu();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyMenu(IntPtr hMenu);

    [DllImport("user32.dll")]
    private static extern bool AppendMenuW(IntPtr hMenu, uint uFlags, uint uIDNewItem, [MarshalAs(UnmanagedType.LPWStr)] string lpNewItem);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

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
    private static App? _app;
    // The window-procedure delegate must stay rooted: native code keeps the callback
    // pointer after RegisterClassW returns, so a GC-collected temporary would crash.
    private static readonly WndProcDelegate _wndProc = WndProc;

    [STAThread]
    private static void Main()
    {
        // Single-instance guard: a second launch (e.g. at login while already
        // running) exits silently instead of spawning a second tray icon.
        using var singleton = new Mutex(initiallyOwned: true, name: @"Global\TokdashCompanion_SingleInstance", out bool createdNew);
        if (!createdNew) return;

        // WPF Application must be created on the STA thread before any UI is used.
        // InitializeComponent() loads App.xaml, which sets ShutdownMode=OnExplicitShutdown.
        // Without it, WPF's default OnLastWindowClose takes effect: closing the first
        // flyout begins shutdown, and the next tray click's new FlyoutWindow() throws
        // "The Application object is being shut down." Program owns the Win32 message
        // loop, so app.Run() is intentionally not called.
        var app = new App();
        app.InitializeComponent();
        _app = app;

        var hInstance = GetModuleHandleW(null);
        var wc = new WNDCLASS
        {
            lpfnWndProc = _wndProc,
            hInstance = hInstance,
            lpszClassName = "TokdashCompanionHidden",
        };
        ushort atom = RegisterClassW(ref wc);
        if (atom == 0)
        {
            MessageBoxW(IntPtr.Zero, $"RegisterClass failed: {Marshal.GetLastWin32Error()}", "Tokdash", 0x10);
            return;
        }

        _hwnd = CreateWindowExW(0, (IntPtr)atom, IntPtr.Zero, 0, 0, 0, 0, 0,
            new IntPtr(-3) /* HWND_MESSAGE */, IntPtr.Zero, hInstance, IntPtr.Zero);
        if (_hwnd == IntPtr.Zero)
        {
            MessageBoxW(IntPtr.Zero, $"CreateWindow failed: {Marshal.GetLastWin32Error()}", "Tokdash", 0x10);
            return;
        }

        _hIcon = LoadCustomIcon() ?? LoadIconW(IntPtr.Zero, new IntPtr(IDI_APPLICATION));
        _nid = NotifyIcon.Create(_hwnd, 1, _hIcon, "Tokdash - connecting…");
        _added = NotifyIcon.Shell_NotifyIconW(NotifyIcon.NIM_ADD, ref _nid);
        if (!_added)
        {
            MessageBoxW(IntPtr.Zero, $"Shell_NotifyIconW failed: {Marshal.GetLastWin32Error()}", "Tokdash", 0x10);
        }

        // Activate NOTIFYICON_VERSION_4 callbacks. Without this the shell delivers
        // legacy callbacks (no coordinates); with it, the cursor x/y arrive in wParam
        // and the mouse message in LOWORD(lParam). Must be sent after NIM_ADD.
        NotifyIcon.Shell_NotifyIconW(NotifyIcon.NIM_SETVERSION, ref _nid);

        // Start the resident refresh scheduler (60s while open, 10min while closed, backoff on failure).
        app.Store.UIDispatcher = app.Dispatcher;
        app.Store.StartScheduler();

        // Sleep/wake: on resume, fire one coalesced refresh (RefreshAsync cancels any
        // in-flight request) so stale post-sleep data refreshes promptly. Periodic work
        // is naturally paused while the system sleeps - timers don't fire. Spec §cadence.
        SystemEvents.PowerModeChanged += (_, e) =>
        {
            if (e.Mode == PowerModes.Resume)
                app.Dispatcher.BeginInvoke(() => _ = app.Store.RefreshAsync());
        };

        // Keep the tray tooltip in sync with connection state + usage.
        app.Store.PropertyChanged += (_, _) => app.Dispatcher.BeginInvoke(UpdateTooltip);
        // Opt-in low-quota notifications: show a tray balloon when a window crosses its threshold.
        app.Store.LowQuotaAlert += rows => app.Dispatcher.BeginInvoke(() => ShowLowQuotaBalloon(rows));

        // Kick off the first refresh so the tooltip updates with real data.
        _ = app.Store.RefreshAsync();

        // Message loop.
        while (GetMessage(out MSG msg, IntPtr.Zero, 0, 0) > 0)
        {
            TranslateMessage(ref msg);
            DispatchMessage(ref msg);
        }

        if (_added) NotifyIcon.Shell_NotifyIconW(NotifyIcon.NIM_DELETE, ref _nid);
        if (_hIcon != IntPtr.Zero) DestroyIcon(_hIcon);
        DestroyWindow(_hwnd);
    }

    /// <summary>Load the Tokdash tray icon from the deployed Assets/tray.ico; fall back to null (caller uses the system icon).</summary>
    private static IntPtr? LoadCustomIcon()
    {
        string path = Path.Combine(AppContext.BaseDirectory, "Assets", "tray.ico");
        if (!File.Exists(path)) return null;
        IntPtr h = LoadImageW(IntPtr.Zero, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE);
        return h == IntPtr.Zero ? null : h;
    }

    private static IntPtr WndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == NotifyIcon.WM_TRAYICON)
        {
            var (x, y, mouseMsg) = ParseTrayCallback(wParam, lParam);
            if (mouseMsg == NotifyIcon.WM_LBUTTONUP || mouseMsg == NotifyIcon.NIN_SELECT || mouseMsg == NotifyIcon.NIN_KEYSELECT)
            {
                _app?.ToggleFlyout(x, y);
            }
            else if (mouseMsg == NotifyIcon.NIN_BALLOONUSERCLICK)
            {
                // Balloon-click cursor coords are undefined under v4: anchor on the
                // icon's own rect (via Shell_NotifyIconGetRect) and open the Low view.
                // Open (don't toggle) so clicking a notification never closes an
                // already-open flyout.
                if (_app is not null) _app.Store.QuotaView = QuotaView.Low;
                if (TryGetTrayIconRect(out int ix, out int iy)) _app?.EnsureFlyoutOpen(ix, iy);
                else _app?.EnsureFlyoutOpen(x, y);
            }
            else if (mouseMsg == NotifyIcon.WM_RBUTTONUP || mouseMsg == NotifyIcon.WM_CONTEXTMENU)
            {
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
        AppendMenuW(hMenu, MF_SEPARATOR, 0, "sep");
        AppendMenuW(hMenu, MF_STRING, IDM_EXIT, "Exit");

        SetForegroundWindow(_hwnd);
        uint cmd = TrackPopupMenuEx(hMenu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_RETURNCMD, x, y, _hwnd, IntPtr.Zero);
        DestroyMenu(hMenu);

        switch (cmd)
        {
            case IDM_EXIT:
                _app?.CloseFlyout();
                PostQuitMessage(0);
                break;
            case IDM_OPEN:
                _app?.ToggleFlyout(x, y);
                break;
            case IDM_REFRESH:
                _ = _app?.Store.RefreshAsync();
                break;
            case IDM_SETTINGS:
                _app?.Dispatcher.BeginInvoke(new Action(() => new SettingsWindow { Store = _app!.Store }.Show()));
                break;
        }
    }

    // NOTIFYICON_VERSION_4 callback layout: wParam holds the cursor screen coords
    // (x in LOWORD, y in HIWORD); lParam holds the mouse message in LOWORD and the
    // icon id in HIWORD (ignored here). Pure so the parsing can be unit-tested.
    internal static (int X, int Y, uint MouseMsg) ParseTrayCallback(IntPtr wParam, IntPtr lParam)
    {
        int x = SignedLOWORD(wParam);
        int y = SignedHIWORD(wParam);
        uint mouseMsg = (uint)(lParam.ToInt64() & 0xFFFF);
        return (x, y, mouseMsg);
    }

    private static int SignedLOWORD(IntPtr p) => unchecked((short)(int)(long)p);
    private static int SignedHIWORD(IntPtr p) => unchecked((short)(((int)(long)p) >> 16));

    /// <summary>Refresh the tray tooltip from the store's current state + usage.</summary>
    private static void UpdateTooltip()
    {
        if (!_added || _app is null) return;
        var store = _app.Store;
        string tip = store.Snapshot is { Today.TotalTokens: > 0 } snap
            ? $"Tokdash - Today {snap.TodayCostText} · {snap.TodayTokensCompact} tokens"
            : store.ConnectionState switch
            {
                ConnectionState.Connecting => "Tokdash - connecting…",
                ConnectionState.Connected => "Tokdash - No usage yet",
                ConnectionState.Busy => "Tokdash - Busy",
                ConnectionState.Offline => "Tokdash - Offline",
                ConnectionState.WrongService => "Tokdash - Not Tokdash",
                _ => "Tokdash",
            };
        if (tip.Length > 127) tip = tip[..127];
        _nid.szTip = tip;
        NotifyIcon.Shell_NotifyIconW(NotifyIcon.NIM_MODIFY, ref _nid);
    }

    /// <summary>Show a tray balloon for newly-low quota windows (opt-in notifications).</summary>
    private static void ShowLowQuotaBalloon(IReadOnlyList<QuotaRow> rows)
    {
        if (!_added || rows.Count == 0) return;
        var first = rows[0];
        string body = rows.Count == 1
            ? $"{first.Provider} {first.BucketLabel} is at {(int)first.Left}% remaining."
            : $"{rows.Count} subscription windows are low. {first.Provider} {first.BucketLabel} at {(int)first.Left}%.";
        var nid = _nid;
        nid.uFlags = NotifyIcon.NIF_MESSAGE | NotifyIcon.NIF_ICON | NotifyIcon.NIF_TIP | NotifyIcon.NIF_INFO;
        nid.szInfo = body.Length > 200 ? body[..200] : body;
        nid.szInfoTitle = "Tokdash - low quota";
        nid.dwInfoFlags = 0;
        NotifyIcon.Shell_NotifyIconW(NotifyIcon.NIM_MODIFY, ref nid);
    }

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

    [DllImport("shell32.dll")]
    private static extern int Shell_NotifyIconGetRect(ref NOTIFYICONIDENTIFIER identifier, out RECT iconRect);

    [StructLayout(LayoutKind.Sequential)]
    private struct NOTIFYICONIDENTIFIER
    {
        public int cbSize;
        public IntPtr hWnd;
        public uint uID;
        public Guid guidItem;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    /// <summary>Get the tray icon's bounding rect (for balloon-click anchoring). Returns false if unavailable.</summary>
    private static bool TryGetTrayIconRect(out int x, out int y)
    {
        var id = new NOTIFYICONIDENTIFIER
        {
            cbSize = Marshal.SizeOf<NOTIFYICONIDENTIFIER>(),
            hWnd = _hwnd,
            uID = 1,
            guidItem = Guid.Empty,
        };
        int rc = Shell_NotifyIconGetRect(ref id, out RECT r);
        if (rc != 0) { x = 0; y = 0; return false; }
        x = r.Right;
        y = r.Bottom;
        return true;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);
}
