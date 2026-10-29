# BrianDump architecture and module contract (v0.1)

This is the contract the modules are built against. Keep it current when an interface changes.

## Layout

```
BrianDump/
  briandump.py                 CLI shim -> briandump.cli.main()   (launch | serve | ptt | doctor | config | ask | agenda | dump | mics | version)
  config.example.json        THE defaults, with "_key" documentation entries (briandump/config.py merges the user's copy over it)
  briandump/
    __init__.py              __version__
    config.py                CONF: paths + settings + sandbox recorder (see its docstring)
    server.py                the panel server (stdlib http.server, loopback by default) + the dump worker thread
    panel.html, panel.ico    the page: Panel | Dumps | Reports | Voice tabs
    launch.py                opens the panel window on the right third of the monitor (Edge --app, or frameless.py)
    frameless.py             optional WebView2 host (pywebview + pythonnet)
    watchdog.pyw             optional: restarts the server if its port goes quiet (logon shortcut)
    sessions.py              Claude Code's live sessions: ~/.claude/sessions/<pid>.json + transcript Tail -> status rows
    voice.py                 the voice router: inbox file -> keystrokes into the target session (panel mode)
    watch_inbox.py           monitor-mode fallback: a session runs it as a background command and reads "VOICE ..." lines
    ptt.py                   hold-to-talk (F9) + dump toggle (F2): mic -> faster-whisper -> inbox file / dump folder
    ptt_service.pyw          runs ptt.py hidden (logon task "BrianDump PTT"), log in <state_dir>/logs/ptt-<host>.log
    list_mics.py             one line per input device (for the installer and `briandump.py mics`)
    transcribe.py            Transcriber class around faster-whisper (the one speech backend)
    dumps.py                 the dump store (<data_dir>/dumps/<id>/), the worker, report writer, import
    cards.py                 cards schema + prompt + `claude -p --json-schema` runner + validator + mock processor
    cards.schema.json        JSON Schema for the card deck
    agenda.py                markdown checklists: agenda.md and questions.md (parse, add with dedupe, tick)
    reports.py               the Reports tab: dump reports + reports.index / roots / discover -> rows, read marks, Open file
    mdhtml.py                a small Markdown -> HTML renderer (standard library; escapes everything, no raw HTML)
    answers.py               🎤 Answer: arm one question for the next talk clip; the clip takes it once; the tick after delivery
    cli.py                   the subcommands, including doctor
    fixtures/                sample-transcript.txt + sample-cards.json (neutral; the mock processor's deck)
  plugin/                    the Claude Code plugin "BrianDump": hooks (SessionStart, Notification) + /briandump:ask, /briandump:agenda
  .claude-plugin/marketplace.json   makes the repo a plugin marketplace: the installer registers the LOCAL checkout
                             (`claude plugin marketplace add <folder>`, then `claude plugin install briandump@briandump`);
                             `/plugin marketplace add KnockKnockGoose/BrianDump` works once the repo is public
  install.ps1, "Install BrianDump.cmd", uninstall.ps1, "Uninstall BrianDump.cmd"
  requirements.txt, requirements-dev.txt, pyproject.toml (ruff + pytest config)
  tests/                     pytest; no model calls, no real keystrokes, no real mic (fakes and fixtures only).
                             tests/conftest.py: every test runs in a throwaway sandboxed home; the `home` fixture re-points the
                             one shared CONF in place (never reload briandump.config); an autouse guard refuses any `claude -p` spawn
  docs/                      how-it-works.md, install.md, this file, flow.svg
```

## Directories at run time (`briandump/config.py`)

