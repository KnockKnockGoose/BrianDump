# Security

BrianDump is a local tool with a local web page and a keyboard hook. This page says what it touches, what it refuses to do, and how to report a problem.

## What runs on your PC

| Part | What it does | Boundary |
|---|---|---|
| The panel server | Python `http.server` on `127.0.0.1:8770`. Reads `~/.claude/projects` and `~/.claude/sessions` (never the `.key` files next to the session files). Reads and writes `%LOCALAPPDATA%\BrianDump` and your data folder (`Documents\BrianDump` by default). | Binds loopback. Any client address that is not loopback and not in `allow` gets 403. Other web sites are kept off the API: the `Host` header must name this PC (against DNS rebinding), a POST must be JSON, and a POST that carries an `Origin` must come from the panel's own page. Serves the page, its icon and a JSON API: no directory listing, and no file path comes from a request (dump and action ids are checked against a pattern before any path is built, and "open folder" takes a name from a fixed list). |
| The talk key | A low-level keyboard hook (pynput) that acts on two keys, F9 and F2 by default, and by default hides them from other apps while it runs. Records from the microphone only while the talk key is held or a dump is recording. | Never calls Claude: Whisper runs on your PC. Writes audio only to `dumps\<id>\audio.wav` in your data folder, and to `last_clip.wav` in the state folder if you turn on `ptt.keep_last_clip`. |
| The voice router | Types inbox lines into one console: the Claude Code session named `Main`, else the most recently active one (`AttachConsole` + `WriteConsoleInputW`). | Only your own inbox files (see the keystroke rule). Types only when the session is idle, you are off the keyboard, and no dialog is on screen; presses Enter only after checking the screen again. |
| The dump worker | One `claude -p` call per dump, started directly (no shell), the transcript on stdin, under your own Claude Code login. A malformed answer gets one more call with the errors. | The call has no tools, no MCP servers and no settings files (so no hooks or plugins), and it is killed after `dump.timeout_s`. Your account and your plan, about one message per dump. `dump.processor: mock` turns the call off. |
| The plugin | Two Claude Code hooks (SessionStart context, Notification to `needs_you.json`) and two slash commands. | Standard library only. Reads `config.json`, writes one state file, and swallows every error so it can never block Claude Code. |

Network traffic, in full: the installer's downloads (Python and Git for Windows if they are missing, the packages), the speech model, downloaded once from Hugging Face, and the call your own Claude Code makes for each dump. BrianDump sends nothing anywhere else. There is no BrianDump server.

## The `allow` list

`allow` in `config.json` is a list of extra client IP addresses that may open the panel. Loopback is always allowed. Keep it empty.

With the default `bind` of `127.0.0.1`, no other machine can even connect, whatever `allow` says. Opening the panel from another PC takes two changes: `bind` set to an address that PC can reach, and that PC's address in `allow`. Then anyone using that PC can type into your Claude Code session (through answers and Send to Claude), read its status, tick your agenda and read your dumps. There is no login on the page in 0.1. If you do it anyway, use one exact address on a private network, never a range, never a public network, and never forward the port through a router. Moving the window, opening folders or the browser, and Open Main still answer only the page on this PC.

## The keystroke rule

Only two kinds of inbox files ever become keystrokes: your own F9 clips and your own panel typing. Two endpoints write panel lines, `/api/answer` and `/api/dumps/<id>/send`, and both carry what you typed on the page. An answer to a question that a session asked is typed into that session and no other, so one agent's question cannot reach another session with your "yes" attached; when that session is no longer running, the answer is refused. No endpoint types text directly: `/api/voice` only drops or retries a line that already exists. Nothing BrianDump reads, such as a transcript, a dump card or a hook event, is typed on its own. The one thing it adds to your words is context: an answer line starts with `[panel answer to: <question>]`, quoting up to 300 characters of the question it answers from where that question came from (the session's transcript, `questions.json`, `questions.md` or a dump card), and it is typed only into the session that asked. Dump transcripts never go to the inbox. "Send to Claude" shows the action's text in an editable box first, and what you send is your own typing. An F9 file with no matching entry in the recorder's log is typed with a visible "not verified" prefix. **🎤 Answer** (0.1.1) arms one question for your next F9 clip, and it adds no new source of keystrokes. The words are still only your clip's. Only a clip with a recorder log entry, pressed within the minute you opened by clicking, can take the arm, and it takes it once. What the arm adds is the question as a quote in front (`[answer to: "..."]`, one line, at most 200 characters, like the quote a typed answer carries) and the tick afterwards. The answer goes where a typed answer to that question would go, never to a session named in the clip. Arming is a POST from the panel's own page, behind the same Host, Origin and JSON checks.

Why it matters: a channel that types into an agent's terminal can instruct, approve and deny. If another agent, a web page or a tool could feed it, that party could pose as you and approve its own actions. Any change that adds a new path from text to keystrokes is a security change and has to be argued for in the pull request.

A web page open in your browser counts as "this PC" too, which is why the server checks `Host`, `Origin` and the content type. Without those checks, any site you visit could post an "answer" and have it typed into your session. The page itself is served with a content security policy that lets it talk only to its own server, and it cannot be loaded inside a frame.

Also by design: `/api/open` accepts `http(s)` URLs only. `BRIANDUMP_SANDBOX=1` turns every outward action (keystrokes, terminal launches, browser opens, window messages, `claude -p`) into a log line, which is how the tests and QA run.

## Known limits

- **Programs that run as you.** Any program running under your Windows account can do what you can: call the local API, including the two endpoints that write panel lines, or put a file in the inbox folder. The keystroke rule covers the paths BrianDump builds. It is not a sandbox against other software on your PC. For an agent, Claude Code's permission prompts are the guard.
- **The keyboard hook** sees key events system-wide; that is what a global hotkey is. It acts on two keys, ignores the rest and logs none of them. Read `briandump/ptt.py` to check.
- **Your words stay on disk.** F9 lines are kept as text in `%LOCALAPPDATA%\BrianDump\inbox` and in the hold-to-talk log. Dumps keep their audio, transcript and cards in your data folder until you delete them (`dump.delete_audio_after_days` trims the audio). Session transcripts stay where Claude Code put them; the panel reads them and copies them nowhere.

## Reporting a problem

Please use GitHub's private vulnerability reporting on this repository: the **Security** tab, then **Report a vulnerability**. If that is not available to you, open an issue that says only "security, please contact me" and how to reach you, and keep the details out of the public issue. One person maintains this, so expect a first reply within a week. Fixes ship as a `0.1.x` release and are listed in `CHANGELOG.md`.

## Supported versions

Alpha: the latest `0.1.x` release only.
