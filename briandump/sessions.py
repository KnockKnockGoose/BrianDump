"""Claude Code's live sessions on this PC, as rows the voice router (and the panel) can use.

Nothing is instrumented: everything comes from files Claude Code already writes.

  registry    ~/.claude/sessions/<pid>.json, one per running `claude` process, removed on a clean exit. Holds the session id,
              the process id, cwd, the --name, the entrypoint ("cli" interactive, "sdk-cli" headless) and Claude Code's own
              busy/idle flag. The <pid>.<hash>.key files next to them are peer-auth secrets and are never read.
  transcript  ~/.claude/projects/<project>/<session id>.jsonl. The Tail below reads it incrementally (each byte once) and
              keeps what a status needs: open tool calls (running), the turn_duration record (idle), open AskUserQuestion
              calls (needs you), the title, model and context size.
  needs_you   <state_dir>/state/needs_you.json, written by the plugin's Notification hook (permission prompt, question).

A row's `status` is one of idle | running | thinking | needs_you (first match wins: needs_you > running > idle > thinking).
`reg_status` carries the registry's own flag unchanged, so the router has a second opinion before it types.

A process is alive when OpenProcess + GetExitCodeProcess say STILL_ACTIVE (ctypes, no psutil) and it was created before the
session's startedAt (a crashed session's pid can be handed to a new process); a registry file whose pid is gone is a crashed
session and is skipped. In sandbox mode (BRIANDUMP_SANDBOX=1) the registry is a fixture, so a fixture process table
<state_dir>/procs.json {"alive": [pids]} decides instead; without that file every fixture session counts as alive.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path, PureWindowsPath

from briandump.config import CONF

HOST = CONF.host
BIG = 200_000                                               # transcript lines over this (screenshots) are skipped, ids excepted
STUCK_MIN = 10                                              # running with a silent transcript for this long = stuck
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
REUSE_SLACK_S = 60                                          # a process created this long after the session started is another one
EPOCH_AS_FILETIME = 11_644_473_600                          # seconds from 1601-01-01 (FILETIME) to 1970-01-01
SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")               # a session id is a UUID; anything else never becomes a glob pattern
TS_RE = re.compile(r'"timestamp":\s*"([^"]+)"')
TOOL_ID_RE = re.compile(r'"tool_use_id":\s*"([^"]+)"')


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


def epoch(s):
    """ISO timestamp (Z or offset, or a naive local one) -> epoch seconds; None if unparsable."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.timestamp() if d.tzinfo else time.mktime(d.timetuple())


