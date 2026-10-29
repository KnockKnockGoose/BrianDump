"""BrianDump's command line: `py briandump.py <command>`, one small function per command.

    launch [--place|--toggle|--reload|--monitor auto|primary|side|--app terminal]   open the panel window (briandump.launch)
    serve [--port N]                     run the panel server in this terminal (briandump.server)
    ptt [--key f8 --device X ...]        the talk key in this terminal, to try it (briandump.ptt); the logon task runs it hidden
    mics [--json]                        the microphones, named the way `ptt.device` matches them
    config [--init] [--set KEY VALUE]... [--show] [--path] [--open]
    ask "question" [--kind ask|think] [--options "a|b"] | ask --done ID [--answer TEXT] | ask --list [--all]
    agenda add "item" | agenda done "item" | agenda undone "item" | agenda list [--all]
    dump <file> [--label L] [--now] [--processor claude|mock]
    doctor [--json]                      a checklist of every part, with a one-line fix under each failure
    version

Why one entry point: a beginner has one file to run, the installer and the Start menu shortcut have one target, and the
Claude Code plugin has one stable path for /briandump:ask and /briandump:agenda (`install_dir` in config.json, written by
`config --init`). Sibling modules are imported inside the command that needs them, so `doctor`, `config` and `version`
still work when a package such as sounddevice is missing: a broken install is exactly when they are needed.

Why the doctor ends on proof: each check answers with (ok, detail, fix). ok None is information, not a failure; any False
makes the exit code 1. The checks run in parallel threads with a deadline, read things without changing them (the panel
check is one GET with a 1 s timeout) and never raise, so the installer can always finish on the checklist.

Files the panel server reads at any moment (questions.json, config.json) are written whole, temp file + os.replace, and the
replace is retried: on Windows it fails with PermissionError while another process has the destination open.
"""
from __future__ import annotations

import argparse
import html
import importlib
import importlib.util
import json
import os
import platform
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from pathlib import Path

from briandump import __version__
from briandump.config import CONF, REPO, ConfigError, defaults, read_config

CLI_PATH = REPO / "briandump.py"
TASK_NAME = "BrianDump PTT"
PLUGIN_ID = "briandump@briandump"
GITHUB = "KnockKnockGoose/BrianDump"
INSTALL_DOCS = "https://code.claude.com/docs"
PIP_FIX = 'py -m pip install -r "' + str(REPO / "requirements.txt") + '"'
INSTALLER_FIX = 'double-click "Install BrianDump.cmd" in ' + str(REPO)
REQUIRED = (("numpy", "numpy"), ("sounddevice", "sounddevice"), ("pynput", "pynput"), ("faster_whisper", "faster-whisper"),
            ("pycaw", "pycaw"), ("comtypes", "comtypes"))
MODEL_SIZE = {"tiny": "about 75 MB", "base": "about 145 MB", "small": "about 460 MB", "medium": "about 1.5 GB"}
EDGE = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
EXTRA_KEYS = {"install_dir"}            # settings that are real but not in config.example.json
REPLACE_TRIES = 20                      # os.replace attempts, 25-50 ms apart
CHECK_TIMEOUT = 5.0                     # per doctor check; they run in parallel, so this bounds the whole run too
RUN_TIMEOUT = 4.0                       # one external command inside a check
STATUS_FRESH_S = 10                     # ptt_status.json is rewritten every 2 s by a live service
QID_RE = re.compile(r"^q?(\d+)$")
SID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")         # a Claude Code session id (sessions.SID_RE)
KEY_RE = re.compile(r"[A-Za-z][\w-]*(\.[A-Za-z][\w-]*)*")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MISSING = object()

Check = namedtuple("Check", "name ok detail fix")


class UsageError(Exception):
    """A wrong command line: the message is printed and the exit code is 2."""


class CommandError(Exception):
    """The command could not do its job: the message is printed and the exit code is 1."""


# ---------- output that survives pythonw (no console: sys.stdout is None) and narrow console code pages ----------
def _write(stream, text):
    if stream is None:
        return
    try:
        try:
            stream.write(text + "\n")
        except UnicodeEncodeError:
            enc = getattr(stream, "encoding", None) or "ascii"
            stream.write((text + "\n").encode(enc, "replace").decode(enc, "replace"))
        stream.flush()
    except (OSError, ValueError):
        pass


def out(text=""):
    _write(sys.stdout, str(text))


def err(text=""):
    _write(sys.stderr, str(text))


# ---------- files the panel reads while we write ----------
def _pause():
    time.sleep(random.uniform(0.025, 0.05))


def retry_locked(fn, *args, **kw):
    """Call a save that ends in os.replace again on PermissionError, about 20 times 25-50 ms apart: on Windows the replace
    fails while any other process has the destination open, and the panel server reads its files every second."""
    for i in range(REPLACE_TRIES):
        try:
            return fn(*args, **kw)
        except PermissionError as e:
            if i == REPLACE_TRIES - 1:
                name = getattr(e, "filename2", None) or getattr(e, "filename", None) or "the file"
                raise CommandError(f"could not save {name}: another program kept it open ({REPLACE_TRIES} tries); "
                                   "try again in a moment") from e
            _pause()
    return None


def read_json(path, default):
    """JSON from a file that may be replaced this very moment (a sharing violation is retried); `default` when it is missing
    or not JSON."""
    for i in range(REPLACE_TRIES):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if i == REPLACE_TRIES - 1:
                return default
            _pause()
        except (OSError, ValueError):
            return default
    return default


