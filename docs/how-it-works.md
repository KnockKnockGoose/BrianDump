# How BrianDump works

Seven decisions shape BrianDump. Each one gives something up, and the trade is stated with it.

```
F9 clip --------+
panel answer ---+--> inbox\ --> voice router --> typed into your Claude Code session
Send to Claude -+

F2 dump --> dumps\<id>\audio.wav --> Whisper --> claude -p --> cards, report, agenda.md, questions.md

~\.claude\projects\*.jsonl --> transcript reader --> panel: status, steps, context, questions

reports\*.md + your reports (roots, byline, index) --> listed by id --> rendered, escaped --> Reports tab, Save as PDF
```

## 1. Zero instrumentation

Claude Code already writes each session to `~/.claude/projects/<project>/<session>.jsonl` and keeps a small file per running process in `~/.claude/sessions/`. The panel reads those files and nothing else: no hooks into Claude Code's internals, no proxy in front of the API.

The reader is incremental. It keeps its byte offset and a partial last line, so each byte is parsed once. A line over 200 kB, such as a pasted screenshot, is never JSON-decoded; its tool id comes from the first 5 kB. An earlier version read only the end of the file, and it broke once screenshots filled the transcript.

The trade: the transcript format is not a public API, so the parser skips lines it does not understand.

## 2. One input channel

Everything that reaches a session is a small text file in the inbox: `<stamp>-<host>.txt` for an F9 clip, `<stamp>-panel-<host>.txt` for a panel answer or a Send to Claude. An answer to a question that one session asked carries that session's id on a first line, and the router types it into that session or nowhere: one session's question never reaches another with your "yes" attached. One path means the session needs no new API and the delivery code exists once.

## 3. Hold to talk, not a wake word

A wake word keeps the microphone open, fires by mistake in conversation, and guesses where a command ends. A held key gives a clear start and end, and letting go is the "send".

Measured on the author's desktop CPU (int8): `small` loads in 1.8 s and transcribes a 21.9 s clip in about 3 s, a real-time factor of 0.14 (about 7 times faster than real time). `base.en` loads in 0.5 s and takes 1.0 s (about 22 times). The model stays loaded, so a five-second line takes under a second.

Dumps use a toggle, because nobody holds a key for ten minutes. A 10-minute dump on `small` should take roughly 1.5 minutes to transcribe, an estimate from the same factor.

## 4. One consumer per PC, and look before you type

A router inside the panel server owns the inbox. It claims each file once and delivers in order, and only when:

- the session is not waiting for you: no permission prompt or question, by its transcript, by Claude Code's own registry flag (`waiting`) and by its screen;
- you have not touched the keyboard or mouse for two seconds (`GetLastInputInfo`);
- the console shows the input line and no dialog (trust question, permission box, question).

A busy session is typed into anyway (`voice.busy_typing`, on by default). Claude Code queues a line typed mid-turn: the transcript records an `enqueue` at once, and the model gets the line with the next tool result, or as the next turn when the turn ends first. Measured in a sandbox on 2.1.302, 20 of 20 lines arrived verbatim without interrupting the running task; the same check on 2.1.304 queued the line in 0.4 s and handed it over 3.9 s later. Waiting for idle instead held lines for minutes behind long turns, so they landed late and out of context. A busy session's input line must be empty, so a half-typed draft of yours is never extended and sent.

Each line ends in one signal on the panel, a short sound and a toast: delivered, queued with the reason, or not sent. A line still waiting `voice.stale_s` (120 s) after you said it is never typed on its own; it waits for **Send anyway** or **Drop**. The age counts from the clip's file time, so a restart's catch-up cannot type a ten-minute-old sentence either.

Delivery uses the console API, not simulated key presses: `AttachConsole(<claude.exe pid>)`, then `WriteConsoleInputW` into that console's input buffer. It works under Windows Terminal because ConPTY owns the buffer, and it needs no focus. The text goes in without Enter. The router reads the screen again and presses Enter only if the words are in the input line and no dialog appeared, because a permission dialog that opened meanwhile would take that Enter as "approve". The line counts as delivered once it shows up in the transcript.

When the server starts and no session named Main is running, it opens one (`voice.auto_open_main`), once per sign-in: the logon session's id goes in `stateuto_main.json` before the open, so a restart never reopens a Main you closed.

