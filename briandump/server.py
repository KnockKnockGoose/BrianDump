"""BrianDump panel server: the page, its JSON API and the dump worker, built on Claude Code's own transcript.

    py -m briandump.server [--port N] [--session <id or .jsonl path>]

Nothing is instrumented. The status strip, steps, context meter and Claude's open questions all come from the session
transcript Claude Code already writes (~/.claude/projects/<project>/<session>.jsonl), read incrementally by Tracker so each
byte is parsed once. Everything else is a file under the state or data directory (briandump/config.py): the plugin's
needs_you.json, the PTT service's ptt_status.json, agenda.md / questions.md (briandump.agenda), the dump store (briandump.dumps)
and the Reports tab's list (briandump.reports, which serves only files it listed itself, by id).
The API is listed in docs/architecture.md; the page is panel.html (one file, no build step).

Sibling modules are imported defensively: if agenda, dumps, voice, sessions or reports fails to import, the server still
starts and serves the page, /api/machine lists the warning, and the endpoints that need the missing module answer 503 with the
reason.

Who may ask. Loopback by default; `allow` in config.json admits other client IPs and everyone else gets 403. The actions that
touch THIS desktop (its window, a folder, a report file, the browser, a new terminal) answer the local page only. A web page
open in your own browser is "local" too, so two more checks keep other sites off the API: the Host header must name this PC
exactly (an IP address, localhost or one of its own names), which defeats DNS rebinding, and a POST must carry JSON and, when
the browser sends an Origin, come from this page's own origin, which defeats cross-site form and fetch posts. Without them any
site you visit could post an "answer" and have it typed into your Claude Code session.

Keystroke rule (non-negotiable): the only endpoints that write inbox files are /api/answer and /api/dumps/<id>/send, and both
carry what the user typed on the panel. Nothing here types text; /api/voice only drops or retries lines that already exist
(Send anyway on a stale line is a retry), and /api/answer/arm only lets your next hold-to-talk clip quote the question it
answers (briandump/answers.py).
An answer to a question a session asked goes to that session only (its id rides on the inbox file's first line), and is
refused when that session is not running, so one session's question never reaches another with the user's "yes" attached.

Windows file contention: os.replace fails while another process has the target open (ptt.py polls its command file every
0.3 s), so state writes retry (atomic_write). Files other processes rewrite are read in one call, and a locked or half-written
one counts as "no change this poll" (read_shared_json), never an error page.

The port: on Windows SO_REUSEADDR lets a second process bind a port that is already listening, so two panels would share it
silently. The server binds without it, and probes the port first: if anything answers, it says so in one line and exits 3.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import functools
import hashlib
import importlib
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from briandump import __version__
from briandump.config import CONF, LOOPBACK, REPO

WARNINGS = {}           # sibling module -> why it did not import (/api/machine shows it; its endpoints answer 503)
EXIT_PORT_IN_USE = 3
AUTO_MAIN_S = 3.0       # seconds after start before auto_open_main looks for Main (the router's first poll is at 1 s)
CREATE_NO_WINDOW = 0x08000000


def log(msg, echo=True):
    """One line to <state_dir>/logs/server.log (pythonw has no console, so the file is the record) and to stdout."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        CONF.logs.mkdir(parents=True, exist_ok=True)
        with open(CONF.logs / "server.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if echo:
        try:
            print(line, flush=True)
        except (OSError, ValueError, UnicodeError):     # a closed pipe or a console that cannot show the text
            pass


_LOGGED = {}


def log_once(key, msg):
    """log() a recurring problem once, not on every poll."""
    if _LOGGED.get(key) != msg:
        _LOGGED[key] = msg
        log(msg)


def _optional(name):
    """Import a sibling module, or None with a warning: a half-written sibling must never keep the panel from starting."""
    try:
        return importlib.import_module(f"briandump.{name}")
    except Exception as e:  # noqa: BLE001 - anything at all: SyntaxError, a missing dependency, a NameError at import
        WARNINGS[name] = f"{type(e).__name__}: {e}"
        return None


agenda = _optional("agenda")
dumps = _optional("dumps")
sessions = _optional("sessions")
voice = _optional("voice")
reports = _optional("reports")
answers = _optional("answers")


class Unavailable(Exception):
    """The endpoint needs a sibling module that did not import: 503 with the reason."""


class Busy(Exception):
    """A file stayed locked by another process through every retry: 503, and the page simply tries again."""


def need(mod, name):
    if mod is None:
        raise Unavailable(f"briandump.{name} is not available ({WARNINGS.get(name, 'not imported')})")
    return mod


FEED_MAX, STEPS_MAX = 60, 12
TS_RE = re.compile(r'"timestamp":\s*"([^"]+)"')
TOOL_ID_RE = re.compile(r'"tool_use_id":\s*"([^"]+)"')
ERROR_RE = re.compile(r'"is_error":\s*true')
# "VOICE <stamp>-<host>: text" (a clip relayed by watch_inbox.py in monitor mode) or "VOICE <stamp>-panel-<host>: text"
VOICE_RE = re.compile(r"VOICE (\d{8}-\d{6})(?:-([\w-]+))?: (.*?)</event>", re.S)
QSENT_RE = re.compile(r"[^.!?\n]*\?")
SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")
DUMP_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")       # \Z: "$" would also take a trailing newline
ACTION_ID_RE = re.compile(r"^[\w-]{1,32}\Z", re.ASCII)
DUMP_ROUTE_RE = re.compile(r"^/api/dumps/([^/]+)(?:/([^/]+))?$")
REPORT_ID_RE = re.compile(r"^[0-9a-f]{12}\Z")                    # reports.rid(): a hash of the path, never the path
REPORT_ROUTE_RE = re.compile(r"^/api/reports/([^/]+)(?:/([^/]+))?$")
NAME_RE = re.compile(r"^\w[\w .-]{0,39}$")          # voice.main_session as it may appear on a command line
URL_RE = re.compile(r"^https?://[^\s\"'<>\\]{1,2000}$", re.I)
PROCESSING = ("recorded", "transcribing", "transcribed", "thinking")
PTT_COMMANDS = ("dump_toggle", "dump_start", "dump_stop")
MAX_BODY = 1_000_000
AGENDA_MAX = 500
ASKED_IN_TERMINAL = "Claude asked this in a dialog in its terminal: answer it there (the panel cannot pick a dialog option)"
# MCP server name -> app name on the panel (any app Claude drives)
APPS = {"premiere-pro": "Premiere", "photoshop": "Photoshop", "after-effects": "After Effects", "blender": "Blender",
        "claude_ai_Gmail": "Gmail", "claude_ai_Google_Drive": "Drive", "claude_ai_Google_Calendar": "Calendar",
        "claude-in-chrome": "Browser", "playwright": "Browser"}
# The page is one inline file: scripts and styles stay inline, but nothing may load from or talk to another origin, and no
# other site may frame it. 'unsafe-eval' is for the hosts that evaluate strings inside the page (pywebview's bridge, a test
# browser); the page itself never evals, and with 'unsafe-inline' already required it grants nothing new.
CSP = ("default-src 'self' 'unsafe-inline' 'unsafe-eval'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")

UPDATE_LOCK = threading.Lock()
TRACKERS = {}           # transcript path -> Tracker
KNOWN_QS = {}           # question id -> {"text", "session"?, ...}: what a panel answer quotes, and which session asked it
RELOAD = [0]            # bumped by POST /api/reload; open pages reload when the value in /api/state changes
SESSION = None          # --session: pin one transcript (id prefix or .jsonl path)
VOICE = None            # voice.Router in panel mode (created in main)
_DUMPS = {"t": 0.0, "dir": None, "rows": []}   # the listing is read at most every 2 s, not once per poll
_DETAILS = {}           # dump id -> last good detail (a half-written meta.json is "no change", not an error)
_LAST = {}              # path -> last good parse of a file another process rewrites


def iso_local():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def short(s, n=90):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def mask(text):
    """The private filter: text matching config private_filter leaves the server as '(private)'."""
    s = "" if text is None else str(text)
    return "(private)" if s and CONF.private_re.search(s) else s


def mask_prose(text):
    """Long text (a transcript, a summary) is masked sentence by sentence, so one private word hides its sentence only."""
    s = "" if text is None else str(text)
    if not s or not CONF.private_re.search(s):
        return s

    def one(m):
        part = m.group(0)
        if not CONF.private_re.search(part):
            return part
        return part[: len(part) - len(part.lstrip())] + "(private)"
    out = SENTENCE_RE.sub(one, s)
    return "(private)" if CONF.private_re.search(out) else out      # a pattern that spans two sentences


def norm(text):
    """agenda.norm when the module is there (ids then follow its dedupe); a plain fold otherwise (ids only)."""
    if agenda is not None:
        try:
            return agenda.norm(text)
        except Exception:  # noqa: BLE001
            pass
    return " ".join(str(text).lower().split())


def hid(text):
    """Stable short id for a checklist line: sha1 of the normalised text."""
    return hashlib.sha1(norm(text).encode("utf-8")).hexdigest()[:10]


def remember_qs(pairs):
    """Question ids the page may answer -> the question: full text and the session that asked (bounded: oldest go first)."""
    KNOWN_QS.update(pairs)
    if len(KNOWN_QS) > 2000:
        for k in list(KNOWN_QS)[:500]:
            KNOWN_QS.pop(k, None)


# ---------- files other processes write too ----------
def read_shared_json(path, default=None):
    """One short read of a JSON file another process rewrites (ptt_status.json every 0.25 s, needs_you.json, questions.json).
    Locked (PermissionError while the writer replaces it) or half-written (invalid JSON) means "no change this poll": the last
    good value comes back. A missing file is a real answer: `default`."""
    key = str(path)
    try:
        val = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LAST.pop(key, None)
        return default
    except (OSError, ValueError):
        return _LAST.get(key, default)
    _LAST[key] = val
    return val


def atomic_write(path, text, tries=20):
    """Write via a temp file + os.replace, so a reader never sees half a file. On Windows the replace raises PermissionError
    while any other process has the target open, so it is retried up to `tries` times, 25-50 ms apart; Busy if it never
    frees."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError:
            if i < tries - 1:
                time.sleep(0.025 + random.random() * 0.025)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise Busy(f"{path.name} stayed locked by another program; try again")


def write_inbox(kind, host, text, meta=None):
    """inbox/<stamp>-<kind>-<host>.txt, never overwriting: the name is the second it was written, so a second message in the
    same second claims the next free second (exclusive create). The stamp format is the one ptt.py and the voice router
    parse. `meta` ({"session": <id>}: where an answer goes) is a first line the router reads (voice.META), written in the same
    create as the text; a text that could pass for such a line gets an empty one."""
    if voice is not None and (meta or str(text).startswith(voice.META)):
        text = voice.META + json.dumps(meta or {}) + "\n" + str(text)
    inbox = CONF.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    t = time.time()
    for i in range(60):
        p = inbox / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(t + i))}-{kind}-{host}.txt"
        try:
            with open(p, "x", encoding="utf-8") as f:
                f.write(text)
            return p
        except FileExistsError:
            continue
    raise Busy("inbox: no free file name in the next 60 s")


# ---------- the transcript ----------
def find_transcript():
    """The pinned session (--session), else the most recently written transcript; None while ~/.claude has none."""
    try:
        if SESSION:
            p = Path(SESSION)
            if p.suffix == ".jsonl" and p.is_file():
                return p
            hits = [h for h in CONF.projects.glob("*/*.jsonl") if h.stem.startswith(SESSION)]
            if hits:
                return hits[0]
        files = list(CONF.projects.glob("*/*.jsonl"))
        return max(files, key=lambda f: f.stat().st_mtime) if files else None
    except OSError:                                   # a transcript vanished between the listing and its stat
        return None


def describe(name, inp):
    """One plain line for what a tool call is doing."""
    inp = inp if isinstance(inp, dict) else {}
    name = str(name or "")
    base = lambda p: PureWindowsPath(str(p or "")).name  # noqa: E731  (transcript paths are Windows paths, on any OS)
    if name in ("Bash", "PowerShell"):
        return f"{name}: {short(inp.get('description') or inp.get('command') or '')}"
    if name == "Read":
        return f"Reading {base(inp.get('file_path'))}"
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"Editing {base(inp.get('file_path') or inp.get('notebook_path'))}"
    if name in ("Grep", "Glob"):
        return f"Searching for {short(inp.get('pattern') or '', 60)}"
    if name == "WebSearch":
        return f"Web search: {short(inp.get('query') or '')}"
    if name == "WebFetch":
        return f"Reading web page {short(inp.get('url') or '', 70)}"
    if name in ("Agent", "Task"):
        return f"Helper agent: {short(inp.get('description') or '')}"
    if name == "Skill":
        return f"Running skill /{inp.get('skill') or ''}"
    if name == "AskUserQuestion":
        qs = inp.get("questions") if isinstance(inp.get("questions"), list) else []
        first = qs[0] if qs and isinstance(qs[0], dict) else {}
        return "Asking you: " + short(first.get("question") or "", 70)
    if name.startswith("mcp__"):
        parts = name.split("__")
        server, tool = parts[1], parts[-1]
        app = APPS.get(server, server)
        if tool == "invoke_tool":                      # some servers wrap their catalog in one tool
            tool = inp.get("name") or inp.get("tool_name") or tool
        squashed = re.escape(server.replace("-", "").replace("_", ""))
        tool = re.sub(rf"^(mcp_)?{squashed}_", "", str(tool), flags=re.I)      # photoshop_get_state -> get_state
        what = inp.get("description") or ""
        return f"{app}: {short(what) if what else tool.replace('_', ' ').replace('-', ' ')}"
    return name


def voice_start(stamp, host):
    """When the user STARTED speaking, as a UTC ISO timestamp: the file stamp is taken after Whisper finishes, so subtract the
    transcription time and the clip length (both in inbox/log-<host>.jsonl). Panel lines have no log entry."""
    t = dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").astimezone()
    if host and not host.startswith("panel"):
        try:
            for line in (CONF.inbox / f"log-{host}.jsonl").read_text(encoding="utf-8").splitlines()[-400:]:
                if f'"at": "{stamp}"' in line:
                    j = json.loads(line)
                    t -= dt.timedelta(seconds=float(j.get("seconds") or 0) + float(j.get("transcribe_s") or 0))
                    break
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user_line(content):
    """The user's side of the conversation: ('typed'|'voice'|'panel', text[, start]) or None for tool results / noise."""
    if isinstance(content, list):
        content = "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    if "<task-notification>" in content:
        m = VOICE_RE.search(content)
        if not m:
            return None
        text = m.group(3).strip()
        if (m.group(2) or "").startswith("panel"):
            return ("panel", text)
        return ("voice", text, voice_start(m.group(1), m.group(2)))
    if content.lstrip().startswith("<") or not content.strip():
        return None
    return ("typed", content.strip())


class Tracker:
    """Incremental reader for one transcript: each byte is parsed once and a rolling state is kept.

    The file is read in CHUNK pieces, so a first look at a transcript of hundreds of MB never holds it in memory. A line
    over BIG (a screenshot, a file dump) is never json-parsed or even kept whole: only its head (where the tool_use_id sits)
    and a rolling tail (timestamp, is_error) survive, so the tool call it answers still finishes. A line that is not JSON, or
    JSON of an unexpected shape, is skipped on its own and never stops the panel."""
    BIG = 200_000
    CHUNK = 1 << 20
    HEAD, TAIL = 5000, 3000

    def __init__(self, path):
        self.path, self.offset, self.buf, self.head = Path(path), 0, b"", None
        self.feed, self.pending, self.done = [], {}, set()
        self.last_ts, self.idle = None, False
        self.steps = []          # finished tool calls: text, ts, end, error
        self.context = 0         # tokens in context on the last API call
        self.model = ""
        self.cwd = ""            # the session's working directory (the project name on the panel)
        self.asking = {}         # AskUserQuestion tool_use id -> its questions (open until answered)
        self.last_qs = []        # "?" sentences from Claude's latest question-asking message

    def finish(self, tool_id, ts, error=False):
        self.done.add(tool_id)
        step = self.pending.pop(tool_id, None)
        if step:
            self.steps.append({**step, "end": ts, "error": bool(error)})
            self.steps = self.steps[-STEPS_MAX:]

    def update(self):
        with UPDATE_LOCK:                 # ThreadingHTTPServer: two polls reading the same new bytes doubled feed lines
            try:
                self._update()
            except OSError:               # deleted or locked between two polls: try again on the next one
                pass

    def _update(self):
        size = self.path.stat().st_size
        if size < self.offset:            # rewritten: start over
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            while True:
                chunk = f.read(self.CHUNK)
                if not chunk:
                    break
                self.offset += len(chunk)
                self._eat(chunk)
        self.feed = self.feed[-FEED_MAX:]

    def _eat(self, chunk):
        *ended, rest = chunk.split(b"\n")
        for part in ended:                # each of these finishes a line
            self._grow(part)
            if self.head is None:
                self._line(self.buf)
            else:
                self._big(self.head, self.buf)
            self.buf, self.head = b"", None
        self._grow(rest)                  # the start of a line that is still being written

    def _grow(self, part):
        if self.head is not None:         # inside a giant line: a rolling tail only
            self.buf = (self.buf + part)[-self.TAIL:]
            return
        self.buf += part
        if len(self.buf) > self.BIG:
            self.head, self.buf = self.buf[: self.HEAD], self.buf[-self.TAIL:]

    def _big(self, head, tail):
        h, t = head.decode("utf-8", "replace"), tail.decode("utf-8", "replace")
        ts = TS_RE.search(t) or TS_RE.search(h)          # Claude Code writes "timestamp" after "message": the tail
        for tid in TOOL_ID_RE.findall(h):
            self.finish(tid, ts.group(1) if ts else None, bool(ERROR_RE.search(h) or ERROR_RE.search(t)))

    def _line(self, raw):
        if not raw.strip():
            return
        try:
            e = json.loads(raw)
        except ValueError:
            return
        if isinstance(e, dict):
            try:
                self.ingest(e)
            except Exception:  # noqa: BLE001 - one odd line is skipped; the next poll carries on after it
                pass

    def ingest(self, e):
        if e.get("isSidechain"):
            return
        if isinstance(e.get("cwd"), str) and e["cwd"]:
            self.cwd = e["cwd"]
        t = e.get("type")
        ts = e.get("timestamp") if isinstance(e.get("timestamp"), str) else None
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        if t == "assistant":
            content = msg.get("content")
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip() not in ("", "✓"):
                    self.feed.append({"who": "claude", "text": b["text"], "ts": ts, "model": str(msg.get("model") or "")})
                    found = extract_questions(b["text"])
                    if found:
                        self.last_qs = [{"text": q, "ts": ts} for q in found]
                elif b.get("type") == "tool_use" and b.get("id"):
                    tid, inp = str(b["id"]), b.get("input") if isinstance(b.get("input"), dict) else {}
                    self.pending[tid] = {"text": describe(b.get("name"), inp), "ts": ts}
                    if b.get("name") == "AskUserQuestion":
                        qs = inp.get("questions") if isinstance(inp.get("questions"), list) else []
                        self.asking[tid] = [dict(q, ts=ts) for q in qs if isinstance(q, dict)]
            u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            num = lambda k: u.get(k) if isinstance(u.get(k), (int, float)) and not isinstance(u.get(k), bool) else 0  # noqa: E731
            if isinstance(u.get("input_tokens"), (int, float)):
                self.context = int(num("input_tokens") + num("cache_read_input_tokens") + num("cache_creation_input_tokens")
                                   + num("output_tokens"))
                self.model = str(msg.get("model") or self.model)
            self.idle = False
        elif t == "user":
            c = msg.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                        tid = str(b["tool_use_id"])
                        self.finish(tid, ts, b.get("is_error"))
                        self.asking.pop(tid, None)
            u = user_line(c) if not e.get("isMeta") else None
            if u:
                self.feed.append({"who": u[0], "text": u[1], "ts": u[2] if len(u) > 2 else ts})
                # questions are NOT cleared when the user speaks: a voice note is often about something else
            self.idle = False
        elif t == "attachment" and isinstance(e.get("attachment"), dict) and e["attachment"].get("type") == "queued_command":
            # lines that land while Claude is mid-turn are absorbed and logged as an attachment, not a user message
            a = e["attachment"]
            u = user_line(a.get("prompt") or "")
            if u:
                self.feed.append({"who": u[0], "text": u[1], "ts": u[2] if len(u) > 2 else a.get("timestamp") or ts})
        elif t == "system" and e.get("subtype") == "turn_duration":
            self.idle = True
        if ts:
            self.last_ts = ts


def extract_questions(md):
    """Question sentences in one Claude message (outside code), markdown stripped, max 5: Claude usually asks in prose
    ("Which one? Say 1, 2 or 3"), so the panel picks them up itself."""
    out, fence = [], False
    for line in str(md).splitlines():
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if fence or "?" not in line or line.lstrip().startswith(("|", ">")):
            continue
        clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)        # [text](url) -> text
        clean = re.sub(r"https?://\S+", "", clean)                     # bare URLs (query strings)
        clean = re.sub(r"[*_`]", "", clean).strip(" -•\t")
        clean = re.sub(r"^\d+[.)]\s*", "", clean)
        qs = [q.strip() for q in QSENT_RE.findall(clean) if len(q.strip()) > 6]
        if qs:
            out.append(clean if len(clean) < 240 else " ".join(qs))
    return out[-5:]


