"""BrianDump Notification hook -> <state_dir>/state/needs_you.json, the panel's "needs you" banner.

Claude Code fires Notification when it wants the user (a permission prompt, a question or dialog waiting) and also for things
that need nobody (idle reminders, a finished background agent, a login that worked). Only the first kind is written: one file
holds the latest event, so an idle reminder must not overwrite a permission prompt that is still waiting. The panel shows the
banner (and chimes) until the session's transcript moves past this timestamp, so no second hook is needed to clear it.

stdlib only; every error is swallowed, so Claude Code is never blocked by this hook. The state folder is BRIANDUMP_HOME when set,
else %LOCALAPPDATA%\\BrianDump (the same rule as briandump/config.py).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# notification_type values that do not need the user (Claude Code 2.1.x); anything else, including types added later, shows
SKIP = {"idle_prompt", "auth_success", "agent_completed", "elicitation_complete", "elicitation_response"}
SKIP_PREFIX = ("quota_auto_resume",)


def state_dir():
    home = os.environ.get("BRIANDUMP_HOME")
    if home:
        return Path(home).resolve()
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "BrianDump"


def read_event():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        data = json.loads(raw or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def record(data):
    """The needs_you.json record for this event, or None when it does not need the user."""
    kind = str(data.get("notification_type") or "")
    if kind in SKIP or kind.startswith(SKIP_PREFIX):
        return None
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "message": str(data.get("message") or "Claude needs you"), "type": kind,
            "session": str(data.get("session_id") or ""), "transcript": str(data.get("transcript_path") or "")}


def write(path, rec, tries=20):
    """tmp + os.replace, retried: Windows refuses the replace while the panel is reading the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    for _ in range(tries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.03)
    tmp.unlink()
    return False


def main():
    try:
        rec = record(read_event())
        if rec:
            write(state_dir() / "state" / "needs_you.json", rec)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