The server runs windowless (`pythonw`), because `AttachConsole` needs a process without a console. The fallback, monitor mode, has each session run `watch_inbox.py` as a background command instead.

## 5. The keystroke rule is a threat model

A channel that types into an agent's terminal can instruct, approve and deny. If a web page, a dump card or another agent could feed it, that party could pose as you and approve its own actions. So only your F9 clips (checked against the recorder's log; an unlogged file gets a visible "not verified" prefix) and your panel typing become keystrokes. `/api/voice` can drop or retry a line (Send anyway on a stale line is a retry) and has no text parameter. Send to Claude shows the action in an editable box first. Dump transcripts never enter the inbox. 🎤 Answer keeps the rule: arming a question only lets your next F9 clip, one the recorder logged and you pressed inside the minute, carry that question as a quote in front of your words, as a typed answer does. Then the question is ticked.

The server binds 127.0.0.1 and answers 403 to addresses not in `allow`. Loopback is not enough on its own, since a web page in your browser connects from 127.0.0.1 too: the `Host` header must be exactly one of this PC's own names or an address (against DNS rebinding, whose page arrives under a domain such as `<pc-name>.attacker.example`), and a POST must be JSON from the panel's own origin. Dump and action ids must match a pattern before any path is built, and no request supplies a path. The rule covers what BrianDump builds; it cannot stop a program that already runs as you.

## 6. The dump pipeline: local speech, one model call, markdown first

The pipeline was ported from Node to Python, so the install needs one runtime. The recorder transcribes `audio.wav` with the model it already has loaded. A worker thread in the panel server then makes one `claude -p` call: no tools, no MCP servers, no settings files, the transcript on stdin. `--json-schema` makes the answer match the card schema, a validator normalizes it, and a malformed answer gets one retry with the errors.

Then everything becomes plain files: `cards.json`, a markdown report, to-dos in `agenda.md` and questions in `questions.md`. An item counts as a duplicate of an open one when the text is equal after normalizing, or when both are over 20 characters and one contains the other. The panel is only a viewer.

## 7. Reports: read in place, never raw

Claude writes reports as Markdown files spread over project folders. The Reports tab gathers them: each dump's report, plus whatever `reports.roots`, a byline search (`reports.discover`) and index files (`reports.index`) add. It shows them newest first with a dot until opened, and renders one inside the panel. Three choices keep that safe on a page that can type into your session:

- **The page never names a file.** The server lists the reports itself and the page asks for one by id, a hash of its path. A file must lie inside a configured root or the data folder both as written and once symlinks and junctions are followed, so a `..`, a link out of the folder or a forged id reaches nothing.
- **A small renderer of our own instead of a Markdown library.** It covers what reports use (headings, tables, lists, code, links, quotes) and escapes every character before adding only its own tags, so a `<script>` in a report is text. Common libraries pass raw HTML through by default and would need that turned off and audited; about 450 lines that a fuzz test checks against an allow-list of tags and attributes is less to trust. The trade: some Markdown corners (setext headings, reference links, footnotes) are not rendered.
- **Save as PDF is the browser's print.** A print stylesheet shows the open report alone on white paper in the same academic style. It needs no headless browser and no install, and the user picks the paper size and file name in the dialog.

The search reads only the first 20 lines of Markdown files, skips `node_modules` and the like, and never opens a cloud-only placeholder, which would download the file just to read its byline.

## Windows window details

- `SetWindowPos` needs `argtypes` under 64-bit Python; without them `HWND_NOTOPMOST` (-2) goes through as a 32-bit int and the call fails silently.
- The launcher is per-monitor DPI aware and fits the visible window (the DWM frame bounds), several times over, because Edge restores its saved size after loading.
- The frameless window keeps a thick frame for Snap and answers `WM_NCCALCSIZE` with the whole window as client area, so no frame is drawn.

## What would change next time

- **Terminal first from day one.** An earlier version had its own message feed, a compose box and a model that sorted every voice line. All of it went: the panel should show only what a terminal is bad at.
- **Check right before the keystroke that commits.** A check made before typing goes stale while typing.
- **Build the sandbox first.** Every outward action goes through one recorder that can log it instead of doing it, which made the tests and QA cheap.
- **Measure on the slowest PC you mean to support.** These numbers come from a desktop; a weak laptop is still unmeasured.
