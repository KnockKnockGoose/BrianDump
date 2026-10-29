"""Open the BrianDump panel as a narrow column on the right third of a monitor, next to the terminal.

    py -m briandump.launch                  # start the server if needed, open the panel (or re-place it)
    py -m briandump.launch --place          # snap an existing panel back to the right third of its monitor
    py -m briandump.launch --app terminal   # ...and fit Windows Terminal into the left two-thirds beside it
    py -m briandump.launch --toggle         # the panel's middle button: right third <-> a smaller window
    py -m briandump.launch --reload         # close and reopen the window
    py -m briandump.launch --monitor side   # put it on another monitor (primary | side = biggest non-primary)

config.json decides the rest: "window" (edge = an Edge --app window with its own profile under the state dir | webview =
frameless.py, a WebView2 window without a title bar | none = just make sure the server runs and print the URL), "scale",
"fraction" (the panel's share of the work area), "monitor" and "port".

The panel is a NORMAL window (not always-on-top), so Windows Snap and the terminal next to it behave as usual. The window
carries its own AppUserModel ID (BrianDump.Panel) + relaunch command, so the taskbar groups it as its own app with the BrianDump
icon and pinning it relaunches this script; a Start-menu shortcut (BrianDump.lnk) with the same ID makes the webview window
(a pythonw.exe process) show the same icon.

Sandbox (BRIANDUMP_SANDBOX=1): the server starts as usual (it lives under the sandbox folder), but the window, the shortcut and
--app placement are recorded in side_effects.jsonl instead of done, and the URL is printed. Importing this module does
nothing: the Win32 setup (prototypes on a private user32 handle, per-monitor DPI awareness) happens in main().
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

if __package__ in (None, ""):                       # run as a file (the relaunch command, the shortcut): find the package
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from briandump.config import CONF, REPO  # noqa: E402

HERE = Path(__file__).resolve().parent
TITLE = "BrianDump · SANDBOX" if CONF.sandbox else "BrianDump"   # = the page title = the window title (server.page_html)
AUMID = "BrianDump.Panel"              # also set by frameless.py for the webview window
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
SHORTCUT = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/BrianDump.lnk"
CREATE_NO_WINDOW = 0x08000000
TITLE_DIP = 29          # Edge app-window title bar (43 px at 144 dpi); drawn inside the client area, so GetClientRect misses it
WINDOWED = (0.6, 0.75)  # --toggle: share of the monitor's work area for the smaller window
APP_EXE = {"terminal": "windowsterminal.exe", "vscode": "code.exe"}   # app -> process (a terminal's title is whatever runs in it)
user32 = None           # private user32 handle with prototypes, set up by _win32()
_ARGV = []              # this run's arguments (the relaunch command keeps the ones that describe the window)


def url(port=None):
    return f"http://127.0.0.1:{port or CONF.port}/"


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_uint32)]


class _PKEY(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", ctypes.c_uint32)]


class _PROPVARIANT(ctypes.Structure):  # only the VT_LPWSTR case; 24 bytes on x64
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("pwsz", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]


def _win32():
    """user32 with the prototypes this launcher needs, on its own WinDLL handle so they never leak into other modules of the
    same process, plus per-monitor DPI awareness v2 (physical pixels). Windows only."""
    global user32
    if user32 is None:
        from ctypes import wintypes as w
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.LoadImageW.restype = w.HANDLE
        u.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        u.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        # 64-bit HWNDs: without argtypes, HWND_NOTOPMOST (-2) is passed as a 32-bit int and the call fails
        u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.MonitorFromWindow.restype = w.HMONITOR
        u.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
        u.GetForegroundWindow.restype = w.HWND
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        user32 = u
    return user32


# ---------- geometry (pure: tests call these) ----------
def is_primary(m):
    return m[0] <= 0 < m[0] + m[2] and m[1] <= 0 < m[1] + m[3]     # the primary monitor holds (0, 0)


def panel_rect(mon, fraction=None):
    """The right `fraction` of a monitor's work area (x, y, w, h)."""
    x, y, w, h = mon
    pw = round(w * (fraction or CONF.fraction))
    return x + w - pw, y, pw, h


