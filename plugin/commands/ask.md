---
description: Put a question on the user's BrianDump panel; they answer there (or by voice) and the answer arrives as input
argument-hint: "<question>" [--options "a|b|c"]
allowed-tools: Bash(py *briandump.py* ask *), PowerShell(py *briandump.py* ask *), Read
---

Ask the user a question through the BrianDump panel instead of in the terminal. Arguments: $ARGUMENTS

1. Find the BrianDump CLI: the session context from the BrianDump SessionStart hook names it (`py ".../briandump.py"`); otherwise
   read `install_dir` from `%LOCALAPPDATA%\BrianDump\config.json` (or `%BRIANDUMP_HOME%\config.json` when BRIANDUMP_HOME is set). If
   neither gives a path, ask the user where BrianDump is installed and stop.
2. Run it with the question as ONE argument, quoted safely for the shell you use (single quotes in Bash, with any single quote
   inside escaped), keeping `--options "a|b|c"` when the user supplied choices:

       py "<install_dir>/briandump.py" ask '<question>' [--options 'a|b|c']

3. Report the one line the command prints and carry on with your work. Do not wait for the answer.

What the user sees: the question appears in the panel's Questions list. When they answer there, it arrives
in this session (BrianDump records which session asked) as a normal user message that starts with
`[panel answer to: <question>]`. Treat it as the user's reply to this question. A spoken answer goes to their main session
unless they start it with this session's name.
