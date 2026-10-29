# BrianDump plugin for Claude Code

The small Claude Code side of BrianDump. The installer adds it; by hand:

    /plugin marketplace add KnockKnockGoose/BrianDump
    /plugin install briandump@briandump

What it contains:

| piece | what it does |
|---|---|
| `hooks/hooks.json` -> `scripts/session_start.py` (SessionStart) | tells the new session how your voice arrives: in panel mode "spoken and panel-typed lines arrive as normal input"; in monitor mode the exact background command that arms `briandump/watch_inbox.py` |
| `hooks/hooks.json` -> `scripts/needs_you.py` (Notification) | writes `<state_dir>/state/needs_you.json`, which lights the panel's "needs you" banner when Claude waits for a permission or an answer (idle reminders and other notices that need nobody are not written) |
| `commands/ask.md` -> `/briandump:ask "question" [--options "a\|b"]` | Claude puts a question on your panel; your answer comes back as input |
| `commands/agenda.md` -> `/briandump:agenda add "item"` | Claude adds an item to your agenda.md checklist |

Both hook scripts are standard-library Python, read only `config.json` under `%LOCALAPPDATA%\BrianDump` (or `BRIANDUMP_HOME`), and
swallow every error, so they can never block Claude Code. The hooks use the exec form (`"command": "py"` plus `args`, no
shell), which needs Claude Code 2.1.139 or newer and the `py` launcher on PATH (the Python.org installer puts it there).
