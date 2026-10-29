# Installing BrianDump

This guide is for people who rarely use a terminal. Every step is a download, a double-click or a question to answer. The only commands you type are in steps 1 and 7, and both start Claude Code.

You need Windows 10 or 11, a microphone, about 1 GB of free disk, and your own Claude account (a Claude plan or an API key).

> **Already using Claude Code?** You can skip this guide: paste one sentence to Claude and it installs BrianDump for you. See [Install from Claude Code](../README.md#install-from-claude-code-easiest) in the README.

## 1. Get Claude Code working

BrianDump works alongside Claude Code, so install that first. Follow Anthropic's setup page, [code.claude.com/docs/en/setup](https://code.claude.com/docs/en/setup). It gives you one line to paste into PowerShell. Then type `claude`, log in, and come back when it answers.

Git for Windows is recommended as well: Claude Code's Bash tool uses it. Without it, Claude Code runs its commands in PowerShell.

> **What you'll see:** Claude Code's welcome text in the terminal, then a `>` prompt waiting for you to type.

## 2. Download BrianDump

On the BrianDump GitHub page, click the green **Code** button, then **Download ZIP**. Right-click the ZIP and choose **Extract All**. Pick a folder where it can stay, for example your user folder. Do not extract it into `Documents\BrianDump`: that is where BrianDump keeps your files.

With git instead: run `git clone https://github.com/KnockKnockGoose/BrianDump.git` from your user folder.

Leave the folder where it is after installing. The installer records its location, and the talk key and the plugin run from it. If you move it, run the installer again.

> **What you'll see:** a folder named `BrianDump-main` (sometimes inside a second folder of the same name) that holds `Install BrianDump.cmd`, `README.md` and the folders `briandump` and `plugin`.

## 3. Start the installer

Double-click `Install BrianDump.cmd`.

### "Windows protected your PC"

Windows may stop and show a blue box titled "Windows protected your PC". Click **More info**, then **Run anyway**.

Here is why it appears. Windows marks files that came from the internet, and SmartScreen warns before it runs a script or program that is not signed by a publisher it knows. The BrianDump installer is not signed. A signature would tell you who made it, not what it does. Reading it tells you what it does: right-click `install.ps1`, choose **Open with**, then **Notepad**. `Install BrianDump.cmd` only starts `install.ps1`.

### What is PowerShell?

PowerShell is the command window that comes with Windows. The installer is a PowerShell script, `install.ps1`. `Install BrianDump.cmd` starts it with `-ExecutionPolicy Bypass`, which lets this one script run without changing any setting on your PC. The installer says what it is about to do, in plain words, before each step.

> **What you'll see:** a PowerShell window that starts with "BrianDump installer" and the folder it runs from, then "Which parts do you want?".

## 4. Answer the questions

First the installer asks which parts you want. Press Enter to take the answer shown in capitals.

1. **The side panel** (yes): see what Claude is doing and answer its questions.
2. **Hold F9 to talk, F2 for a brain dump** (yes): about 700 MB to download the first time.
3. **Connect to Claude Code** (yes): installs the BrianDump plugin.
4. **Start the panel server every time you log in** (yes in the default panel mode). Keep the yes: in panel mode the panel server is what types your F9 words, so without it F9 works only once you have opened the panel.

Then it works through the steps, and says what each one does before it does it:

- **Python.** If there is no Python 3.11 or newer, it installs Python 3.13 for your user account with `winget`. No administrator rights are needed.
- **Git for Windows.** If it is missing, the installer asks whether to install it with `winget` (about 300 MB).
- **Claude Code.** If it is not found, the installer stops and tells you how to install it. Run the installer again afterwards.
- **Settings:** `%LOCALAPPDATA%\BrianDump\config.json`. Running the installer again keeps your changes.
- **Python packages:** faster-whisper, sounddevice, pynput and a few helpers, about 250 MB. The panel itself needs none of them.
- **Microphone and keys.** It lists your microphones by number. Type a number, `0` for "always the Windows default", or press Enter to keep the current choice. Then it asks for the talk key (Enter = f9) and the brain-dump key (Enter = f2). On many laptops F9 needs the Fn key; f8 or scroll_lock work too.
- **Speech model:** about 460 MB, downloaded once. It lets the talk key understand you without sending audio anywhere.
- **The talk-key service:** the logon task `BrianDump PTT`. The installer starts it and waits up to two minutes for it to be ready.
- **Start menu:** a **BrianDump** entry that opens the panel, and starts the panel server when it is not running.
- **Panel server at sign-in** (Enter = yes in the default panel mode, because that server is what types your F9 words): a `BrianDump panel` shortcut in your Startup folder that keeps the panel server running. The installer also starts it right away, so you do not have to sign out first.
- **The Claude Code plugin** (step 5).

> **What you'll see:** numbered steps, a green `OK` line after each one that worked, a progress message while the model downloads, and a short two-note chime when the talk key is ready.

For people at home in PowerShell, the installer also takes switches: `-All` (every part; it still asks for the microphone and keys), `-Panel`, `-F9`, `-Hooks`, `-Startup`, `-Model tiny|base|small`, `-Key <key>`, `-DumpKey <key>`, `-Mic <part of a name, or default>`, `-Webview` (the frameless window), and `-DryRun`, which prints what each step would do and changes nothing. For example: `powershell -ExecutionPolicy Bypass -File install.ps1 -All -Key f8`.

## 5. Connect Claude Code

The installer adds the BrianDump plugin to Claude Code for you. It registers your BrianDump folder as a plugin marketplace, then installs the plugin from it:

```
claude plugin marketplace add "C:\path\to\your\BrianDump-folder"
claude plugin install briandump@briandump
```

If that fails, it copies the same two steps to your clipboard, written for Claude Code's prompt. Open Claude Code and paste them:

```
/plugin marketplace add C:\path\to\your\BrianDump-folder
/plugin install briandump@briandump
```

You can also add the marketplace from GitHub: `/plugin marketplace add KnockKnockGoose/BrianDump`.

Hooks load when a session starts, so start a new Claude Code session afterwards.

The plugin holds two hooks and two commands. The SessionStart hook tells each new session how your voice arrives. The Notification hook turns the panel's strip red when Claude needs you. `/briandump:ask` and `/briandump:agenda` let Claude put a question on your panel or an item on your agenda.

> **What you'll see:** `OK  plugin briandump@briandump installed` and a reminder to restart open sessions (type `/exit`, then `claude`), or a note that the two lines are on your clipboard.

## 6. The doctor checklist

The installer ends with `py briandump.py doctor`. Each line is one check. `[ ok ]` (green) passed. `[FAIL]` (red) failed, and a `fix:` line under it says what to do. `[info]` (gray) is optional or just information, sometimes with a `tip:`. Run it again whenever something seems off.

> **What you'll see:** a column of `[ ok ]` lines that ends with "Everything BrianDump needs is in place." (with a `[FAIL]` line, it ends with the number of problems instead). Then "Try it: hold F9 and say hello; press F2, talk, press F2 again." and the question "Open the panel now?".

## 7. Try it

1. Open **BrianDump** from the Start menu, or answer yes to "Open the panel now?". The panel appears on the right third of your screen.
2. Start a Claude Code session named Main: open a terminal in a project folder and type `claude --name Main`. The **Open Main** button on the panel's Voice tab does the same.
3. Hold F9, say "say hello", and let go. You hear a high beep when you press, a low beep when you let go, and a double beep when the text is written. A moment later your words appear in the Claude Code prompt and are sent.
4. Press F2 and talk for a minute about something you plan to do. Press F2 again. A dump starts with two rising beeps and stops with two falling ones. Open the **Dumps** tab: the dump shows "Transcribing…", then "Making cards…", then its cards.

> **What you'll see:** Claude answering "say hello" in the terminal while the panel's status shows it working, and a new dump at the top of the Dumps tab.

## Where things are installed

| What | Where |
|---|---|
| The code | The folder you extracted. BrianDump runs from there and does not copy itself anywhere. |
| Settings and working files | `%LOCALAPPDATA%\BrianDump`: `config.json`, `state\`, `inbox\`, `logs\`, and the panel's browser profile (`edge-profile\`, or `webview\`). |
| Your files | `%USERPROFILE%\Documents\BrianDump`: `agenda.md`, `questions.md`, `reports\` and `dumps\`. This is the Documents folder inside your user folder, which stays on this PC even when Windows backs up the Documents folder Explorer shows to the cloud. |
| Python packages | The Python that `py` starts, installed with pip. |
| The speech model | `%USERPROFILE%\.cache\huggingface\hub\models--Systran--faster-whisper-small`, about 460 MB. |
| The talk key | A Task Scheduler task named `BrianDump PTT` that runs at logon, hidden, as your user. |
| Shortcuts | **BrianDump** in the Start menu. **BrianDump panel** in your Startup folder, if you kept the panel server at sign-in. |
| The plugin | Claude Code's own plugin folder, `%USERPROFILE%\.claude\plugins`, managed by Claude Code. |

To open `%LOCALAPPDATA%\BrianDump`, paste it into the Explorer address bar.

## Change the microphone or the key

Run the installer again. It is safe to re-run: it keeps your settings, asks again, and replaces the `BrianDump PTT` task instead of adding a second one. To redo only the talk-key part, run it from PowerShell in the BrianDump folder: `powershell -ExecutionPolicy Bypass -File install.ps1 -F9`.

Or change one setting from a terminal in the BrianDump folder:

```
py briandump.py mics
py briandump.py config --set ptt.device "USB"
py briandump.py config --set ptt.key f8
```

`ptt.device` takes any part of a microphone's name from the `mics` list. The talk key reads its settings when it starts, so restart it afterwards. In PowerShell:

```
Stop-ScheduledTask -TaskName 'BrianDump PTT'
Start-ScheduledTask -TaskName 'BrianDump PTT'
```

Signing out of Windows and back in does the same, and restarts the panel server too.

`py briandump.py config --open` opens the whole settings file. Every setting is explained in it, and in the configuration table in the [README](../README.md#configuration).

## Commands

Run these in the BrianDump folder.

| Command | What it does |
|---|---|
| `py briandump.py launch` | Starts the panel server if needed and opens the panel. `--monitor side` or `primary` picks a monitor; `--place` snaps it back to the right third; `--reload` reopens it. |
| `py briandump.py serve` | Runs only the server, in this terminal. `--port` picks a port. A server started this way does not type your F9 words (it keeps its own console), so use `launch` day to day. |
| `py briandump.py doctor` | The checklist. `--json` prints it as JSON. |
| `py briandump.py config` | Shows the settings. `--open` opens the file; `--set key value` changes one. |
| `py briandump.py ptt` | Runs the talk key in this terminal, to try a setting. Stop the `BrianDump PTT` task first: while it runs, a second recorder says "another BrianDump hold-to-talk is already running" and exits. |
| `py briandump.py mics` | Lists your microphones. |
| `py briandump.py dump <file>` | Turns an audio or text file into a dump. `--now` processes it right away. |
| `py briandump.py ask "question"` | Puts a question on your panel. `/briandump:ask` runs this. |
| `py briandump.py agenda add "item"` | Adds an item to your agenda. `/briandump:agenda` runs this. |
| `py briandump.py version` | Prints the version. |

## Remove BrianDump

1. Double-click `Uninstall BrianDump.cmd`. It removes the `BrianDump PTT` task, stops BrianDump, deletes the Start menu and Startup shortcuts, and asks before it deletes `%LOCALAPPDATA%\BrianDump` (your settings, the logs, the voice inbox and the panel window's browser profile). It never touches `Documents\BrianDump`: your dumps, reports, agenda and questions stay.
2. Remove the plugin. Claude Code removes its own plugins, so the uninstaller prints the two commands. Run them in a terminal:
   ```
   claude plugin uninstall briandump@briandump
   claude plugin marketplace remove briandump
   ```
   Inside Claude Code, the same is `/plugin uninstall briandump@briandump`, then `/plugin marketplace remove briandump`.
3. Delete the BrianDump folder.
4. Optional: remove the Python packages (`py -m pip uninstall faster-whisper sounddevice pynput pycaw comtypes`; other programs may use numpy) and the speech model folder under `%USERPROFILE%\.cache\huggingface\hub`.
