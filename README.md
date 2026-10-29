# BrianDump

A side panel and a push-to-talk key for Claude Code: talk instead of type, dump your thoughts and get cards back, see what Claude is doing and answer its questions.

[![CI](https://github.com/KnockKnockGoose/BrianDump/actions/workflows/ci.yml/badge.svg)](https://github.com/KnockKnockGoose/BrianDump/actions/workflows/ci.yml)
![Windows 10 and 11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

<!-- MEDIA: the 20-second GIF goes here (capture 2 below). Real screen captures only, never AI-generated media. -->

<!-- MEDIA: the five real captures. Record them on a fresh Windows account so no personal project names show.
  1. The panel on the right third of the screen, beside the Claude Code terminal, while Claude works.
  2. GIF, about 20 s: hold F9, say "say hello", let go. The line lands in the terminal and the panel's status flips to working.
  3. A question card on the panel: click an option and Claude carries on.
  4. The red "needs you" strip while Claude waits on a permission.
  5. `py briandump.py doctor` with every line green.
-->

> **This is an alpha, and I'm new at this.** I'm a chemical engineer who started writing software with Claude Code in April. I built this so I could talk to my computer instead of typing at it, and it runs on my two Windows machines every day. It has never been installed anywhere else. Expect rough edges, a 460 MB Whisper download, and a keyboard hook your antivirus may side-eye (it listens for two keys; the code is right there). If it breaks, open an issue and tell me what you said to it.

## What it is

BrianDump sits next to Claude Code on a Windows PC. It has four parts:

- **A side panel.** A narrow window on the right third of your screen. It shows what Claude is doing, how full its context is, the questions it is waiting on, your agenda and your brain dumps.
- **A talk key.** Hold F9, speak, let go. Your words are typed into your Claude Code session, as if you had typed them.
- **A brain-dump key.** Press F2, talk for as long as you like, press F2 again. The recording is transcribed on your PC and turned into a card deck: a summary, themes, proposed actions, to-dos and questions.
- **A small Claude Code plugin.** It lights the panel when Claude needs you, and it lets Claude put a question on your panel or an item on your agenda.

## What it is not

- **Not an Anthropic product.** It is an independent tool that works alongside Claude Code.
- **Not a cloud service.** It reads files on your PC: the session transcripts Claude Code writes to `~/.claude/projects` and its list of running sessions in `~/.claude/sessions`. Apart from installing (Python packages and a one-time download of the speech model), the only thing that leaves your PC is the call your own Claude Code makes for each brain dump.
- **Not a way for software to type into your session.** Only your own words become keystrokes: what you say with the talk key and what you type in the panel.

## Ethos

| Principle | What it means for you |
|---|---|
| **Talk, don't type** | Voice is the main input. Hold F9 for a quick instruction. Start a dump when you have a lot to say. |
| **Two speeds** | *Directing:* F9 goes to the live session and you see the answer in seconds. *Dumping:* talk for 3 to 20 minutes and get cards back later. |
| **Local first** | Speech is transcribed on your PC with faster-whisper. Audio never leaves your PC. The panel serves only 127.0.0.1. |
| **Zero instrumentation** | The panel reads files Claude Code already writes. Nothing is patched or proxied. |
| **Bring your own Claude** | BrianDump uses your Claude Code login. No API key, no server, no shared account. |
| **Only your words become keystrokes** | Text is typed into a session only from your own voice clips and your own typing in the panel. Nothing BrianDump reads (a transcript, a dump card, a hook event) is typed on its own: an answer only quotes the question it answers, and goes back to the session that asked it. |
| **Readable over packaged** | Plain Python scripts and a `.cmd` installer, not an unsigned `.exe`. You can read the code that installs a keyboard hook before you run it. |
| **Readable output** | Large, high-contrast text and table-first cards. Your outputs are plain markdown files that open without BrianDump. |

## Two speeds

![Two speeds. Top, the brain dump: press F2 and talk for minutes, Whisper transcribes on your PC, one claude -p call organizes it into reports, questions and to-dos, and you read them; a build goes to agents and a QA check, research questions are answered by voice with F9. Bottom, live: hold F9 and talk, Whisper, typed into your session, Claude acts, you read the answer in the terminal.](docs/flow.svg)

Talking to an AI is easy. Making it useful means reading what comes back and answering it, and the two keys split that work: F2 for everything you know about a project at once, F9 for the next instruction and for answering the questions a dump leaves you.

**Directing: hold F9.** For a quick instruction to the session you are working in. You get the answer in the terminal, in seconds.

```
F9   hold -> speak -> let go
     -> Whisper transcribes it on your PC -> one small file in the inbox
     -> waits until you are off the keyboard and nothing on screen asks you something
     -> typed into the session named "Main" (else your most recent session) -> Enter
        (busy? Claude Code queues it and reads it after the current step)
```

Every line ends in a short sound and a note at the bottom of the panel: delivered, or queued and why. A line that waited more than two minutes is not typed on its own; the panel shows it with **Send anyway** and **Drop**.

Start a line with a session's name and a comma to send it there instead: "Docs, fix the heading" goes to the session named Docs.

**Dumping: press F2, talk, press F2.** For thinking out loud about a project, for minutes. You read the result when it suits you.

```
F2   press -> talk for minutes -> press again
     -> Whisper transcribes the recording on your PC, in the background
     -> one `claude -p` call under your login turns it into cards
     -> the Dumps tab, a report in reports\, to-dos in agenda.md, questions in questions.md
```

Timing, measured on the author's desktop CPU with the default `small` model: it loads in 1.8 s and transcribes a 22-second clip in about 3 s, about 7 times faster than real time. At that rate a 10-minute dump takes roughly 1.5 minutes to transcribe (an estimate, not a measurement). `base.en` loads in 0.5 s and takes 1.0 s for the same clip, about 22 times faster than real time.

**Answering by voice: 🎤 Answer.** Every open question on the panel, and every question in a dump's deck, has a **🎤 Answer** button. Click it, then hold F9 within a minute and say your answer. That one clip is typed as `[answer to: "<the question>"] <your words>` into the session that asked (Main for a question from a dump), and the question is ticked. A strip at the top of the panel shows what is armed and the seconds left. Esc or Cancel disarms it, and so do the timeout and arming another question (the last one wins).

One microphone and one speech model do both jobs, one at a time: F9 is ignored while a dump records, and an F9 line waits while a long dump is being transcribed.

## Requirements

- Windows 10 or 11.
- Python 3.11 or newer. The installer offers to install Python 3.13 if there is none.
- Claude Code 2.1.139 or newer, installed and logged in with your own account (a Claude plan or an API key). The plugin's hooks need that version.
- Git for Windows, recommended. Claude Code's Bash and Monitor tools use it, and BrianDump's `monitor` voice mode runs through them. Without it, Claude Code runs commands in PowerShell, and the default panel mode works the same.
- A microphone. Whisper listens for English by default. For another language, set `ptt.language` to its code (such as `de`) or to `auto`, and keep a multilingual model such as `small` (the `.en` models know only English).
- About 1 GB of disk, including the speech model: `small` is about 460 MB. On a slow PC use `base.en` (about 140 MB) or `tiny` (about 75 MB).
- Microsoft Edge, which comes with Windows, for the panel window.

Tested with Claude Code 2.1.302, Python 3.14 and Windows 11.

## Install from Claude Code (easiest)

Already in a Claude Code session you are logged in to? Paste this one sentence to Claude:

```
Install BrianDump from https://github.com/KnockKnockGoose/BrianDump: clone it to my user folder (not Documents\BrianDump), read install.ps1 first, then run it with -All (it takes several minutes, so allow a 10-minute timeout). Use my default microphone.
```

What Claude does:

1. Clones the repository into your user folder (`git clone https://github.com/KnockKnockGoose/BrianDump.git`), or downloads and extracts the ZIP when Git is missing. Not into `Documents\BrianDump`: that is where BrianDump keeps your files.
2. Reads `install.ps1`, then runs it: `powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -All`. It ends with `py briandump.py doctor`, and Claude tells you about any red line.

Claude's commands have no keyboard to answer the installer's questions, so each question takes its default: the Windows default microphone, F9 to talk, F2 for a brain dump and the `small` speech model (about 460 MB). Want something else? Say so in your sentence and Claude adds the flag:

| Flag | What it changes |
|---|---|
| `-Mic <part of a microphone name>` | The microphone. `-Mic default` = whatever Windows uses. |
| `-Key <key>` | The talk key: `f1` to `f24`, `scroll_lock`, `pause`, `insert`, `home`, `end`, `page_up` or `page_down`. |
| `-DumpKey <key>` | The brain-dump key, from the same list. |
| `-Model tiny`, `base` or `small` | The speech model. `base` (about 145 MB) suits a slow PC. |
| `-Webview` | Also installs the frameless panel window (pywebview). |
| `-DryRun` | Says what each step would do and changes nothing. Ask for it first if you want to see the plan. |

**Claude asks before it acts.** Expect a permission prompt for the clone and one for the installer. The installer is plain PowerShell, so you or Claude can read every step before it runs.

**Afterwards:** the installer starts the panel server, and a few seconds later the server opens the session your voice goes to, Main, in a new Windows Terminal tab (`voice.auto_open_main`). Hold F9 in any window, say "say hello", let go: the words land in Main. The plugin loads only when a session starts, so if that tab appeared before the installer finished, close it once and click **Open Main** on the panel (**BrianDump** in the Start menu) for a fresh one. No tab at all? Start it yourself in a terminal: `claude --name Main`.

**1M context window?** The panel's context meter counts up to 200k tokens. If your account has the 1M window, ask Claude to run `py briandump.py config --set context_limit 1000000` in the BrianDump folder.

## Quickstart (5 minutes)

Step-by-step with what you will see on each screen: [docs/install.md](docs/install.md).

1. **Get Claude Code working first.** Follow Anthropic's [setup page](https://code.claude.com/docs/en/setup), type `claude`, log in, and come back when it answers.
2. **Download BrianDump.** On this page click **Code**, then **Download ZIP**. Right-click the ZIP and choose **Extract All**. Or clone it from your user folder: `git clone https://github.com/KnockKnockGoose/BrianDump.git`. Keep the code out of `Documents\BrianDump`: that is where BrianDump keeps your files.
3. **Run the installer.** Double-click `Install BrianDump.cmd`. If Windows says "Windows protected your PC", click **More info**, then **Run anyway**. Windows shows that warning for unsigned scripts from the internet, and this one is not signed. You can open `install.ps1` in Notepad and read it first.
4. **Answer its questions.** First it asks which parts you want: say yes to all four. The last one starts the panel server every time you sign in. Keep it: in the default panel mode that server is what types your F9 words, so Enter means yes. Later it lists your microphones and asks for the talk key (F9) and the dump key (F2). On a laptop, F8 or Scroll Lock may work better.
5. **Connect Claude Code.** The installer adds the plugin for you: it registers your BrianDump folder as a plugin marketplace and installs `BrianDump` from it. If that step fails, it copies two lines to your clipboard. Paste them into Claude Code:
   ```
   /plugin marketplace add C:\path\to\your\BrianDump-folder
   /plugin install briandump@briandump
   ```
   Or add the marketplace straight from GitHub: `/plugin marketplace add KnockKnockGoose/BrianDump`. Start a new Claude Code session afterwards: hooks load when a session starts.
6. **Check.** The installer ends with `py briandump.py doctor`, a checklist that should be all green. Each red line comes with a one-line fix.
7. **Try it.** Open **BrianDump** from the Start menu: the panel opens a session named Main for you in a new terminal tab (or start one yourself in a project folder: `claude --name Main`). Hold F9, say "say hello", let go. Then press F2, talk for a minute about something you are planning, press F2 again and watch the Dumps tab.

## What you get

Plain files that are yours, in `Documents\BrianDump` inside your user folder (`%USERPROFILE%\Documents\BrianDump`). The panel is only a viewer.

| File | What is in it |
|---|---|
| `agenda.md` | A markdown checklist (`- [ ] item`). Dump to-dos land here, checked against the open items so nothing is added twice. Ticking an item on the panel edits the file. |
| `questions.md` | Open questions for you, from your dumps. Answering one on the panel, typed or with 🎤 Answer, ticks it. |
| `reports\<id>-<slug>.md` | One readable report per dump: summary, themes, actions, to-dos, questions and the transcript. The Reports tab lists them. |
| `dumps\<id>\` | The raw dump: `audio.wav`, `transcript.txt`, `cards.json` and `meta.json`. |

If Windows backs up your Documents folder to the cloud, the Documents folder Explorer shows is a different one. BrianDump keeps your files in the local folder on purpose, so your recordings stay on this PC. The **Open reports folder** button on the Dumps tab takes you there, `py briandump.py config --show` prints the path, and `data_dir` moves it.

The panel has four tabs:

- **Panel.** A status strip shows what Claude is doing, and for how long. Below it: the context meter, the **Dump** button with a timer and a level meter, **Questions for you**, your **Agenda** and the finished **Steps**. A question has option buttons, a text box and **🎤 Answer** (click it, then hold F9 and say it), and your answer is typed into the session that asked it (the card names it). A question Claude puts up as a dialog in the terminal shows its options too, but you answer it there: the panel never types into a dialog. The strip turns red, with a chime, when Claude needs you.
- **Dumps.** Every brain dump, newest first. Open one to read its cards. **Send to Claude** on an action opens an editable box, and what you send is typed into your session as your own words. **Add to agenda** puts a to-do on your agenda. Each question has its own answer box. The transcript is folded at the bottom, and a failed dump shows its error and a **Retry** button.
- **Reports.** Every Markdown report, newest first, with a dot until you open it: the report of each brain dump, and, once you set `reports` (see [Configuration](#configuration)), the reports Claude writes for you in your project folders. Click one to read it right on the panel, laid out like a paper: serif text, plain hairline tables, on light or dark paper. **Save as PDF** opens the print dialog, where you pick "Save as PDF" as the printer. **Open file** opens the file in its own app. A link to another listed report opens that one.
- **Voice.** Where each spoken line went: queued, typed or delivered, with drop and retry, and **Send anyway** / **Drop** for a line that waited too long. **Open Main** starts the Main session if it is not running.

The plugin gives Claude two commands. `/briandump:ask "question" --options "a|b"` puts a question on your panel, and your answer comes back to the session that asked, as a normal message. `/briandump:agenda add "item"` adds an item to `agenda.md`.

You can also turn an existing recording or text file into a dump: `py briandump.py dump memo.m4a`.

## Configuration

Settings live in `%LOCALAPPDATA%\BrianDump\config.json`, a copy of `config.example.json` made on first run. `py briandump.py config --open` opens it in your editor, and `py briandump.py config --set ptt.key f8` changes one value. After a change, restart the panel and the hold-to-talk service: signing out of Windows and back in does both. `null` means "use the default". If the file has a mistake in it, such as a stray comma, BrianDump runs on the defaults, `doctor` names the line, and neither `config --set` nor the installer writes over it until you fix it. A single value it cannot use, such as a port in words, falls back to its default, and `doctor` says which.

**Panel and server**

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | `null` | Where your files go: `agenda.md`, `questions.md`, `reports\` and `dumps\`. `null` = `%USERPROFILE%\Documents\BrianDump`, which stays on this PC. |
| `port` | `8770` | The panel's local port. If another program uses it, pick another: `py briandump.py config --set port 8771`. |
| `bind` | `"127.0.0.1"` | The address the server listens on. 127.0.0.1 means this PC only. Keep it. |
| `allow` | `[]` | Extra client IP addresses that may open the panel. Loopback is always allowed. Anyone who can reach the page can type into your Claude session, so keep this empty unless you need it (read [SECURITY.md](SECURITY.md) first). |
| `context_limit` | `200000` | Tokens the context meter counts up to: 200000 for most plans, 1000000 if your account has the 1M window. |
| `needs_you_chime` | `true` | A two-note chime when Claude needs you, and the short delivered / queued sounds for your F9 lines. `false` = the red strip and the notes only. |

**Window**

| Key | Default | Meaning |
|---|---|---|
| `window` | `"edge"` | `edge` = an Edge app window (nothing to install). `webview` = a frameless WebView2 window (`py -m pip install pywebview pythonnet`). `none` = serve only, open http://127.0.0.1:8770 yourself. |
| `scale` | `1.0` | Text size on the panel. |
| `fraction` | `0.333` | The panel's share of the monitor's width. |
| `monitor` | `"auto"` | `auto` = the monitor of the terminal you launched from. `primary`. `side` = the biggest non-primary monitor. |
| `title_bar` | `null` | `null` = the Edge title bar sits just above the screen and the panel draws its own slim bar with window buttons. `native` = keep the normal title bar. |

**Voice delivery**

| Key | Default | Meaning |
|---|---|---|
| `voice.mode` | `"panel"` | `panel` = the panel server types your F9 words into your Claude Code session. `monitor` = each session watches the inbox itself with a background command (the plugin tells it how). |
| `voice.main_session` | `"Main"` | The session your voice goes to. Start it with `claude --name Main`, or with **Open Main** on the panel. |
| `voice.auto_open_main` | `true` | When the panel server starts and no session named `voice.main_session` is running, it opens one in a new Windows Terminal tab, once per sign-in (one you close stays closed until you sign in again). `false` = start it yourself. |
| `voice.input_idle_s` | `2.0` | Seconds without keyboard or mouse input before a line is typed, so it never mixes with your own typing. |
| `voice.max_chars` | `4000` | The longest line that is typed. A longer panel answer, counting the question it quotes, is refused. |
| `voice.busy_typing` | `true` | Type a line even while the session is working: Claude Code queues it and reads it after the current step. It still waits while the session asks you something. `false` = only into an idle session. |
| `voice.stale_s` | `120` | Seconds a line may wait after you said it. An older line waits for **Send anyway** or **Drop** on the panel. `0` = no limit. |

**Talk key and dump key**

| Key | Default | Meaning |
|---|---|---|
| `ptt.key` | `"f9"` | Hold to talk. pynput key names: `f9`, `f8`, `scroll_lock`, `pause` and so on. Laptops may need F8 or `scroll_lock` (Fn-lock). |
| `ptt.dump_key` | `"f2"` | Press once to start a brain dump, press again to stop. A toggle, not a hold. |
| `ptt.device` | `null` | Part of the microphone's name (`py briandump.py mics` lists them). `null` = the Windows default microphone. |
| `ptt.model` | `"small"` | The faster-whisper model: `tiny` (fastest, about 75 MB), `base`, `small` (the default, about 460 MB, accurate). The English-only `base.en` also works. |
| `ptt.language` | `"en"` | The language you speak: `en`, another code such as `de`, or `auto` to let Whisper detect it. The `.en` models know only English. |
| `ptt.hotwords` | `["Claude", "Claude Code"]` | Spelling hints for Whisper: names it tends to mishear. Add your project names. |
| `ptt.keep_last_clip` | `false` | `true` = keep `last_clip.wav` of your latest F9 clip in the state folder, to check the mic level by ear. |
| `ptt.beeps` | `true` | Beeps on press and release, when a line is written, and when a dump starts and stops. |
| `ptt.suppress_key` | `true` | `true` = other apps never see the talk and dump keys while BrianDump runs (a held F9 would otherwise scroll your terminal). `false` = pass them through too. |

**Brain dumps**

| Key | Default | Meaning |
|---|---|---|
| `dump.processor` | `"claude"` | `claude` = one `claude -p` call per dump under your own login (about one Claude message of usage). `mock` = canned cards and no model call, for trying the panel. |
| `dump.model` | `null` | `null` = the Claude Code default model. Or `sonnet`, `opus`, `haiku`. |
| `dump.effort` | `null` | `null` = the Claude Code default effort. Or `low`, `medium`, `high`, `xhigh`, `max`. |
| `dump.claude_cmd` | `"claude"` | The command that starts Claude Code, if `claude` alone does not work on your PC. |
| `dump.timeout_s` | `360` | Seconds the cards call may take before it is stopped. |
| `dump.max_minutes` | `30` | A dump recording stops itself after this many minutes. |
| `dump.delete_audio_after_days` | `null` | `null` = keep every recording. A number = delete `audio.wav` once the dump is older than that many days. The transcript and cards stay. |
| `dump.prompt_file` | `null` | A text file that replaces the built-in system prompt for the cards step. |
| `dump.user_name` | `null` | Your name, for the cards prompt. `null` = "the user". |

**Agenda, questions and privacy**

| Key | Default | Meaning |
|---|---|---|
| `agenda.file` | `null` | A markdown checklist (`- [ ] item`). `null` = `agenda.md` in your data folder. Dump to-dos are added here; ticking in the panel edits the file. |
| `agenda.heading` | `null` | Only read items under this `## heading`. `null` = the whole file. Lets you point `agenda.file` at a bigger notes file. |
| `questions.file` | `null` | A markdown checklist of open questions for you. `null` = `questions.md` in your data folder. |
| `private_filter` | `[]` | Regular expressions (one that does not compile is matched as plain text, and `doctor` says so). An agenda item, question, dump line, voice line or report title that matches shows as "(private)" on the panel; a masked answer option still answers with its real words. A report whose title, author or path matches stays off the panel (**Open file** still opens it), and in any other report a matching paragraph, list item, table cell or code line shows as "(private)". Empty = show everything. |

**Reports**

The Reports tab always lists your brain-dump reports. These settings add your other Markdown reports. The panel only ever opens a file that lies inside `reports.roots` or your data folder, symlinks and junctions included.

| Key | Default | Meaning |
|---|---|---|
| `reports.roots` | `[]` | The folders your reports live in, for example `["~/Projects"]`. |
| `reports.discover` | `null` | A regular expression. Every Markdown file under `reports.roots` whose first 20 lines match it is listed. `"^Prepared for "` lists every report that starts with that byline. |
| `reports.index` | `null` | A JSON file, or a list of them, that names reports: `[{"path": "garden/docs/plan.md", "title": "Shed plan", "date": "2026-10-19", "by": "Main"}]`. Only `path` is needed. A relative path is looked up in `reports.roots`. |

The easy way to fill the tab is a byline. Ask Claude to start every report with one, for example with a line in your `CLAUDE.md` ("Start each report you write with a line `Prepared for Sam · <date>`"), then point `discover` at it:

```json
"reports": {"roots": ["~/Projects"], "discover": "^Prepared for "}
```

Each listed report shows its first `# ` heading as its title, the date in its file name or first lines, and, for index entries, `by`. Index entries win over what is found. The folders are searched again every 5 minutes, or at once with the tab's ↻ button. A cloud-only file (one your sync app has not downloaded) is never opened by the search, so it is not downloaded just to read a byline. Which reports you have opened is kept in `%LOCALAPPDATA%\BrianDump\state\reports_read.json`.

## Uses your own Claude

BrianDump runs on your own Claude Code login and nothing else. There is no BrianDump server, no API key and no shared account, so nobody else pays for your usage or holds a secret of yours. The panel and the talk key make no model calls: the panel reads files Claude Code already writes, and Whisper runs on your PC. A brain dump makes one `claude -p` call under your login to turn the transcript into cards. That is about one Claude message of usage per dump, counted against your plan's limits or billed to your API key. Set `dump.processor` to `mock` to try the Dumps tab without any call.

## Troubleshooting

Start with `py briandump.py doctor`. It checks Python, the packages, Claude Code, the plugin, the microphone, the hold-to-talk service and the panel port, and prints a fix under each red line. Logs are in `%LOCALAPPDATA%\BrianDump\logs`.

- **F9 does nothing, often on a laptop.** The F keys may be media keys. Turn on Fn-lock, or pick another key: run the installer again, or `py briandump.py config --set ptt.key f8` (or `scroll_lock`) and [restart the hold-to-talk service](docs/install.md#change-the-microphone-or-the-key).
- **You hear the beeps, but nothing reaches Claude.** In panel mode the panel server types your words, so the panel must be running: open **BrianDump** from the Start menu. Then look at the **Voice** tab. It says why a line is waiting, for example "you are at the keyboard" or "Main is running". No session open at all? Start one with `claude --name Main`. A server you started in a terminal with `py briandump.py serve` never types: it would have to give up its own console to reach Claude's. Close it and open the panel from the Start menu.
- **Monitor mode: files land in the inbox, but nothing happens.** There are three usual causes. The plugin is not installed (`doctor` checks it). The session started before the plugin did: start a new one. Or another session owns the voice: delete `%LOCALAPPDATA%\BrianDump\state\voice_watcher.json`.
- **"Thanks for watching", or empty text.** Whisper heard almost nothing. The microphone is too quiet or it is the wrong one. Check `py briandump.py mics` and the input level in Windows Sound settings. Set `ptt.keep_last_clip` to `true` to hear what was recorded. Clips that stay below -46 dBFS are dropped with one low beep.
- **`py briandump.py ptt` exits at once with "another BrianDump hold-to-talk is already running".** The `BrianDump PTT` task is the recorder, and only one runs at a time. To try a setting in a terminal, stop the task first (`Stop-ScheduledTask -TaskName 'BrianDump PTT'` in PowerShell), then start it again.
- **Your words were typed but not sent.** Something appeared on screen while the line was being typed, or the words were not visible in the input line. The router stops before Enter on purpose: Enter could approve a permission dialog. The Voice tab marks the line as failed. Send or clear it yourself.
- **The panel does not open, or opens blank.** The server is not running, or another program uses its port. `doctor` checks both, and `py briandump.py launch` in a terminal prints the reason. Move to a free port with `py briandump.py config --set port 8771`, then open the panel again.
- **The panel opens on the wrong monitor.** `py briandump.py launch --monitor side` (or `primary`), or set `monitor` in the config.
- **The first launch is slow.** Edge builds the panel's own browser profile the first time, which can take 10 seconds or more. Later launches are quick.
- **403 Forbidden from another PC.** The panel answers this PC only. Opening it from another machine takes both `bind` and `allow`, and anyone on that machine can then type into your session. Read [SECURITY.md](SECURITY.md) first. Use this PC's address or its plain name in the address bar: a longer name such as `mypc.home.example` is refused unless Windows itself has it as the PC's full name, because a DNS-rebinding page comes in under names like that.
- **An F9 line takes a long time right after a dump.** One speech model does both jobs, one at a time, so the line waits until the dump is transcribed.
- **A report is missing from the Reports tab.** Read the notes at the top of the tab: they name a folder that is not there, a pattern that does not compile, index entries outside `reports.roots`, or entries whose file has moved. The search looks only at Markdown files, only at their first 20 lines, and skips hidden folders, `node_modules` and virtual environments. Press ↻ to search again now.
- **You cannot find your files.** The **Open reports folder** button on the Dumps tab opens them. `py briandump.py config --show` prints the exact `data_dir`.
- **Your code or Documents folder syncs to the cloud.** The code folder is fine: settings, state and the inbox live in `%LOCALAPPDATA%\BrianDump`, outside it. Your dumps, audio included, live in your data folder. If that folder syncs, they sync too. Set `data_dir` to a local folder to keep them on this PC.
- **Your antivirus warns about a keyboard hook.** The talk key uses a low-level keyboard hook (pynput). It acts on two keys and logs no keystrokes. Read `briandump/ptt.py` to check.

## How it works

The panel follows the session transcript that Claude Code already writes to disk, reading each new byte once, so nothing inside Claude Code is patched. Your F9 lines and panel answers are small files in one inbox. A router on your PC types each one into the Claude Code console, even mid-turn (Claude Code queues it), but never while something on screen could be approved or answered by a keystroke. A brain dump is transcribed on your PC and turned into cards by one `claude -p` call that has no tools and must answer in a fixed JSON shape.

The design decisions, with the trade-offs: [docs/how-it-works.md](docs/how-it-works.md).

## Roadmap

- **0.2:** an Agents view (every Claude session and background agent on your PC), extras for recording screen videos, and opt-in running of approved dump actions.
- **macOS and Linux:** Windows for now. The server and the page are portable (standard-library Python and one HTML file). The hotkey, window and console code is Win32. PRs welcome.

## Contributing and security

- Issues and small pull requests are welcome: [CONTRIBUTING.md](CONTRIBUTING.md).
- What BrianDump touches, what it refuses to do, and how to report a problem: [SECURITY.md](SECURITY.md).
- [Code of conduct](CODE_OF_CONDUCT.md) and [changelog](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).

---

Brian Mahoney · GitHub [KnockKnockGoose](https://github.com/KnockKnockGoose)

The name is a pun on brain dump. The icon is my dog, Biscuit.
