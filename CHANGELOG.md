# Changelog

All notable changes to BrianDump are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1] - 2026-10-30

### Renamed

- **bis-kit is now BrianDump** (a pun on brain dump). The repository is `KnockKnockGoose/BrianDump`, the package and CLI are `briandump` (`py briandump.py ...`), the plugin is `briandump@briandump` with the commands `/briandump:ask` and `/briandump:agenda`, the installers are `Install BrianDump.cmd` and `Uninstall BrianDump.cmd`, the Start menu entry is **BrianDump**, the logon task is `BrianDump PTT`, settings and state live in `%LOCALAPPDATA%\BrianDump`, your files in `Documents\BrianDump`, and the environment variables are `BRIANDUMP_HOME`, `BRIANDUMP_DATA` and so on.
- Upgrading from bis-kit 0.1.0: run `Install BrianDump.cmd`. It removes the `bis-kit PTT` task, the old shortcuts and the old plugin, stops what still runs under the old name, and moves `%LOCALAPPDATA%\bis-kit` and `Documents\bis-kit` to the new names (a folder that already has the new name is never written over, and a `data_dir` of your own stays where it is). `Uninstall BrianDump.cmd` removes both the old and the new names.

### Added

- Reports tab: every Markdown report, newest first, with a dot until you open it and an unread count on the tab. The report of each brain dump is always listed. Three opt-in settings add your other reports, such as the ones Claude writes for you: `reports.roots` (the folders they live in), `reports.discover` (a byline to look for in the first 20 lines) and `reports.index` (JSON files that name reports, with an optional title, date and author). A report opens on the panel, laid out like a paper (serif text, hairline tables, light or dark paper). **Save as PDF** prints it alone through the browser's own dialog, **Open file** opens it in its own app, and a link to another listed report opens that one. Read marks are kept in `state\reports_read.json`. A bad `reports` value is a `doctor` Settings line and a note on the tab, never a crash.
- 🎤 Answer: every open question (the Panel tab and the dump decks) has a button that arms it for your next F9 clip. Click it, then hold F9 within 60 seconds: the clip is typed as `[answer to: "<question>"] <your words>` into the session that asked it (Main for a question from a dump), and the question is ticked in `questions.md` (or closed in `questions.json`) once the line is delivered. A strip on every tab shows what is armed and the seconds left. Esc, Cancel, the timeout or that one clip disarm it, and a second arm replaces the first. Monitor mode does the same.
- README: the two-speeds flow diagram (`docs/flow.svg`).
- `voice.auto_open_main` (on by default): when the panel server starts (Start menu, `launch`, the sign-in watchdog) and no session named Main is running, it opens one in a new Windows Terminal tab. Once per sign-in: a Main you close stays closed when the server restarts. `false` keeps the old behaviour; `doctor` names the setting.
- Voice signals: every line the router handles ends in one short sound and a note at the bottom of the panel: delivered (one soft high note), queued with the reason, such as "Main is waiting for you (a permission prompt)" (two soft low notes, only when it is not in within 3 seconds), or not sent. The sound plays only on this PC's own panel and follows `needs_you_chime`.
- Stale lines: a line still waiting `voice.stale_s` seconds (default 120) after you said it is not typed on its own, because a late line lands out of context. The Panel tab and the Voice tab show it with **Send anyway** and **Drop**. A line you send anyway still waits for any prompt to clear, and is never held back again. `0` turns the limit off.

### Changed