def write_json_atomic(path, data):
    """The whole file next to the target, then os.replace (retried): a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        retry_locked(os.replace, tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


def _mod(name):
    """A sibling module, imported when a command needs it, with a message that says what to do when it cannot be."""
    try:
        return importlib.import_module(f"briandump.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"briandump.{name}":
            raise CommandError(f"briandump/{name}.py is missing from this copy of BrianDump ({REPO})") from e
        raise CommandError(f"briandump.{name} needs the package '{e.name}': {PIP_FIX}") from e
    except ImportError as e:
        raise CommandError(f"briandump.{name} could not be imported ({e}); {PIP_FIX}") from e


# ---------- launch / serve / ptt: handed to the module with their arguments untouched ----------
def run_launch(rest):
    try:
        launch = importlib.import_module("briandump.launch")
    except Exception as e:                      # ctypes.windll and friends: Windows only
        raise CommandError(f"the panel window needs Windows ({type(e).__name__}: {e})") from e
    return launch.main(rest) or 0               # its own arguments: it also rebuilds the taskbar relaunch command from them


def run_serve(rest):
    return _mod("server").main(rest) or 0


def run_ptt(rest):
    return _mod("ptt").main(rest) or 0


PASSTHROUGH = {"launch": run_launch, "serve": run_serve, "ptt": run_ptt}
PASSTHROUGH_HELP = {
    "launch": "open the panel window (--place --toggle --reload --monitor auto|primary|side --app terminal)",
    "serve": "run the panel server in this terminal (--port N)",
    "ptt": "run the talk key in this terminal to try it (--key f8 --device NAME --model base --list-devices)",
}


# ---------- mics ----------
def cmd_mics(a):
    try:
        rows = _mod("list_mics").devices()
    except Exception as e:
        raise CommandError(f"cannot list microphones ({type(e).__name__}: {e}); {PIP_FIX}") from e
    if a.json:
        out(json.dumps(rows))                     # ASCII-escaped: PowerShell reads it whatever the console code page
        return 0
    if not rows:
        out("no input devices found (is a microphone plugged in, and allowed in Settings > Privacy & security > Microphone?)")
        return 1
    for d in rows:
        out(f"{d['index']:>3}  {d['name']}{'   (default)' if d.get('default') else ''}")
    return 0


# ---------- config ----------
def _lookup(tree, dotted):
    cur = tree
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def parse_value(key, raw):
    """A --set value: JSON when it parses (8771, true, null, ["a", "b"]), else the text as typed; checked against the type
    of the documented default, because a config.json that fails to load would stop every BrianDump process."""
    if not KEY_RE.fullmatch(str(key)):
        raise UsageError(f"not a setting name: {key!r} (dotted names, for example ptt.key)")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    default = _lookup(defaults(), key)
    if isinstance(default, dict):
        raise UsageError(f"{key} is a group of settings: set one of them, for example {key}.{next(iter(default), 'name')}")
    if key == "reports.discover" and value is not None:   # a pattern that does not compile would search nothing
        try:
            re.compile(str(value), re.M)
        except re.error as e:
            raise UsageError(f"reports.discover: {value!r} is not a valid pattern ({e})") from e
        value = raw if not isinstance(value, str) else value
    if key == "reports.index" and value is not None and not (
            isinstance(value, str) or (isinstance(value, list) and all(isinstance(s, str) for s in value))):
        raise UsageError(f'{key} takes a path or a JSON list of paths, for example ["~/notes/reports.json"]')
    if default is _MISSING or default is None or value is None:
        return value
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise UsageError(f"{key} takes true or false, not {raw!r}")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise UsageError(f"{key} takes a whole number, not {raw!r}")
        if key == "port" and not 1 <= value <= 65535:
            raise UsageError("port takes a number from 1 to 65535")
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UsageError(f"{key} takes a number, not {raw!r}")
    elif isinstance(default, str):
        if not isinstance(value, str):
            value = raw                             # f8, 1: the text exactly as typed
    elif isinstance(default, list) and not isinstance(value, list):
        raise UsageError(f'{key} takes a JSON list, for example ["a", "b"]')
    if key == "private_filter":                     # patterns: one that does not compile would only be matched as plain text
        for w in value:
            try:
                re.compile(str(w))
            except re.error as e:
                raise UsageError(f"private_filter: {w!r} is not a valid pattern ({e}); put a backslash before a special "
                                 r"character, for example \( for a bracket") from e
    return value


def cmd_config(a):
    pairs = [(k, parse_value(k, v)) for k, v in (a.set or [])]     # every pair checked before anything is written
    did = False
    if a.init:
        existed = CONF.config_file.exists()
        retry_locked(CONF.write_default_config)
        CONF.ensure_dirs()
        retry_locked(CONF.save_user_value, "install_dir", str(REPO))
        out(f"config:  {CONF.config_file}" + ("  (kept, with your settings)" if existed else "  (new, from config.example.json)"))
        out(f"data:    {CONF.data_dir}")
        out(f"install: {REPO}")
        did = True
    known = defaults()
    for key, value in pairs:
        retry_locked(CONF.save_user_value, key, value)
        note = "" if _lookup(known, key) is not _MISSING or key in EXTRA_KEYS else "  (not a documented setting: check the spelling)"
        out(f"set {key} = {json.dumps(value, ensure_ascii=False)}{note}")
        did = True
    if a.path:
        out(str(CONF.config_file))
        did = True
    if a.open:
        did = True
        retry_locked(CONF.write_default_config)
        if CONF.record("open_file", path=str(CONF.config_file)):
            out(f"(sandbox) would open {CONF.config_file}")
        elif hasattr(os, "startfile"):
            os.startfile(str(CONF.config_file))
            out(f"opened {CONF.config_file}")
        else:
            out(str(CONF.config_file))
    if a.show or not did:
        out(json.dumps(CONF.describe(), indent=2))  # ASCII-escaped on purpose: the installer parses it
    return 0


# ---------- ask: questions.json, the manual questions the panel shows ----------
def _now_local():
    return time.strftime("%Y-%m-%dT%H:%M:%S")      # local, the same format the server's 48 h cut-off compares against


def _norm(text):
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


def load_questions():
    qs = read_json(CONF.manual_questions, [])
    return [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []


def _next_qid(qs):
    nums = []
    for q in qs:
        m = QID_RE.match(str(q.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return f"q{max(nums, default=0) + 1}"


def _prune(qs, keep=200):
    """At most `keep` entries: the oldest closed ones go first."""
    extra = len(qs) - keep
    if extra <= 0:
        return qs
    kept = []
    for q in qs:
        if extra > 0 and q.get("done"):
            extra -= 1
            continue
        kept.append(q)
    return kept[-keep:]


def asking_session(env=None):
    """The Claude Code session running this command (/briandump:ask runs `ask` through its shell tool): Claude Code puts the id in
    CLAUDE_CODE_SESSION_ID, and its own pid in CLAUDE_PID (whose registry file names the session), for every command it runs.
    None when run by hand: the answer then goes to the voice router's usual target."""
    env = os.environ if env is None else env
    sid = str(env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if SID_RE.fullmatch(sid):
        return sid
    pid = str(env.get("CLAUDE_PID") or "").strip()
    if pid.isdigit():
        rec = read_json(CONF.sessions / f"{pid}.json", None)
        sid = str(rec.get("sessionId") or "") if isinstance(rec, dict) else ""
        if SID_RE.fullmatch(sid):
            return sid
    return None


def _qline(q):
    opts = f"  [{' | '.join(q.get('options') or [])}]" if q.get("options") else ""
    state = "done" if q.get("done") else q.get("kind") or "ask"
    answer = f"  -> {q['answer']}" if q.get("done") and q.get("answer") else ""
    return f"{q.get('id')} [{state}] {q.get('text')}{opts}{answer}"


def cmd_ask(a):
    qs = load_questions()
    text = " ".join(a.text).strip()
    if a.list:
        rows = qs if a.all else [q for q in qs if not q.get("done")]
        for q in rows:
            out(_qline(q))
        if not rows:
            out("no open questions" if not a.all else "no questions yet")
        return 0
    if a.done:
        want = str(a.done).strip()
        m = QID_RE.match(want)
        ids = {f"q{m.group(1)}", m.group(1)} if m else {want}
        hit = next((q for q in qs if str(q.get("id")) in ids), None)
        if hit is None:
            raise CommandError(f"no question {want} (py briandump.py ask --list shows them)")
        hit.update(done=True, done_at=_now_local())
        if a.answer:
            hit["answer"] = a.answer
        write_json_atomic(CONF.manual_questions, qs)
        out(f"{hit.get('id')} closed" + (f": {a.answer}" if a.answer else ""))
        return 0
    if not text:
        raise UsageError('say what to ask: ask "question" [--options "a|b"]   (or: ask --list, ask --done ID)')
    options = [o.strip() for o in (a.options or "").split("|") if o.strip()]
    dup = next((q for q in qs if not q.get("done") and _norm(q.get("text", "")) == _norm(text)), None)
    if dup:
        out(f"{dup.get('id')} is already on the BrianDump panel: {dup.get('text')}")
        return 0
    qid = _next_qid(qs)
    sid = asking_session()                                  # the panel answer goes back to this session, and only to it
    qs.append({"id": qid, "text": text, "kind": a.kind, "options": options, "asked": _now_local(), "done": False,
               **({"session": sid} if sid else {})})
    write_json_atomic(CONF.manual_questions, _prune(qs))
    tail = f"  [{' | '.join(options)}]" if options else ""
    out(f"{qid} is on the BrianDump panel: {text}{tail}" + ("  (to think about; nothing waits on it)" if a.kind == "think" else ""))
    return 0


# ---------- agenda ----------
def cmd_agenda(a):
    agenda = _mod("agenda")
    path, heading = CONF.agenda_file, CONF.get("agenda.heading")
    text = " ".join(a.text).strip()
    if a.action == "list":
        items = agenda.read(path, heading) or []
        rows = items if a.all else [i for i in items if not i.get("done")]
        for i in rows:
            out(f"- [{'x' if i.get('done') else ' '}] {i.get('text')}")
        n_open = sum(1 for i in items if not i.get("done"))
        out(f"({n_open} open, {len(items) - n_open} done: {path})")
        return 0
    if not text:
        raise UsageError(f'say which item: agenda {a.action} "item"')
    try:
        if a.action == "add":
            added = agenda.add(path, [text], heading=heading)
            out(f"added to the agenda: {added[0]}" if added else f"already on the agenda (it matches an open item): {text}")
            return 0
        done = a.action == "done"
        found = agenda.set_done(path, text, done, heading=heading)
    except RuntimeError as e:                               # the file kept changing under us (an editor saving it)
        raise CommandError(str(e)) from e
    if found:
        out(f"{'ticked' if done else 'reopened'}: {text}")
        return 0
    raise CommandError(f"no agenda item matches: {text} (py briandump.py agenda list shows them)")


# ---------- dump ----------
def cmd_dump(a):
    src = Path(a.file).expanduser()
    if not src.is_file():
        raise UsageError(f"no such file: {src}")
    dumps = _mod("dumps")
    try:
        did = dumps.import_file(str(src.resolve()), label=a.label)
    except ValueError as e:                                 # not a transcript or a recording, or an empty file
        raise UsageError(str(e)) from e
    if not a.now:
        out(f"imported as dump {did}: the panel server will process it (see the panel's Dumps tab)")
        return 0
    out(f"imported as dump {did}; processing it now")
    try:
        meta = dumps.process(did, processor=a.processor, log=out) or {}   # transcribes first when it is a recording
    except (KeyError, RuntimeError) as e:
        raise CommandError(f"dump {did}: {e}") from e
    if meta.get("status") != "ready" or not meta.get("report"):
        raise CommandError(f"dump {did} did not finish: {meta.get('error') or meta.get('status') or 'no report written'}")
    out(f"report: {meta['report']}")
    if meta.get("warning"):
        out(f"warning: {meta['warning']}")
    return 0


# ---------- version ----------
def cmd_version(a):
    out(f"BrianDump {__version__}")
    return 0


# ---------- doctor: probes (small, read-only, patched by the tests) ----------
def _run(argv, timeout=RUN_TIMEOUT, encoding="utf-8"):
    """(exit code, stdout + stderr) of a short read-only command; (None, why) when it cannot start or does not finish."""
    try:
        r = subprocess.run([str(x) for x in argv], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None, f"no answer within {timeout:.0f} s"
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    return r.returncode, ((r.stdout or b"") + (r.stderr or b"")).decode(encoding, "replace")


def _on_windows():
    return sys.platform == "win32"


def _windows_build():
    """(major, build) on Windows, else None."""
    if sys.platform != "win32":
        return None
    v = sys.getwindowsversion()
    return v.major, v.build


def _voice_mode():
    return "monitor" if str(CONF.get("voice.mode", "panel") or "panel").lower() == "monitor" else "panel"


def _startup_shortcut():
    """The 'BrianDump panel' shortcut the installer puts in the Startup folder: it runs the watchdog at every sign-in."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "BrianDump panel.lnk"


def _import_ok(module):
    """(True, '') when the package imports, else (False, why). A real import, so a broken DLL shows up too."""
    try:
        importlib.import_module(module)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:100]
    return True, ""


def _has_module(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _dist_version(dist):
    try:
        from importlib import metadata
        return metadata.version(dist)
    except Exception:
        return ""


def _claude_exe():
    return shutil.which(str(CONF.get("dump.claude_cmd", "claude") or "claude")) or shutil.which("claude")


def _json_in(text):
    """The JSON object in a command's output (ignoring any chatter around it), or None."""
    s, e = str(text).find("{"), str(text).rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except ValueError:
        return None


def _account_in_claude_json():
    """True / False from the oauthAccount entry of Claude Code's .claude.json (read only, nothing of it is shown), None when
    there is no such file."""
    cands = [CONF.claude_dir / ".claude.json"] if os.environ.get("BRIANDUMP_CLAUDE_DIR") else [Path.home() / ".claude.json"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        cands.insert(0, Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json")
    for p in cands:
        data = read_json(p, None)
        if isinstance(data, dict):
            return bool(data.get("oauthAccount"))
    return None


def _registry():
    """Claude Code's live-session registry, each record with `alive` (briandump.sessions does the process check)."""
    return list(_mod("sessions").read_registry() or [])


def _hf_cache():
    """Where huggingface_hub keeps downloaded models (faster-whisper uses its default)."""
    env = os.environ
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if env.get(var):
            return Path(env[var])
    if env.get("HF_HOME"):
        return Path(env["HF_HOME"]) / "hub"
    base = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else Path.home() / ".cache"
    return base / "huggingface" / "hub"


def _model_repo(name):
    try:
        from faster_whisper.utils import _MODELS  # the package's own size -> repository table
        if name in _MODELS:
            return _MODELS[name]
    except Exception:
        pass
    return name if "/" in name else f"Systran/faster-whisper-{name}"


def _mic_list():
    if not _has_module("sounddevice"):              # devices() would answer [] and look like "no microphone"
        raise ImportError("sounddevice is not installed")
    return _mod("list_mics").devices()


def _task_info(name):
    """{"enabled", "args"} of a scheduled task from `schtasks /query /xml` (the XML element names are never translated,
    unlike the table output), or None when there is no such task."""
    code, text = _run(["schtasks", "/query", "/tn", name, "/xml"], encoding="oem" if _on_windows() else "utf-8")
    if code != 0 or "<Task" not in text:
        return None
    settings = re.search(r"<Settings>(.*?)</Settings>", text, re.S)
    enabled = not (settings and re.search(r"<Enabled>\s*false\s*</Enabled>", settings.group(1), re.I))
    args = re.search(r"<Arguments>(.*?)</Arguments>", text, re.S)
    return {"enabled": bool(enabled), "args": html.unescape(args.group(1)).strip() if args else ""}


def _process_name(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32")                 # a private handle: argtypes set here touch no other module
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, int(pid))    # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return Path(buf.value).name if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        k32.CloseHandle(h)


def _port_owner(port):
    """(pid, program) of whatever listens on TCP `port` on this PC, None when nothing does, "unknown" off Windows.

    Reads the table `netstat -ano` shows (iphlpapi GetExtendedTcpTable), so no connection is made. A plain connect() is no
    test on Windows: a refused loopback connection takes about 2 s there (the SYN is retried), longer than the 1 s budget."""
    if not _on_windows():
        return "unknown"
    import ctypes
    from ctypes import wintypes
    iphlpapi = ctypes.WinDLL("iphlpapi")
    # (address family, row size, offset of the port, offset of the pid): MIB_TCPROW_OWNER_PID, MIB_TCP6ROW_OWNER_PID
    for family, row, port_at, pid_at in ((2, 24, 8, 20), (23, 56, 20, 52)):
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family, 3, 0)   # 3 = TCP_TABLE_OWNER_PID_LISTENER
        raw = b""
        for _ in range(3):                                                             # the table can grow in between
            buf = ctypes.create_string_buffer(size.value + 4096)
            size = wintypes.DWORD(len(buf))
            if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, 3, 0) == 0:
                raw = buf.raw
                break
        if not raw:
            return "unknown"
        (count,) = struct.unpack_from("<I", raw, 0)
        for i in range(count):
            base = 4 + i * row
            (lport,) = struct.unpack_from("<I", raw, base + port_at)
            if socket.ntohs(lport & 0xFFFF) == int(port):
                (pid,) = struct.unpack_from("<I", raw, base + pid_at)
                return pid, _process_name(pid)
    return None


def _get_json(host, port, path, timeout):
    import http.client
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "application/json", "User-Agent": "briandump-doctor"})
        resp = conn.getresponse()
        body, status, server = resp.read(256 * 1024), resp.status, resp.getheader("Server") or ""
    finally:
        conn.close()
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = None
    return status, data, server