| | default | holds |
|---|---|---|
| `state_dir` | `%LOCALAPPDATA%\BrianDump` | `config.json`, `state/`, `inbox/`, `logs/`, `edge-profile/`, `webview/` |
| `data_dir` | `%USERPROFILE%\Documents\BrianDump` (the local folder on purpose, even when Explorer's Documents is cloud-backed: recordings stay on the PC and no sync client locks files mid-write) | `agenda.md`, `questions.md`, `reports/`, `dumps/<id>/` |

`BRIANDUMP_HOME=<dir>` puts both under `<dir>` (tests, QA sandbox). `BRIANDUMP_CLAUDE_DIR` relocates `~/.claude`. `BRIANDUMP_SANDBOX=1` turns every outward action into a line in `<state_dir>/side_effects.jsonl` (`CONF.record(kind, **detail)` returns True = skip the real action).

## State files (`<state_dir>/state/`)

| file | writer | reader | shape |
|---|---|---|---|
| `needs_you.json` | plugin `scripts/needs_you.py` (Notification hook) | server | `{"ts","message","type","session","transcript"}` (ts = UTC ISO with milliseconds, `...Z`); notices that need nobody are not written |
| `ptt_status.json` | ptt.py, every 0.25 s while recording, every 2 s otherwise | server `/api/ptt` | `{"pid","ts","ready","key","dump_key","device","model","recording","mode":"talk"|"dump"|null,"started","level_db","peak_db","transcribing":<dump id>|null,"last_error"}` |
| `ptt_command.json` | server (`POST /api/ptt`, only while the service is alive) | ptt.py polls every 0.3 s, acts once per `nonce`, deletes the file; a command older than 10 s is logged and dropped | `{"cmd":"dump_toggle"|"dump_start"|"dump_stop","t",<epoch>,"nonce":<str>}` |
| `voice_router.json` | voice.Router | server `/api/voice` | `{"items":[...],"host","ts"}` |
| `voice_watcher.json`, `voice_handled.txt` | voice.Router / watch_inbox.py | each other | lock `{"session","pid","t"}`; one consumed inbox file name per line |
| `answer_arm.json` | server (`POST /api/answer/arm`, `/api/answer/disarm`) | voice.Router (panel mode) or watch_inbox.py (monitor mode), which take it; server (`/api/state` `armed`) | `{"id","question","tick":"md"|"manual"|null,"session","dump","armed_at","expires_at","nonce"}`; `expires_at` = `armed_at` + 60 s. A verified clip whose key press (inbox stamp minus `seconds` and `transcribe_s` from its PTT log line) falls in [`armed_at` - 1.5 s, `expires_at`] takes it, once (the file is removed). The press decides, not when the router reads the file. A file 180 s past `expires_at` is removed |
| `questions.json` | `briandump.py ask` (Claude asking you via the plugin command) | server | `[{"id","text","kind","options","asked","done","session"?}]`; ids `q<n>`; `session` = the asking session (`CLAUDE_CODE_SESSION_ID`, else the registry record of `CLAUDE_PID`); closing one adds `answer` and `done_at` |
| `reports_read.json` | server (a report opened on the page, Open file, Mark all read) | server `/api/reports` | `{"<resolved path>": "<first opened, local ISO>"}`; keys compare case-insensitively on Windows; a file that is not a JSON object is moved to `reports_read.bad.json` and a new one started |

Every writer of these files goes through tmp + `os.replace` retried about 20 times (Windows refuses the replace while any reader holds the file: `config.replace_retry`); every reader reads in one call and treats a locked or half-written file as "no change", keeping the last good value.

## Inbox (`<state_dir>/inbox/`)

`<YYYYMMDD-HHMMSS>-<host>.txt` = one F9 clip (written by ptt.py after Whisper; a matching `{"at": "<stamp>", ...}` line goes to `log-<host>.jsonl`). `<stamp>-panel-<host>.txt` = something you typed on the panel (an answer, a Send-to-Claude text). Files are created exclusively (a same-second name steps to the next second). Host names may contain hyphens (the Windows default `DESKTOP-XXXX`), so a file is matched on the exact host, never by splitting the name. The voice router (panel mode) or watch_inbox.py (monitor mode) consumes this host's files exactly once: it marks a file handled before typing, delivers first-in-first-out, claims nothing during its 6 s start-up grace, and a second panel server's router stays passive (`snapshot()["passive"]`). A server started from a terminal (`serve`) never types: `FreeConsole` would take its own console away for good (measured), so it says so on the panel.

**Keystroke rule.** Only these inbox files ever become keystrokes: your own F9 clips and your own panel typing. No HTTP endpoint and no agent can hand the router text to type; `/api/voice` only drops or retries an existing line. Dump transcripts never go to the inbox. "Send to Claude" shows the action's text in an editable box first; what you send is panel typing. 🎤 Answer (answers.py) changes no source: the clip that takes the arm is typed as `[answer to: "<question>"] <its words>`, into the session that asked the question (else Main, never by a spoken name), and the question is ticked after delivery. The quote is the question's own text, looked up from its id (one line, at most 200 characters), like the quote a typed answer carries.

## Dump store (`<data_dir>/dumps/<id>/`)

`id` = `YYYYMMDD-HHMMSS-xxxx` (local time + 4 hex). Files: `audio.wav` (16 kHz mono int16), `meta.json`, `transcript.txt`, `cards.json`. The report goes to `<data_dir>/reports/<id>-<slug>.md`.

`meta.json`: `{"id","createdAt" (local ISO with offset),"updatedAt","durationSec","status","error","attempts","timings":{"transcribe_s","think_s"},"words","source":"ptt"|"import","label","processor","model","cost_usd","report":<path>,"sent":{<action id>: ts},"agenda_added":[...],"questions_added":[...],"host","worker","requeued","warning","calls","audioDeleted"}`

Status machine: `recorded` -> `transcribing` -> `transcribed` -> `thinking` -> `ready`, or `error` (message in `error`, one automatic retry, then `POST /api/dumps/<id>/retry`).

Claim rules (the recorder and the server are different processes): a claim is the next status written atomically before any work. The worker takes `transcribed` at once; `recorded` only when `source` is `import` and 5 s have passed (so `briandump.py dump --now` processes its own import), or when it is more than 2 minutes old; `transcribing` after 15 minutes untouched (the recorder claims its own dumps with its pid: while that process runs, an hour; at once when it has exited); a `thinking` dump after 30 minutes, or at once when the process that claimed it on this PC has exited. Dumps recorded on another PC (`host`) are left alone. One dump at a time.

Who does what:
- **ptt.py** records, writes `audio.wav` + meta `recorded`, flips to `transcribing` before returning, transcribes with its loaded model, writes `transcript.txt` + meta `transcribed` (`words`, `timings.transcribe_s`). Never calls Claude.
- **server.py dump worker** (`dumps.Worker`, a thread, scans every 2 s): `transcribed` -> `thinking` -> `cards.process()` -> `cards.json` -> report -> agenda/questions merge -> `ready`. It also transcribes `recorded` items itself (imports, or a PTT that died) with a lazily loaded `transcribe.Transcriber`, and retries `transcribing` items untouched for 15 min.
- **`briandump.py dump <file>`** imports an audio or text file as a new dump (status `recorded` / `transcribed`); `--now` processes it in-process (tests use `--processor mock`).

## cards.py

```
SCHEMA                 the schema dict (loaded from cards.schema.json; `$schema`/`$id` stripped for the CLI)
system_prompt(user_name=None) -> str      (or the text of dump.prompt_file)
user_prompt(transcript, recorded_at=None, duration_s=None) -> str
validate(obj) -> (ok: bool, errors: list[str], cards: dict|None)   # normalises: ids a1.., status proposed, effort, trims
extract_json(text) -> dict                                          # fences / chatter tolerated
run_claude(prompt, system, timeout_s, model=None, claude_cmd="claude") -> {"text","structured","cost_usd","ms"}  # spawns claude.exe directly, no shell; prompt on stdin
process(transcript, *, recorded_at=None, duration_s=None, processor="claude", log=print, effort=None, stats=None, **kw) -> cards dict (validated)
mock_cards() -> cards dict (fixtures/sample-cards.json, actions reset to proposed)
```
The call: `claude -p --output-format json --no-session-persistence --tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands --system-prompt <text> --json-schema <json>` (+ `--model`, `--effort` when set), the prompt on stdin, no shell, `CLAUDECODE` removed from the environment, killed on timeout; one "fix it" retry on a validation failure. In the sandbox the would-be call is recorded as `claude_p` in `side_effects.jsonl` and the mock deck is used. Measured once (2026-10-18, the fixture transcript, CLI default model): 18.7 s, $0.069, valid first time.
Cards keys: `executiveSummary`, `talkingPoints[{theme,bullets[]}]`, `actions[{id,title,deliverable,why,status,effort}]`, `nextPrompts[{question,topic?}]`, `project?`, `todos?[]`.

## agenda.py (agenda.md and questions.md)

Plain markdown checklists, human-editable: `- [ ] text` open, `- [x] text` done. Optional `## heading` filter.
```
parse(text, heading=None) -> [{"text","done","line","base"}]   # base = the text without BrianDump's "(from brain dump ...)" note
read(path, heading=None) -> items
add(path, texts, *, heading=None, suffix=None) -> added texts      # dedupe: normalised text equal, or one contains the other (both > 20 chars)
set_done(path, text, done=True) -> bool
norm(text) -> str
```

## HTTP API (server.py; JSON)

**Binding.** `bind` (default 127.0.0.1) and `port` (default 8770). Before binding, the server connects to `127.0.0.1:<port>`: if anything answers it prints one line and exits with code 3, because with SO_REUSEADDR Windows would let two servers share the port silently (measured). It binds without SO_REUSEADDR, and starts the voice router and the dump worker only after the bind succeeded.

**Guards on every request.** 403 for a client IP outside loopback + `allow`; 403 when the `Host` header is not an IP, `localhost` or exactly one of this PC's own names (its host name, or the full DNS name Windows records for it; never a name that merely starts with it), against DNS rebinding; 403 when a browser `Origin` is not the page's own; POST bodies must be `application/json` (415 otherwise, which forces a CORS preflight the server never answers, so no other web page can post); responses carry a CSP and `X-Frame-Options`. 503 means a module failed to import (`/api/machine` lists it) or a file stayed locked.

| method | path | body / result |
|---|---|---|
| GET | `/`, `/favicon.ico` | the page, the icon |
| GET | `/api/state` | `{version, session, project, activity{state: working|thinking|idle|needs, text, since}, steps[{text,ts,end,error}], context{used,limit,model}, questions[{id,text,kind,options,source,asked}], voice{mode,target,main,queued,last,error?}, ptt{alive,ready,recording,mode,started,seconds,level_db,key,dump_key,transcribing}, dumps{processing,transcribing,queued,thinking,ready,errors,newest,unavailable?} (the work in progress counts this PC's dumps only), reload, armed{id, text, left, listening, nonce, dump?, to?} | null}` (`left` = seconds to start answering; `listening` = a talk clip that started in time is being recorded) |
| GET | `/api/machine` | `{host, scale, window, title_bar, sandbox, version, data_dir, config_file, processor, chime, voice_router, key, dump_key, warnings, missing}` |
| POST | `/api/answer/arm` | `{"id","dump"?}` -> `{"ok","armed"}`: arms that question for the next talk clip (`answer_arm.json`); a second arm replaces the first. 404 when the question is not open; 409 for an AskUserQuestion card or a session that cannot get the answer (the same checks as `/api/answer`). Writes no inbox file |
| POST | `/api/answer/disarm` | `{"nonce"?}` -> `{"ok","disarmed"}`: Esc or Cancel on the page (with `nonce`, only that arm) |
| GET | `/api/agenda` | `{"items":[{"text","base","done","id","line"}]}` from agenda.md (open first; `line` = file order, so the page keeps a row it just ticked in place for its 2-minute undo) |
| POST | `/api/agenda` | `{"action":"add"|"done"|"undone","text"}` -> `{"ok", "added"?}` (done/undone by id or index; "(private)" is never written to the file) |
| POST | `/api/answer` | `{"id","text","dump"?}` -> inbox panel file `[panel answer to: <question>] <text>` (or `[BrianDump question: ...]` for a questions.md item, which is then ticked). A question a session asked (its transcript, or `ask` run from it) is answered into that session only: the file gets the routing line `#BrianDump {"session": "<id>"}` first, and the answer is refused with 409 when that session is not running (in monitor mode: when it does not hold the voice). An AskUserQuestion card (`source` `asked`) is refused with 409 too: it is a dialog in the terminal, which the router never types into |
| GET | `/api/voice` | router snapshot `{mode, host, main, fallback, queued, items[], lock, grace}` (monitor mode: `{"mode":"monitor"}`) |
| POST | `/api/voice` | `{"action":"drop"|"retry","id"}` or `{"action":"open_main"}` (loopback only; launches `claude --name <main_session>` in a new terminal) |
| GET | `/api/ptt` | the status file + `alive` |
| POST | `/api/ptt` | `{"action":"dump_toggle"}` -> writes the command file -> `{"ok","msg"}` |
| GET | `/api/dumps` | `[{id, createdAt, durationSec, status, error, summaryLine, project, counts{themes,actions,todos,questions}, report, host}]` newest first (the page keeps "seen" ids in localStorage) |
| GET | `/api/dumps/<id>` | meta + `transcript` + `cards` + `questions` + `todo_added` |
| POST | `/api/dumps/<id>/retry` | re-queue an `error` dump |
| POST | `/api/dumps/<id>/send` | `{"action_id","text"}` -> inbox panel file (text as edited by the user), meta.sent updated |
| POST | `/api/dumps/<id>/agenda` | `{"text"}` or `{"index"}` -> agenda add -> `{"ok","added"}` |
| POST | `/api/open` | `{"url"}` http(s) only, loopback only -> default browser |
| POST | `/api/open-folder` | `{"what":"data"|"reports"|"dumps"|"state"|"config"}` loopback only -> Explorer on that known folder (no path from the client) |
| POST | `/api/window` | `{"action":"min"|"max"|"close"|"drag"}` loopback only |
| POST | `/api/reload` | bumps `reload` so open pages refresh |
| GET | `/api/reports` | `{items:[{id, title, date, by, where, path, source: dump|index|found, ext, viewable, unread, opened_at, private}], unread, warnings[], configured}` newest first; `path` is relative to its root or the data folder (no absolute path leaves the server); `?refresh=1` searches the roots again now |
| GET | `/api/reports/<id>` | the row + `html` (the rendered report with a byline, or null) + `note` (why there is none: private, another file type, too large, unreadable) |
| POST | `/api/reports/<id>/read`, `/api/reports/all/read` | marks read -> `{"ok"}` / `{"ok","marked"}` |
| POST | `/api/reports/<id>/open` | loopback only: the listed file in its default app (Explorer with it selected when no app is set) -> `{"ok","msg"}`; marks it read |

Dump ids and action ids are validated with `^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z` / `^[A-Za-z0-9_-]{1,32}\Z` before any path is built; report ids with `^[0-9a-f]{12}\Z` before the listing is consulted, and an id that is not in it is 404. In sandbox mode window, open, open-folder, open_main and a report's Open file only record a side effect.

## reports.py and mdhtml.py (the Reports tab)

```
reports.listing(refresh=False) -> {"items", "unread", "warnings", "configured"}
reports.view(id) -> row + {"html", "note"} | None         reports.mark_read(id) -> True | None
reports.mark_all_read() -> int                              reports.open_file(id) -> (ok, msg) | None
reports.rid(path) -> 12 hex digits (sha1 of the resolved path, case-folded on Windows)
mdhtml.render(text, link=None, private=None, id_prefix="") -> HTML fragment
mdhtml.first_heading(text) -> the first "# " heading without markup | None
```

**Sources**, merged and de-duplicated by resolved path, in this order (so an index entry's title, date and author win over a found file's): the brain-dump reports `<data_dir>/reports/*.md` (always); `reports.index`, JSON files of `[{"path", "title"?, "date"?, "by"?, "project"?}]` or plain path strings (BOM accepted; relative paths are tried under each root in turn; `~` and `%VARS%` expand); `reports.discover`, a multi-line regex searched in the first 20 lines (4 KB) of every `.md` / `.markdown` file under `reports.roots`. The walk skips hidden folders, `node_modules`, `__pycache__`, `venv`, `env` and `site-packages`, never walks into a symlink or a junction, never reads a cloud-only placeholder (`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`, `RECALL_ON_OPEN` or `OFFLINE`: reading it would download it), stops with a warning after 200,000 files or 20 s, and is cached 5 minutes. Listed types: `.md .markdown .txt` (rendered) and `.pdf .html .htm .docx .doc .odt .rtf .xlsx .csv .pptx` (Open file only). Title: the index's, else the first `# ` heading, else the file name. Date: the index's, else a date in the file name (a dump id counts), else the first date in the first 1,500 characters, else the modified day. Order: date, then modified time, newest first.

**Containment.** A path is looked at only if, as written, it lies inside the data folder or a root (either spelling: as configured, or resolved), so nothing outside is even resolved; then its resolved path must lie inside one of them too, which refuses `..`, absolute paths elsewhere, and symlinks and junctions that lead out. The page can name a report only by its id, and an id is looked up in a fresh listing.

**Settings.** `CONF.reports_index`, `CONF.reports_roots` (lists of paths as written) and `CONF.reports_discover` (compiled with `re.M`, or None) are read in config.py: a value of the wrong kind or a pattern that does not compile gives the default and a line in `CONF.problems` (doctor's Settings row). A root that is not there, an index file that is missing or not a JSON list, and entries outside the roots are warnings on the tab. None of them stops the panel.

**Rendering.** `mdhtml` covers ATX headings (with ids, prefixed `r-` on the panel so they never clash with the page's own), paragraphs, hard breaks, bold, italic, strike, code spans, fenced code, links, autolinks and bare URLs, images (as their alt text), nested bullet, numbered and task lists, blockquotes, tables with alignment, rules, backslash escapes, and front matter (dropped). It escapes every character of the source and writes only its own tags, plus `<br>` for a literal `<br>` in the source. A link keeps its target only when it is http(s), an anchor, or another listed report (`data-report`); anything else becomes text with the target as its tooltip. Emphasis spans are bounded (1,000 characters) so every pattern runs in linear time; a block over 20,000 characters, every block after 3 s, and lists or quotes nested deeper than 12 are shown as escaped text. A row whose title, author or path matches `private_filter` is `(private)` and is not rendered; in other reports a matching heading, paragraph, list item, table cell or code line is replaced by `(private)` (a masked heading's id says nothing about its text). The page prints a report alone through `@media print` (white paper, 10pt serif, the browser's paper size), so Save as PDF needs no headless browser.

## CLI (`briandump/cli.py`)

`doctor [--json]` runs about 18 read-only checks in parallel against a deadline; `--json` prints `[{"name","ok","detail","fix"}]` with `ok` true / false / null (null = info: optional or not applicable, never affects the exit code); exit 1 when a required check fails. `agenda add|done|undone|list`, `ask "q" [--options "a|b"] | --done ID [--answer ...] | --list`, `config [--init|--open|--show|--path|--set KEY VALUE ...]`, `dump <file> [--now] [--processor claude|mock]`.

## Plugin (`plugin/`)

`.claude-plugin/plugin.json` name `BrianDump`. `hooks/hooks.json` uses the exec form (`"command": "py", "args": [...]`, no shell, so paths with spaces are safe; needs Claude Code 2.1.139 or newer): SessionStart -> `scripts/session_start.py` (emits `additionalContext`: in panel mode "lines from your F9 key and the BrianDump panel arrive as normal input"; in monitor mode the exact background command to arm `watch_inbox.py`), Notification -> `scripts/needs_you.py`. Both are stdlib-only and swallow every error. Commands `/briandump:ask "question" [--options a|b]` and `/briandump:agenda add "item"` tell Claude to run `py "<repo>/briandump.py" ask ...` / `agenda add ...`; the repo path comes from `install_dir` in `config.json` (the installer writes it) or the plugin falls back to asking.

## transcribe.py

```
class Transcriber:
    def __init__(self, model="small", hotwords=(), device="cpu", compute_type="int8"): ...   # loads lazily on first use
    def load(self) -> float                      # seconds the model took to load (cached after the first call)
    def transcribe(self, audio, *, vad_retry=True, on_segment=None) -> {"text": str, "seconds": float, "took_s": float, "words": int}
        # audio = a float32 numpy array at 16 kHz, or a path to a .wav/.m4a/.mp3 file (faster-whisper decodes files itself)
        # VAD on first; if the text comes back empty, once more with VAD off (quiet speech); hotwords via initial_prompt
```
One model per process: ptt.py keeps one loaded for F9 and dumps; the server loads one lazily only when it has to transcribe an imported or orphaned dump itself.

## ptt.py (the only process that touches the microphone)

- Hold `ptt.key`: record, release -> resample to 16 kHz, normalise, Whisper, inbox file + log line. Clips shorter than 0.4 s or quieter than -46 dBFS are ignored (one low beep).
- Press `ptt.dump_key` (toggle) or `POST /api/ptt {"action":"dump_toggle"}` (the command file): start a dump recording (two rising beeps); press again (or the command, or `dump.max_minutes` elapsed) to stop (two falling beeps). The audio goes to `<data_dir>/dumps/<id>/audio.wav` with `meta.json` (`recorded` -> `transcribing`), then the loaded model transcribes it in a background thread -> `transcript.txt` + meta `transcribed`. A dump transcript never goes to the inbox.
- F9 while a dump is recording: ignored with a low beep (one microphone, one recording at a time).
- Status file every 0.25 s while recording (level in dBFS from the last 250 ms), every 2 s otherwise; `ready` only once the keyboard hook is in; `key` is comma-joined when several talk keys are set.
- One recorder per BrianDump home (a second one exits). Talk clips are typed in the order spoken. A dump whose audio has no recognised speech ends as `error`.
- Self-test (2026-10-18, keys F7/F6 under a temp home, the TTS clip through the speakers): a 22.3 s talk clip took 3.5 s in Whisper; a 22 s F6 dump transcribed 61 words in 2.9 s; a command-file dump ran and a replayed nonce was ignored; 520 status samples, peak -11.7 dBFS.
