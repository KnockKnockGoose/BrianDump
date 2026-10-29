"""The voice router: ONE consumer per PC for the inbox, delivering your words as typed input into the console of the Claude
Code session you are talking to (panel mode; monitor mode uses watch_inbox.py instead).

  files     <state_dir>/inbox/<stamp>-<host>.txt (a hold-to-talk clip) and <stamp>-panel-<host>.txt (typed on the panel).
            Only THIS host's files, exactly once: the file name goes into voice_handled.txt (the marker watch_inbox.py
            shares) the moment it is claimed, and every item lives on in voice_router.json with its status. A name is split
            at the stamp, not by a character class: Windows host names often contain hyphens (DESKTOP-1A2B3C4).
  routing   "Docs session, fix the heading" -> the live session whose name matches (case-insensitive, exact or a unique prefix
            of 3+ characters). Ambiguous or unknown -> the whole line, unchanged, goes to the main session (`voice.main_session`,
            the registry name set by `claude --name`). No main session -> the most recently active interactive one, with a note.
            None -> queued and shown on the panel. A panel answer to a question a session asked carries that session's id on
            a first META line: it goes to that session and nowhere else, and fails (never moves on to Main) when it is gone.
            A clip that answers the question armed with the panel's 🎤 Answer (answers.py) is typed as `[answer to: "<the
            question>"] <your words>`, into the session that asked it (else Main, never by a spoken name), and the question is
            ticked once the line is delivered.
  gate      typed when you have not touched the keyboard or mouse for `voice.input_idle_s` seconds (GetLastInputInfo), the
            target's screen is readable and shows its input line, and the session is not waiting for YOU: never at a permission
            prompt, an AskUserQuestion or the trust question (transcript status needs_you, the registry's "waiting", or the
            dialog on its screen), where keys could answer it. A held line goes in the moment that clears. A busy session is
            fine (`voice.busy_typing`, on by default): Claude Code queues a line typed mid-turn and hands it to the model at the
            next tool boundary; its input line must be empty, so a draft of yours is never extended and sent. Otherwise the
            line waits, visibly, and the panel says why.
  stale     a line still waiting `voice.stale_s` seconds (default 120) after it was spoken (the file's time) is not typed on
            its own: late, it lands out of context. It parks as "stale" until you pick Send anyway or Drop on the panel.
  signals   every line ends in one short sound and a panel toast: delivered, queued with the reason (when it is not in within
            HOLD_SIGNAL_S), or not sent (stale / failed). The router numbers these events; /api/state carries the last few.
  delivery  AttachConsole(<claude.exe pid>) + WriteConsoleInputW key events; works under Windows Terminal's ConPTY because the
            pseudoconsole owns the input buffer. Still attached, the screen is read once more right before Enter, and Enter goes
            only if the end of the typed text is visible in the input line and no dialog is up: a permission dialog that pops up
            while keys are in flight swallows the text, and the Enter would then APPROVE its highlighted option. In that case the
            line is marked failed and nothing is approved. Delivery is confirmed by the session's transcript receiving the line
            (as a user message, or queued while a turn runs).
  one owner The router heartbeats voice_watcher.json. A second panel server on this PC (another port) finds a live router
            holding it and stays passive, so no line is typed twice. A new router also waits HANDOFF_GRACE_S before it claims
            anything, so a monitor-mode watch_inbox.py that still runs sees the lock change and exits first.
  console   AttachConsole needs this process to have no console of its own, and a freed console is gone for good: print()
            then fails with "the handle is invalid", even after re-attaching (measured). The panel server normally runs
            hidden (pythonw, no console). Started in a terminal (`py briandump.py serve`) it keeps its console, and the router
            refuses to type with a note on the panel instead of breaking that terminal.

SAFETY RULE (the reason this module has the shape it has): keystrokes are only ever generated from your own inbox files:
hold-to-talk clips (cross-checked against the PTT log; an unlogged file is typed with a visible "not verified" prefix) and
panel text you typed. No HTTP endpoint and no agent can hand this module text to type: `act()` only drops or retries an
existing line (Send anyway on a stale line is a retry) and has no text parameter. A process that could type into a Claude
Code terminal could impersonate you and launder approvals through a second session (answer its permission prompts, tell it
to run things), so there is deliberately no such path.
"""
from __future__ import annotations

import calendar
import ctypes
import json
import os
import re
import threading
import time
from pathlib import Path

from briandump import answers, sessions
from briandump.config import CONF

OWNER = "panel-router"
CATCHUP_MIN = 15              # at start, files younger than this that nobody consumed yet are still delivered
STABLE_S = 0.4                # a file is read only once its mtime is this old (ptt.py writes the text, then the log line)
HANDOFF_GRACE_S = 6           # after taking the lock, give an older watch_inbox.py one heartbeat to see it and exit
SETTLE_S = 3.0                # after a delivery, wait before the next one (the session needs a tick to show as busy)
BEAT_S = 5                    # lock heartbeat
LOCK_STALE_S = 30             # a router heartbeat older than this belongs to a router that is gone
KEEP = 60
HANDLED_MAX = 4000            # voice_handled.txt is cut to its newest half past this many lines (only recent names matter)
NEEDLE = 16                   # trailing characters of the typed text (whitespace ignored) that must be visible before Enter
TARGET_GONE_S = 5             # an answer whose session is not running fails after this long (a registry rewrite can hide one)
META = "#BrianDump "            # first line of a panel inbox file that answers a session's question: {"session": <its id>}
ENTER_WITHHELD = "Enter withheld"
HOLD_SIGNAL_S = 3.0           # a line not typed this long after it was spoken gets its one "queued: <why>" signal (a line that
                              # only waited out the keyboard check or the settle window is not worth a sound)