def auto_questions(tr, session=None):
    """Claude's open questions: AskUserQuestion cards (with options) and '?' sentences from its latest message. `session` = the
    id of the session whose transcript asked them: an answer goes there and nowhere else."""
    qs = []
    for tid, items in tr.asking.items():
        for i, q in enumerate(items):
            opts = q.get("options") if isinstance(q.get("options"), list) else []
            qs.append({"id": f"a{str(tid)[-6:]}{i}", "text": str(q.get("question") or ""), "kind": "ask",
                       "options": [str(o.get("label") or "") for o in opts if isinstance(o, dict)], "asked": q.get("ts"),
                       "source": "asked", "session": session})
    for i, q in enumerate(tr.last_qs):                     # shown while Claude works too: answers can land mid-turn
        qs.append({"id": f"c{q['ts']}-{i}", "text": q["text"], "kind": "ask", "options": [], "asked": q["ts"], "source": "message",
                   "session": session})
    return qs


def project_name(cwd):
    """The folder Claude Code runs in: its own name, 'home' for the home directory, None before a line with a cwd."""
    if not cwd:
        return None
    s = str(cwd).replace("\\", "/").rstrip("/")
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    if s.lower() == home.lower():
        return "home"
    return s.split("/")[-1] or s


def load_needs_you(path, tr):
    """The Notification hook's flag, if it is for this session and nothing has happened since. idle_prompt is skipped:
    'Waiting for you' already covers it, and it fires after every turn."""
    n = read_shared_json(CONF.needs_you)
    if not isinstance(n, dict) or n.get("type") == "idle_prompt" or str(n.get("session") or "") not in ("", path.stem):
        return None
    if tr.last_ts and str(n.get("ts") or "") and tr.last_ts[:23] > str(n["ts"])[:23]:   # the log moved on -> answered
        return None
    return {"message": str(n.get("message") or "Claude needs you"), "ts": n.get("ts")}


