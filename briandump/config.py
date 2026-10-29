"""BrianDump configuration: one JSON file with documented defaults, and every path in one place.

    from briandump.config import CONF
    CONF.port, CONF.bind, CONF.allowed            # server
    CONF.inbox, CONF.state, CONF.dumps_dir, ...   # paths (all created lazily by whoever writes there)
    CONF.get("ptt.key"), CONF.ptt, CONF.voice     # settings (dotted lookup or the sub-dicts)
    CONF.outward, CONF.record("open_url", url=u)  # sandbox: record a side effect instead of doing it

Two directories:

  state_dir   %LOCALAPPDATA%\\BrianDump    config.json, state/ (needs_you.json, voice_router.json, ptt_status.json ...),
                                        inbox/ (one .txt per spoken line), logs/, edge-profile/, webview/
  data_dir    Documents\\BrianDump        the files that are yours to read: agenda.md, questions.md, reports/, dumps/<id>/

Defaults live in config.example.json (repo root); the user's %LOCALAPPDATA%\\BrianDump\\config.json is merged over
them, so a new key in a later version gets its default without touching the user's file. Keys that start with "_" are
documentation and are ignored. Nothing in the file can stop an import: a file that cannot be read gives CONF.load_error and
the defaults (and is never written over), and a value that cannot be used gives its default and a line in CONF.problems.

Environment overrides (tests, the QA sandbox, a second copy on the same PC):
  BRIANDUMP_HOME=<dir>        both directories under <dir> (<dir>/config.json, <dir>/state, <dir>/inbox, <dir>/data/...)
  BRIANDUMP_DATA=<dir>        just the data directory
  BRIANDUMP_CLAUDE_DIR=<dir>  where Claude Code's own files are read from (default ~/.claude): projects/, sessions/
  BRIANDUMP_SANDBOX=1         outward actions are recorded in <state_dir>/side_effects.jsonl instead of being done: no
                           keystrokes, no terminal launch, no browser, no window messages, no `claude -p`
  BRIANDUMP_HOST=<name>       the host tag used in inbox file names (default: this machine's hostname, lower case)
"""
from __future__ import annotations

import copy
import json
import os
import re
import socket
import threading
import time
from pathlib import Path

APP = "BrianDump"
HERE = Path(__file__).resolve().parent          # the package: panel.html, panel.ico, cards.schema.json, fixtures/
REPO = HERE.parent
EXAMPLE = REPO / "config.example.json"
LOOPBACK = {"127.0.0.1", "::1"}


def _read_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class ConfigError(Exception):
    """config.json exists but cannot be used (a syntax error, not a JSON object): nothing is ever written over it."""


def read_config(p, tries=20, wait=0.03):
    """The user's config.json -> (settings dict, None), ({}, None) when there is none, or ({}, why) when it exists but cannot
    be used. A sharing violation (another process replacing the file this very moment) is retried first. A BOM is fine:
    Notepad and PowerShell 5.1 write one."""
    p = Path(p)
    for i in range(tries):
        try:
            text = p.read_text(encoding="utf-8-sig")
            break
        except FileNotFoundError:
            return {}, None
        except PermissionError as e:
            if i == tries - 1:
                return {}, f"{p} stayed locked by another program ({e.strerror or e})"
            time.sleep(wait)
        except (OSError, UnicodeError) as e:
            return {}, f"{p} cannot be read ({getattr(e, 'strerror', None) or e})"
    try:
        data = json.loads(text)
    except ValueError as e:                     # the message names the line and column
        return {}, f"{p} has a syntax error ({e})"
    if not isinstance(data, dict):
        return {}, f"{p} is not a JSON object"
    return data, None


def strip_doc_keys(d):
    """A copy without the '_comment' / '_key' documentation entries (recursively)."""
    if isinstance(d, dict):
        return {k: strip_doc_keys(v) for k, v in d.items() if not str(k).startswith("_")}
    if isinstance(d, list):
        return [strip_doc_keys(v) for v in d]
    return d


