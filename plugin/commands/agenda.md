---
description: Add an item to the user's BrianDump agenda (the agenda.md checklist shown on the panel)
argument-hint: add "<item>"
allowed-tools: Bash(py *briandump.py* agenda add *), PowerShell(py *briandump.py* agenda add *), Read
---

Add an item to the user's BrianDump agenda. Arguments: $ARGUMENTS

1. Find the BrianDump CLI: the session context from the BrianDump SessionStart hook names it (`py ".../briandump.py"`); otherwise
   read `install_dir` from `%LOCALAPPDATA%\BrianDump\config.json` (or `%BRIANDUMP_HOME%\config.json` when BRIANDUMP_HOME is set). If
   neither gives a path, ask the user where BrianDump is installed and stop.
2. Run it with the item as ONE argument (one short imperative line, no trailing period), quoted safely for the shell you use
   (single quotes in Bash, with any single quote inside escaped):

       py "<install_dir>/briandump.py" agenda add '<item>'

3. Report the line the command prints: it says whether the item was added or merged into an existing one (duplicates are
   detected on normalised text).

What the user sees: the item appears at the top of the panel's Agenda list as an open checkbox, and as `- [ ] <item>` in
`Documents\BrianDump\agenda.md` (or the file set as `agenda.file`), which they can also edit by hand.