def tracker(path):
    return TRACKERS.get(path) or TRACKERS.setdefault(path, Tracker(path))


def build_state():
    limit = CONF.context_limit
    path = find_transcript()
    if not path:
        return {"session": None, "project": None, "activity": {"state": "idle", "text": "No Claude Code session yet", "since": None},
                "steps": [], "context": {"used": 0, "limit": limit, "model": ""}, "auto_questions": [], "feed": []}
    tr = tracker(path)
    tr.update()
    running = [v for k, v in tr.pending.items() if k not in tr.done]
    if running and not tr.idle:
        activity = {"state": "working", "text": mask(running[-1]["text"]), "since": running[-1]["ts"]}
    elif tr.idle:
        activity = {"state": "idle", "text": "Waiting for you", "since": tr.last_ts}
    else:
        activity = {"state": "thinking", "text": "Thinking…", "since": tr.last_ts}
    needs = load_needs_you(path, tr)
    if needs:
        activity = {"state": "needs", "text": mask(needs["message"]), "since": needs["ts"]}
    project = project_name(tr.cwd)
    return {"auto_questions": auto_questions(tr, path.stem), "session": path.stem[:8], "project": mask(project) if project else project,
            "feed": sorted(tr.feed, key=lambda m: m.get("ts") or ""), "activity": activity,
            "steps": [dict(s, text=mask(s["text"])) for s in tr.steps],
            "context": {"used": tr.context, "limit": limit, "model": tr.model}}


# ---------- questions, agenda, PTT ----------
def manual_questions():
    """questions.json, written by `briandump.py ask` (Claude asking you through the plugin command); `session` = the session
    that ran it, when Claude Code said (CLAUDE_CODE_SESSION_ID)."""
    qs = read_shared_json(CONF.manual_questions, [])
    out = []
    for q in qs if isinstance(qs, list) else []:
        if isinstance(q, dict) and q.get("text"):
            opts = q.get("options") if isinstance(q.get("options"), list) else []
            out.append({"id": str(q.get("id") or hid(q["text"])), "text": str(q["text"]), "kind": str(q.get("kind") or "ask"),
                        "options": [str(o) for o in opts], "asked": q.get("asked"), "done": bool(q.get("done")), "source": "manual",
                        "session": str(q.get("session") or "") or None})
    return out


def open_manual_questions(max_age_h=48):
    """The ones still open and asked within the last two days (an ask nobody answered fades out)."""
    cutoff = time.time() - max_age_h * 3600
    return [q for q in manual_questions() if not q["done"] and (_epoch(q.get("asked")) or time.time()) >= cutoff]


def mark_manual_done(qid):
    """Best effort: flip done on the questions.json entry a panel answer just covered, so the card goes away."""
    qs = read_shared_json(CONF.manual_questions, [])
    hit = False
    for q in qs if isinstance(qs, list) else []:
        if isinstance(q, dict) and str(q.get("id")) == qid and not q.get("done"):
            q["done"], hit = True, True
    if hit:
        try:
            atomic_write(CONF.manual_questions, json.dumps(qs, indent=2, ensure_ascii=False))
        except (OSError, Busy) as e:
            log(f"questions.json: {e}")


