"""One line per microphone, for the installer and `py briandump.py mics`.

Uses the WASAPI names (MME truncates them to 31 characters) and drops duplicates, so the list matches what Windows Sound
settings shows. ptt.py matches `ptt.device` as a substring, case-insensitive, so any distinctive part of a name will do.

    py -m briandump.list_mics            # "  3  Microphone (USB Audio)   (default)"
    py -m briandump.list_mics --json     # [{"index", "name", "default"}] for scripts (install.ps1)
"""
from __future__ import annotations

import argparse
import json
import sys


def devices():
    """[{"index", "name", "default"}] for every input device (WASAPI entries when that host API exists, else all, deduplicated).
    [] when sounddevice or PortAudio is missing, or the device list cannot be read."""
    try:
        import sounddevice as sd
    except (ImportError, OSError):                              # not installed, or no PortAudio library
        return []
    try:
        all_devs, apis = sd.query_devices(), sd.query_hostapis()
    except Exception:
        return []
    wasapi = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
    try:
        default_in = apis[wasapi]["default_input_device"] if wasapi is not None else sd.default.device[0]
    except Exception:
        default_in = -1
    try:
        default_name = str(sd.query_devices(kind="input")["name"])
    except Exception:
        default_name = ""
    seen, out = set(), []
    for i, d in enumerate(all_devs):
        if d["max_input_channels"] <= 0 or (wasapi is not None and d["hostapi"] != wasapi):
            continue
        name = str(d["name"])
        if name in seen:
            continue
        seen.add(name)
        is_default = i == default_in or (bool(default_name) and (name == default_name or name.startswith(default_name[:31])))
        out.append({"index": i, "name": name, "default": bool(is_default)})
    if any(x["default"] for x in out):                          # exactly one default
        first = next(x for x in out if x["default"])
        for x in out:
            x["default"] = x is first
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="list the microphones BrianDump can use")
    ap.add_argument("--json", action="store_true", help="print the list as JSON")
    args = ap.parse_args(argv)
    rows = devices()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0 if rows else 1
    if not rows:
        print("no input devices found (is a microphone plugged in and allowed in Windows privacy settings?)")
        return 1
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for d in rows:
        print(f"{d['index']:>3}  {d['name']}{'   (default)' if d['default'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