EVENTS_KEEP = 20              # panel signals kept in memory; the page plays the ones it has not seen
STAMP_RE = re.compile(r"^(?P<stamp>\d{8}-\d{6})-(?P<rest>.+)\.txt$")
UNVERIFIED_PREFIX = "[inbox file without a PTT log entry, not verified as spoken] "
TRAIL = re.compile(r"\b(agent|session|tab|terminal|window)s?\b")
LEAD = re.compile(r"^\s*(?:(?:hey|hi|ok|okay|to|the|for)\s+)*(?P<name>[^,:\n]{1,48}?)\s*[,:]\s+(?P<rest>\S.*)$", re.S)
# What a Claude Code screen shows while it must NOT be typed into (the trust question, a permission box, a question, a working
# turn). "Do you want to" only as a dialog line ("Do you want to proceed?"), case-sensitive: a reply ending "What do you want
# to work on?" is not a question box.
BLOCK_RE = re.compile(r"esc to interrupt|Enter to confirm|Esc to cancel|trust this folder|(?-i:^[\s│]*Do you want to)|"
                      r"Yes, proceed|\(y/n\)|Enter to select|Press Enter|allow once|allow always|to interrupt", re.I | re.M)
BUSY_RE = re.compile(r"esc to interrupt", re.I)    # a running turn: the one blocking text busy typing and the pre-Enter check tolerate
PROMPT_RE = re.compile(r"^\s*[>❯›]\s?")      # the input line: "> " (or ❯ / › in other themes)
EMPTY_PROMPT_RE = re.compile(r"^\s*[>❯›]\s*$")  # an input line with nothing typed in it (no draft of yours)
BUSY_STATES = ("running", "thinking")
JOINERS = "‌‍"      # zero-width non-joiner and joiner: format characters, not control ones (emoji sequences, Persian)
# box-drawing characters (input box rules) and the joiners, which a console may store apart or drop: left out of screen checks
BOX = str.maketrans("", "", "".join(chr(c) for c in range(0x2500, 0x2580)) + JOINERS)


def _cfg():
    main = str(CONF.get("voice.main_session", "Main") or "Main")
    try:
        idle = float(CONF.get("voice.input_idle_s", 2.0))
    except (TypeError, ValueError):
        idle = 2.0
    try:
        chars = int(CONF.get("voice.max_chars", 4000))
    except (TypeError, ValueError):
        chars = 4000
    busy = CONF.get("voice.busy_typing", True) is not False      # false = the old rule: type only into an idle session
    try:
        stale = max(0.0, float(CONF.get("voice.stale_s", 120) or 0))   # 0 = never stale
    except (TypeError, ValueError):
        stale = 120.0
    return main, idle, chars, busy, stale