def md_questions():
    """Open items of questions.md (the dump worker adds a dump's next prompts there) as question cards."""
    if agenda is None:
        return []
    try:
        items = agenda.read(CONF.questions_file)
    except Exception as e:  # noqa: BLE001 - a broken file must not take /api/state down with it
        log_once("questions.md", f"questions.md: {type(e).__name__}: {e}")
        return []
    return [{"id": "qmd-" + hid(i["text"]), "text": str(i.get("base") or i["text"]), "kind": "think", "options": [], "asked": None,
             "source": "dump"} for i in items or [] if isinstance(i, dict) and i.get("text") and not i.get("done")]


def public_question(q, names=None):
    """A question card as the page gets it: text and options through the private filter; `to` = who asked, when a session
    did (its name from `names`, the live sessions, or "session ended")."""
    out = {k: q.get(k) for k in ("id", "text", "kind", "options", "source", "asked")}
    out["text"] = mask(q.get("text"))
    out["options"] = [mask(o) for o in q.get("options") or []]
    if q.get("topic"):
        out["topic"] = mask(q["topic"])
    if q.get("session"):
        out["to"] = mask((names or {}).get(q["session"]) or "session ended")
    return out


def question(qid, dump_id=None):
    """The question behind an id the page sent back: {"text", "session"?, ...} with the full text (the page may only have
    '(private)'), or None."""
    if not qid:
        return None
    if qid in KNOWN_QS:
        return KNOWN_QS[qid]
    for q in open_manual_questions() + md_questions():
        if q["id"] == qid:
            return q
    if dump_id and DUMP_ID_RE.match(str(dump_id)) and dumps is not None:
        try:
            dump_detail(str(dump_id))                 # registers that dump's prompts in KNOWN_QS
        except (Unavailable, Busy):
            pass
        return KNOWN_QS.get(qid)
    return None


def answer_route(q):
    """Where an answer to question `q` goes: (its session id, None) when a live session asked it; (None, None) for the router's
    usual target when no session did (a brain-dump question, `ask` run by hand); (None, why) when the session that asked
    cannot get it: the answer is then refused, never sent to another session."""
    sid = str((q or {}).get("session") or "")
    if not sid or voice is None:
        return None, None
    if configured_mode() == "monitor":                    # one watcher session takes every line
        lock = read_shared_json(CONF.voice_lock, {})
        if not isinstance(lock, dict) or str(lock.get("session") or "") != sid:
            return None, ("monitor mode types every line into the session that holds the voice, and another session asked "
                          "this: answer it in that session's terminal")
        return None, None
    if voice.find_session(session_rows(), sid, CONF.host) is None:
        return None, "the session that asked this is not running any more, so the answer was not sent"
    return sid, None


def max_chars():
    """The longest line the voice router types (voice.max_chars, read when it started): it cuts anything longer."""
    if voice is not None:
        return voice.MAX_CHARS
    try:
        return int(CONF.get("voice.max_chars", 4000) or 4000)
    except (TypeError, ValueError):
        return 4000


def session_names():
    """Live interactive sessions on this PC: id -> name (a question card says who asked)."""
    if voice is None:
        return {}
    try:
        return {r["id"]: r["name"] for r in session_rows() if voice.is_interactive(r, CONF.host)}
    except Exception as e:  # noqa: BLE001 - a card without a name is fine
        log_once("session_names", f"session names: {type(e).__name__}: {e}")
        return {}


def agenda_items():
    """agenda.md as the page shows it: open first (file order within), `base` = the text without BrianDump's
    "(from brain dump ...)" note, both through the private filter. `line` (the file order) lets the page keep a row it just
    ticked in its place for the undo window."""
    ag = need(agenda, "agenda")
    items = ag.read(CONF.agenda_file, heading=CONF.get("agenda.heading"))
    rows = [{"text": mask(i["text"]), "base": mask(i.get("base") or i["text"]), "done": bool(i.get("done")), "id": hid(i["text"]),
             "line": i.get("line")} for i in items or [] if isinstance(i, dict) and i.get("text")]
    rows.sort(key=lambda r: r["done"])
    return rows


def agenda_text_for(iid):
    """The real text behind an agenda id (the page only has '(private)' for a masked item)."""
    ag = need(agenda, "agenda")
    items = ag.read(CONF.agenda_file, heading=CONF.get("agenda.heading"))
    return next((i["text"] for i in items or [] if isinstance(i, dict) and i.get("text") and hid(i["text"]) == iid), None)


def _epoch(v):
    """Epoch seconds from a status-file time: a number, or an ISO string (with or without a trailing Z)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            d = dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return (d if d.tzinfo else d.astimezone()).timestamp()
        except ValueError:
            return None
    return None


def ptt_state():
    """ptt_status.json plus `alive` (written within the last 5 s): a stale file is not a running service."""
    out = {"alive": False, "ready": False, "recording": False, "mode": None, "started": None, "seconds": 0, "level_db": None,
           "peak_db": None, "key": str(CONF.get("ptt.key", "f9")), "dump_key": str(CONF.get("ptt.dump_key", "f2")),
           "transcribing": None, "last_error": None, "device": None, "model": None, "pid": None}
    st = read_shared_json(CONF.ptt_status)
    if not isinstance(st, dict):
        return out
    now = time.time()
    ts = _epoch(st.get("ts"))
    if ts is None:
        try:
            ts = CONF.ptt_status.stat().st_mtime
        except OSError:
            return out
    out.update({k: st[k] for k in out if k in st and k not in ("alive", "seconds")})
    out["alive"] = -30 < now - ts < 5                 # a stamp far in the future (the clock was set back) is not "alive" either
    started = _epoch(st.get("started")) if st.get("recording") else None
    out["seconds"] = int(max(0, now - started)) if started else 0
    if not out["alive"]:
        out.update(ready=False, recording=False, mode=None, seconds=0)
    return out


# ---------- dumps ----------
def _dump_call(fn, *a):
    """A dump-store call, retried briefly while Windows reports a file busy (another process is replacing it)."""
    for i in range(4):
        try:
            return fn(*a)
        except PermissionError as e:
            if i == 3:
                raise Busy("a dump file is being written; try again") from e
            time.sleep(0.03)
    return None


def public_row(r):
    """A listing row through the private filter (summary line, project, import label)."""
    return dict(r, **{k: mask(r[k]) for k in ("summaryLine", "project", "label") if r.get(k)})


def dump_rows(fresh=False):
    """The dump listing (newest first), read at most every 2 s; summary lines and project labels pass the private filter. A
    failed read keeps the previous listing (no change this poll)."""
    ds = need(dumps, "dumps")
    now, where = time.time(), str(CONF.dumps_dir)
    if _DUMPS["dir"] != where:
        _DUMPS.update(t=0.0, dir=where, rows=[])
    if fresh or now - _DUMPS["t"] > 2:
        try:
            rows = _dump_call(ds.listing) or []
            _DUMPS["rows"] = [public_row(r) for r in rows if isinstance(r, dict)]
        except Exception as e:  # noqa: BLE001 - locked or half-written: keep the last listing
            log_once("dumps.listing", f"dumps.listing: {type(e).__name__}: {e}")
        _DUMPS["t"] = now
    return _DUMPS["rows"]


def dump_counts():
    """/api/state.dumps: what the Dump strip and the tab badge need."""
    if dumps is None:
        return {"processing": 0, "transcribing": 0, "queued": 0, "thinking": 0, "ready": 0, "errors": 0, "newest": None,
                "unavailable": WARNINGS.get("dumps")}
    rows = dump_rows()
    st = [r.get("status") for r in rows]
    # the work in progress counts this PC's dumps only, as the worker does: another PC's (a synced folder) are that PC's job
    busy = [r.get("status") for r in rows if r.get("host") in (None, "", CONF.host)]
    newest = rows[0] if rows else None
    return {"processing": sum(s in PROCESSING for s in busy), "transcribing": busy.count("recorded") + busy.count("transcribing"),
            "queued": busy.count("transcribed"), "thinking": busy.count("thinking"), "ready": st.count("ready"),
            "errors": st.count("error"),
            "newest": {k: newest.get(k) for k in ("id", "status", "createdAt", "summaryLine")} if newest else None}


def dump_raw(did):
    """dumps.detail(id) as the module returns it, or None for a dump that is not there."""
    ds = need(dumps, "dumps")
    try:
        d = _dump_call(ds.detail, did)
    except (ValueError, Busy) as e:                     # a half-written or locked meta.json: the last good copy
        if did in _DETAILS:
            return _DETAILS[did]
        raise Busy("this dump is being written; try again") from e
    except (FileNotFoundError, NotADirectoryError, LookupError):
        d = None
    if not isinstance(d, dict):
        _DETAILS.pop(did, None)
        return None
    _DETAILS[did] = d
    while len(_DETAILS) > 50:
        _DETAILS.pop(next(iter(_DETAILS)))
    return d


def dump_detail(did):
    """meta + transcript + cards for the page: a copy with the private filter applied, `questions` = the next prompts as
    answer cards (ids registered for /api/answer), `todo_added` = which to-dos are already on the agenda."""
    raw = dump_raw(did)
    if raw is None:
        return None
    d = copy.deepcopy(raw)
    cards = d.get("cards") if isinstance(d.get("cards"), dict) else None
    qs = []
    if cards:
        prompts, known = [], {}
        for p in cards.get("nextPrompts") or []:
            q = str((p.get("question") if isinstance(p, dict) else p) or "").strip()
            if not q:
                continue
            qid = "qmd-" + hid(q)
            known[qid] = {"text": q}
            topic = str((p.get("topic") if isinstance(p, dict) else "") or "")
            qs.append({"id": qid, "text": mask(q), "topic": mask(topic)})
            prompts.append({**(p if isinstance(p, dict) else {}), "question": mask(q), "topic": mask(topic), "qid": qid})
        remember_qs(known)
        cards["nextPrompts"] = prompts
        cards["executiveSummary"] = mask_prose(cards.get("executiveSummary"))
        cards["talkingPoints"] = [dict(t, theme=mask(t.get("theme")),
                                       bullets=[mask(b) for b in (t["bullets"] if isinstance(t.get("bullets"), list) else [])])
                                  for t in cards.get("talkingPoints") or [] if isinstance(t, dict)]
        cards["actions"] = [dict(a, **{k: mask(a.get(k)) for k in ("title", "deliverable", "why") if a.get(k) is not None})
                            for a in cards.get("actions") or [] if isinstance(a, dict)]
        todos = [str(t) for t in cards.get("todos") or [] if t]
        added = {norm(t) for t in d.get("agenda_added") or [] if isinstance(t, str)}
        cards["todos"] = [mask(t) for t in todos]
        d["todo_added"] = [norm(t) in added for t in todos]
        if cards.get("project"):
            cards["project"] = mask(cards["project"])
        d["cards"] = cards
    d["questions"] = qs
    d["transcript"] = mask_prose(d.get("transcript"))
    for k in ("agenda_added", "questions_added"):
        if isinstance(d.get(k), list):
            d[k] = [mask(x) for x in d[k]]
    for k in ("label", "summaryLine", "project"):
        if d.get(k):
            d[k] = mask(d[k])
    return d


# ---------- voice ----------
def configured_mode():
    """'panel' (the router here owns the inbox) or 'monitor' (each session runs watch_inbox.py); anything else means panel."""
    return "monitor" if str(CONF.get("voice.mode", "panel") or "panel").lower() == "monitor" else "panel"


def queued_inbox(feed, max_age=1800):
    """This machine's inbox lines not yet in the transcript (a session picks them up only between steps, so they can be
    invisible for minutes). Shown as 'queued' until the real one lands; used for the Panel tab's one-liner in monitor mode."""
    norm_ = lambda t: " ".join(t.split())[:60]  # noqa: E731
    seen = {norm_(m["text"]) for m in feed if m.get("who") != "claude"}
    out, now = [], time.time()
    try:
        files = sorted(CONF.inbox.glob(f"*-{CONF.host}.txt"))
    except OSError:
        files = []
    for p in files:
        try:
            mt = p.stat().st_mtime
            if now - mt > max_age:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            text = (voice.split_meta(text)[1] if voice is not None else text).strip()      # the words, not the routing line
        except OSError:
            continue
        if text and norm_(text) not in seen:
            out.append({"who": "panel" if "-panel-" in p.name else "voice", "text": text, "queued": True,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mt))})
    return out


