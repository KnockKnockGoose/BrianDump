"""Keeps the BrianDump panel server alive on this machine (optional: a logon shortcut, or run it by hand).

    pythonw briandump/watchdog.pyw

Every 10 s it checks the panel's port (config.json "port"); two failed checks in a row -> start `-m briandump.server` again,
hidden. A port held by another program counts as "up": the server would only refuse to start there (it says so in its
log), so restarting it would be a loop. A named mutex (Local\\BrianDumpWatchdog) makes a second copy exit at once.
Log: <state_dir>/logs/watchdog.log. Importing this file does nothing; main() runs the loop.
"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from briandump.config import CONF, REPO  # noqa: E402

CHECK_S = 10
ERROR_ALREADY_EXISTS = 183


def log(msg):
    try:
        CONF.logs.mkdir(parents=True, exist_ok=True)
        with open(CONF.logs / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def up(port=None):
    """Does anything accept connections on the panel's port?"""
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port or CONF.port)) == 0


def start_server():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else sys.executable), "-m", "briandump.server"], cwd=str(REPO),
                     creationflags=0x08000000 | 0x00000008)   # CREATE_NO_WINDOW | DETACHED_PROCESS


def single_instance():
    """True for the first watchdog of this user session; the mutex handle stays open (and owned) until the process exits."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = k32.CreateMutexW(None, False, "Local\\BrianDumpWatchdog")
    return bool(handle) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def main(checks=None):
    """The loop; `checks` limits it (tests)."""
    if os.name != "nt":
        sys.exit("watchdog.pyw is for Windows")
    if not single_instance():
        return 0
    log(f"watchdog started (port {CONF.port})")
    misses, n = 0, 0
    while checks is None or n < checks:
        n += 1
        misses = 0 if up() else misses + 1
        if misses >= 2:
            log("server down -> restarting")
            try:
                start_server()
            except Exception as e:  # noqa: BLE001
                log(f"restart failed: {e}")
            misses = 0
            time.sleep(CHECK_S)                                   # give it time to bind before checking again
        time.sleep(CHECK_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
