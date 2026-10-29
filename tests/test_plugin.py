"""Tests for the Claude Code plugin (plugin/) and the marketplace file: manifests, the hook wiring (exec form), both hook
scripts run as real processes with a piped JSON event under a temp BRIANDUMP_HOME (panel and monitor mode), stdlib-only
imports, no personal data, the command files, and `claude plugin validate` when the CLI is installed."""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
SCRIPTS = PLUGIN / "scripts"
STDLIB_OK = {"json", "os", "re", "sys", "time", "datetime", "pathlib"}


def run(script, event, extra_env=None):
    raw = event if isinstance(event, bytes) else json.dumps(event, ensure_ascii=False).encode("utf-8")
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run([sys.executable, str(SCRIPTS / script)], input=raw, capture_output=True, env=env, timeout=60)


def context_of(p):
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    out = json.loads(p.stdout.decode("ascii"))                               # plain ASCII JSON, nothing else on stdout
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return out["hookSpecificOutput"]["additionalContext"]


# ---------- manifests ----------
def test_plugin_manifest():
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert m["name"] == "briandump" and m["version"] == "0.1.1" and m["author"]["name"] and m["license"] == "MIT"


def test_marketplace_manifest_points_at_the_plugin():
    m = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert m["name"] == "briandump" and m["owner"]["name"]
    (p,) = m["plugins"]
    assert p["name"] == "briandump" and p["source"] == "./plugin" and (REPO / p["source"]).is_dir()


def test_hooks_use_the_exec_form_and_point_at_real_scripts():
    h = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(h) == {"SessionStart", "Notification"}
    for event, script in (("SessionStart", "session_start.py"), ("Notification", "needs_you.py")):
        (group,) = h[event]
        (hook,) = group["hooks"]
        assert hook["type"] == "command" and hook["command"] == "py" and hook["timeout"] == 10
        assert hook["args"] == [f"${{CLAUDE_PLUGIN_ROOT}}/scripts/{script}"] and (SCRIPTS / script).is_file()


def test_hook_scripts_are_stdlib_only():
    for p in SCRIPTS.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert mods <= STDLIB_OK, (p.name, mods - STDLIB_OK)


def test_commands_have_frontmatter_and_grant_only_the_briandump_cli():
    for name, sub in (("ask.md", "ask"), ("agenda.md", "agenda add")):
        text = (PLUGIN / "commands" / name).read_text(encoding="utf-8")
        front = text.split("---")[1]
        fields = dict(line.split(":", 1) for line in front.strip().splitlines())
        assert fields["description"].strip() and "$ARGUMENTS" in text and f'briandump.py" {sub}' in text
        tools = [t.strip() for t in fields["allowed-tools"].split(",")]
        shells = [t for t in tools if t.startswith(("Bash(", "PowerShell("))]
        assert shells and all("briandump.py" in t and sub in t for t in shells)     # never a blanket `py *`


def test_no_personal_data_in_the_plugin():
    """Generic checks only: no private word list ships in this repo. The author's name may appear in the author and owner
    fields and nowhere else; nothing may look like a home-folder path, a private-network (CGNAT / Tailscale) address, an
    e-mail address or a token."""
    author = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["author"]["name"]
    names = [w for w in re.split(r"\W+", author) if len(w) > 2]
    assert names
    rx = re.compile("|".join((
        r"\b(?:" + "|".join(map(re.escape, names)) + r")\b",                # the author, outside the author / owner fields
        r"\b[A-Za-z]:[\\/]+Users[\\/]+\w",                                    # C:\Users\<someone> (%USERPROFILE% is fine)
        r"/(?:home|Users)/\w",                                                # a POSIX home folder
        r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b",    # the 100.64/10 range: CGNAT, Tailscale
        r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b",                                   # an e-mail address
        r"sk-an[t]-|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}",            # a token ([t]: the pattern never matches itself)
    )), re.I)
    files = [p for p in PLUGIN.rglob("*") if p.is_file() and "__pycache__" not in p.parts] + [REPO / ".claude-plugin" / "marketplace.json"]
    for p in files:
        text = p.read_text(encoding="utf-8")
        if p.name in ("plugin.json", "marketplace.json"):                    # the author / owner field may name the author
            text = re.sub(r'"name":\s*"[^"]*"', '"name": ""', text)
        assert not rx.search(text), (p, rx.search(text).group(0))