def session_rows():
    if sessions is None:
        return []
    try:
        return list(sessions.rows() or [])
    except Exception as e:  # noqa: BLE001
        log_once("sessions.rows", f"sessions.rows: {type(e).__name__}: {e}")
        return []


def voice_compact(feed):
    """/api/state.voice: the router's one-line summary, or (monitor mode, or no router) what sits in the inbox unconsumed."""
    if VOICE:
        try:
            return VOICE.compact(private=CONF.private_re)
        except Exception as e:  # noqa: BLE001 - the router's trouble never takes the status strip down
            log_once("voice.compact", f"voice.compact: {type(e).__name__}: {e}")
    q = queued_inbox(feed)
    last = q[-1] if q else None
    out = {"mode": configured_mode(), "target": None, "main": False, "queued": len(q),
           "last": {"status": "queued", "text": mask(last["text"]), "queued_at": last["ts"]} if last else None}
    if out["mode"] == "panel":
        out["error"] = router_missing()
    return out


def router_missing():
    return "the voice router is not running" + (f" (briandump.voice: {WARNINGS['voice']})" if "voice" in WARNINGS else "")


def armed_state(now=None):
    """The 🎤 Answer arm as the page shows it: the question (through the private filter), the seconds left to start
    answering, and `listening` while a talk clip that started in time is still being recorded. None when nothing is armed."""
    if answers is None:
        return None
    now = time.time() if now is None else now
    a = answers.read(now)
    if a is None:
        return None
    p = ptt_state()
    started = _epoch(p.get("started")) if p.get("alive") and p.get("recording") and p.get("mode") == "talk" else None
    listening = started is not None and a["armed_at"] - answers.SLACK_S <= started <= a["expires_at"]
    if not a["open"] and not listening:
        return None
    to = mask(session_names().get(a["session"]) or "") if a.get("session") else ""
    return {"id": a["id"], "text": mask(a["question"]), "left": max(0, int(a["expires_at"] - now + 0.999)),
            "listening": listening, "nonce": a["nonce"], **({"dump": a["dump"]} if a.get("dump") else {}),
            **({"to": to} if to else {})}


def full_state():
    st = build_state()
    feed = st.pop("feed", [])
    qs = st.pop("auto_questions", []) + open_manual_questions() + md_questions()
    remember_qs({q["id"]: q for q in qs})
    names = session_names() if any(q.get("session") for q in qs) else {}
    st["questions"] = [public_question(q, names) for q in qs]
    st.update(version=__version__, voice=voice_compact(feed), ptt=ptt_state(), dumps=dump_counts(), reload=RELOAD[0],
              armed=armed_state())
    return st


def machine():
    """Per-machine page settings; `sandbox` makes the page show its badge; `warnings` = sibling modules that did not load."""
    return {"host": CONF.host, "scale": CONF.scale, "window": CONF.window, "sandbox": CONF.sandbox, "version": __version__,
            "data_dir": str(CONF.data_dir), "state_dir": str(CONF.state_dir), "config_file": str(CONF.config_file),
            "voice_mode": configured_mode(), "voice_router": bool(VOICE), "main_session": str(CONF.get("voice.main_session") or "Main"),
            "ptt_key": str(CONF.get("ptt.key", "f9")), "dump_key": str(CONF.get("ptt.dump_key", "f2")),
            "processor": str(CONF.get("dump.processor", "claude")), "chime": bool(CONF.get("needs_you_chime", True)),
            "title_bar": CONF.get("title_bar"), "missing": sorted(WARNINGS),
            "warnings": [f"briandump.{k}: {v}" for k, v in sorted(WARNINGS.items())]}


def newest_cwd():
    """The folder of the most recent session, if it still exists (where Open Main starts the new session)."""
    path = find_transcript()
    if not path:
        return None
    tr = tracker(path)
    tr.update()
    return tr.cwd if tr.cwd and Path(tr.cwd).is_dir() else None


def open_main():
    """Open the main session (`claude --name <main_session>`) in a new Windows Terminal tab, or a new console without wt.
    Sandbox: recorded, nothing launched."""
    name = str(CONF.get("voice.main_session") or "Main").strip()
    if not NAME_RE.match(name):
        return False, "voice.main_session must be a short plain name (letters, digits, spaces, . _ -)"
    rows = session_rows()
    live = None
    if voice is not None:
        try:
            live = voice.find_main(rows, CONF.host, name)
        except Exception:  # noqa: BLE001
            live = None
    live = live or next((r for r in rows if str(r.get("name") or "").strip().lower() == name.lower()), None)
    if live:
        return True, f"{name} is already open ({live.get('status') or 'live'})"
    folder = newest_cwd() or str(Path.home())
    if ";" in folder:                                  # Windows Terminal reads ';' as its own command separator
        folder = str(Path.home())
    if CONF.record("launch_main", name=name, folder=folder):
        return True, f"sandbox: would open {name} in {folder}"
    if os.name != "nt":
        return False, "Open Main needs Windows"
    claude = str(CONF.get("dump.claude_cmd") or "claude")
    exe = shutil.which(claude) or (sessions.claude_exe() if sessions is not None and claude == "claude" else None)
    if not exe or not Path(exe).exists():
        return False, f"{claude} was not found on PATH"
    run = [exe, "--name", name] if exe.lower().endswith(".exe") else ["cmd", "/c", exe, "--name", name]
    wt = shutil.which("wt")
    cmd = [wt, "-w", "0", "new-tab", "--title", name, "-d", folder, *run] if wt else ["cmd", "/c", "start", "", *run]
    subprocess.Popen(cmd, cwd=folder, creationflags=CREATE_NO_WINDOW)   # no window for the launcher; wt / start open their own
    return True, f"opened {name} in {folder}" + ("" if wt else " (new console: Windows Terminal not found)")