def _panel_probe(port):
    """("briandump", /api/machine JSON) | ("other", who holds the port) | ("free", "") for the panel port. Read only: one
    GET with a 1 s timeout, and only when something listens there."""
    import http.client
    owner = _port_owner(port)
    if owner is None:
        return "free", ""
    who = f"pid {owner[0]} ({owner[1] or 'unknown program'})" if isinstance(owner, tuple) else "a program"
    host = CONF.bind if CONF.bind not in ("", "0.0.0.0", "::", "localhost") else "127.0.0.1"
    timeout = 1.0 if owner != "unknown" else 2.5    # unknown (off Windows, or no table): a refusal may take its time
    try:
        status, data, server = _get_json(host, port, "/api/machine", timeout=timeout)
    except ConnectionRefusedError:
        return "free", ""
    except TimeoutError:
        return "other", f"{who} accepted the connection but sent no answer within {timeout:g} s"
    except (OSError, http.client.HTTPException) as e:
        return "other", f"{who} does not answer like BrianDump ({type(e).__name__})"
    if status == 200 and isinstance(data, dict) and "version" in data and "data_dir" in data:
        return "briandump", data
    return "other", f"{who} answered HTTP {status}" + (f" ({server})" if server else "")


# ---------- doctor: the checks ----------
CHECKS = []