def deep_merge(base, over):
    """base updated by over, recursively for dicts; lists and scalars in `over` replace."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def defaults():
    """The documented defaults from config.example.json (doc keys stripped)."""
    return strip_doc_keys(_read_json(EXAMPLE, {}))


def replace_retry(src, dst, tries=20, wait=0.03):
    """os.replace, retried: Windows refuses it (PermissionError, WinError 5) while any other process has `dst` open, for
    example the panel server reading config.json at that moment."""
    for i in range(tries):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


NEVER = re.compile(r"(?!x)x")                    # the private filter with no patterns


class AnyOf:
    """Patterns that cannot share one regex (a global flag such as (?i) is allowed only at its start): search() returns the first
    match of any of them, as a compiled alternation would."""

    def __init__(self, patterns):
        self.patterns = patterns

    def search(self, text, *args):
        return next((m for m in (p.search(text, *args) for p in self.patterns) if m), None)


class Config:
    def __init__(self, env=None):
        self._env = env
        env = os.environ if env is None else env
        home = Path.home()
        briandump_home = env.get("BRIANDUMP_HOME")
        if briandump_home:
            self.state_dir = Path(briandump_home).resolve()
            default_data = self.state_dir / "data"
        else:
            self.state_dir = Path(env.get("LOCALAPPDATA") or (home / "AppData" / "Local")) / APP
            default_data = home / "Documents" / APP
        self.config_file = self.state_dir / "config.json"
        self.user_cfg, self.load_error = read_config(self.config_file)      # a broken file: the defaults, and the reason
        base = defaults()
        self.cfg = deep_merge(base, strip_doc_keys(self.user_cfg))
        # A value that cannot be used gets its default and a line here (doctor's Settings row shows them, the server logs them):
        # one typo in config.json must never stop every BrianDump command at import, `doctor` and `config --set` included.
        self.problems = []

        # ---- paths ----
        data = env.get("BRIANDUMP_DATA") or self.cfg.get("data_dir")
        self.data_dir = self._path("data_dir", data, default_data)
        self.state = self.state_dir / "state"
        self.inbox = self.state_dir / "inbox"
        self.logs = self.state_dir / "logs"
        self.edge_profile = self.state_dir / "edge-profile"
        self.webview_store = self.state_dir / "webview"
        self.dumps_dir = self.data_dir / "dumps"
        self.reports_dir = self.data_dir / "reports"
        self.agenda_file = self._path("agenda.file", self.get("agenda.file"), self.data_dir / "agenda.md")
        self.questions_file = self._path("questions.file", self.get("questions.file"), self.data_dir / "questions.md")
        self.claude_dir = Path(env.get("BRIANDUMP_CLAUDE_DIR") or (home / ".claude")).resolve()
        self.projects = self.claude_dir / "projects"      # <project>/<session>.jsonl transcripts
        self.sessions = self.claude_dir / "sessions"      # <pid>.json live registry
        # state files (names are part of the contract: the plugin scripts and the PTT service write some of them)
        self.needs_you = self.state / "needs_you.json"
        self.ptt_status = self.state / "ptt_status.json"
        self.ptt_command = self.state / "ptt_command.json"
        self.voice_router = self.state / "voice_router.json"
        self.voice_lock = self.state / "voice_watcher.json"
        self.voice_handled = self.state / "voice_handled.txt"
        self.manual_questions = self.state / "questions.json"
        self.reports_read = self.state / "reports_read.json"
        self.answer_arm = self.state / "answer_arm.json"     # the question your next talk clip answers (answers.py)
        self.side_effects = self.state_dir / "side_effects.jsonl"
        # package assets
        self.panel_html = HERE / "panel.html"
        self.panel_ico = HERE / "panel.ico"
        self.schema_file = HERE / "cards.schema.json"
        self.fixtures = HERE / "fixtures"

        # ---- settings ----
        self.host = str(env.get("BRIANDUMP_HOST") or socket.gethostname()).lower()
        self.port = self._number("port", int, 8770, 1, 65535)
        self.bind = str(self.cfg.get("bind") or "127.0.0.1")
        self.allowed = set(LOOPBACK) | {str(ip) for ip in self._list("allow")}
        self.context_limit = self._number("context_limit", int, 200_000, 1)
        self.window = str(self.cfg.get("window") or "edge")
        self.scale = self._number("scale", float, 1.0, 0.25, 4)
        self.fraction = self._number("fraction", float, 1 / 3, 0.1, 1)
        self.monitor = str(self.cfg.get("monitor") or "auto")
        self.voice, self.ptt, self.dump = (self._group(k, base) for k in ("voice", "ptt", "dump"))
        self.private_words = [str(w) for w in self._list("private_filter") if str(w).strip()]
        self.private_re = self._private(self.private_words)
        reports = self._group("reports", base)                  # the Reports tab (briandump/reports.py checks the folders)
        self.reports_index = self._paths("reports.index", reports.get("index"))
        self.reports_roots = self._paths("reports.roots", reports.get("roots"))
        self.reports_discover = self._pattern("reports.discover", reports.get("discover"))
        self.sandbox = bool(env.get("BRIANDUMP_SANDBOX"))
        self.outward = not self.sandbox
        self._lock = threading.Lock()

    # ---- reading settings that may be wrong (each one: the default, and a line in `problems`) ----
    def _path(self, key, value, default):
        if not value:
            return default
        try:
            return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()
        except (OSError, ValueError) as e:
            self.problems.append(f"{key} = {json.dumps(str(value))} is not a usable path ({e}); using {default}")
            return default

    def _number(self, key, kind, default, lo, hi=None):
        v = self.cfg.get(key)
        if v is None or v == "" or v == 0:              # unset: the default (0 always meant that here)
            return default
        try:
            n = None if isinstance(v, bool) else kind(v)
        except (TypeError, ValueError, OverflowError):
            n = None
        if n is None or n != n or n < lo or (hi is not None and n > hi):
            what = "a whole number" if kind is int else "a number"
            what += f" from {lo:g} to {hi:g}" if hi is not None else f" of at least {lo:g}"
            self.problems.append(f"{key} = {json.dumps(v, ensure_ascii=False)} is not {what}; using {default:g}")
            return default
        return n

    def _list(self, key):
        v = self.cfg.get(key)
        if v is None or isinstance(v, list):
            return v or []
        self.problems.append(f'{key} = {json.dumps(v, ensure_ascii=False)[:80]} is not a list such as ["a", "b"]; using []')
        return []

    def _group(self, key, base):
        v = self.cfg.get(key)
        if isinstance(v, dict):
            return dict(v)
        if v is not None:
            shown = json.dumps(v, ensure_ascii=False)[:80]
            self.problems.append(f"{key} = {shown} is not a group of settings {{...}}; using the defaults")
        self.cfg[key] = copy.deepcopy(base.get(key) or {})   # so CONF.get("<key>.<name>") answers the defaults too
        return dict(self.cfg[key])

    def _paths(self, key, v):
        """A path or a list of paths -> the paths as written (the strings in a list that holds other things too)."""
        if v is None or v == "":
            return []
        items = [v] if isinstance(v, str) else v if isinstance(v, list) else None
        if items is None or not all(isinstance(s, str) for s in items):
            shown = json.dumps(v, ensure_ascii=False)[:80]
            self.problems.append(f'{key} = {shown} is not a path or a list of paths such as ["~/notes"]; '
                                 + ("using the paths in it" if items is not None else "using none"))
        return [s.strip() for s in items or [] if isinstance(s, str) and s.strip()]

    def _pattern(self, key, v):
        """A regular expression (multi-line: ^ and $ match at each line) -> compiled, or None with a problem line."""
        if v is None or v == "":
            return None
        shown = json.dumps(v, ensure_ascii=False)[:80]
        if not isinstance(v, str):
            self.problems.append(f"{key} = {shown} is not a regular expression (a string); nothing is searched")
            return None
        try:
            return re.compile(v, re.M)
        except re.error as e:
            self.problems.append(f"{key} = {shown} is not a valid pattern ({e}); nothing is searched")
            return None

    def _private(self, words):
        """The private filter as one regex. A pattern that does not compile is reported and matched as plain text; patterns
        that compile alone but not together (a global flag such as (?i) inside the list) are searched one by one (AnyOf)."""
        pats = []
        for w in words:
            try:
                re.compile(w)
                pats.append(w)
            except re.error as e:
                self.problems.append(f"private_filter: {json.dumps(w, ensure_ascii=False)} is not a valid pattern ({e}); "
                                     "matched as plain text")
                pats.append(re.escape(w))
        if not pats:
            return NEVER
        try:
            return re.compile("|".join(pats), re.I)
        except re.error:
            return AnyOf([re.compile(p, re.I) for p in pats])

    # ---- settings access ----
    def get(self, dotted, default=None):
        """CONF.get('ptt.key') -> 'f9'; missing keys give `default` (so a module can add a key before the example has it)."""
        cur = self.cfg
        for part in str(dotted).split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    # ---- first run ----
    def ensure_dirs(self):
        for p in (self.state_dir, self.state, self.inbox, self.logs, self.data_dir, self.dumps_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)

    def write_default_config(self, force=False):
        """Copy config.example.json to the user's config.json (documentation keys included). Returns the path."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if force or not self.config_file.exists():
            text = EXAMPLE.read_text(encoding="utf-8") if EXAMPLE.exists() else json.dumps(self.cfg, indent=2)
            tmp = self.config_file.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            replace_retry(tmp, self.config_file)
        return self.config_file

    def save_user_value(self, dotted, value):
        """Write one setting into the user's config.json (creating it from the example first) and into self.cfg. A config.json
        that exists but cannot be used is never replaced (ConfigError says why): it holds every other setting the user made."""
        self.write_default_config()
        raw, why = read_config(self.config_file)
        if why:
            raise ConfigError(f"{why}. Nothing was changed: fix the file (py briandump.py config --open), or delete it to start "
                              "again from the defaults")
        cur = raw
        parts = str(dotted).split(".")
        for part in parts[:-1]:
            if not isinstance(cur.get(part), dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value
        tmp = self.config_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        replace_retry(tmp, self.config_file)
        self.__init__(env=self._env)  # re-derive paths and typed settings from the same environment

    # ---- sandbox ----
    def record(self, kind, **detail):
        """Sandbox: append what WOULD have happened to side_effects.jsonl and return True (the caller then skips the real
        action). Real mode: return False and do nothing."""
        if not self.sandbox:
            return False
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": str(kind), **detail}
        try:
            with self._lock:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                with open(self.side_effects, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        return True

    def side_effect_log(self):
        out = []
        try:
            for ln in self.side_effects.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    # ---- introspection ----
    def paths(self):
        return {k: str(v) for k, v in vars(self).items() if isinstance(v, Path)}

    def describe(self):
        return {"state_dir": str(self.state_dir), "data_dir": str(self.data_dir), "config_file": str(self.config_file),
                "config_exists": self.config_file.exists(), "host": self.host, "port": self.port, "bind": self.bind,
                "allowed": sorted(self.allowed), "sandbox": self.sandbox, "claude_dir": str(self.claude_dir),
                "settings": self.cfg}


CONF = Config()