def logon_id():
    """This sign-in's logon session: the AuthenticationId LUID of the process token (Windows), so the auto-open flag is good
    for one logon, however the server was started. Elsewhere, or if the token cannot be read: "unknown"."""
    if os.name != "nt":
        return "unknown"
    import ctypes
    from ctypes import wintypes as w
    adv, k32 = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = w.HANDLE
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    k32.CloseHandle.argtypes = [w.HANDLE]
    tok = w.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):          # TOKEN_QUERY
        return "unknown"
    try:
        buf, n = (ctypes.c_ubyte * 64)(), w.DWORD()
        if not adv.GetTokenInformation(tok, 10, buf, ctypes.sizeof(buf), ctypes.byref(n)):   # TokenStatistics
            return "unknown"
        low, high = int.from_bytes(bytes(buf[8:12]), "little"), int.from_bytes(bytes(buf[12:16]), "little")
        return f"{high:x}-{low:08x}"                   # TOKEN_STATISTICS: TokenId (8 bytes), then AuthenticationId
    finally:
        k32.CloseHandle(tok)


def auto_open_main(logon=None):
    """At server start (voice.auto_open_main, on by default): open Main once per logon if it is not running. The logon is
    marked in state/auto_main.json BEFORE the open, so a server restart within the same logon (the watchdog, a crash, a
    second launch) never reopens a Main you closed on purpose. Sandbox: open_main records launch_main instead."""
    if CONF.get("voice.auto_open_main", True) is False:
        return False, "voice.auto_open_main is off"
    logon = logon or logon_id()
    path = CONF.state / "auto_main.json"
    flag = read_shared_json(path, {})
    if isinstance(flag, dict) and flag.get("logon") == logon:
        return False, "already checked in this logon"
    try:
        atomic_write(path, json.dumps({"logon": logon, "ts": iso_local()}))
    except (OSError, Busy) as e:
        return False, f"auto-open skipped: {CONF.state} is not writable ({e})"   # without the flag it could open twice
    try:
        ok, msg = open_main()
    except Exception as e:  # noqa: BLE001 - never take the server down for a convenience
        ok, msg = False, f"{type(e).__name__}: {e}"
    log(f"auto-open main session: {msg}")
    return ok, msg


def page_html():
    html = CONF.panel_html.read_bytes()
    if CONF.sandbox:                                   # the window title too, so launch.py never mistakes it for the real panel
        html = html.replace(b"<title>BrianDump</title>", "<title>BrianDump · SANDBOX</title>".encode("utf-8"), 1)
    return html


# ---------- who may ask ----------
@functools.lru_cache(maxsize=1)
def os_names():
    """This PC's own names as the OS records them, read locally (no DNS): the host name and, on Windows, the full DNS name
    (the host name plus the primary DNS suffix, set only by a domain or by hand)."""
    names = {socket.gethostname()}
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32")
            size = ctypes.c_ulong(0)
            k32.GetComputerNameExW(3, None, ctypes.byref(size))           # 3 = ComputerNameDnsFullyQualified
            buf = ctypes.create_unicode_buffer(size.value + 1)
            if k32.GetComputerNameExW(3, buf, ctypes.byref(size)):
                names.add(buf.value)
        except Exception:  # noqa: BLE001 - no full name: the bare one still works
            pass
    return frozenset(n.lower().rstrip(".") for n in names if n)


def name_ok(name):
    """A host name this PC answers to by its own choice: an IP literal, localhost, or exactly one of its own names (CONF.host,
    or a name in os_names()). A DNS-rebinding page arrives under the attacker's own domain, which can well start with this
    PC's name (<pc-name>.attacker.example), so a name is never accepted for its prefix."""
    n = str(name or "").strip().strip("[]").rstrip(".").lower()
    if not n:
        return False
    if n == "localhost" or n == CONF.host or n in os_names():
        return True
    try:
        ipaddress.ip_address(n)
        return True
    except ValueError:
        return False


def host_ok(host):
    """The Host header: absent (not a browser) or naming this PC."""
    if not host:
        return True
    try:
        return name_ok(urlsplit("//" + host.strip()).hostname)
    except ValueError:
        return False