def app_rect(mon, fraction=None):
    """What is left of the work area beside the panel: the left part."""
    x, y, w, h = mon
    return x, y, w - round(w * (fraction or CONF.fraction)), h


# ---------- monitors and windows ----------
def work_area(hmon):
    """(x, y, w, h) of a monitor's work area: stays clear of that monitor's taskbar."""
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    rc = mi.rcWork
    return rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top


def monitors():
    from ctypes import wintypes as w
    out = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)

    def cb(h, dc, r, lp):
        out.append(work_area(h))
        return 1
    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    return out


def pick_monitor(which, hwnd=None):
    """Work area to use. which: auto | primary | side. auto = the panel's own monitor if it is open, else the monitor of the
    foreground window (normally the terminal that ran this), else the primary."""
    mons = monitors()
    primary = next((m for m in mons if is_primary(m)), mons[0])
    if which == "primary":
        return primary
    if which == "side":
        others = [m for m in mons if not is_primary(m)] or mons
        return max(others, key=lambda m: m[2] * m[3])
    ref = hwnd or user32.GetForegroundWindow()
    if ref:
        return work_area(user32.MonitorFromWindow(ref, 2))          # MONITOR_DEFAULTTONEAREST
    return primary


def find_window(match=None):
    """The visible top-level window titled exactly TITLE (a browser tab named "BrianDump" gives its window "BrianDump - Browser")."""
    from ctypes import wintypes as w
    match = match or (lambda t: t == TITLE)
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)

    def cb(hwnd, lp):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if match(buf.value):
                found.append(hwnd)
        return True
    user32.EnumWindows(proc(cb), 0)
    return found[0] if found else None


def set_icon(hwnd):
    """Taskbar / Alt-Tab icon = panel.ico (Edge app windows otherwise show the Edge logo)."""
    ico = str(CONF.panel_ico)
    for size, which in ((16, 0), (48, 1)):                       # ICON_SMALL, ICON_BIG
        h = user32.LoadImageW(None, ico, 1, size, size, 0x10)    # IMAGE_ICON, LR_LOADFROMFILE
        if h:
            user32.SendMessageW(hwnd, 0x0080, which, h)          # WM_SETICON
    try:
        set_app_identity(hwnd, ico)
    except Exception as e:  # noqa: BLE001 - cosmetic only, never block the launch
        print("app identity not set:", e)