def mins(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600} h {sec % 3600 // 60} min" if sec >= 3600 else f"{sec // 60} min" if sec >= 60 else f"{sec} s"


def short_model(m):
    """'claude-opus-5-5' -> 'opus'; a bare alias ('sonnet') passes through."""
    if not m:
        return None
    x = re.match(r"claude-([a-z]+)", str(m))
    return x.group(1) if x else str(m)


def project_name(cwd, home=None):
    """The folder's own name; the home directory is 'home'."""
    s = str(cwd or "").replace("\\", "/").rstrip("/")
    if not s:
        return ""
    h = str(home or Path.home()).replace("\\", "/").rstrip("/")
    if s.lower() == h.lower():
        return "home"
    return s.split("/")[-1] or s


def describe(name, inp):
    """One short line for a tool call: 'Bash: list files', 'Edit server.py', 'Agent: review the diff'."""
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Bash", "PowerShell"):
        return f"{name}: {inp.get('description') or str(inp.get('command') or '')[:60]}".strip(": ")
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = str(inp.get("file_path") or inp.get("notebook_path") or "")
        return f"{name} {PureWindowsPath(p).name}".strip() if p else name   # Windows paths, on any OS
    if name in ("Agent", "Task"):
        return f"Agent: {inp.get('description') or ''}".strip(": ")
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern') or ''}".strip()
    if name == "Monitor":
        return f"Monitor: {inp.get('description') or 'watching'}"
    if name == "AskUserQuestion":
        return "Asking you a question"
    return name


def row(**kw):
    r = {"id": "", "kind": "session", "machine": HOST, "name": "", "pid": None, "cwd": "", "project": "", "title": "",
         "model": None, "context": None, "started": None, "last_activity": None, "status": "idle", "reg_status": None,
         "step": "", "since": None, "stuck": False, "note": "", "version": None, "link": {}}
    r.update(kw)
    return r


# ---------- transcript tail ----------
class Tail:
    """Incremental reader for one transcript: pending / done tool calls, idle flag, open questions, title, model, context."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset, self.buf = 0, b""
        self.pending, self.done = {}, set()          # tool_use id -> {name, text, ts}; finished ids
        self.idle, self.last_ts = False, None
        self.title, self.model, self.context = "", "", 0
        self.asking = set()                          # AskUserQuestion tool_use ids without a result yet
        self.pending_agents, self.summary = 0, ""
        self.lock = threading.Lock()

    def update(self):
        with self.lock:
            try:
                self._update()
            except OSError:
                pass

    def _update(self):
        size = self.path.stat().st_size
        if size < self.offset:                       # truncated / replaced: start over
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = self.buf + f.read()
            self.offset = f.tell()
        lines = data.split(b"\n")
        self.buf = lines.pop()
        for raw in lines:
            if len(raw) > BIG:                       # a screenshot result: only its tool ids matter
                head = raw[:5000].decode("utf-8", "replace")
                ts = TS_RE.search(raw[-3000:].decode("utf-8", "replace"))
                for tid in TOOL_ID_RE.findall(head):
                    self.finish(tid, ts.group(1) if ts else None)
                continue
            try:
                self.ingest(json.loads(raw))
            except (ValueError, TypeError, AttributeError, KeyError):
                pass                                 # a line in an unexpected shape is skipped, never the whole session

    def finish(self, tid, ts):
        self.done.add(tid)
        self.pending.pop(tid, None)
        self.asking.discard(tid)

    def ingest(self, e):
        if not isinstance(e, dict) or e.get("isSidechain"):
            return
        t, msg, ts = e.get("type"), e.get("message") or {}, e.get("timestamp")
        if not isinstance(msg, dict):
            msg = {}
        if t == "assistant":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    name = b.get("name", "")
                    self.pending[b["id"]] = {"name": name, "text": describe(name, b.get("input") or {}), "ts": ts}
                    if name == "AskUserQuestion":
                        self.asking.add(b["id"])
            u = msg.get("usage") or {}
            if isinstance(u, dict) and u.get("input_tokens") is not None:
                self.context = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                                "cache_creation_input_tokens", "output_tokens"))
                self.model = msg.get("model") or self.model
            self.idle = False
        elif t == "user":
            c = msg.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self.finish(b.get("tool_use_id"), ts)
            self.idle = False
        elif t == "system":
            sub = e.get("subtype")
            if sub == "turn_duration":
                self.idle = True
                self.pending_agents = int(e.get("pendingBackgroundAgentCount") or 0)
            elif sub == "away_summary":
                self.summary = str(e.get("content") or "")[:300]
        elif t == "ai-title":
            self.title = str(e.get("aiTitle") or "")[:120]
        if ts and t in ("assistant", "user", "system"):
            self.last_ts = ts

    def open_calls(self):
        return [v for k, v in self.pending.items() if k not in self.done]


def block_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


TAILS = {}                                                   # transcript path -> Tail (shared by every poll)
_TRANSCRIPTS = {}                                            # (projects dir, session id) -> transcript path
_K32 = []


# ---------- processes ----------
def _k32():
    """A private kernel32 handle with the argtypes this module needs (64-bit handles, reliable last-error codes)."""
    if not _K32:
        import ctypes.wintypes as w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.GetExitCodeProcess.argtypes, k.GetExitCodeProcess.restype = [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL
        k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        k.GetProcessTimes.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        _K32.append(k)
    return _K32[0]


def _win_pid_alive(pid, started_ms=None):
    import ctypes.wintypes as w
    k = _k32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ctypes.get_last_error() == 5                   # access denied = it exists but belongs to someone else
    try:
        code = w.DWORD(0)
        if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            return False
        try:
            started_s = float(started_ms) / 1000 if started_ms else None
        except (TypeError, ValueError):
            started_s = None
        if started_s:
            times = [ctypes.c_ulonglong(0) for _ in range(4)]   # creation, exit, kernel, user (FILETIME, 100 ns since 1601)
            if k.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
                created = times[0].value / 1e7 - EPOCH_AS_FILETIME
                if created > started_s + REUSE_SLACK_S:
                    return False                              # the session's pid now belongs to a newer process
        return True
    finally:
        k.CloseHandle(h)


def os_pid_alive(pid, started_ms=None):
    """The real process table: OpenProcess + GetExitCodeProcess == STILL_ACTIVE on Windows (with `started_ms`, a process
    created after that moment is a reused pid, not the session), os.kill(pid, 0) elsewhere. Unknown -> True."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            return _win_pid_alive(pid, started_ms)
        os.kill(pid, 0)                                       # POSIX only: on Windows os.kill(pid, 0) TERMINATES the process
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True                                           # unknown: trust the registry (removed on a clean exit)


def pid_alive(pid, started_ms=None):
    """Is this session's process running? The real process table (os_pid_alive), except in the sandbox, where
    <state_dir>/procs.json {"alive": [...]} decides (no file = trust the fixture registry)."""
    if CONF.sandbox:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        try:
            d = json.loads((CONF.state_dir / "procs.json").read_text(encoding="utf-8"))
            return pid in {int(p) for p in d.get("alive") or []}
        except (OSError, ValueError, TypeError, AttributeError):
            return True
    return os_pid_alive(pid, started_ms)


# ---------- registry + transcripts ----------
def read_registry(sessions_dir=None, alive=None):
    """Live session records (only <pid>.json; the <pid>.<hash>.key peer-auth files are never opened), each with 'alive' from
    the process table (`alive(pid)` replaces it in tests)."""
    out = []
    try:
        files = sorted(Path(sessions_dir or CONF.sessions).glob("*.json"))
    except OSError:
        files = []
    for p in files:
        if p.name.count(".") != 1:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(d, dict) and d.get("sessionId"):
            d.pop("messagingSocketPath", None)
            try:
                d["alive"] = bool(alive(d.get("pid")) if alive else pid_alive(d.get("pid"), d.get("startedAt")))
            except Exception:
                d["alive"] = False
            out.append(d)
    return out


def find_transcript(session_id, projects_dir=None):
    root = Path(projects_dir or CONF.projects)
    sid = str(session_id or "")
    if not SID_RE.match(sid):
        return None
    p = _TRANSCRIPTS.get((str(root), sid))
    if p and p.exists():
        return p
    try:
        hits = list(root.glob(f"*/{sid}.jsonl"))
    except OSError:
        hits = []
    if hits:
        _TRANSCRIPTS[(str(root), sid)] = hits[0]
    return hits[0] if hits else None


def needs_you_for(session_id, last_ts, path=None):
    """The Notification hook's flag if it is for this session and the transcript has not moved past it (idle_prompt skipped)."""
    try:
        n = json.loads(Path(path or CONF.needs_you).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(n, dict) or n.get("type") == "idle_prompt" or n.get("session") not in ("", None, session_id):
        return None
    if last_ts and n.get("ts") and str(last_ts)[:23] > str(n["ts"])[:23]:
        return None
    return n


def session_status(r, tl, d, now, needs):
    """Status rules (first match wins): needs_you > running > idle > thinking; 'stuck' = running with a silent transcript."""
    last = epoch(r["last_activity"]) or 0
    calls = tl.open_calls() if tl else []
    if needs or (tl and tl.asking) or d.get("status") == "waiting":
        r.update(status="needs_you", since=(needs or {}).get("ts") or r["last_activity"],
                 step=(needs or {}).get("message") or ("Asking you a question" if tl and tl.asking else "Waiting for a permission"))
    elif calls and not (tl and tl.idle):
        r.update(status="running", step=calls[-1]["text"], since=calls[-1]["ts"])
    elif (tl and tl.idle) or now - last > 300 or not tl:
        r.update(status="idle", since=(tl.last_ts if tl else None) or r["last_activity"])
    else:
        r.update(status="thinking", since=tl.last_ts or r["last_activity"])
    r["stuck"] = r["status"] == "running" and now - last > STUCK_MIN * 60
    if r["stuck"]:                                            # appended: a "headless" note must stay first (never a voice target)
        r["note"] = (r["note"] + " · " if r.get("note") else "") + "no transcript activity for " + mins(now - last)
    if tl and tl.pending_agents:
        r["pending_agents"] = tl.pending_agents
    return r


def rows(now=None, *, sessions_dir=None, projects_dir=None, needs_you=None, host=None, tails=None, alive=None):
    """One row per live interactive or headless session on this PC (crashed registry entries skipped)."""
    now = time.time() if now is None else now
    host = host or HOST
    tails = TAILS if tails is None else tails
    needs_path = needs_you or CONF.needs_you
    out = []
    for d in read_registry(sessions_dir, alive):
        sid = str(d["sessionId"])
        if not d.get("alive") or not SID_RE.match(sid):
            continue
        try:
            out.append(_session_row(d, sid, now, host, tails, projects_dir, needs_path))
        except Exception:                                     # one odd registry record never hides the other sessions
            continue
    return out


def _session_row(d, sid, now, host, tails, projects_dir, needs_path):
    path = find_transcript(sid, projects_dir)
    tl = None
    if path:
        tl = tails.get(path) or tails.setdefault(path, Tail(path))
        tl.update()
    try:
        mtime = path.stat().st_mtime if path else None
    except OSError:
        mtime = None
    try:
        started = iso(float(d.get("startedAt")) / 1000) if d.get("startedAt") else None
    except (TypeError, ValueError):
        started = None
    name = str(d.get("name") or sid[:8])
    cwd = str(d.get("cwd") or "")
    r = row(id=sid, name=name, pid=d.get("pid"), cwd=cwd, project=project_name(cwd), title=(tl.title if tl else "") or name,
            model=short_model(tl.model if tl else None), context=tl.context if tl else None,
            started=started, last_activity=iso(mtime), version=d.get("version"),
            reg_status=d.get("status"), machine=host, note="headless" if d.get("entrypoint") == "sdk-cli" else "",
            link={"resume": f"claude --resume {sid}", "transcript": str(path) if path else "", "cwd": cwd})
    if tl and tl.summary:
        r["summary"] = tl.summary
    return session_status(r, tl, d, now, needs_you_for(sid, tl.last_ts if tl else None, needs_path))


def claude_exe():
    """Where `claude` lives (for the panel's Open Main button)."""
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