def origin_ok(origin, host):
    """Browsers send Origin with every cross-origin POST: if there is one it must be this page's own origin. Scripts, curl
    and old same-origin requests send none."""
    if origin is None:
        return True
    try:
        o = urlsplit(origin.strip())
        return o.scheme == "http" and bool(host) and o.netloc.lower() == host.strip().lower() and name_ok(o.hostname)
    except ValueError:
        return False


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "briandump"
    timeout = 30                                     # a client that stops sending mid-body frees its thread

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json")) else ""))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if ctype == "text/html":
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def js(self, code, obj):
        return self.send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def client_ip(self):
        """The requester as the policy sees it (tests monkeypatch this). IPv4-mapped IPv6 counts as the IPv4 address."""
        ip = str(self.client_address[0])
        return ip[7:] if ip.startswith("::ffff:") else ip

    def deny(self, why):
        self.js(403, {"error": why})
        return False

    def guard(self, post=False):
        """403 unless this client may use the API at all (see the module docstring)."""
        if self.client_ip() not in CONF.allowed:
            return self.deny("forbidden")
        if not host_ok(self.headers.get("Host")):
            return self.deny("this server answers to its own address only (Host header)")
        if post and not origin_ok(self.headers.get("Origin"), self.headers.get("Host")):
            return self.deny("cross-site request refused")
        return True

    def local_only(self):
        """Actions on THIS desktop (its window, folders, browser, terminal) are for the local page only: another allowed
        machine's page hides those controls, and its request is refused rather than moving a window it cannot see."""
        if self.client_ip() in LOOPBACK:
            return True
        return self.deny("local page only")

    def private_ok(self):
        """?private=1 shows private voice lines in full: only for the local page."""
        return "private=1" in self.path.split("?", 1)[-1] and self.client_ip() in LOOPBACK

    def run(self, fn, *a):
        """Every request ends in a reply: 503 for a missing module or a busy file, 500 for anything else. A client that went
        away gets nothing."""
        try:
            fn(*a)
        except (Unavailable, Busy) as e:
            self.js(503, {"error": str(e)})
        except ConnectionError:
            pass
        except Exception as e:  # noqa: BLE001 - never drop the connection: the page shows the error
            log(f"{self.command} {self.path}: {type(e).__name__}: {e}")
            try:
                self.js(500, {"error": f"{type(e).__name__}: {e}"[:300]})
            except OSError:
                pass

    def do_GET(self):
        if self.guard():
            self.run(self.get, self.path.split("?", 1)[0])

    def get(self, path):
        if path in ("/", "/index.html"):
            return self.send(200, page_html(), "text/html")
        if path == "/favicon.ico":
            return self.send(200, CONF.panel_ico.read_bytes(), "image/x-icon")
        if path == "/api/state":
            return self.js(200, full_state())
        if path == "/api/machine":
            return self.js(200, machine())
        if path == "/api/agenda":
            return self.js(200, {"items": agenda_items()})
        if path == "/api/voice":
            if VOICE:
                return self.js(200, VOICE.snapshot(show_private=self.private_ok(), private=CONF.private_re))
            if configured_mode() == "panel":
                raise Unavailable(router_missing())
            return self.js(200, {"mode": "monitor", "host": CONF.host, "main": None, "queued": 0, "items": []})
        if path == "/api/ptt":
            return self.js(200, ptt_state())
        if path == "/api/dumps":
            return self.js(200, dump_rows())
        m = DUMP_ROUTE_RE.match(path)
        if m:
            if not DUMP_ID_RE.match(m.group(1)):          # validated before anything touches the disk
                return self.js(400, {"error": "bad dump id"})
            if m.group(2):
                return self.js(404, {"error": "POST for dump actions"})
            d = dump_detail(m.group(1))
            return self.js(200, d) if d else self.js(404, {"error": "no such dump"})
        if path == "/api/reports":
            return self.js(200, need(reports, "reports").listing(refresh="refresh=1" in self.path.split("?", 1)[-1]))
        m = REPORT_ROUTE_RE.match(path)
        if m:
            rp = need(reports, "reports")
            if not REPORT_ID_RE.match(m.group(1)):         # an id from the listing, never a path
                return self.js(400, {"error": "bad report id"})
            if m.group(2):
                return self.js(404, {"error": "POST for report actions"})
            d = rp.view(m.group(1))
            return self.js(200, d) if d else self.js(404, {"error": "no such report"})
        self.js(404, {"error": "not found"})

    def do_POST(self):
        if not self.guard(post=True):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            return self.js(400, {"error": "bad Content-Length"})
        if n > MAX_BODY:
            return self.js(413, {"error": "body too large"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if n and ctype != "application/json":            # a form or text/plain post is what a foreign page can send unasked
            return self.js(415, {"error": "send JSON (Content-Type: application/json)"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):                    # a bad body must get a reply, not a dropped connection
            return self.js(400, {"error": "body is not JSON"})
        self.run(self.post, self.path.split("?", 1)[0], body if isinstance(body, dict) else {})

    def post(self, path, body):
        routes = {"/api/answer": self.answer, "/api/answer/arm": self.answer_arm, "/api/answer/disarm": self.answer_disarm,
                  "/api/agenda": self.agenda, "/api/voice": self.voice, "/api/ptt": self.ptt,
                  "/api/open": self.open_url, "/api/open-folder": self.open_folder, "/api/window": self.window,
                  "/api/reload": self.reload}
        if path in routes:
            return routes[path](body)
        m = DUMP_ROUTE_RE.match(path)
        if m:
            return self.dump_action(m.group(1), m.group(2), body)
        m = REPORT_ROUTE_RE.match(path)
        if m:
            return self.report_action(m.group(1), m.group(2), body)
        self.js(404, {"error": "not found"})

    def reload(self, body):
        RELOAD[0] += 1
        self.js(200, {"ok": True, "reload": RELOAD[0]})

    # -- the Reports tab: read marks, and Open file (a listed file in its own app; this desktop only) --
    def report_action(self, rid, act, body):
        rp = need(reports, "reports")
        if act not in ("read", "open"):
            return self.js(404, {"error": "not found"})
        if not (REPORT_ID_RE.match(rid) or (rid == "all" and act == "read")):
            return self.js(400, {"error": "bad report id"})
        try:
            if rid == "all":
                return self.js(200, {"ok": True, "marked": rp.mark_all_read()})
            if act == "read":
                return self.js(200, {"ok": True}) if rp.mark_read(rid) else self.js(404, {"error": "no such report"})
            if not self.local_only():
                return None
            res = rp.open_file(rid)
        except rp.Busy as e:                              # the read-state file stayed locked: the page tries again
            raise Busy(str(e)) from e
        if res is None:
            return self.js(404, {"error": "no such report"})
        return self.js(200 if res[0] else 400, {"ok": res[0], "msg": res[1]})

    # -- the two inbox writers (panel typing only) --
    def typed(self, body):
        """The user's own text from the page, or None after replying 400."""
        text = str(body.get("text") or "").strip()
        if not text:
            self.js(400, {"error": "empty"})
            return None
        if len(text) > max_chars():
            self.js(400, {"error": "too long"})
            return None
        return text

    def answer(self, body):
        qid = str(body.get("id") or "")[:80]
        q = question(qid, body.get("dump"))
        opts = [str(o) for o in (q or {}).get("options") or []]
        opt = body.get("option")                           # an option button: its index, so a masked one maps back
        if isinstance(opt, int) and not isinstance(opt, bool) and 0 <= opt < len(opts) and mask(opts[opt]) == str(body.get("text")):
            body = dict(body, text=opts[opt])              # the option's own words (the page may only have '(private)')
        text = self.typed(body)
        if text is None:
            return
        if text == "(private)":                            # the private filter's placeholder is never typed as an answer
            return self.js(400, {"error": "that is the private filter's placeholder, not an answer"})
        qtext = q.get("text") if q else None
        if q and q.get("source") == "asked":               # AskUserQuestion: a dialog the router never types into, so a panel
            return self.js(409, {"ok": False, "error": ASKED_IN_TERMINAL})   # answer could only land after it closed: stale
        to, why = answer_route(q)
        if why:
            return self.js(409, {"ok": False, "error": why})
        if qid.startswith("qmd-") and qtext:
            line = f"[BrianDump question: {short(qtext, 300)}] {text}"
        elif qtext:
            line = f"[panel answer to: {short(qtext, 300)}] {text}"
        else:
            line = text
        size = len(" ".join(line.split()))                 # as typed: the router joins lines and cuts at max_chars
        if size > max_chars():
            return self.js(400, {"error": f"too long: with the question it quotes, the line is {size} characters, over {max_chars()}"})
        write_inbox("panel", CONF.host, line, {"session": to} if to else None)
        if qid.startswith("qmd-") and qtext:              # the answer is safe in the inbox: now tick the question
            if agenda is not None:
                try:
                    agenda.set_done(CONF.questions_file, qtext, True)
                except Exception as e:  # noqa: BLE001 - the answer is sent; a tick that fails shows the card again
                    log(f"questions.md tick: {type(e).__name__}: {e}")
        elif qtext:
            mark_manual_done(qid)
        self.js(200, {"ok": True})

    def answer_arm(self, body):
        """🎤 Answer: arm one open question for your next talk clip (answers.py). Nothing is typed here: later, your clip's
        own words are, with the question quoted in front. The same checks as a typed answer decide where it may go."""
        ans = need(answers, "answers")
        qid = str(body.get("id") or "")[:80]
        dump = str(body.get("dump") or "")
        q = question(qid, dump or None)
        if not q or not q.get("text"):
            return self.js(404, {"ok": False, "error": "that question is not open any more"})
        if q.get("source") == "asked":                     # a dialog in the terminal: the router never types into one
            return self.js(409, {"ok": False, "error": ASKED_IN_TERMINAL})
        to, why = answer_route(q)
        if why:
            return self.js(409, {"ok": False, "error": why})
        tick = "md" if qid.startswith("qmd-") else "manual" if q.get("source") == "manual" else None
        ans.arm(qid, q["text"], tick=tick, session=to, dump=dump if DUMP_ID_RE.match(dump) else None)
        self.js(200, {"ok": True, "armed": armed_state()})

    def answer_disarm(self, body):
        """Esc or Cancel on the panel. With `nonce`, only that arm (a newer one, armed from another window, stays)."""
        ans = need(answers, "answers")
        self.js(200, {"ok": True, "disarmed": ans.disarm(str(body.get("nonce") or "")[:64] or None)})

    def dump_action(self, did, act, body):
        if not DUMP_ID_RE.match(did):                     # validated before any path is built
            return self.js(400, {"error": "bad dump id"})
        if act not in ("retry", "send", "agenda"):
            return self.js(404, {"error": "not found"})
        aid = str(body.get("action_id") or "")
        if act == "send" and not ACTION_ID_RE.match(aid):
            return self.js(400, {"error": "bad action id"})
        ds = need(dumps, "dumps")
        raw = dump_raw(did)
        if raw is None:
            return self.js(404, {"error": "no such dump"})
        cards = raw.get("cards") if isinstance(raw.get("cards"), dict) else {}
        if act == "retry":
            try:
                ok = ds.retry(did) is not False
            except ValueError as e:                      # the store's own refusal (e.g. not a failed dump)
                return self.js(409, {"ok": False, "error": str(e)})
            _DUMPS["t"] = 0.0
            return self.js(200 if ok else 409, {"ok": ok})
        if act == "agenda":
            need(agenda, "agenda")
            todos = [str(t) for t in cards.get("todos") or [] if t]
            idx = body.get("index")
            if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(todos):
                text = todos[idx]                            # the file's own words (the page may only have '(private)')
            else:
                text = " ".join(str(body.get("text") or "").split())
            if not text or text == "(private)" or len(text) > AGENDA_MAX:
                return self.js(400, {"error": "empty or too long"})
            try:
                added = ds.add_to_agenda(did, text)
            except RuntimeError as e:                    # agenda.md kept changing under an editor or a sync client
                raise Busy(str(e)) from e
            _DETAILS.pop(did, None)
            _DUMPS["t"] = 0.0
            return self.js(200, {"ok": True, "added": added})
        if aid not in {str(a.get("id")) for a in cards.get("actions") or [] if isinstance(a, dict)}:
            return self.js(404, {"error": "no such action in this dump"})
        text = self.typed(body)
        if text is None:
            return
        write_inbox("panel", CONF.host, text)            # the text as the user edited it in the Send box
        try:
            ds.mark_sent(did, aid, text)
        except Exception as e:  # noqa: BLE001 - the words are on their way; the "sent" mark is cosmetic
            log(f"mark_sent {did}/{aid}: {type(e).__name__}: {e}")
        _DETAILS.pop(did, None)
        _DUMPS["t"] = 0.0
        self.js(200, {"ok": True, "sent": iso_local()})

    # -- files the user owns --
    def agenda(self, body):
        ag = need(agenda, "agenda")
        act = str(body.get("action") or "")
        if act not in ("add", "done", "undone"):
            return self.js(400, {"error": "action must be add, done or undone"})
        text = " ".join(str(body.get("text") or "").split())
        iid = str(body.get("id") or "")
        if iid and act != "add":                          # an existing line: the file's own words win over the page's copy
            text = agenda_text_for(iid) or text
        if not text or text == "(private)":
            return self.js(400, {"error": "empty"})
        if len(text) > AGENDA_MAX:
            return self.js(400, {"error": "too long"})
        heading = CONF.get("agenda.heading")
        try:
            if act == "add":
                return self.js(200, {"ok": True, "added": ag.add(CONF.agenda_file, [text], heading=heading)})
            ok = bool(ag.set_done(CONF.agenda_file, text, act == "done", heading=heading))
        except RuntimeError as e:                        # the file kept changing under an editor or a sync client
            raise Busy(str(e)) from e
        self.js(200 if ok else 404, {"ok": ok})

    def ptt(self, body):
        act = str(body.get("action") or "")
        if act not in PTT_COMMANDS:
            return self.js(400, {"error": "action must be " + ", ".join(PTT_COMMANDS)})
        if not ptt_state()["alive"]:
            try:                                          # nobody listens: a command left behind would start a recording
                CONF.ptt_command.unlink()                 # whenever the service next starts
            except OSError:
                pass
            return self.js(200, {"ok": False, "msg": "the hold-to-talk service is not running: start the BrianDump PTT task"})
        cmd = {"cmd": act, "t": time.time(), "nonce": uuid.uuid4().hex[:12]}
        atomic_write(CONF.ptt_command, json.dumps(cmd))
        self.js(200, {"ok": True, "msg": "", "nonce": cmd["nonce"]})

    # -- the voice router --
    def voice(self, body):
        act = str(body.get("action") or "")
        if act == "open_main":
            if not self.local_only():
                return
            try:
                ok, msg = open_main()
            except Exception as e:  # noqa: BLE001 - never a 500 with a half-opened window
                ok, msg = False, f"{type(e).__name__}: {e}"[:200]
            return self.js(200 if ok else 400, {"ok": ok, "msg": msg})
        if act not in ("drop", "retry"):                  # deliberately no action that types text: the inbox is the only path
            return self.js(400, {"ok": False, "msg": "action must be drop, retry or open_main"})   # Send anyway = retry
        if not VOICE:
            if configured_mode() == "panel":
                raise Unavailable(router_missing())
            return self.js(400, {"ok": False, "mode": "monitor", "msg": "this machine is in monitor mode"})
        ok, msg = VOICE.act(act, str(body.get("id") or ""))
        self.js(200 if ok else 400, {"ok": ok, "msg": msg})

    # -- this desktop (local page only; sandbox: recorded, nothing else) --
    def open_url(self, body):
        if not self.local_only():
            return
        url = str(body.get("url") or "")
        if not URL_RE.match(url):
            return self.js(400, {"error": "http(s) only"})
        if CONF.record("open_url", url=url[:500]):
            return self.js(200, {"ok": True, "sandbox": True})
        webbrowser.open(url)
        self.js(200, {"ok": True})

    def open_folder(self, body):
        if not self.local_only():
            return
        what = str(body.get("what") or "")
        targets = {"data": CONF.data_dir, "reports": CONF.reports_dir, "dumps": CONF.dumps_dir, "state": CONF.state_dir,
                   "config": CONF.config_file}
        if what not in targets:
            return self.js(400, {"error": "what must be " + ", ".join(targets)})
        p = targets[what]
        if CONF.record("open_folder", what=what, path=str(p)):
            return self.js(200, {"ok": True, "sandbox": True, "path": str(p)})
        if os.name != "nt":
            return self.js(400, {"error": "opening folders needs Windows", "path": str(p)})
        if what == "config":
            CONF.write_default_config()
        else:
            p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))                             # one of the fixed paths above, never a client path
        self.js(200, {"ok": True, "path": str(p)})

    def window(self, body):
        if not self.local_only():
            return
        act = str(body.get("action") or "")
        if act not in ("min", "max", "close", "drag"):
            return self.js(400, {"error": "action must be min, max, close or drag"})
        if CONF.record("window", action=act):            # sandbox: FindWindow would hit the real panel's window
            return self.js(200, {"ok": True, "sandbox": True})
        if os.name != "nt":
            return self.js(400, {"error": "window actions need Windows"})
        import ctypes
        user32 = ctypes.WinDLL("user32")
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        hwnd = user32.FindWindowW(None, "BrianDump")
        if act == "max":                                                   # right third <-> a smaller window
            subprocess.Popen([sys.executable, "-m", "briandump.launch", "--place", "--toggle"], cwd=str(REPO),
                         creationflags=CREATE_NO_WINDOW)
        elif act == "min" and hwnd:
            user32.ShowWindow(hwnd, 6)                                     # SW_MINIMIZE
        elif act == "close" and hwnd:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)                        # WM_CLOSE
        elif act == "drag" and hwnd:                                       # the strip is the title bar: Windows' own move loop
            user32.PostMessageW(hwnd, 0x0112, 0xF012, 0)                   # WM_SYSCOMMAND, SC_MOVE | HTCAPTION
        self.js(200, {"ok": bool(hwnd) or act == "max"})


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without Windows' port sharing. http.server sets allow_reuse_address, and on Windows SO_REUSEADDR
    lets a second process bind a port that is already listening: two panels then share it silently, each request landing on
    either one (measured). A plain bind refuses that and still rebinds at once after a restart. On POSIX SO_REUSEADDR only
    skips TIME_WAIT, so it stays on there."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)      # not HTTPServer's: its getfqdn() can stall on a slow resolver
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if not isinstance(err, ConnectionError):
            log(f"request from {client_address[0]}: {type(err).__name__}: {err}")