def _launcher():
    """pythonw + this file + the arguments that describe the window (the relaunch command, the shortcut target)."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    return exe, [a for a in _ARGV if a not in ("--toggle", "--place", "--reload")]


def _store_fn(store, index, *argtypes):
    vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])


def set_app_identity(hwnd, ico):
    """Pinned-taskbar icon fix. WM_SETICON only covers the running window; a pinned item uses the pin target (Edge) unless the
    window carries its own AppUserModel ID + relaunch command / icon. With these the taskbar groups the panel as its own app,
    and pinning it pins the BrianDump icon and relaunches through this script."""
    from ctypes import wintypes as w
    fmtid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le)
    iid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99").bytes_le)  # IPropertyStore
    store = ctypes.c_void_p()
    hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(w.HWND(hwnd), ctypes.byref(iid), ctypes.byref(store))
    if hr != 0:
        raise OSError(f"SHGetPropertyStoreForWindow 0x{hr & 0xFFFFFFFF:08X}")
    set_value = _store_fn(store, 6, ctypes.POINTER(_PKEY), ctypes.POINTER(_PROPVARIANT))
    exe, argv = _launcher()
    cmd = subprocess.list2cmdline([exe, str(HERE / "launch.py"), *argv])
    try:
        # RelaunchCommand (2), RelaunchIconResource (3), RelaunchDisplayNameResource (4), then ID (5) last
        for pid, val in ((2, cmd), (3, f"{ico},0"), (4, "BrianDump"), (5, AUMID)):
            hr = set_value(store, ctypes.byref(_PKEY(fmtid, pid)), ctypes.byref(_PROPVARIANT(31, 0, 0, 0, val)))
            if hr != 0:
                raise OSError(f"SetValue pid {pid} 0x{hr & 0xFFFFFFFF:08X}")
        _store_fn(store, 7)(store)  # Commit
    finally:
        _store_fn(store, 2)(store)  # Release
    ensure_shortcut(ico, exe, argv, fmtid, iid)


def _ps_quote(s):
    """A PowerShell single-quoted string literal (a quote inside, e.g. in a user name, is doubled)."""
    return "'" + str(s).replace("'", "''") + "'"


def ensure_shortcut(ico, exe, argv, fmtid, iid):
    """Start-menu shortcut with the panel's AppUserModel ID. The webview panel is a pythonw.exe window, and the taskbar keeps
    showing the Python icon despite the window properties; a shortcut with the matching ID is what Windows uses for the
    button + pin icon. Created once (delete it to rebuild)."""
    if SHORTCUT.exists():
        return
    args = subprocess.list2cmdline([str(HERE / "launch.py"), *argv])
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(SHORTCUT)});"
          f"$s.TargetPath={_ps_quote(exe)};$s.Arguments={_ps_quote(args)};$s.WorkingDirectory={_ps_quote(REPO)};"
          f"$s.IconLocation={_ps_quote(f'{ico},0')};$s.Description='BrianDump panel';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, creationflags=CREATE_NO_WINDOW)
    ctypes.windll.ole32.CoInitialize(None)
    store = ctypes.c_void_p()
    hr = ctypes.windll.shell32.SHGetPropertyStoreFromParsingName(str(SHORTCUT), None, 2, ctypes.byref(iid),   # GPS_READWRITE
                                                                 ctypes.byref(store))
    if hr != 0:
        raise OSError(f"shortcut property store 0x{hr & 0xFFFFFFFF:08X}")
    try:
        set_value = _store_fn(store, 6, ctypes.POINTER(_PKEY), ctypes.POINTER(_PROPVARIANT))
        set_value(store, ctypes.byref(_PKEY(fmtid, 5)), ctypes.byref(_PROPVARIANT(31, 0, 0, 0, AUMID)))
        hr = _store_fn(store, 7)(store)  # Commit
        if hr != 0:
            raise OSError(f"shortcut commit 0x{hr & 0xFFFFFFFF:08X}")
    finally:
        _store_fn(store, 2)(store)


def frame(hwnd):
    """(window rect, DWM visible rect, hidden Edge title-bar height) in physical px."""
    win, vis = RECT(), RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win))
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(vis), ctypes.sizeof(vis))
    has_bar = user32.GetWindowLongW(hwnd, -16) & 0x00C00000      # WS_CAPTION: Edge yes, frameless.py no
    hide_bar = has_bar and CONF.get("title_bar") != "native"     # Edge's grey bar goes above the slot's top edge
    return win, vis, round(TITLE_DIP * user32.GetDpiForWindow(hwnd) / 96) if hide_bar else 0


def fit(hwnd, x, y, w, h):
    """Fit the *visible* window to (x, y, w, h): invisible resize borders come from DWM. Normal z-order: HWND_NOTOPMOST also
    clears the always-on-top flag older versions set."""
    from ctypes import wintypes as wt
    HWND_NOTOPMOST, SWP_NOACTIVATE, SWP_SHOWWINDOW = -2, 0x10, 0x40
    flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
    user32.ShowWindow(hwnd, 9 if user32.IsZoomed(hwnd) else 4)  # SW_RESTORE a maximized one; else SW_SHOWNOACTIVATE
    user32.SetWindowPos(hwnd, wt.HWND(HWND_NOTOPMOST), x, y, w, h, flags)
    win, vis, cap = frame(hwnd)
    il, it = vis.left - win.left, vis.top - win.top          # invisible borders
    ir, ib = win.right - vis.right, win.bottom - vis.bottom
    user32.SetWindowPos(hwnd, wt.HWND(HWND_NOTOPMOST), x - il, y - cap - it, w + il + ir, h + cap + it + ib, flags)


def windowed_rect():
    """The windowed rect frameless.py saved after the last move/resize, if it still lands on a monitor."""
    try:
        r = json.loads((CONF.state / "window.json").read_text(encoding="utf-8"))
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    on = any(m[0] <= x + w // 2 < m[0] + m[2] and m[1] <= y + 20 < m[1] + m[3] for m in monitors())
    return (x, y, w, h) if on and w >= 300 and h >= 200 else None


def toggle(hwnd):
    """Middle button: in the slot -> a smaller window (where it was last left, else centred); otherwise back."""
    mon = pick_monitor("auto", hwnd)
    x, y, w, h = panel_rect(mon)
    win, vis, cap = frame(hwnd)
    edges = zip((vis.left, vis.top + cap, vis.right, vis.bottom), (x, y, x + w, y + h), strict=True)
    in_slot = not user32.IsZoomed(hwnd) and all(abs(a - b) <= 24 for a, b in edges)
    if user32.IsZoomed(hwnd) or in_slot:
        saved = windowed_rect()
        if saved:
            user32.ShowWindow(hwnd, 9 if user32.IsZoomed(hwnd) else 4)
            user32.SetWindowPos(hwnd, None, *saved, 0x0004 | 0x0010 | 0x0040)   # NOZORDER | NOACTIVATE | SHOW
            return "windowed (saved)"
        mx, my, mw, mh = mon
        ww, wh = round(mw * WINDOWED[0]), round(mh * WINDOWED[1])
        fit(hwnd, mx + (mw - ww) // 2, my + (mh - wh) // 2, ww, wh)
        return "windowed"
    fit(hwnd, x, y, w, h)
    return "slot"


def find_app_window(exe):
    """Largest visible top-level window owned by a process named `exe`."""
    from ctypes import wintypes as w
    k32 = ctypes.windll.kernel32
    best = [None, 0]
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)

    def cb(hwnd, lp):
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            return True
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf, n = ctypes.create_unicode_buffer(520), w.DWORD(520)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) and buf.value.lower().endswith("\\" + exe):
                r = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                area = (r.right - r.left) * (r.bottom - r.top)
                if user32.IsIconic(hwnd):
                    area = 1                                  # minimised: still a candidate
                if area > best[1]:
                    best[:] = [hwnd, area]
            k32.CloseHandle(h)
        return True
    user32.EnumWindows(proc(cb), 0)
    return best[0]


def place_app(app, mon):
    """Fit another app's visible frame into the left two-thirds of the panel's monitor (normal z-order)."""
    hwnd = find_app_window(APP_EXE[app])
    if not hwnd:
        return print(f"{app} window not found")
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE: out of minimised/maximised so it can be sized
    x, y, w, h = app_rect(mon)
    SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
    user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    win, vis = RECT(), RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win))
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(vis), ctypes.sizeof(vis))
    il, it = vis.left - win.left, vis.top - win.top
    ir, ib = win.right - vis.right, win.bottom - vis.bottom
    user32.SetWindowPos(hwnd, None, x - il, y - it, w + il + ir, h + it + ib, SWP_NOZORDER | SWP_NOACTIVATE)
    print(f"{app} at", (x, y, w, h))


# ---------- the server ----------
def server_up(port=None):
    """Does anything accept connections on the panel's port?"""
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port or CONF.port)) == 0


