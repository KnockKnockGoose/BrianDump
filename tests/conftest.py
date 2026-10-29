"""Shared test setup: every test runs against a throwaway BrianDump home, never the real %LOCALAPPDATA%\\BrianDump,
Documents\\BrianDump or ~/.claude.

The environment is set here, at import time, before any test module imports briandump, so the module-level CONF singleton
is built against a temp folder with BRIANDUMP_SANDBOX=1 (no keystrokes, no terminal, no browser, no `claude -p`).

    def test_x(home):           # a fresh home for one test; `home` IS briandump.config.CONF, re-pointed in place
        home.inbox, home.dumps_dir, home.projects ...

`home` re-runs CONF.__init__ with a new environment instead of reloading modules: every module holds the same CONF
object, so they all see the new paths at once (module-level constants copied from CONF at import keep the session
values). Subprocesses inherit the per-test environment through monkeypatch.

A guard refuses to spawn `claude -p` / `claude --print` from any test: the suite makes no model calls. A test that checks
the argv of the cards call replaces subprocess.Popen (or cards.resolve_claude) with its own fake, which wins over this.
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_SESSION = Path(tempfile.mkdtemp(prefix="briandump-test-"))
atexit.register(shutil.rmtree, _SESSION, True)


def briandump_env(home, claude_dir):
    """The environment that points BrianDump at `home` (state + data) and a fake ~/.claude, sandboxed."""
    return {"BRIANDUMP_HOME": str(home), "BRIANDUMP_CLAUDE_DIR": str(claude_dir), "BRIANDUMP_HOST": "testhost", "BRIANDUMP_SANDBOX": "1"}


SESSION_ENV = briandump_env(_SESSION / "home", _SESSION / "claude")
os.environ.update(SESSION_ENV)
os.environ.pop("BRIANDUMP_DATA", None)
for _name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):     # a suite run from inside Claude Code must not look like one of its
    os.environ.pop(_name, None)                            # sessions to `briandump.py ask` (it records the asking session)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh BrianDump home (state + data + fake ~/.claude with projects/ and sessions/) for one test. Yields CONF."""
    from briandump.config import CONF
    claude = tmp_path / "claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "sessions").mkdir(parents=True)
    env = briandump_env(tmp_path / "home", claude)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BRIANDUMP_DATA", raising=False)
    CONF.__init__(env=env)
    CONF.ensure_dirs()
    yield CONF
    CONF.__init__(env=SESSION_ENV)


_RealPopen = subprocess.Popen


class _NoModelPopen(_RealPopen):
    def __init__(self, args, *a, **kw):
        argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else str(args).split()
        exe = Path(argv[0].strip('"')).name.lower() if argv else ""
        if exe.startswith("claude") and ("-p" in argv or "--print" in argv):
            raise RuntimeError("the test suite must not call the model (claude -p); use the mock processor or a fake Popen")
        super().__init__(args, *a, **kw)


@pytest.fixture(autouse=True)
def _no_model_calls(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _NoModelPopen)