def check(label):
    """Register a doctor check: a function returning (ok, detail, fix); ok None = information, not a failure."""
    def deco(fn):
        fn.label = label
        CHECKS.append(fn)
        return fn
    return deco


def _same_path(a, b):
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except (TypeError, ValueError):
        return False


def _ago(seconds):
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    if s < 90 * 60:
        return f"{round(s / 60)} min"
    if s < 36 * 3600:
        return f"{round(s / 3600)} h"
    return f"{round(s / 86400)} days"


def _epoch(v):
    """Epoch seconds from a status-file time: a number, or an ISO string with or without a trailing Z."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        import datetime as dt
        try:
            d = dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (d if d.tzinfo else d.astimezone()).timestamp()
    return None


@check("BrianDump")
def check_briandump():
    return None, f"{__version__} in {REPO}" + ("  (SANDBOX: BRIANDUMP_SANDBOX=1)" if CONF.sandbox else ""), ""


@check("Windows")
def check_windows():
    win = _windows_build()
    if win is None:
        return False, f"this is {platform.system() or sys.platform}: BrianDump 0.1 runs on Windows 10 and 11", ""
    major, build = win
    if major < 10:
        return False, f"Windows {major} (build {build}): BrianDump needs Windows 10 or 11", ""
    return True, f"Windows {'11' if build >= 22000 else '10'} (build {build})", ""


@check("Python")
def check_python():
    v = sys.version_info
    here = f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"
    get = "winget install -e --id Python.Python.3.13 --scope user, then run the installer again"
    if v < (3, 11):
        return False, f"{here}: BrianDump needs Python 3.11 or newer", get
    if not _on_windows():
        return True, here, ""
    py = shutil.which("py")
    if not py:
        return False, f"{here}, but the py launcher is missing (the installer, the task and the plugin commands use it)", get
    code, text = _run([py, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3]); print(sys.executable)"])
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if code != 0 or len(lines) < 2 or not re.fullmatch(r"\d+\.\d+\.\d+", lines[0]):
        return False, f"py does not start a Python ({(lines or ['no output'])[0][:100]})", get
    ver, exe = lines[0], lines[1]
    if tuple(int(x) for x in ver.split(".")) < (3, 11):
        return False, f"py starts Python {ver} ({exe}): BrianDump needs 3.11 or newer", get
    note = "" if _same_path(exe, sys.executable) else f"; this check runs in {here}"
    return True, f"{ver} via py ({exe}){note}", ""


@check("Packages")
def check_packages():
    have, missing = [], []
    for module, dist in REQUIRED:
        if dist in ("pycaw", "comtypes") and not _on_windows():     # Windows-only in requirements.txt as well
            continue
        ok, why = _import_ok(module)
        if ok:
            have.append(f"{dist} {_dist_version(dist)}".strip())
        else:
            missing.append(f"{dist} ({why})")
    if missing:
        if not (_on_windows() and _task_info(TASK_NAME) is not None):    # no talk key installed: nothing here needs them
            return (None, "not installed: " + "; ".join(missing) + ". Only the talk key and brain dumps need them (the panel "
                    "does not)", "to add the talk key: " + INSTALLER_FIX)
        return False, "missing: " + "; ".join(missing) + ". The talk key and brain dumps need them (the panel does not)", PIP_FIX
    return True, ", ".join(have), ""


@check("Panel window")
def check_window():
    mode, url = CONF.window, f"http://127.0.0.1:{CONF.port}/"
    frameless = _has_module("webview") and _has_module("clr")
    if mode == "none":
        return None, f"window = none: open {url} in any browser", ""
    if mode == "webview":
        if frameless:
            return True, "frameless WebView2 window (pywebview + pythonnet)", ""
        return (False, "window = webview, but pywebview and pythonnet are not installed",
                "py -m pip install pywebview pythonnet   (or: py briandump.py config --set window edge)")
    edge = next((p for p in EDGE if Path(p).exists()), None) or shutil.which("msedge")
    extra = "pywebview + pythonnet installed" if frameless else "not installed (py -m pip install pywebview pythonnet)"
    if edge:
        return True, f"Microsoft Edge app window; optional frameless window: {extra}", ""
    return (False, "Microsoft Edge was not found (the panel window is an Edge app window)",
            f"py briandump.py config --set window none, then open {url} in your browser")


@check("Claude Code")
def check_claude():
    exe = _claude_exe()
    if not exe:
        return (False, "the claude command was not found on PATH",
                f"install Claude Code ({INSTALL_DOCS}), open a new terminal, run claude and log in")
    code, text = _run([exe, "--version"])
    first = (text.strip().splitlines() or [""])[0][:80]
    if code != 0:
        return False, f"claude --version failed: {first or 'no output'} ({exe})", f"reinstall Claude Code: {INSTALL_DOCS}"
    return True, f"{first} ({exe})", ""


@check("Claude login")
def check_login():
    exe = _claude_exe()
    if not exe:
        return None, "skipped: no claude command", ""
    code, text = _run([exe, "auth", "status", "--json"])
    data = _json_in(text)
    if isinstance(data, dict) and "loggedIn" in data:
        if data.get("loggedIn"):
            how = ", ".join(str(x) for x in (data.get("authMethod"), data.get("subscriptionType")) if x)
            return True, f"logged in ({how})" if how else "logged in", ""
        provider = str(data.get("apiProvider") or "")
        if provider and provider.lower() not in ("firstparty", "anthropic"):
            return True, f"using {provider} credentials", ""
        return False, "Claude Code is not logged in", "run claude in a terminal and log in (or: claude auth login)"
    account = _account_in_claude_json()
    if account is not None:
        if account:
            return True, "an account is set up (found in .claude.json)", ""
        return False, "no account in .claude.json", "run claude in a terminal and log in"
    return None, f"could not tell (claude auth status: {(text.strip() or 'no output')[:80]})", ""


@check("Git for Windows")
def check_git():
    """Optional: without Git Bash, Claude Code on Windows uses its PowerShell tool; only the information row changes."""
    exe = shutil.which("git")
    if exe:
        code, text = _run([exe, "--version"])
        first = (text.strip().splitlines() or [""])[0][:80]
        return True, first if code == 0 and first else f"git at {exe}", ""
    bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if bash and Path(bash).exists():
        return True, f"Git Bash at {bash} (CLAUDE_CODE_GIT_BASH_PATH)", ""
    return (None, "not installed. Recommended: Claude Code's Bash and Monitor tools need it (the monitor voice mode relies on "
            "them); panel mode works without it", "winget install --id Git.Git -e, then open a new terminal")


@check("Settings")
def check_config():
    f, again = CONF.config_file, f'py "{CLI_PATH}" config --init'
    if not f.exists():
        return False, f"no settings file yet ({f})", again
    raw, why = read_config(f)                                 # the same reader the settings come from
    if why:
        return False, f"cannot be read, so every setting is at its default: {why}"[:300], \
            "fix it (py briandump.py config --open), or delete it and run: " + again
    if CONF.problems:                                         # values BrianDump could not use: their defaults are in force
        return False, ("a setting cannot be used, so its default is in force: " + "; ".join(CONF.problems))[:400], \
            "fix it with py briandump.py config --set KEY VALUE, or in the file: py briandump.py config --open"
    inst = raw.get("install_dir")
    if not inst:
        return False, f"{f} has no install_dir, so /briandump:ask and /briandump:agenda cannot find briandump.py", again
    if not (Path(str(inst)) / "briandump.py").exists():
        return False, f"install_dir is {inst}, which has no briandump.py (moved?)", again
    if not _same_path(inst, REPO):
        return None, f"{f}; install_dir is another copy ({inst}): the plugin commands use that one", "to use this copy: " + again
    return True, str(f), ""


@check("Data folder")
def check_data():
    d = CONF.data_dir
    fix = 'set data_dir to a folder you can write to: py briandump.py config --set data_dir "<folder>"'
    if not d.exists():
        parent = next((p for p in d.parents if p.exists()), None)
        if parent is not None and os.access(parent, os.W_OK):
            return True, f"{d} (created on first use)", ""
        return False, f"{d} does not exist and cannot be created", fix
    try:
        with tempfile.NamedTemporaryFile(dir=d, prefix=".briandump-doctor-", delete=True) as fh:
            fh.write(b"ok")
    except OSError as e:
        return False, f"{d} is not writable ({e.strerror or e})", fix
    return True, f"{d} (agenda, questions, dumps, reports)", ""


@check("Transcripts")
def check_transcripts():
    root, n, newest = CONF.projects, 0, 0.0
    try:
        for proj in os.scandir(root):
            if proj.is_dir():
                for f in os.scandir(proj.path):
                    if f.name.endswith(".jsonl"):
                        n += 1
                        newest = max(newest, f.stat().st_mtime)
    except OSError:
        pass
    other = os.environ.get("CLAUDE_CONFIG_DIR")
    hint = ""
    if other and not _same_path(other, CONF.claude_dir):
        hint = f"; Claude Code uses CLAUDE_CONFIG_DIR={other}: set BRIANDUMP_CLAUDE_DIR to the same folder"
    if not n:
        return (None, f"no Claude Code transcripts in {root} yet: the panel shows a session once one has run",
                "start one: open a terminal and run claude" + hint)
    return True, f"{n} session transcript(s); the newest changed {_ago(time.time() - newest)} ago", ""


@check("Claude sessions")
def check_sessions():
    try:
        rows = _registry()
    except Exception as e:
        return None, f"could not read the session registry ({type(e).__name__}: {e})"[:160], ""
    live = [r for r in rows if r.get("alive")]
    main = str(CONF.get("voice.main_session", "Main") or "Main")
    if _voice_mode() == "monitor":
        return None, f"{len(live)} running; voice mode is monitor (each session watches the inbox itself)", ""
    auto = (f"; the panel opens '{main}' once per sign-in, when it starts (voice.auto_open_main)"
            if CONF.get("voice.auto_open_main", True) is not False else "; voice.auto_open_main is off, so you start it yourself")
    if any(str(r.get("name") or "").lower() == main.lower() for r in live):
        return True, f"{len(live)} running; '{main}' is one of them, so your F9 words go there", ""
    if live:
        return (None, f"{len(live)} running, none named '{main}': F9 goes to the most recently active one{auto}",
                f"name one: claude --name {main} (or the panel's Open Main button)")
    return None, f"no Claude Code session is running{auto}", f"start one: claude --name {main}"


@check("BrianDump plugin")
def check_plugin():
    data = read_json(CONF.claude_dir / "plugins" / "installed_plugins.json", None)
    plugins = data.get("plugins", data) if isinstance(data, dict) else {}
    plugins = plugins if isinstance(plugins, dict) else {}
    ids = [k for k in plugins if str(k).split("@")[0] == "briandump"]
    if not ids:
        return (False, "not installed: no 'needs you' light, no voice hint when a session starts, no /briandump:ask",
                f'claude plugin marketplace add "{REPO}", then claude plugin install {PLUGIN_ID} (from GitHub instead: '
                f"claude plugin marketplace add {GITHUB}); then restart Claude Code")
    pid = ids[0]
    settings = read_json(CONF.claude_dir / "settings.json", {})
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    if isinstance(enabled, dict) and enabled.get(pid) is False:
        return False, f"{pid} is installed but disabled", f"claude plugin enable {pid}"
    entry = plugins[pid][0] if isinstance(plugins[pid], list) and plugins[pid] else plugins[pid]
    ver = entry.get("version") if isinstance(entry, dict) else None
    return True, f"{pid} {ver or ''}".strip() + " installed", ""


@check("Speech model")
def check_model():
    name = str(CONF.get("ptt.model", "small") or "small")
    if Path(name).is_dir():                                    # a local CTranslate2 model folder
        ok = (Path(name) / "model.bin").exists()
        return ok, f"local model folder {name}" + ("" if ok else " has no model.bin"), ""
    folder = _hf_cache() / ("models--" + _model_repo(name).replace("/", "--"))
    if any(folder.glob("snapshots/*/model.bin")):
        return True, f"'{name}' is downloaded ({folder})", ""
    return (None, f"'{name}' is not downloaded yet: the first use downloads {MODEL_SIZE.get(name, 'a few hundred MB')}",
            'download it now: py -c "from faster_whisper import WhisperModel; WhisperModel(' + repr(name) + ')"')


@check("Microphone")
def check_mic():
    try:
        rows = _mic_list()
    except Exception as e:
        return None, f"skipped: cannot list microphones ({type(e).__name__}: {e})"[:160], PIP_FIX
    want = str(CONF.get("ptt.device") or "").strip()
    if not rows:
        return False, "no microphone found", "plug one in, or allow desktop apps to use it: Settings > Privacy & security > Microphone"
    default = next((r["name"] for r in rows if r.get("default")), rows[0]["name"])
    if not want:
        return True, f"{len(rows)} found; F9 uses the Windows default: {default}", ""
    hits = [str(r["name"]) for r in rows if want.lower() in str(r["name"]).lower()]
    if hits:
        return True, f"ptt.device '{want}' = {hits[0]}", ""
    names = ", ".join(str(r["name"]) for r in rows[:4])
    return (False, f"no microphone matches ptt.device '{want}' (found: {names})",
            'py briandump.py mics, then py briandump.py config --set ptt.device "<part of its name>" (or null for the Windows default)')


@check("Talk-key task")
def check_task():
    if not _on_windows():
        return None, "skipped: Windows only", ""
    info = _task_info(TASK_NAME)
    if info is None:
        if not _import_ok("faster_whisper")[0]:                          # no task and no speech package: a part never installed
            return None, "not installed (the installer's talk-key part): F9 and F2 do nothing", "to add it: " + INSTALLER_FIX
        return False, f"the logon task '{TASK_NAME}' is not installed: F9 and F2 do nothing until it is", INSTALLER_FIX
    if not info.get("enabled", True):
        return False, f"'{TASK_NAME}' is disabled", f"Enable-ScheduledTask -TaskName '{TASK_NAME}'   (in PowerShell)"
    args = str(info.get("args") or "")
    if args and "\ufffd" not in args and str(REPO).lower() not in args.lower():   # (a garbled path proves nothing)
        return False, f"'{TASK_NAME}' runs BrianDump from another folder: {args}", "run the installer of this copy: " + INSTALLER_FIX
    return True, f"'{TASK_NAME}' starts the talk key at every logon", ""


@check("Talk-key service")
def check_service():
    log = CONF.logs / f"ptt-{CONF.host}.log"
    start = f"Start-ScheduledTask -TaskName '{TASK_NAME}' (or log off and on); the log: {log}"
    st = read_json(CONF.ptt_status, None)
    has_task = _on_windows() and _task_info(TASK_NAME) is not None
    if not isinstance(st, dict):
        if not has_task:
            return None, "not running", ""
        return False, "the talk-key task has not reported in", start
    ts = _epoch(st.get("ts"))
    if ts is None:
        try:
            ts = CONF.ptt_status.stat().st_mtime
        except OSError:
            ts = 0.0
    age = time.time() - ts
    if age > STATUS_FRESH_S:
        if not has_task:
            return None, f"not running (last seen {_ago(age)} ago)", ""
        why = f" (last error: {st.get('last_error')})" if st.get("last_error") else ""
        return False, f"not running: the last sign of life was {_ago(age)} ago{why}"[:220], start
    if not st.get("ready"):
        if st.get("last_error"):
            return False, f"running, but: {st.get('last_error')}"[:200], f"see the log: {log}"
        return None, f"starting: loading the speech model '{st.get('model') or CONF.get('ptt.model', 'small')}'", ""
    talk = str(st.get("key") or CONF.get("ptt.key", "f9")).upper()
    dump = str(st.get("dump_key") or CONF.get("ptt.dump_key", "f2")).upper()
    mic = st.get("device") or "the Windows default"
    return True, f"ready: hold {talk} to talk, press {dump} for a brain dump (mic: {mic}; model: {st.get('model') or '?'})", ""


@check("Panel")
def check_panel():
    port = CONF.port
    kind, info = _panel_probe(port)
    if kind == "briandump":
        same = _same_path(info.get("data_dir"), CONF.data_dir)
        detail = f"BrianDump {info.get('version')} answers on http://127.0.0.1:{port}/"
        if info.get("sandbox"):
            detail += " (in SANDBOX mode)"
        if not same:
            return None, f"{detail}, for another data folder ({info.get('data_dir')})", ""
        return True, detail, ""
    if kind == "other":
        return (False, f"port {port} is held by another program: {info}",
                f"give the panel another port: py briandump.py config --set port {port + 1 if port < 65535 else 8771}")
    start_now = "start it now: py briandump.py launch (or BrianDump in the Start menu)"
    if _startup_shortcut().exists():
        return None, f"not running right now (port {port} is free); the Startup shortcut starts it at every sign-in", start_now
    # In panel mode the server is what types the F9 words: with the talk key installed, a server that nothing starts at
    # sign-in means F9 goes quiet after every restart.
    if _voice_mode() == "panel" and _on_windows() and _task_info(TASK_NAME) is not None:
        return (False, "not running, and nothing starts it at sign-in: in panel mode the panel server is what types your F9 words",
                f'{start_now}; to start it at every sign-in: powershell -ExecutionPolicy Bypass -File "{REPO / "install.ps1"}" -Startup')
    return None, f"not running (port {port} is free)", start_now


def run_checks(checks=None, timeout=CHECK_TIMEOUT + 1.0):
    """Every check in its own daemon thread, all against one deadline; never raises. A check that crashes or does not
    answer in time becomes an information row: it says nothing about the machine."""
    checks = list(CHECKS if checks is None else checks)
    results = [None] * len(checks)

    def work(i, fn):
        try:
            ok, detail, fix = fn()
        except Exception as e:
            ok, detail = None, f"the check itself failed: {type(e).__name__}: {e}"[:200]
            fix = "please report this with the output of: py briandump.py doctor --json"
        results[i] = Check(getattr(fn, "label", fn.__name__), ok, str(detail), str(fix or ""))

    threads = [threading.Thread(target=work, args=(i, fn), daemon=True, name=f"doctor-{i}") for i, fn in enumerate(checks)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [r if r is not None else Check(getattr(fn, "label", fn.__name__), None, f"no answer within {timeout:.0f} s (skipped)", "")
            for r, fn in zip(results, checks, strict=True)]


def _color_ok(stream):
    """ANSI colours: only on a real console, never with NO_COLOR; Windows consoles need virtual-terminal mode switched on."""
    try:
        if stream is None or os.environ.get("NO_COLOR") or not stream.isatty():
            return False
    except (AttributeError, ValueError, OSError):
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetStdHandle.restype = ctypes.c_void_p
        k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        h, mode = k32.GetStdHandle(-11), ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004) or bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def render(checks, color=False):
    marks = {True: ("ok", "32"), False: ("FAIL", "31"), None: ("info", "90")}
    width = max((len(c.name) for c in checks), default=8)
    lines = [f"BrianDump doctor  ({__version__}, {REPO})", ""]
    for c in checks:
        tag, code = marks[c.ok]
        tag = f"[{tag:^4}]"
        if color:
            tag = f"\x1b[{code}m{tag}\x1b[0m"
        lines.append(f"  {tag} {c.name:<{width}}  {c.detail}")
        if c.fix and c.ok is not True:
            lines.append(f"  {'':6} {'':<{width}}  {'fix' if c.ok is False else 'tip'}: {c.fix}")
    fails = sum(1 for c in checks if c.ok is False)
    lines.append("")
    lines.append("Everything BrianDump needs is in place." if not fails else
                 f"{fails} problem{'s' if fails != 1 else ''}: follow the fix line under each FAIL, then run py briandump.py doctor again.")
    return "\n".join(lines)


def cmd_doctor(a):
    checks = run_checks()
    if a.json:
        out(json.dumps([c._asdict() for c in checks], indent=2))
    else:
        out(render(checks, color=_color_ok(sys.stdout)))
    return 1 if any(c.ok is False for c in checks) else 0


# ---------- the parser ----------
def build_parser():
    ap = argparse.ArgumentParser(prog="py briandump.py", description="BrianDump: a side panel and a push-to-talk key for Claude Code.",
                                 epilog="Not sure where to start? py briandump.py doctor")
    ap.add_argument("--version", action="version", version=f"BrianDump {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="<command>")
    for name, text in PASSTHROUGH_HELP.items():
        sub.add_parser(name, help=text, add_help=False)       # listed here; main() hands their arguments over untouched

    m = sub.add_parser("mics", help="list the microphones")
    m.add_argument("--json", action="store_true", help='[{"index", "name", "default"}]')
    m.set_defaults(func=cmd_mics)

    c = sub.add_parser("config", help="show or change the settings (config.json)")
    c.add_argument("--init", action="store_true",
                   help="create config.json from the documented defaults, make the folders, record this folder as install_dir")
    c.add_argument("--set", nargs=2, action="append", metavar=("KEY", "VALUE"),
                   help="change one setting, for example --set ptt.key f8 (repeatable; JSON values such as 8771, true, null)")
    c.add_argument("--show", action="store_true", help="the paths and the merged settings as JSON (the default)")
    c.add_argument("--path", action="store_true", help="the path of config.json")
    c.add_argument("--open", action="store_true", help="open config.json in your editor")
    c.set_defaults(func=cmd_config)

    q = sub.add_parser("ask", help="put a question on the panel (what /briandump:ask runs)")
    q.add_argument("text", nargs="*", help="the question")
    q.add_argument("--kind", choices=("ask", "think"), default="ask",
                   help="ask = something waits on the answer (default); think = something to mull over")
    q.add_argument("--options", help='quick-answer buttons, separated by | ("yes|no")')
    q.add_argument("--done", metavar="ID", help="close a question (q3)")
    q.add_argument("--answer", help="with --done: the answer, kept with the question")
    q.add_argument("--list", action="store_true", help="the open questions")
    q.add_argument("--all", action="store_true", help="with --list: closed ones too")
    q.set_defaults(func=cmd_ask)

    g = sub.add_parser("agenda", help="the agenda checklist (what /briandump:agenda runs)")
    g.add_argument("action", choices=("add", "done", "undone", "list"))
    g.add_argument("text", nargs="*", help="the item")
    g.add_argument("--all", action="store_true", help="list: the ticked items too")
    g.set_defaults(func=cmd_agenda)

    d = sub.add_parser("dump", help="import an audio or text file as a brain dump")
    d.add_argument("file")
    d.add_argument("--label", help="a name for it on the panel")
    d.add_argument("--now", action="store_true",
                   help="transcribe and make the cards and the report here and now, instead of leaving it to the panel server")
    d.add_argument("--processor", choices=("claude", "mock"),
                   help="claude = one claude -p call (dump.processor in config.json); mock = canned cards, no model call")
    d.set_defaults(func=cmd_dump)

    dr = sub.add_parser("doctor", help="check every part and say how to fix what is missing")
    dr.add_argument("--json", action="store_true", help="[{name, ok, detail, fix}] with ok true, false or null (information)")
    dr.set_defaults(func=cmd_doctor)

    v = sub.add_parser("version", help="print the version")
    v.set_defaults(func=cmd_version)
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PASSTHROUGH:
        try:
            return PASSTHROUGH[argv[0]](argv[1:])
        except CommandError as e:
            err(f"BrianDump {argv[0]}: {e}")
            return 1
        except KeyboardInterrupt:
            return 130
    parser = build_parser()
    a = parser.parse_args(argv)
    if not getattr(a, "func", None):
        parser.print_help()
        return 2
    try:
        return int(a.func(a) or 0)
    except UsageError as e:
        err(f"BrianDump {a.command}: {e}")
        return 2
    except (CommandError, ConfigError, OSError) as e:          # ConfigError: a broken config.json is never written over
        err(f"BrianDump {a.command}: {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