def port_answers(port, host="127.0.0.1", timeout=0.5):
    """Does anything accept connections on this port already?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_briandump(port, timeout=1.5):
    """Is the thing on this port a BrianDump panel? (its Server header; no proxy, even if one is configured)"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/machine", timeout=timeout) as r:
            return str(r.headers.get("Server") or "").startswith("briandump")
    except Exception:  # noqa: BLE001 - anything else on the port: not ours
        return False


def port_message(port):
    hint = f"pick another with `py briandump.py config --set port {port + 1}`"
    if is_briandump(port):
        return f"port {port} is in use by another BrianDump panel (http://127.0.0.1:{port}/); close it first, or {hint}"
    return f"port {port} is in use by another program; {hint}"


# ---------- main ----------
def main(argv=None):
    global SESSION, VOICE
    ap = argparse.ArgumentParser(prog="py -m briandump.server", description="the BrianDump panel server")
    ap.add_argument("--port", type=int, default=None, help=f"default {CONF.port} (config.json); 0 = any free port")
    ap.add_argument("--session", help="pin a session id (prefix) or a .jsonl path (default: the newest transcript)")
    args = ap.parse_args(argv)
    SESSION = args.session
    port = CONF.port if args.port is None else args.port
    CONF.ensure_dirs()
    # the port first: nothing (router, dump worker) may start in a process that is not going to serve
    if port and port_answers(port):
        msg = port_message(port)
        log(msg, echo=False)
        print(msg, file=sys.stderr, flush=True)
        return EXIT_PORT_IN_USE
    try:
        httpd = Server((CONF.bind, port), Handler)
    except OSError as e:
        msg = f"port {port} is in use by another program ({e.strerror or e}); pick another with " \
              f"`py briandump.py config --set port {port + 1}`"
        log(msg, echo=False)
        print(msg, file=sys.stderr, flush=True)
        return EXIT_PORT_IN_USE
    port = httpd.server_address[1]
    for name, why in sorted(WARNINGS.items()):
        log(f"warning: briandump.{name} did not import ({why}); the endpoints that need it answer 503")
    for why in ([CONF.load_error] if CONF.load_error else []) + CONF.problems:
        log(f"warning: config.json: {why} (py briandump.py doctor)")
    try:
        atomic_write(CONF.state / "server.json", json.dumps({"port": port, "pid": os.getpid(), "sandbox": CONF.sandbox,
                                                             "url": f"http://127.0.0.1:{port}/", "version": __version__}))
    except (OSError, Busy):
        pass
    if configured_mode() == "panel":
        if voice is None:
            log("voice.mode is panel but briandump.voice did not import: nothing types your F9 lines until it does")
        else:
            VOICE = voice.Router(session_rows, host=CONF.host)
            threading.Timer(1.0, VOICE.start).start()    # a beat for the first poll, so the router sees live sessions
    if CONF.get("voice.auto_open_main", True) is not False:
        opener = threading.Timer(AUTO_MAIN_S, auto_open_main)   # after the first session scan; it re-scans before opening
        opener.daemon = True
        opener.start()
    if dumps is not None:
        try:
            dumps.Worker(interval=2.0).start()
        except Exception as e:  # noqa: BLE001
            log(f"dump worker did not start: {type(e).__name__}: {e}")
    where = f"http://127.0.0.1:{port}/" + (f" and http://{CONF.host}:{port}/" if CONF.bind not in LOOPBACK else "")
    log(f"BrianDump panel on {where}" + (f" (SANDBOX: nothing outward, everything under {CONF.state_dir})" if CONF.sandbox else "")
        + f" - voice: {configured_mode()} mode")
    if CONF.bind not in LOOPBACK:
        log(f"warning: bound to {CONF.bind}; only the IPs in config `allow` are answered, but keep this off public networks")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