# ---------- needs_you.py ----------
def test_needs_you_writes_the_flag(home):
    ev = {"session_id": "abc-123", "transcript_path": "C:\\t\\abc-123.jsonl", "hook_event_name": "Notification",
          "message": "Claude needs your permission to use Bash", "notification_type": "permission_prompt"}
    p = run("needs_you.py", ev)
    assert p.returncode == 0 and not p.stdout
    rec = json.loads(home.needs_you.read_text(encoding="utf-8"))
    assert set(rec) == {"ts", "message", "type", "session", "transcript"}
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", rec["ts"])     # comparable with transcript timestamps
    assert rec["type"] == "permission_prompt" and rec["session"] == "abc-123" and rec["transcript"].endswith("abc-123.jsonl")
    assert not list(home.state.glob("*.tmp"))


def test_needs_you_ignores_notices_that_need_nobody(home):
    run("needs_you.py", {"session_id": "s1", "message": "permission", "notification_type": "permission_prompt"})
    before = home.needs_you.read_text(encoding="utf-8")
    for kind in ("idle_prompt", "agent_completed", "auth_success", "elicitation_complete", "quota_auto_resume_fired"):
        assert run("needs_you.py", {"session_id": "s1", "message": "x", "notification_type": kind}).returncode == 0
    assert home.needs_you.read_text(encoding="utf-8") == before                # the waiting permission stays flagged
    run("needs_you.py", {"session_id": "s1", "message": "pick one", "notification_type": "elicitation_dialog"})
    assert json.loads(home.needs_you.read_text(encoding="utf-8"))["type"] == "elicitation_dialog"


def test_needs_you_survives_garbage_and_unicode(home):
    assert run("needs_you.py", b"\xff\xfe not json").returncode == 0
    assert run("needs_you.py", b"").returncode == 0
    p = run("needs_you.py", {"session_id": "s2", "message": "caf\u00e9 \u2713", "notification_type": "permission_prompt"})
    assert p.returncode == 0 and json.loads(home.needs_you.read_text(encoding="utf-8"))["message"] == "caf\u00e9 \u2713"


# ---------- session_start.py ----------
def test_session_start_panel_mode_by_default(home):
    ctx = context_of(run("session_start.py", {"session_id": "abc-123", "source": "startup"}, {"CLAUDE_PLUGIN_ROOT": ""}))
    assert "panel mode" in ctx and "do not start any inbox watcher" in ctx
    for prefix in ("[panel answer to: ...]", "[BrianDump question: ...]"):
        assert prefix in ctx
    assert "[BrianDump dump" not in ctx          # Send to Claude sends the user's edited text as is: no prefix to explain


def test_session_start_monitor_mode_gives_the_exact_command(home):
    (home.state_dir / "config.json").write_text(json.dumps({"voice": {"mode": "monitor"}, "install_dir": str(REPO)}), encoding="utf-8")
    ctx = context_of(run("session_start.py", {"session_id": "abc-123", "source": "startup"}))
    watcher = (REPO / "briandump" / "watch_inbox.py").as_posix()
    assert f'py "{watcher}" --session abc-123 --takeover --once' in ctx
    assert f'py "{watcher}" --session abc-123 --once' in ctx and "handed off" in ctx
    assert f'py "{(REPO / "briandump.py").as_posix()}"' in ctx


def test_session_start_finds_the_repo_from_the_plugin_root_and_sanitises_the_id(home):
    (home.state_dir / "config.json").write_text(json.dumps({"voice": {"mode": "monitor"}}), encoding="utf-8")
    ctx = context_of(run("session_start.py", {"session_id": "abc; rm -rf /"}, {"CLAUDE_PLUGIN_ROOT": str(PLUGIN)}))
    assert "--session abcrm-rf --takeover" in ctx and (REPO / "briandump" / "watch_inbox.py").as_posix() in ctx


def test_session_start_survives_garbage(home):
    (home.state_dir / "config.json").write_text("{broken", encoding="utf-8")
    assert "panel mode" in context_of(run("session_start.py", b"\xff garbage"))


# ---------- the validator ----------
@pytest.mark.skipif(shutil.which("claude") is None, reason="the claude CLI is not installed")
@pytest.mark.parametrize("target", ["./plugin", "."])
def test_claude_plugin_validate(target):
    p = subprocess.run([shutil.which("claude"), "plugin", "validate", target], cwd=REPO, capture_output=True, timeout=120)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    # Newer Claude Code versions add advisory plugin-directory checks (a listing icon, for one) in a block that ends with
    # "These plugin directory checks do not change the result below."; the validation itself must have no warning.
    checked = re.sub(r"(?s)Found \d+ plugin directory warnings?:.*?do not change the result below\.", "", out)
    assert p.returncode == 0 and "Validation passed" in out and "warning" not in checked.lower(), out
