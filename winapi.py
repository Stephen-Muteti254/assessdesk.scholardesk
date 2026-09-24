"""Windows native API helpers via ctypes.
Handles win32 display affinity (capture evasion), window style modifications,
focus prevention, and native WNDPROC subclassing for WM_PRINT interception.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import logging

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# --- Constants ---
GWL_EXSTYLE       = -20
GWL_STYLE         = -16
GWLP_WNDPROC      = -4

WS_POPUP          = 0x80000000
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020   # Click-through
WS_EX_TOOLWINDOW  = 0x00000080   # Hide from Alt-Tab / Taskbar
WS_EX_NOACTIVATE  = 0x08000000   # Never steal keyboard focus

WDA_NONE               = 0x00
WDA_MONITOR            = 0x01   # Legacy: black window in captures
WDA_EXCLUDEFROMCAPTURE = 0x11   # Fully invisible in captures (Win10 2004+)

SW_SHOWNOACTIVATE = 0x04

# --- WM_PRINT Interception Constants ---
WM_PAINT       = 0x000F
WM_ERASEBKGND  = 0x0014
WM_PRINT       = 0x0317
WM_PRINTCLIENT = 0x0318

WHITE_BRUSH    = 0
TRACE_PRINT_MESSAGES = False

# --- CTypes Prototypes ---
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, wt.DWORD]
user32.SetWindowDisplayAffinity.restype = wt.BOOL

user32.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
user32.GetWindowDisplayAffinity.restype = wt.BOOL

user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long

user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long

if ctypes.sizeof(ctypes.c_void_p) == 8:
    user32.GetWindowLongPtrW = user32.GetWindowLongPtrW
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

    user32.SetWindowLongPtrW = user32.SetWindowLongPtrW
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

    user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
else:
    user32.GetWindowLongPtrW = user32.GetWindowLongW
    user32.SetWindowLongPtrW = user32.SetWindowLongW
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.CallWindowProcW.restype = ctypes.c_long

user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL

user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL

user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT), ctypes.c_void_p]
user32.FillRect.restype = ctypes.c_int

gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = ctypes.c_void_p

_wndproc_hooks = {}


def windows_build() -> int:
    return sys.getwindowsversion().build


def supports_capture_exclusion() -> bool:
    return sys.getwindowsversion().build >= 19041


def install_wm_print_blocker(widget):
    """Subclasses the target window's native WNDPROC once to block WM_PRINT."""
    if widget is None:
        return
    hwnd = int(widget.winId())
    if hwnd in _wndproc_hooks:
        return

    old_proc = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

    logging.info("[winapi] Installing WNDPROC hook on HWND: %s", hex(hwnd))
    logging.debug("[winapi] Original WNDPROC handle: %s", hex(old_proc))

    def py_wndproc(hwnd_in, msg, wparam, lparam):
        if TRACE_PRINT_MESSAGES and msg in (WM_PRINT, WM_PRINTCLIENT,
                                            WM_ERASEBKGND, WM_PAINT):
            logging.debug("[trace] msg=0x%X wParam=0x%X lParam=0x%X",
                         msg, wparam, lparam)

        if msg in (WM_PRINT, WM_PRINTCLIENT):
            # proctor asked us to render into THEIR DC — paint it white
            logging.info("[winapi] WM_PRINT intercepted (msg=0x%X)", msg)
            if wparam:
                rect = wt.RECT()
                user32.GetClientRect(hwnd_in, ctypes.byref(rect))
                user32.FillRect(wparam, ctypes.byref(rect),
                                gdi32.GetStockObject(WHITE_BRUSH))
            return 0   # message handled

        # everything else (incl. WM_ERASEBKGND) -> Qt's original proc
        return user32.CallWindowProcW(old_proc, hwnd_in, msg, wparam, lparam)

    new_proc = WNDPROC(py_wndproc)

    widget._wndproc_ref = new_proc        # keep alive (GC would crash wndproc)
    _wndproc_hooks[hwnd] = (new_proc, old_proc)

    proc_ptr = ctypes.cast(new_proc, ctypes.c_void_p).value
    prev_proc = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, proc_ptr)
    if prev_proc == 0:
        err = ctypes.get_last_error()
        logging.error("[winapi] SetWindowLongPtrW FAILED, GetLastError = %d", err)


def apply_stealth(widget, click_through: bool = True, layered: bool = True) -> bool:
    hwnd = int(widget.winId())
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_POPUP)

    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style = ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if click_through:
        ex_style |= WS_EX_TRANSPARENT
    if layered:
        ex_style |= WS_EX_LAYERED
    else:
        ex_style &= ~WS_EX_LAYERED
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)

    ok = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    if not ok:
        err = ctypes.get_last_error()
        logging.error(
            "[winapi] SetWindowDisplayAffinity FAILED, GetLastError=%d, "
            "win build=%d, exstyles=0x%08X",
            err, windows_build(),
            user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
    install_wm_print_blocker(widget)
    return ok


def show_without_activation(widget):
    user32.ShowWindow(int(widget.winId()), SW_SHOWNOACTIVATE)


def is_hidden_from_capture(widget) -> bool:
    val = wt.DWORD()
    user32.GetWindowDisplayAffinity(int(widget.winId()), ctypes.byref(val))
    return val.value == WDA_EXCLUDEFROMCAPTURE