def is_briandump(port=None, timeout=1.5):
    """Is the thing on the port a BrianDump panel? (its Server header; no proxy, even if one is configured)"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url(port) + "api/machine", timeout=timeout) as r:
            return str(r.headers.get("Server") or "").startswith("briandump")
    except Exception:  # noqa: BLE001
        return False


def server_state(port=None):
    """'up' (a BrianDump panel answers), 'other' (another program holds the port) or 'down'."""
    if not server_up(port):
        return "down"
    for _ in range(4):                                   # a panel that is just starting may not answer HTTP yet
        if is_briandump(port):
            return "up"
        time.sleep(0.5)
    return "other"


def start_server(port=None):
    """The server as a hidden process (pythonw, no console), started from the repo so `-m briandump.server` resolves. True once
    it answers; False if it exits (port taken) or stays silent for 10 s."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    proc = subprocess.Popen([str(pyw if pyw.exists() else sys.executable), "-m", "briandump.server"], cwd=str(REPO),
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    for _ in range(50):
        if server_up(port):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.2)
    return server_up(port)


def wait_window(seconds):
    for _ in range(int(seconds / 0.2)):
        hwnd = find_window()
        if hwnd:
            return hwnd
        time.sleep(0.2)
    return None


def main(argv=None):
    global _ARGV
    _ARGV = list(sys.argv[1:] if argv is None else argv)
    monitor_default = CONF.monitor if CONF.monitor in ("auto", "primary", "side") else "auto"
    ap = argparse.ArgumentParser(prog="py -m briandump.launch", description="open the BrianDump panel window")
    ap.add_argument("--place", action="store_true", help="only re-place an open panel (never starts anything)")
    ap.add_argument("--toggle", action="store_true", help="right third <-> a smaller window")
    ap.add_argument("--monitor", choices=("auto", "primary", "side"), default=monitor_default,
                    help="auto = the panel's current monitor, else the foreground window's; side = biggest non-primary")
    ap.add_argument("--app", choices=sorted(APP_EXE), help="fit this app into the left two-thirds of the same monitor")
    ap.add_argument("--reload", action="store_true", help="close and reopen the panel window")
    args = ap.parse_args(_ARGV)
    port = CONF.port

    if not args.place:
        state = server_state(port)
        if state == "other":
            sys.exit(f"port {port} is in use by another program; pick another with `py briandump.py config --set port {port + 1}`")
        if state == "down" and not start_server(port):
            sys.exit(f"the panel server did not come up on port {port}: see {CONF.logs / 'server.log'} (or run py -m briandump.server)")
    if CONF.window == "none":
        print(f"panel server on {url(port)} (config window = none: open it in any browser)")
        return 0
    if CONF.sandbox:
        CONF.record("open_window", url=url(port), window=CONF.window, place=args.place, toggle=args.toggle, app=args.app)
        print(f"sandbox: the panel window is recorded, not opened; open {url(port)} in a browser to look at it")
        return 0
    if os.name != "nt":
        sys.exit(f"the panel window needs Windows; open {url(port)} in a browser")
    _win32()

    hwnd = find_window()
    mon = pick_monitor(args.monitor, hwnd)               # decided once: Edge may wander while it restores bounds
    if hwnd and args.reload:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)        # WM_CLOSE
        time.sleep(2)
        hwnd = None

    x, y, w, h = panel_rect(mon)
    hwnd = hwnd or find_window()
    if not hwnd and not args.place and CONF.window == "webview":
        s = user32.GetDpiForSystem() / 96              # pywebview takes 96-DPI units; fit() re-places in physical px
        pyw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(HERE / "frameless.py"), url(port),
                          *(str(round(v / s)) for v in (x, y, w, h))], cwd=str(REPO))
        hwnd = wait_window(30)
    if not hwnd and not args.place:
        edge = next((p for p in EDGE if Path(p).exists()), None)
        if not edge:
            sys.exit(f"Microsoft Edge was not found; set \"window\": \"none\" in config.json and open {url(port)} yourself")
        CONF.edge_profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([edge, f"--app={url(port)}", f"--user-data-dir={CONF.edge_profile}",
                          f"--window-position={x},{y}", f"--window-size={w},{h}",
                          "--no-first-run", "--disable-features=Translate",
                          "--autoplay-policy=no-user-gesture-required"])  # lets the chime play
        hwnd = wait_window(30)                         # the first launch builds a fresh Edge profile: can take >10 s
    if not hwnd:
        sys.exit("panel window not found" if args.place else "panel window did not appear")
    if args.toggle:
        print("panel", toggle(hwnd))
        return 0
    for _ in range(3):  # Edge restores its saved bounds once the page loads; win the race
        fit(hwnd, x, y, w, h)
        time.sleep(1.0)
    fit(hwnd, x, y, w, h)
    set_icon(hwnd)
    print("panel at", (x, y, w, h))
    if args.app:
        place_app(args.app, mon)
    return 0


if __name__ == "__main__":
    sys.exit(main())