- README: **Install from Claude Code**, one sentence to paste into a Claude Code session, the defaults the installer takes when no one can answer it, and the flags that change them. The installer's dry run, with no keyboard to ask on, now names those defaults instead of saying it would ask.
- The voice router types into a busy session (`voice.busy_typing`, on by default). Claude Code queues a line typed mid-turn and hands it to the model at the next tool result, so you no longer wait minutes for a long turn to end. It still holds a line while the session waits for you (a permission prompt, an AskUserQuestion, the trust question: by the session's status, its registry flag and its screen) and types it the moment that clears. A busy session's input line must be empty, so a draft of yours is never sent. `false` restores the idle-only rule.

### Security

- The Reports tab opens only files it listed itself, and only from inside `reports.roots` or the data folder: a path is checked as written and again after symlinks and junctions are followed, so nothing outside is even opened. The page asks for a report by an id from that list, never by path. Markdown is rendered by a small built-in renderer that escapes everything, so no HTML from a report reaches the page. Every link keeps its target only when it is http(s), an anchor in the page, or another listed report. The private filter hides private reports and masks private lines in the others.
- 🎤 Answer keeps the keystroke rule. The words still come only from your own F9 clip, and only a clip whose recorder log entry shows it was pressed inside the minute you opened can take the arm. Arming adds the quoted question (one line, at most 200 characters, like the quote a typed answer carries) and the tick. Arming is a POST from the panel itself, behind the same Host, Origin and JSON checks as every other.
- Busy typing keeps the keystroke rule and the dialog guard. **Send anyway** is the same as Retry: it re-queues a line that is already in the inbox and takes no text. The screen check right before Enter still applies to every line, busy or idle.

## [0.1.0-alpha.1] - 2026-10-18

The first alpha, under the name bis-kit (never tagged). Windows only. Extracted from a private repo started 2026-10-01; fresh history.

### Added

- Hold-to-talk (F9 by default): recorded while the key is held, transcribed on the PC with faster-whisper, typed into the Claude Code session named `Main` (else the most recently active one). A line that starts with a live session's name and a comma goes to that session.
- Brain dumps (F2 toggle, or the panel's Dump button with a timer and level meter): local transcription, then one `claude -p` call under your own Claude Code login that returns cards (summary, themes, proposed actions, to-dos, questions).
- Dumps tab with the card deck. "Send to Claude" on an action (editable text, delivered as your own panel typing). To-dos go into `agenda.md` and questions into `questions.md`, checked against the open items first. One markdown report per dump in `reports\`.
- `py briandump.py dump <file>` turns an existing recording or text file into a dump.
- Panel: status strip with the current step, context meter, questions for you with buttons and a text box, agenda checklist, "needs you" strip with a chime, finished steps.
- Voice tab: the router's target and queue, with drop, retry and Open Main.
- Voice router (panel mode): one inbox consumer per PC, look-before-you-type gating (session idle, you off the keyboard, prompt on screen, no dialog), ConPTY keystroke delivery, and a second screen check right before Enter. Monitor mode as the fallback.
- Claude Code plugin `briandump`: SessionStart hook (context), Notification hook ("needs you"), commands `/briandump:ask` and `/briandump:agenda`. The installer adds it from the local checkout; `/plugin marketplace add KnockKnockGoose/BrianDump` works from GitHub.
- Window modes `edge` (default), `webview`, `none`; right-third placement on the monitor you launched from.
- CLI `py briandump.py launch | serve | ptt | doctor | config | ask | agenda | dump | mics | version`.
- Installer `Install BrianDump.cmd` (microphone, keys, logon task `BrianDump PTT`, plugin), `Uninstall BrianDump.cmd`, and `doctor` with a green checklist.
- One documented config file, `%LOCALAPPDATA%\BrianDump\config.json`, merged over `config.example.json`. State in `%LOCALAPPDATA%\BrianDump`, your files in `Documents\BrianDump`. Default port 8770.
- Sandbox mode (`BRIANDUMP_SANDBOX=1`) that records outward actions instead of doing them. Tests without model calls, keystrokes or a microphone. CI on Windows and Ubuntu with lint and plugin validation.
- Docs: README, `docs/how-it-works.md` (the design decisions), `docs/install.md` (the beginner path).

### Security

- The panel binds `127.0.0.1` and answers 403 to any address that is not loopback and not in `allow`.
- Other web sites are kept off the local API: the `Host` header must be exactly one of this PC's own names, `localhost` or an address (against DNS rebinding), POSTs must be JSON, and a POST with a foreign `Origin` is refused. The page is served with a content security policy and cannot be framed.
- A panel answer to a question a session asked is typed into that session only, and refused when it has ended.
- The keystroke rule: only your own F9 clips and your own panel typing can become keystrokes. Dump transcripts never reach the inbox, and an F9 file with no recorder log entry is typed with a visible "not verified" prefix.

[Unreleased]: https://github.com/KnockKnockGoose/BrianDump/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/KnockKnockGoose/BrianDump/releases/tag/v0.1.1
