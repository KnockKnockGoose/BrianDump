"""Frameless host for the BrianDump panel: the same page in a WebView2 window with no title bar, so the whole top line is the
panel's own navy strip (Edge --app windows always draw a grey bar). Optional: `pip install pywebview pythonnet` and
"window": "webview" in config.json; launch.py starts it.

    pythonw briandump/frameless.py <url> <x> <y> <w> <h>

Dragging: the navy strip is a native caption (CSS `app-region: drag` through WebView2's non-client region support), so
drag-to-edge snapping, Snap Layouts at the top of the screen and double-click maximise all come from Windows itself.
Snap: the window gets a thick frame (collapsed to zero by WM_NCCALCSIZE), so Win+Left/Right and Snap Assist treat it like
any resizable window next to the terminal. Not always-on-top. Min / windowed<->right third / close: the panel's own
buttons -> server.py /api/window. External links never navigate the panel: they open in the default browser (sandbox:
recorded). Importing this module does nothing; pywebview is loaded by main().
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from briandump.config import CONF  # noqa: E402

AUMID = "BrianDump.Panel"
TITLE = "BrianDump · SANDBOX" if CONF.sandbox else "BrianDump"   # launch.py finds the window by this exact title
GRAB = 6                # px of resize grab zone at the window's edges (includes the 5 px padding)
_keep = {}              # the pywebview window, our window procedure and the original one (alive for the whole process)


# ---------- the window never leaves the panel ----------
def origin(url):
    u = urlsplit(str(url))
    return f"{u.scheme}://{u.netloc}".lower()


def leaves_panel(uri, panel_origin):
    """A top-level navigation to an http(s) page on another origin: cancelled and handed to the default browser."""
    return bool(re.match(r"https?://", str(uri), re.I)) and origin(uri) != panel_origin


def to_browser(uri):
    """Open an http(s) link in the default browser (sandbox: recorded). Anything else (file:, javascript:) is dropped."""
    uri = str(uri)
    if not re.match(r"https?://", uri, re.I):
        return False
    if CONF.record("open_url", url=uri[:500], via="frameless"):
        return True
    webbrowser.open(uri)
    return True


def install_hooks(ec, panel_origin):
    """Host-level navigation policy (it holds even if the page's own JS is stale) and native drag, patched into pywebview's
    EdgeChrome class: any navigation off the panel origin and any new-window request goes to the default browser."""
    nav, ready = ec.EdgeChrome.on_navigation_start, ec.EdgeChrome.on_webview_ready

    def on_navigation_start(self, sender, args):
        uri = str(args.Uri)
        if leaves_panel(uri, panel_origin):
            args.Cancel = True
            to_browser(uri)
            return None
        return nav(self, sender, args)

    def on_new_window_request(self, sender, args):
        args.set_Handled(True)
        to_browser(str(args.get_Uri()))

    # pywebview's own drag region moves the window from JS (SetWindowPos per mousemove), so Windows never sees a title-bar
    # drag: no Aero Snap, no Snap Layouts. WebView2's non-client region support turns CSS `app-region: drag` into a real
    # HTCAPTION, so Windows runs its own move loop.
    def on_webview_ready(self, sender, args):
        if args.IsSuccess:
            try:
                sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
            except Exception as e:  # noqa: BLE001 - older WebView2 SDK/runtime: pywebview's JS drag still works
                print("native drag:", e)
        return ready(self, sender, args)

    ec.EdgeChrome.on_navigation_start = on_navigation_start
    ec.EdgeChrome.on_new_window_request = on_new_window_request
    ec.EdgeChrome.on_webview_ready = on_webview_ready


# ---------- icon, resizable edges, remembered windowed rect ----------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]


def hit_code(px, py, r, edge=GRAB):
    """WM_NCHITTEST answer for a point near the window's edges (HTLEFT 10 ... HTBOTTOMRIGHT 17), None inside."""
    left, right, top, bottom = px < r.left + edge, px >= r.right - edge, py < r.top + edge, py >= r.bottom - edge
    return {(True, False, True, False): 13, (False, True, True, False): 14, (True, False, False, True): 16,
            (False, True, False, True): 17, (True, False, False, False): 10, (False, True, False, False): 11,
            (False, False, True, False): 12, (False, False, False, True): 15}.get((left, right, top, bottom))


def set_icon():
    """panel.ico on the window + taskbar (the WinForms form otherwise shows the Python icon)."""
    try:
        import clr  # noqa: F401  (pythonnet, already loaded by pywebview)
        from System import Action
        from System.Drawing import Icon
        win = _keep["win"]
        ico = Icon(str(CONF.panel_ico))
        win.native.Invoke(Action(lambda: setattr(win.native, "Icon", ico)))
    except Exception as e:  # noqa: BLE001
        print("icon:", e)


def _user32():
    """user32 with the prototypes the window procedure needs, on a private handle."""
    if "u32" not in _keep:
        from ctypes import wintypes as w
        u = ctypes.WinDLL("user32")
        u.CallWindowProcW.restype = ctypes.c_ssize_t
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
        u.SetWindowLongPtrW.restype = ctypes.c_void_p
        u.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_void_p]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.GetWindowRect.argtypes = [w.HWND, ctypes.c_void_p]
        u.IsZoomed.argtypes = [w.HWND]
        u.GetDpiForWindow.argtypes = [w.HWND]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.argtypes = [w.HWND, ctypes.c_uint]
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _keep["u32"] = u
    return _keep["u32"]


