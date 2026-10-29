"""BrianDump SessionStart hook: tell the new Claude Code session how the user's voice reaches it.

Reads <state_dir>/config.json (BRIANDUMP_HOME when set, else %LOCALAPPDATA%\\BrianDump, the same rule as briandump/config.py) for
voice.mode and install_dir, then prints {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}.
Panel mode (the default): spoken and panel-typed lines arrive as ordinary input, typed in by the panel's voice router.
Monitor mode: the exact background command that arms briandump/watch_inbox.py for this session.

stdlib only; every error is swallowed and the output is plain ASCII JSON, so Claude Code is never blocked by this hook.
"""
import json
import os
import re
import sys
from pathlib import Path


def state_dir():
    home = os.environ.get("BRIANDUMP_HOME")
    if home:
        return Path(home).resolve()
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "BrianDump"


def config():
    try:
        d = json.loads((state_dir() / "config.json").read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def read_event():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", "replace") or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def install_dir(cfg):
    """Where BrianDump is installed: config.json install_dir (the installer writes it), else the plugin's parent folder when it
    is a BrianDump checkout (a plugin loaded straight from the repo); '' when unknown."""
    d = cfg.get("install_dir")
    if d and (Path(str(d)) / "briandump.py").exists():
        return str(Path(str(d)))
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        guess = Path(root).resolve().parent
        if (guess / "briandump.py").exists():
            return str(guess)
    return str(d) if d else ""


def context(data, cfg):
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id") or "")) or "unknown"
    voice = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
    mode = str(voice.get("mode") or "panel").lower()
    repo = install_dir(cfg)
    if mode == "monitor":
        watcher = ((Path(repo) / "briandump" / "watch_inbox.py").as_posix() if repo
                   else "<BrianDump install folder>/briandump/watch_inbox.py")
        cmd = f'py "{watcher}" --session {sid}'
        text = ("BrianDump voice (monitor mode): before anything else in your first reply, start this as a BACKGROUND command "
                f"(run_in_background: true): {cmd} --takeover --once\n"
                "It exits right after it delivers speech. Its output lines 'VOICE <stamp>-<host>: <text>' are the user speaking "
                "to you (a 'VOICE <stamp>-panel-<host>' line is something they typed on the BrianDump panel): treat each as a "
                "user message and act on it. Every time it exits, start it again in the background WITHOUT --takeover: "
                f"{cmd} --once\n"
                "If it prints 'handed off' or 'not started', another session owns the voice: stop restarting it. Never start "
                "briandump/ptt.py yourself (the BrianDump PTT logon task already listens).")
    else:
        text = ("BrianDump voice (panel mode): lines from the user's push-to-talk key and from the BrianDump panel arrive as normal "
                "input, typed into this session by the panel's voice router, so do not start any inbox watcher. Such a line may "
                "start with [panel answer to: ...] or [BrianDump question: ...], which only marks where it came from.")
    if repo:
        cli = (Path(repo) / "briandump.py").as_posix()
        text += f' The BrianDump CLI is py "{cli}" (what /briandump:ask and /briandump:agenda run).'
    return text


def main():
    try:
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context(read_event(), config())}}
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