MAIN, INPUT_IDLE_S, MAX_CHARS, BUSY_TYPING, STALE_S = _cfg()
HOST = CONF.host


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def epoch(s):
    """iso() back to seconds; None when it is not one."""
    try:
        return float(calendar.timegm(time.strptime(str(s or ""), "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def waited(s):
    return f"{int(s)} s" if s < 90 else f"{round(s / 60)} min"


def write_atomic(path, text, tries=20, pause=0.025):
    """tmp + os.replace, retried: on Windows the replace is refused (access denied) while any other process has the target
    open, e.g. the panel reading it at that instant. -> True when written; never raises (the next write replaces it)."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
    except OSError:
        return False
    for i in range(max(1, tries)):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(pause * (1 + i % 2))
        except OSError:
            break
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def trim_handled(path, limit=HANDLED_MAX):
    """voice_handled.txt only needs names young enough to be caught up (CATCHUP_MIN): past `limit` lines keep the newest half."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if len(lines) <= limit:
        return False
    return write_atomic(path, "\n".join(lines[-(limit // 2):]) + "\n")


def voice_mode():
    """'panel' (this router owns the inbox) or 'monitor' (each session runs watch_inbox.py); anything else means panel."""
    return "monitor" if str(CONF.get("voice.mode", "panel") or "panel").lower() == "monitor" else "panel"


def proc_alive(pid):
    """The real process table (also in the sandbox: a lock holder is a real process, not a fixture session)."""
    try:
        return sessions.os_pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


# ---------- inbox files ----------
def classify(name, host=None):
    """'voice' | 'panel' for this host's inbox files, None for anything else (other hosts, logs, clips, unknown kinds)."""
    m = STAMP_RE.match(str(name))
    if not m:
        return None
    host, rest = host or HOST, m.group("rest")
    return "voice" if rest == host else "panel" if rest == f"panel-{host}" else None


def parse_name(name):
    """(stamp, kind, host) of any host's inbox file name, or None (watch_inbox.py --host all)."""
    m = STAMP_RE.match(str(name))
    if not m:
        return None
    rest = m.group("rest")
    if rest.startswith("panel-") and len(rest) > 6:
        return m.group("stamp"), "panel", rest[6:]
    return m.group("stamp"), "voice", rest


def split_meta(raw):
    """A panel inbox file -> (routing, text). The server writes a META line first when the line answers a question a session
    asked ({"session": <its id>}); any other file is all text."""
    s = str(raw or "")
    if s.startswith(META):
        head, _, body = s.partition("\n")
        try:
            meta = json.loads(head[len(META):])
        except ValueError:
            meta = None
        if isinstance(meta, dict):
            return meta, body
    return {}, s


def one_line(text, limit=None):
    """Verbatim, except: newlines become spaces (Enter would submit), control chars go, and the length is capped. The two
    joiners stay: they are part of emoji sequences and of words in scripts such as Persian."""
    s = "".join(ch if ch.isprintable() or ch == " " or ch in JOINERS else " "
                for ch in str(text or "").replace("\r", "\n").replace("\n", " "))
    s = " ".join(s.split())
    return s[:MAX_CHARS if limit is None else limit]


def ptt_entry(stamp, host=None, inbox=None, text=None, tail_lines=400):
    """ptt.py's log line for this clip, {"at": "<stamp>", "seconds", "transcribe_s", "text"} from inbox/log-<host>.jsonl, or
    None. With `text`, the logged words must also be the file's words."""
    try:
        lines = (Path(inbox or CONF.inbox) / f"log-{host or HOST}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines[-tail_lines:]):
        if f'"at": "{stamp}"' not in ln:
            continue
        try:
            entry = json.loads(ln)
        except ValueError:
            continue
        if isinstance(entry, dict) and (text is None or one_line(entry.get("text")) == one_line(text)):
            return entry
    return None


def ptt_logged(stamp, host=None, inbox=None, text=None, tail_lines=400):
    """Did ptt.py log this clip? Every hold-to-talk transcript gets a line in inbox/log-<host>.jsonl (ptt_entry)."""
    return ptt_entry(stamp, host, inbox, text, tail_lines) is not None


def compose(kind, raw, logged=True):
    """The line as it will be typed: verbatim, except that a voice file ptt.py did not log is marked as unverified (defence in
    depth: some other process wrote a bare voice file). Panel text is yours and is never flagged."""
    text = one_line(raw)
    if kind == "voice" and not logged and text:
        return UNVERIFIED_PREFIX + text
    return text


QUOTE_ROOM = len(answers.prefix("x" * answers.QUOTE_MAX))


def answer_line(stamp, raw, text, host=None, inbox=None):
    """A verified clip that answers the question armed with 🎤 Answer -> {"text": the line with the quoted question in front,
    "answer": what to tick once it is delivered, "to": the session that asked (when one did)}. {} for an ordinary clip.
    Only a clip whose PTT log line puts its key press inside the arm's window takes the arm, and only once: the press time
    decides, not when the router gets to the file (a clip started in time may still be in Whisper when the window closes)."""
    if not text or not answers.path().exists():        # the usual case: nothing armed, no log read
        return {}
    started = answers.clip_started(stamp, ptt_entry(stamp, host, inbox, text=raw))
    if len(text) + QUOTE_ROOM > MAX_CHARS:                # the arm stays for a shorter clip
        return {"note": "too long to carry the question's quote, so it went as a plain line"} if answers.covers(started) else {}
    got = answers.take(started)
    if not got:
        return {}
    out = {"text": answers.prefix(got["question"]) + text,
           "answer": {"id": got.get("id"), "tick": got.get("tick"), "question": got["question"], "words": text}}
    if got.get("session"):
        out["to"] = got["session"]
    return out


# ---------- routing ----------
def norm(s):
    s = re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower())
    return " ".join(TRAIL.sub(" ", s).split())


def match_name(spoken, names):
    """The one name the spoken words mean: exact (normalised: case, punctuation, a trailing 'agent'/'session'/'tab' word
    ignored) or a unique prefix of at least 3 characters. Two candidates or none -> None (never a guess)."""
    sp = norm(spoken)
    if not sp:
        return None
    table = {}
    for n in names:
        if n:
            table.setdefault(norm(n), n)
    if sp in table:
        return table[sp]
    if len(sp) < 3:
        return None
    hits = [v for k, v in table.items() if k.startswith(sp)]
    return hits[0] if len(hits) == 1 else None


def parse_prefix(text, names):
    """'Docs session, fix the heading' -> ('Docs', 'fix the heading'); no match -> (None, text unchanged)."""
    m = LEAD.match(text or "")
    if not m:
        return None, text
    target = match_name(m.group("name"), names)
    if not target:
        return None, text
    return target, m.group("rest").strip()


def is_interactive(r, host=None):
    return bool(r.get("kind") == "session" and r.get("machine") == (host or HOST) and r.get("pid") and r.get("name")
                and not str(r.get("note") or "").startswith("headless"))


def find_main(rows, host=None, name=None):
    return next((r for r in rows if is_interactive(r, host) and norm(r["name"]) == norm(name or MAIN)), None)


def find_session(rows, sid, host=None):
    """The live interactive session with this id on this PC, or None."""
    return next((r for r in rows if is_interactive(r, host) and r.get("id") == sid), None) if sid else None


def most_recent(rows, host=None):
    live = [r for r in rows if is_interactive(r, host)]
    return max(live, key=lambda r: (r.get("last_activity") or r.get("started") or "")) if live else None


def resolve(item, rows, card_names=(), host=None):
    """-> (session row or None, text to type, note). An answer to a session's question (item "to") goes to that session or
    nowhere. Any other line: names come from live sessions (plus optional extra names)."""
    host = host or HOST
    live = [r for r in rows if is_interactive(r, host)]
    text, note = item["text"], ""
    if item.get("to"):
        hit = find_session(live, item["to"], host)
        return (hit, text, f"to {hit['name']}, which asked") if hit else (None, text, "the session that asked is not running")
    names = [r["name"] for r in live] + [n for n in (card_names or ()) if n] + [MAIN]
    tname, rest = (None, text) if item.get("answer") else parse_prefix(text, names)   # an answer never follows a spoken name
    if tname:
        hit = next((r for r in live if norm(r["name"]) == norm(tname)), None)
        if hit:
            return hit, rest, f"to {hit['name']}"
        if norm(tname) != norm(MAIN):
            note = f"{tname} is not running"
    main = find_main(live, host)
    if main:
        return main, text, note
    fb = most_recent(live, host)
    if fb:
        return fb, text, (note + " · " if note else "") + f"{MAIN} not running → {fb['name']}"
    return None, text, (note + " · " if note else "") + "no interactive session on this machine"


# ---------- Windows console: look, then type ----------
class Win:
    """ctypes wrappers; every call swallows errors into (None | False, reason). Tests and the sandbox replace this object.
    A process is attached to one console at a time, so every call holds the lock for its attach ... detach span."""
    GENERIC_READ, GENERIC_WRITE, SHARE, OPEN_EXISTING, KEY_EVENT = 0x80000000, 0x40000000, 3, 3, 1

    def __init__(self):
        self.ok, self.attached, self.lock = os.name == "nt", False, threading.RLock()
        if not self.ok:
            return
        import ctypes.wintypes as w
        self.w = w
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)   # private instances: the argtypes set here stay here
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        class CSBI(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", w.WORD), ("srWindow", SMALL_RECT),
                        ("dwMaximumWindowSize", COORD)]

        class KEY(ctypes.Structure):
            _fields_ = [("bKeyDown", w.BOOL), ("wRepeatCount", w.WORD), ("wVirtualKeyCode", w.WORD), ("wVirtualScanCode", w.WORD),
                        ("UnicodeChar", w.WCHAR), ("dwControlKeyState", w.DWORD)]

        class EVENT(ctypes.Union):
            _fields_ = [("KeyEvent", KEY), ("pad", ctypes.c_byte * 16)]

        class INPUT_RECORD(ctypes.Structure):
            _fields_ = [("EventType", w.WORD), ("Event", EVENT)]

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

        self.COORD, self.CSBI, self.INPUT_RECORD, self.LASTINPUTINFO = COORD, CSBI, INPUT_RECORD, LASTINPUTINFO
        assert ctypes.sizeof(INPUT_RECORD) == 20
        k = self.k32
        k.AttachConsole.argtypes = [w.DWORD]
        k.CreateFileW.restype = w.HANDLE                     # without this the handle truncates to 32 bits
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]
        k.GetConsoleScreenBufferInfo.argtypes = [w.HANDLE, ctypes.POINTER(CSBI)]
        k.ReadConsoleOutputCharacterW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, COORD, ctypes.POINTER(w.DWORD)]
        k.WriteConsoleInputW.argtypes = [w.HANDLE, ctypes.POINTER(INPUT_RECORD), w.DWORD, ctypes.POINTER(w.DWORD)]
        k.GetConsoleProcessList.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD]
        k.GetConsoleProcessList.restype = w.DWORD
        k.GetTickCount.restype = w.DWORD
        self.u32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]

    def ready(self):
        """(ok, why not): can this process attach to another console at all?"""
        if not self.ok:
            return False, "not Windows"
        if not self.attached and self._own_console():
            return False, ("this panel server runs in a console window of its own (py briandump.py serve?) and typing would cost it "
                           "that console: start the panel with py briandump.py launch (it runs hidden)")
        return True, ""

    def _own_console(self):
        buf = (self.w.DWORD * 4)()
        try:
            return bool(self.k32.GetConsoleProcessList(buf, 4))
        except Exception:
            return True                                      # unknown: never risk the server's own console

    def input_idle_s(self):
        """Seconds since the last keyboard / mouse input in this desktop session; None when unknown."""
        if not self.ok:
            return None
        try:
            li = self.LASTINPUTINFO()
            li.cbSize = ctypes.sizeof(li)
            if not self.u32.GetLastInputInfo(ctypes.byref(li)):
                return None
            return max(0.0, ((self.k32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0)
        except Exception:
            return None

    def _attach(self, pid):
        ok, why = self.ready()
        if not ok:
            return why
        if not self.k32.AttachConsole(int(pid)):
            return f"AttachConsole(pid {pid}) failed: error {ctypes.get_last_error()}"
        self.attached = True
        return ""

    def _detach(self):
        if self.attached:
            self.k32.FreeConsole()
            self.attached = False

    def _open(self, name):
        # the inherited std handles are invalid after AttachConsole: always open the CON* device fresh
        h = self.k32.CreateFileW(name, self.GENERIC_READ | self.GENERIC_WRITE, self.SHARE, None, self.OPEN_EXISTING, 0, None)
        return None if h in (None, -1, 0xFFFFFFFFFFFFFFFF) else h

    def _rows_attached(self, rows=30):
        """The visible window of the console this process is attached to right now (no attach / detach), or None."""
        h = self._open("CONOUT$")
        if h is None:
            return None
        try:
            info = self.CSBI()
            if not self.k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
                return None
            width, top, bottom = info.dwSize.X, info.srWindow.Top, info.srWindow.Bottom
            out = []
            for y in range(top, bottom + 1):         # the whole window: a fresh session's content sits at the top of a tall
                buf = ctypes.create_unicode_buffer(width + 1)   # window with blank rows below, so a fixed bottom slice
                n = self.w.DWORD(0)                             # would miss the input line
                self.k32.ReadConsoleOutputCharacterW(h, buf, width, self.COORD(0, y), ctypes.byref(n))
                out.append(buf.value[:n.value].rstrip())
            while out and not out[-1]:
                out.pop()
            return out[-rows:]
        finally:
            self.k32.CloseHandle(h)

    def screen_rows(self, pid, rows=30):
        """The last `rows` non-blank lines of the console window the process is attached to (what the tab shows), or None."""
        if not self.ok:
            return None
        with self.lock:
            if self._attach(pid):
                return None
            try:
                return self._rows_attached(rows)
            except Exception:
                return None
            finally:
                self._detach()

    def _key(self, ch, down):
        r = self.INPUT_RECORD()
        r.EventType = self.KEY_EVENT
        k = r.Event.KeyEvent
        k.bKeyDown, k.wRepeatCount, k.dwControlKeyState = (1 if down else 0), 1, 0
        if ch == "\r":
            vk = 0x0D
        else:
            vk = self.u32.VkKeyScanW(ord(ch)) & 0xFF
            vk = 0 if vk == 0xFF else vk
        k.wVirtualKeyCode = vk
        k.wVirtualScanCode = self.u32.MapVirtualKeyW(vk, 0) if vk else 0
        k.UnicodeChar = ch
        return r

    def _write(self, h, recs):
        arr = (self.INPUT_RECORD * len(recs))(*recs)
        n = self.w.DWORD(0)
        return bool(self.k32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n))) and n.value == len(recs)

    def type_text(self, pid, text, enter=True, chunk=24, pause=0.015, before_enter=0.35, verify=None):
        """Key events for every UTF-16 unit of `text`, in small chunks (so the app sees typing, not a paste that could turn
        the Enter into a newline), then Enter on its own after a pause. -> (ok, reason).

        verify (default pre_enter_check) looks at the screen once more right before Enter, still attached, and withholds Enter
        unless the text sits in the input line and no dialog is up: Enter on a permission dialog that popped up while the
        keys were typed APPROVES its highlighted option, and the typed characters are swallowed by the dialog."""
        verify = pre_enter_check if verify is None else verify
        if not self.ok:
            return False, "not Windows"
        units = text.encode("utf-16-le", "surrogatepass")
        chars = [units[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(units), 2)]
        with self.lock:
            err = self._attach(pid)
            if err:
                return False, err
            try:
                h = self._open("CONIN$")
                if h is None:
                    return False, f"CONIN$ failed: error {ctypes.get_last_error()}"
                try:
                    for i in range(0, len(chars), chunk):
                        recs = []
                        for ch in chars[i:i + chunk]:
                            recs += [self._key(ch, True), self._key(ch, False)]
                        if not self._write(h, recs):
                            return False, f"WriteConsoleInputW failed: error {ctypes.get_last_error()}"
                        time.sleep(pause)
                    if enter:
                        time.sleep(before_enter)
                        if verify:
                            ok, why = verify(self._rows_attached(), text)
                            if not ok:
                                return False, f"{ENTER_WITHHELD}: {why}"
                        if not self._write(h, [self._key("\r", True), self._key("\r", False)]):
                            return False, f"Enter failed: error {ctypes.get_last_error()}"
                    return True, ""
                finally:
                    self.k32.CloseHandle(h)
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
            finally:
                self._detach()


def gate(row, win, input_idle_s=None, allow_busy=False):
    """May this line be typed into this session right now? -> (ok, why not). Never blind: an unreadable screen waits.

    Never while the session waits for you: status needs_you (a permission prompt or an AskUserQuestion), the registry's
    "waiting", or any dialog text on its screen, because typed keys could answer it. allow_busy (the router passes
    voice.busy_typing, on by default): a running turn is no reason to wait, since Claude Code queues a line typed mid-turn and
    hands it to the model at the next tool boundary; "esc to interrupt" is then the one blocking text ignored, and a busy
    session's input line must be empty (a half-typed draft of yours is never extended and sent)."""
    name, st = row.get("name"), row.get("status")
    reg = str(row.get("reg_status") or "idle")
    if st == "needs_you" or reg == "waiting":
        return False, f"{name} is waiting for you ({row.get('step') or 'a permission or a question'})"
    busy = allow_busy and st in BUSY_STATES
    if st != "idle" and not busy:
        return False, f"{name} is {str(st or 'unknown').replace('_', ' ')}"
    if reg not in ("idle", "") and not (allow_busy and reg == "busy"):
        return False, f"{name}: registry says {reg}"
    busy = busy or (allow_busy and reg == "busy")
    ready = getattr(win, "ready", None)
    if ready is not None:
        ok, why = ready()
        if not ok:
            return False, why
    age = win.input_idle_s()
    if age is not None and age < (INPUT_IDLE_S if input_idle_s is None else input_idle_s):
        return False, "you are at the keyboard"
    rows = win.screen_rows(row["pid"])
    if rows is None:
        return False, f"{name}: its screen cannot be read"
    tail = rows[-25:]
    screen = "\n".join(tail)
    m = BLOCK_RE.search(BUSY_RE.sub("", screen) if allow_busy else screen)
    if m:
        return False, f"{name} shows a prompt ({m.group(0).strip()})"
    prompts = [ln for ln in tail if PROMPT_RE.match(ln)]
    if not prompts:
        return False, f"{name}: no input line on screen"
    if busy and not EMPTY_PROMPT_RE.match(prompts[-1]):
        return False, f"{name} is busy and its input line is not empty"
    return True, ""


def _squash(s):
    return "".join(str(s or "").translate(BOX).split())


def pre_enter_check(rows, text):
    """Right before Enter (still attached): no dialog on the screen and the end of `text` visible in the input line, wrapping
    and whitespace ignored. -> (ok, why not). "esc to interrupt" is the one blocking text tolerated here: a turn that started
    on its own while the keys were typed only queues the line. An unreadable screen is a refusal: Enter is the keystroke that
    can approve something. Input-box lines that are the typed words themselves are left out of the dialog search, so saying
    "press enter to submit" does not hold your own line back; a dialog's question line sits above its menu and stays in."""
    if rows is None:
        return False, "the screen could not be read"
    tail = [str(ln) for ln in rows[-25:]]
    idx = [i for i, ln in enumerate(tail) if PROMPT_RE.match(ln)]
    if not idx:
        return False, "no input line on screen"
    start, flat = idx[-1], _squash(text)
    box = [_squash(PROMPT_RE.sub("", ln, count=1) if i == 0 else ln) for i, ln in enumerate(tail[start:])]
    masked = tail[:start] + ["" if b and b in flat else ln for b, ln in zip(box, tail[start:], strict=True)]
    m = BLOCK_RE.search(BUSY_RE.sub("", "\n".join(masked)))
    if m:
        return False, f"a dialog appeared while typing ({m.group(0).strip()})"
    needle = flat[-NEEDLE:]
    if needle and needle not in "".join(box):
        return False, "the typed words are not in the input line"
    return True, ""


def confirm(sid, text, tries=20, interval=0.4, offset=0, find=None, sleep=time.sleep):
    """True once the session's transcript (after byte `offset`) holds the line as a user message, or as a queue entry (a line
    typed while a turn runs is enqueued first). Polls `tries` times with `sleep(interval)` between (no wall-clock deadline, so
    a mocked clock cannot wedge it); ~8 s in production."""
    find = find or sessions.find_transcript
    # Claude Code writes raw UTF-8, so search for that form and the escaped one
    needles = {json.dumps(text[:80], ensure_ascii=a)[1:-1].encode("utf-8") for a in (False, True)}
    for i in range(max(1, tries)):
        p = find(sid)
        if p:
            try:
                with open(p, "rb") as f:
                    f.seek(offset)
                    data = f.read()
                if any(n in data for n in needles) and (b'"type":"user"' in data or b'"operation":"enqueue"' in data):
                    return True
            except OSError:
                pass
        if i < tries - 1:
            sleep(interval)
    return False


def transcript_size(sid, find=None):
    find = find or sessions.find_transcript
    try:
        p = find(sid)
        return p.stat().st_size if p else 0
    except OSError:
        return 0


class FakeWin:
    """Win without a console, used automatically when BRIANDUMP_SANDBOX=1. Fixture files in <state_dir> steer it:

      keyboard_busy    you are at the keyboard: the router waits
      dialog_up        the target shows a permission dialog: the gate refuses
      dialog_on_type   a dialog pops up WHILE the keys are typed: the pre-Enter check withholds Enter (nothing approved)

    A delivered line is appended to the target's transcript (pid -> registry -> transcript) as a user message plus a short
    stand-in reply and the end of the turn, so confirm() and the next idle check see it, and recorded in side_effects.jsonl."""

    RULE = "─" * 40
    HISTORY = ["❯ Read notes.md", "  Done.", ""]
    DIALOG = ["  ⎿  Waiting…", "", " Bash command", " mkdir sandbox_probe", " Do you want to proceed?", " ❯ 1. Yes", "   2. No", "",
              " Esc to cancel · Tab to amend"]

    def __init__(self, state_dir=None, sessions_dir=None, projects_dir=None):
        self.state_dir = Path(state_dir or CONF.state_dir)
        self.sessions_dir, self.projects_dir = Path(sessions_dir or CONF.sessions), Path(projects_dir or CONF.projects)
        self.typed = []                                        # (pid, text, entered)

    def _flag(self, name):
        return (self.state_dir / name).exists()

    def ready(self):
        return True, ""

    def input_idle_s(self):
        return 0.2 if self._flag("keyboard_busy") else 30.0

    def screen_rows(self, pid, rows=30):
        return list(self.DIALOG) if self._flag("dialog_up") else self.HISTORY + [self.RULE, "❯ ", self.RULE]

    def type_text(self, pid, text, enter=True, verify=None, **_):
        verify = pre_enter_check if verify is None else verify
        swallowed = self._flag("dialog_on_type") or self._flag("dialog_up")
        screen = list(self.DIALOG) if swallowed else self.HISTORY + [self.RULE, "❯ " + text, self.RULE]
        if enter and verify:
            ok, why = verify(screen, text)
            if not ok:
                self.typed.append((pid, text, False))
                CONF.record("enter_withheld", pid=pid, text=text[:200], why=why)
                return False, f"{ENTER_WITHHELD}: {why}"
        self.typed.append((pid, text, bool(enter)))
        if enter:
            self._deliver(pid, text)
        return True, ""

    def _deliver(self, pid, text):
        sid, path = None, None
        try:
            sid = json.loads((self.sessions_dir / f"{int(pid)}.json").read_text(encoding="utf-8")).get("sessionId")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if sid:
            path = sessions.find_transcript(sid, self.projects_dir)
        if path:
            t = time.time()

            def ts(d):
                return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t + d)) + ".000Z"

            usage = {"input_tokens": 900, "cache_read_input_tokens": 120_000, "cache_creation_input_tokens": 100, "output_tokens": 20}
            lines = [{"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts(0), "sandbox": "typed"},
                     {"type": "assistant", "timestamp": ts(0.5), "sandbox": "reply",
                      "message": {"role": "assistant", "model": "claude-sandbox", "usage": usage,
                                  "content": [{"type": "text", "text": "Noted (sandbox stand-in reply)."}]}},
                     {"type": "system", "subtype": "turn_duration", "durationMs": 500, "timestamp": ts(0.6),
                      "pendingBackgroundAgentCount": 0}]
            try:
                with open(path, "a", encoding="utf-8") as f:
                    for ln in lines:
                        f.write(json.dumps(ln, ensure_ascii=False, separators=(",", ":")) + "\n")   # compact, like Claude Code
            except OSError:
                pass
        CONF.record("type_text", pid=pid, session=sid, transcript=str(path) if path else None, text=text[:200])


# ---------- the router ----------
class Router:
    def __init__(self, sessions_fn, cards_fn=None, host=None, inbox=None, state_dir=None, win=None, clock=time.time,
                 sleep=time.sleep, find=None, allow_busy=None, stale_s=None):
        self.sessions_fn, self.cards_fn, self.host = sessions_fn, cards_fn or (lambda: []), host or HOST
        self.inbox, self.state_dir = Path(inbox or CONF.inbox), Path(state_dir or CONF.state)
        if win is None and CONF.sandbox:                       # never a real console in the sandbox
            win = FakeWin()
        self.win, self.clock, self.sleep, self.find = win or Win(), clock, sleep, find
        # voice.busy_typing and voice.stale_s, read once at start (None = from the config)
        self.allow_busy = BUSY_TYPING if allow_busy is None else bool(allow_busy)
        self.stale_s = STALE_S if stale_s is None else max(0.0, float(stale_s))
        self.events, self.seq, self.boot = [], 0, f"{os.getpid()}-{int(clock())}"   # panel signals, in memory only
        self.items, self.lock, self.started = [], threading.Lock(), False
        self.t0, self.last_beat, self.last_delivery, self.first_scan = None, None, 0.0, True
        self.passive, self.seen = "", set()
        self._load()

    # state
    @property
    def lock_file(self):
        return self.state_dir / CONF.voice_lock.name

    @property
    def handled_file(self):
        return self.state_dir / CONF.voice_handled.name

    @property
    def state_file(self):
        return self.state_dir / CONF.voice_router.name

    def _load(self):
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.items = [i for i in d.get("items") or [] if isinstance(i, dict) and i.get("id")]
        except (OSError, ValueError, AttributeError):
            self.items = []
        for i in self.items:                                   # a restart mid-typing: never retype on our own
            if i.get("status") == "typing":
                i.update(status="failed", note="interrupted by a panel restart while typing; use Retry if it did not arrive")
        self.seen = {i["id"] for i in self.items}

    def _save(self):
        self.items = self.items[-KEEP:]
        write_atomic(self.state_file, json.dumps({"items": self.items, "host": self.host, "ts": iso(self.clock())}, ensure_ascii=False))

    def handled(self):
        try:
            return set(self.handled_file.read_text(encoding="utf-8").split())
        except OSError:
            return set()

    def mark_handled(self, name):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.handled_file, "a", encoding="utf-8") as f:
            f.write(name + "\n")

    def trim_handled(self):
        trim_handled(self.handled_file)

    def read_lock(self):
        try:
            d = json.loads(self.lock_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_lock(self, now=None):
        write_atomic(self.lock_file, json.dumps({"session": OWNER, "pid": os.getpid(), "t": self.clock() if now is None else now}))

    def other_router(self, now):
        """The pid of another live router (a second panel server on this PC) whose heartbeat is fresh, else None."""
        lock = self.read_lock()
        try:
            pid, t = int(lock.get("pid") or 0), float(lock.get("t") or 0)
        except (TypeError, ValueError):
            return None
        if lock.get("session") != OWNER or pid in (0, os.getpid()) or now - t > LOCK_STALE_S:
            return None
        return pid if proc_alive(pid) else None

    # thread
    def start(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self.run, daemon=True, name="voice-router").start()

    def run(self):
        self.trim_handled()
        while True:
            try:
                self.tick()
            except Exception:
                pass
            self.sleep(0.5)

    def tick(self, now=None):
        now = now if now is not None else self.clock()
        if self.t0 is None:
            self.t0 = now
        if self.last_beat is None or now - self.last_beat >= BEAT_S:
            self.last_beat = now
            other, was = self.other_router(now), self.passive
            self.passive = f"another panel server (pid {other}) owns the voice inbox on this PC" if other else ""
            if not self.passive:
                if was:
                    self.t0 = now                              # taking over: a grace period again
                self.write_lock(now)                           # an older watch_inbox.py sees the owner change and exits
        if self.passive or now - self.t0 < HANDOFF_GRACE_S:
            return                                             # nothing is claimed while another consumer may still run
        self.scan(now)
        self.deliver(now)

    # inbox -> items
    def scan(self, now):
        done = self.handled() | self.seen
        cutoff = now - CATCHUP_MIN * 60 if self.first_scan else None
        try:
            files = sorted(self.inbox.glob(f"*-{self.host}.txt"))
        except OSError:
            files = []
        for p in files:
            kind = classify(p.name, self.host)
            if not kind or p.name in done:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if cutoff is not None and mt < cutoff:
                self.seen.add(p.name)                          # old and never consumed: skip, do not replay
                continue
            if now - mt < STABLE_S:
                continue                                       # still being written
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self.claim(p.name, kind, raw, now, born=min(mt, now))
        self.first_scan = False

    def claim(self, name, kind, raw, now, born=None):
        """born = when the line was written (the file's mtime): its age decides the queued signal and staleness."""
        stamp = STAMP_RE.match(name).group("stamp")
        meta, raw = split_meta(raw) if kind == "panel" else ({}, raw)
        logged = kind != "voice" or ptt_logged(stamp, self.host, self.inbox, text=raw)
        text = compose(kind, raw, logged)
        self.mark_handled(name)                                # durable, before anything else: never twice
        self.seen.add(name)
        if not text:
            return
        to = str(meta.get("session") or "")
        armed = answer_line(stamp, raw, text, self.host, self.inbox) if kind == "voice" and logged else {}
        text, to = armed.get("text", text), armed.get("to", to)
        with self.lock:
            self.items.append({"id": name, "kind": kind, "stamp": stamp, "text": text, "status": "queued", "queued_at": iso(now),
                               "born": born if born is not None else now,
                               "delivered_at": None, "target": None, "target_id": None, "note": "", "confirmed": False,
                               **({"to": to} if sessions.SID_RE.fullmatch(to) else {}),
                               **({"answer": armed["answer"]} if armed.get("answer") else {}),
                               **({"answer_note": armed["note"]} if armed.get("note") else {})})
            self._save()

    def _rows(self):
        try:
            return list(self.sessions_fn() or [])
        except Exception:
            return []

    # items -> keystrokes
    def deliver(self, now):
        self._expire(now)                                      # too old to type on its own, whatever the gate would say
        if now - self.last_delivery < SETTLE_S:
            return
        with self.lock:
            queued = [i for i in self.items if i.get("status") == "queued"]
        if not queued:
            return
        item = queued[0]                                       # strict FIFO: lines arrive in the order they were said
        rows = self._rows()
        try:
            cards = list(self.cards_fn() or [])
        except Exception:
            cards = []
        target, text, note = resolve(item, rows, cards, self.host)
        if target is None and item.get("to"):
            self._gone(item, now, note)
            return
        if target is None:
            self._hold(queued, note or "waiting for a session", now)
            return
        item.pop("gone_since", None)
        ok, why = gate(target, self.win, allow_busy=self.allow_busy)
        if not ok:
            self._hold(queued, why, now, target.get("name"))
            return
        busy = target.get("status") in BUSY_STATES or target.get("reg_status") == "busy"
        with self.lock:
            if item.get("status") != "queued":
                return                                         # dropped from the panel while the gate looked at the screen
            item.update(status="typing", target=target.get("name"), target_id=target.get("id"), note=note)
            self._save()
        size = transcript_size(target["id"], self.find)
        try:
            sent, err = self.win.type_text(int(target["pid"]), text)
        except Exception as e:                                 # a fake or a future Win must not leave the item "typing"
            sent, err = False, f"{type(e).__name__}: {e}"
        if not sent:
            if str(err).startswith(ENTER_WITHHELD):
                err = (f"{err}. Nothing was approved; if the words are in {target.get('name')}'s input line, send or clear them "
                       "there (Retry types them again)")
            with self.lock:
                item.update(status="failed", note=err)
                self._save()
            self._event("failed", item, err)
            self.last_delivery = now
            return
        seen = confirm(target["id"], text, offset=size, find=self.find, sleep=self.sleep)
        result = "confirmed in the transcript" if seen else "typed; not seen in the transcript yet"
        if busy:
            result += " · busy: read after its current step"
        if item.get("answer") and answers.tick(item["answer"], item["answer"].get("words", "")):
            result += " · question ticked"
        if item.get("answer_note"):
            result += f" · {item['answer_note']}"
        with self.lock:
            item.update(status="delivered", delivered_at=iso(self.clock()), confirmed=bool(seen),
                        note=(note + " · " if note else "") + result)
            self._save()
        self._event("delivered", item, f"{target.get('name')} is busy: it reads this after its current step" if busy else "")
        self.last_delivery = self.clock()

    def _born(self, item):
        """When the line was spoken or typed (the file's mtime); state files from before busy typing only have queued_at."""
        b = item.get("born")
        if isinstance(b, (int, float)):
            return float(b)
        b = epoch(item.get("queued_at"))
        return b if b is not None else self.clock()

    def _hold(self, queued, why, now, target=None):
        """The head line waits (why = the gate's or the router's reason). Every queued line that has now waited HOLD_SIGNAL_S
        gets its one 'queued' signal; the ones behind the head wait for the same reason (strict FIFO)."""
        head, fresh = queued[0], []
        with self.lock:
            changed = head.get("status") == "queued" and head.get("note") != why
            if changed:
                head["note"] = why
            for i in queued:
                if i.get("status") == "queued" and not i.get("signalled") and now - self._born(i) >= HOLD_SIGNAL_S:
                    i["signalled"] = True
                    fresh.append(i)
            if changed or fresh:
                self._save()
        for i in fresh:
            if i is head:
                self._event("queued", i, why, target)
            else:                                              # its own target is only resolved once it reaches the head
                self._event("queued", i, f"behind an earlier line: {why}")

    def _expire(self, now):
        """Lines that waited longer than stale_s leave the queue: typed late, they land out of context, so you decide (Send
        anyway / Drop). A line you re-sent by hand is never held back again."""
        if not self.stale_s:
            return
        with self.lock:
            old = [i for i in self.items if i.get("status") == "queued" and not i.get("forced") and now - self._born(i) > self.stale_s]
            for i in old:
                why = i.get("note") or ""
                i.pop("gone_since", None)
                i.update(status="stale", note=f"waited {waited(now - self._born(i))}: Send anyway or Drop" + (f" · {why}" if why else ""))
            if old:
                self._save()
        for i in old:
            self._event("stale", i, i["note"])

    def _event(self, kind, item, reason="", target=None):
        """One panel signal (a short sound and a toast): delivered | queued | stale | failed. The page plays every seq it has
        not seen yet; nothing is persisted (after a restart the page starts over from the new boot id)."""
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "kind": kind, "id": item.get("id"), "item_kind": item.get("kind"),
                                "text": (item.get("text") or "")[:120], "target": target or item.get("target"), "reason": reason,
                                "at": iso(self.clock())})
            self.events = self.events[-EVENTS_KEEP:]

    def _gone(self, item, now, note):
        """An answer for a session that is not running: it waits TARGET_GONE_S (a registry file being rewritten can hide a live
        session for a moment), then fails, so the lines behind it move on. It is never typed anywhere else."""
        failed = False
        with self.lock:
            if item.get("status") != "queued":
                return                                         # dropped from the panel meanwhile
            since = item.setdefault("gone_since", now)
            if now - since >= TARGET_GONE_S:
                item.pop("gone_since", None)
                item.update(status="failed", target="the session that asked",
                            note=f"{note}: nothing was typed (Retry types it there once it runs again)")
                failed = True
            elif item.get("note") == note:
                return
            else:
                item["note"] = note
            self._save()
        if failed:
            self._event("failed", item, item["note"])

    # api
    def act(self, action, item_id):
        """drop a queued / stale / failed line, or retry a failed / dropped / stale one (the panel's Send anyway is a retry). The
        text is always the one read from the inbox file, never new text. A line re-sent by hand is not held back as stale again."""
        with self.lock:
            item = next((i for i in self.items if i["id"] == item_id), None)
            if not item:
                return False, "no such line"
            if action == "drop" and item["status"] in ("queued", "failed", "stale"):
                item.update(status="dropped", note="dropped from the panel")
            elif action == "retry" and item["status"] in ("failed", "dropped", "stale"):
                item.update(status="queued", note="sent anyway from the panel" if item["status"] == "stale" else "retry from the panel",
                            confirmed=False, forced=True, signalled=False)
            else:
                return False, f"cannot {action} a {item['status']} line"
            self._save()
            return True, ""

    def snapshot(self, show_private=False, private=None):
        rows = self._rows()
        main = find_main(rows, self.host)
        fb = None if main else most_recent(rows, self.host)
        with self.lock:
            items = [dict(i) for i in self.items[-30:]]
        for i in items:
            if i.get("answer"):
                i["answer"] = True                             # the question is in the line itself, and masked with it
        if private is not None and not show_private:
            for i in items:
                if private.search(i.get("text") or ""):
                    i["text"], i["private"] = "(private line)", True
        items.reverse()
        return {"mode": "panel", "host": self.host, "main": {k: main.get(k) for k in ("id", "name", "status", "pid")} if main else None,
                "fallback": fb.get("name") if fb else None, "queued": sum(1 for i in items if i["status"] == "queued"),
                "stale": sum(1 for i in items if i["status"] == "stale"), "busy_typing": self.allow_busy, "stale_s": self.stale_s,
                "items": items, "lock": self.read_lock().get("session"), "passive": self.passive or None,
                "grace": bool(self.t0 is not None and self.clock() - self.t0 < HANDOFF_GRACE_S)}

    def compact(self, private=None):
        """For /api/state: enough for the Panel tab's one-line indicator, its Send anyway / Drop buttons for a stale line, and
        the signal events (sound and toast) the page has not played yet. Private text is masked, as everywhere on the Panel tab."""
        snap = self.snapshot(show_private=False, private=private)
        last = next((i for i in snap["items"] if i["status"] != "dropped"), None)
        head = next((i for i in reversed(snap["items"]) if i["status"] == "queued"), None)    # items are newest first
        stale = [{k: i.get(k) for k in ("id", "text", "note", "queued_at")} for i in snap["items"] if i["status"] == "stale"]
        with self.lock:
            events = [dict(e) for e in self.events[-8:]]
        if private is not None:
            for e in events:
                if any(private.search(str(e.get(k) or "")) for k in ("text", "target", "reason")):
                    e.update(text="(private line)", target=None, reason="", private=True)
        return {"mode": "panel", "target": (snap["main"] or {}).get("name") or snap["fallback"], "main": bool(snap["main"]),
                "queued": snap["queued"], "why": head.get("note") if head else "", "stale": snap["stale"], "stale_items": stale[:3],
                "last": {k: last.get(k) for k in ("status", "text", "target", "delivered_at", "queued_at", "note")} if last else None,
                "seq": self.seq, "boot": self.boot, "events": events}