def _rect(hwnd):
    r = RECT()
    _user32().GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _proc(hwnd, msg, wp, lp):
    """A frameless form has no sizing border and the WebView2 child covers the client area, so it would swallow the
    hit-test. A 5 px navy padding leaves a strip the form owns; WM_NCHITTEST there returns the edge/corner codes and Windows
    does the sizing. When a move/resize ends outside the fill slot, the rect goes to window.json (launch.toggle() uses it)."""
    u32 = _user32()
    if msg == 0x0083 and wp:                                  # WM_NCCALCSIZE: no frame drawn, client = whole window
        if u32.IsZoomed(hwnd):                                # maximized windows overhang the monitor by the frame
            dpi = u32.GetDpiForWindow(hwnd)
            b = u32.GetSystemMetricsForDpi(32, dpi) + u32.GetSystemMetricsForDpi(92, dpi)   # SM_CXFRAME + SM_CXPADDEDBORDER
            r = ctypes.cast(lp, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents.rgrc[0]
            r.left += b
            r.top += b
            r.right -= b
            r.bottom -= b
        return 0
    if msg == 0x0084 and not u32.IsZoomed(hwnd):              # WM_NCHITTEST
        code = hit_code(ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value, _rect(hwnd))
        if code:
            return code
    elif msg == 0x0232:                                       # WM_EXITSIZEMOVE: remember a windowed rect
        r = _rect(hwnd)
        mi = (ctypes.c_long * 10)()
        mi[0] = 40                                            # MONITORINFO: cbSize + rcMonitor + rcWork + flags
        u32.GetMonitorInfoW(u32.MonitorFromWindow(hwnd, 2), mi)
        work = (mi[5], mi[6], mi[7], mi[8])
        if (r.right - r.left) < (work[2] - work[0]) - 40 or (r.bottom - r.top) < (work[3] - work[1]) - 40:
            try:
                p = CONF.state / "window.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}), encoding="utf-8")
            except OSError:
                pass
    return u32.CallWindowProcW(_keep["old"], hwnd, msg, wp, lp)


def enable_resize():
    try:
        from ctypes import wintypes as w

        import System
        from System.Windows.Forms import Padding
        f = _keep["win"].native
        f.Invoke(System.Action(lambda: setattr(f, "Padding", Padding(5))))
        hwnd = f.Handle.ToInt64()
        proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM)
        _keep["proc"] = proto(_proc)
        _keep["old"] = _user32().SetWindowLongPtrW(hwnd, -4, ctypes.cast(_keep["proc"], ctypes.c_void_p))   # GWLP_WNDPROC
        enable_snap(hwnd)
    except Exception as e:  # noqa: BLE001
        print("resize:", e)


def enable_snap(hwnd):
    """Windows Snap (Win+Left/Right, Snap Assist next to the terminal) only offers resizable windows: add a thick frame +
    min/max boxes. WM_NCCALCSIZE above collapses that frame to nothing, so the page still fills the window."""
    u32 = _user32()
    style = u32.GetWindowLongPtrW(hwnd, -16)                                   # GWL_STYLE
    u32.SetWindowLongPtrW(hwnd, -16, ctypes.c_void_p(style | 0x00040000 | 0x00020000 | 0x00010000))  # THICKFRAME|MIN|MAX
    u32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010)   # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER|NOACTIVATE


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pythonw briandump/frameless.py", description="the BrianDump panel in a frameless WebView2 window")
    ap.add_argument("url")
    for k in ("x", "y", "w", "h"):
        ap.add_argument(k, type=int)
    args = ap.parse_args(argv)
    if os.name != "nt":
        sys.exit("frameless.py needs Windows (WebView2)")
    import webview  # optional dependency: only this window needs it
    import webview.platforms.edgechromium as ec
    # own taskbar identity, so it isn't grouped under pythonw with the Python icon (launch.py sets the BrianDump icon)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    CONF.webview_store.mkdir(parents=True, exist_ok=True)   # localStorage survives restarts
    install_hooks(ec, origin(args.url))
    win = webview.create_window(TITLE, args.url, x=args.x, y=args.y, width=args.w, height=args.h, frameless=True,
                                easy_drag=False, resizable=True, background_color="#0a1020", text_select=True)
    _keep["win"] = win
    win.events.shown += set_icon
    win.events.shown += enable_resize
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(CONF.webview_store))
    return 0


if __name__ == "__main__":
    sys.exit(main())
