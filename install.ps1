<#
.SYNOPSIS
    Install BrianDump: a side panel and a push-to-talk key for Claude Code on Windows.

.DESCRIPTION
    Double-click "Install BrianDump.cmd" (it runs this file), or run it from PowerShell:

        powershell -ExecutionPolicy Bypass -File install.ps1               asks which parts you want
        powershell -ExecutionPolicy Bypass -File install.ps1 -All          everything (asks only for the microphone and keys)
        powershell -ExecutionPolicy Bypass -File install.ps1 -F9 -Key f8 -Mic default
        powershell -ExecutionPolicy Bypass -File install.ps1 -All -DryRun  says what each step would do and changes nothing

    Parts (with no part switch, the installer asks yes or no for each):
        -Panel     the side panel: a "BrianDump" entry in the Start menu
        -F9        hold F9 to talk, F2 for a brain dump: the Python packages, your microphone and keys, the speech model,
                   and the "BrianDump PTT" task that starts the talk key at every logon
        -Hooks     the BrianDump plugin for Claude Code, installed from this folder
        -Startup   start the panel server when you sign in and keep it running (a shortcut in your Startup folder). In the
                   default panel mode that server is what types your F9 words, so the question defaults to yes.
    Options:
        -Model tiny|base|small   the speech model (small is the default: about 460 MB, the most accurate of the three)
        -Key <key> -DumpKey <key>   the talk key and the brain-dump key (f9 and f2 unless you pick others)
        -Mic <part of a microphone name, or default>
        -Webview   also install the frameless panel window (pywebview + pythonnet)

    Safe to run again: it replaces the task and the shortcuts, keeps your settings unless you change them, and never
    deletes anything of yours. Plain PowerShell 5.1 on purpose: you can read every step before you run it.

    BrianDump was called bis-kit up to version 0.1.0. An older install is moved over: its "bis-kit PTT" task, shortcuts
    and Claude Code plugin are removed, and %LOCALAPPDATA%\bis-kit and Documents\bis-kit move to the BrianDump names
    (unless a folder with the new name is already there).
#>
[CmdletBinding()]
param(
    [switch]$Panel,
    [switch]$F9,
    [switch]$Hooks,
    [switch]$Startup,
    [switch]$All,
    [ValidateSet("tiny", "base", "small")]
    [string]$Model,
    [string]$Key,
    [string]$DumpKey,
    [string]$Mic,
    [switch]$Webview,
    [switch]$DryRun
)

$Repo = $PSScriptRoot
$Cli = Join-Path $Repo "briandump.py"
$TaskName = "BrianDump PTT"
$PluginId = "briandump@briandump"
$KeyPattern = '^(f([1-9]|1[0-9]|2[0-4])|scroll_lock|pause|insert|home|end|page_up|page_down|num_lock|caps_lock|menu|print_screen)$'
$StrayPattern = 'briandump[\\/.]ptt(\.py)?\b|briandump\.py"?\s+ptt\b'
$ModelSize = @{ "tiny" = "about 75 MB"; "base" = "about 145 MB"; "small" = "about 460 MB" }
$Interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
# The names up to 0.1.0, when BrianDump was called bis-kit: an older install is moved over or taken down (see below)
$OldTaskName = "bis-kit PTT"
$OldPluginName = "bis-kit"
$OldPluginId = "bis-kit@bis-kit"
$OldPattern = 'biskit[\\/](watchdog\.pyw|ptt_service\.pyw|frameless\.py|launch\.py)|-m\s+biskit\.(server|ptt|launch)\b|bis-kit\.py"?\s+(launch|serve|ptt)\b'
$script:StepNo = 0

# ------------------------------------------------------------------ helpers
function Show-Step([string]$Title, [string]$Why) {
    $script:StepNo++
    Write-Host ""
    Write-Host ("[{0}] {1}" -f $script:StepNo, $Title) -ForegroundColor Cyan
    if ($Why) { Write-Host "    $Why" }
}
function Show-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Show-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }
function Show-Would([string]$Text) { Write-Host "    (dry run) would $Text" -ForegroundColor DarkGray }
function Stop-Setup([string]$Text) {
    Write-Host ""
    Write-Host "Setup stopped: $Text" -ForegroundColor Red
    exit 1
}

function Read-YesNo([string]$Question, [bool]$Default) {
    # Enter = the default; without a console to ask in (a script, a test), the default is the answer
    if (-not $Interactive) { return $Default }
    $hint = "[y/N]"
    if ($Default) { $hint = "[Y/n]" }
    while ($true) {
        $answer = ([string](Read-Host "    $Question $hint")).Trim().ToLower()
        if ($answer -eq "") { return $Default }
        if ($answer -eq "y" -or $answer -eq "yes") { return $true }
        if ($answer -eq "n" -or $answer -eq "no") { return $false }
        Write-Host "    Please answer y or n."
    }
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # One external program with its output on screen (or, in a dry run, only the command line); $true when it exits with 0
    $shown = ($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join " "
    if ($DryRun) { Show-Would "run: $Exe $shown"; return $true }
    & $Exe @Arguments | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Update-PathFromRegistry {
    # winget installs into folders this window's PATH does not know yet: read PATH again from the registry
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
    $launcher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher"
    if ((Test-Path $launcher) -and ($env:Path -notlike "*$launcher*")) { $env:Path = "$env:Path;$launcher" }
}

function Get-PyVersion {
    # "3.13.1" from the py launcher, or "" when there is no working py
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return "" }
    # all output first: piping a program into Select-Object -First stops it early and sets $LASTEXITCODE to -1
    $lines = @(& py -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0) { return "" }
    $v = ([string]$lines[0]).Trim()
    if ($v -match '^\d+\.\d+\.\d+$') { return $v }
    return ""
}

function Get-Pythonw {
    # pythonw.exe (Python without a console window) next to the python.exe that py starts; "" without py
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return "" }
    $lines = @(& py -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0) { return "" }
    return (Join-Path (Split-Path ([string]$lines[0]).Trim()) "pythonw.exe")
}

function Get-BrianDumpSettings {
    # The folders and the merged settings from `config --show` (read only); $null when Python cannot run it yet
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $null }
    $raw = & py $Cli config --show 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    try { return (($raw -join "`n") | ConvertFrom-Json) } catch { return $null }
}

function Test-KeyName([string]$Name) {
    # pynput key names, several with commas (f9,f13). No letters: BrianDump keeps its key from other apps while it runs.
    if (-not $Name -or $Name.Trim() -eq "") { return $false }
    foreach ($part in ($Name -split ",")) { if ($part.Trim().ToLower() -notmatch $KeyPattern) { return $false } }
    return $true
}

function Read-KeyName([string]$Prompt, [string]$Default) {
    if (-not $Interactive) { return $Default }
    while ($true) {
        $answer = ([string](Read-Host "    $Prompt (Enter = $Default)")).Trim().ToLower()
        if ($answer -eq "") { return $Default }
        if (Test-KeyName $answer) { return $answer }
        Write-Host "    '$answer' is not a key BrianDump can use: f1 to f24, scroll_lock, pause, insert, home, end, page_up or page_down."
    }
}

function Select-Microphone([string]$Current) {
    # The ptt.device value: part of a microphone's name (at most 31 characters, so it also matches the shorter name some
    # audio drivers report), or "null" = always the Windows default microphone
    $mics = @()
    $raw = & py $Cli mics --json 2>$null
    if ($LASTEXITCODE -eq 0 -and $raw) {
        try { $parsed = ($raw -join "`n") | ConvertFrom-Json; $mics = @($parsed) } catch { $mics = @() }
    }
    if ($mics.Count -eq 0) {
        Show-Note "No microphone found right now, so BrianDump will use the Windows default microphone."
        Show-Note "(Plug one in, or allow desktop apps to use it: Settings > Privacy & security > Microphone.)"
        return "null"
    }
    $default = 0
    for ($i = 0; $i -lt $mics.Count; $i++) { if ($mics[$i].default) { $default = $i } }
    Write-Host "    Your microphones:"
    for ($i = 0; $i -lt $mics.Count; $i++) {
        $mark = ""
        if ($i -eq $default) { $mark = "   <- the Windows default" }
        Write-Host ("      {0}) {1}{2}" -f ($i + 1), $mics[$i].name, $mark)
    }
    $keep = "null"
    $keepText = "the Windows default"
    if ($Current) { $keep = $Current; $keepText = "keep '$Current'" }
    if (-not $Interactive) { return $keep }
    $answer = ([string](Read-Host "    Number of the microphone (0 = always the Windows default; Enter = $keepText)")).Trim()
    if ($answer -eq "") { return $keep }
    if ($answer -eq "0") { return "null" }
    if ($answer -match '^\d+$' -and [int]$answer -ge 1 -and [int]$answer -le $mics.Count) {
        $name = [string]$mics[[int]$answer - 1].name
        return $name.Substring(0, [Math]::Min(31, $name.Length)).Trim()
    }
    Show-Note "There is no microphone number ${answer}, so it is the Windows default."
    return "null"
}

function Stop-StrayPtt {
    # One microphone, one listener: a talk key started by hand in a terminal would hear every clip twice
    $all = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue)
    $strays = @($all | Where-Object { $_.CommandLine -match $StrayPattern -and $_.CommandLine -notmatch 'ptt_service' })
    foreach ($p in $strays) {
        if ($DryRun) { Show-Would "stop the talk key that was started by hand (pid $($p.ProcessId))"; continue }
        Write-Host "    Stopping the talk key that was started by hand (pid $($p.ProcessId))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Register-PttTask([string]$Pythonw) {
    # At logon, as you, no window (pythonw), restarted if it stops, no time limit, on battery too; -Force replaces an old one
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $service = Join-Path $Repo "briandump\ptt_service.pyw"
    $action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"' + $service + '"') -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $opts = @{
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
        StartWhenAvailable = $true
        MultipleInstances = "IgnoreNew"
        RestartCount = 5
        RestartInterval = (New-TimeSpan -Minutes 1)
        ExecutionTimeLimit = (New-TimeSpan -Seconds 0)
    }
    $taskSettings = New-ScheduledTaskSettingsSet @opts
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $desc = "BrianDump: hold the talk key to speak into Claude Code, press the dump key for a brain dump. " +
        "Speech is turned into text on this PC. Settings: py briandump.py config"
    $reg = @{
        TaskName = $TaskName; Action = $action; Trigger = $trigger; Settings = $taskSettings; Principal = $principal
        Description = $desc; Force = $true; ErrorAction = "Stop"
    }
    Register-ScheduledTask @reg | Out-Null
}

function Wait-PttReady([string]$StatusFile, [datetime]$Since) {
    # The service rewrites ptt_status.json every 2 s; "ready" turns true once the speech model is loaded
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (-not (Test-Path $StatusFile)) { continue }
        try {
            if ((Get-Item $StatusFile -ErrorAction Stop).LastWriteTime -gt $Since) {
                $st = Get-Content $StatusFile -Raw -ErrorAction Stop | ConvertFrom-Json
                if ($st.ready) { return $true }
            }
        } catch { }
    }
    return $false
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$Icon, [string]$Description) {
    if ($DryRun) { Show-Would "create the shortcut $Path -> $Target $Arguments"; return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($Path)
        $link.TargetPath = $Target
        $link.Arguments = $Arguments
        $link.WorkingDirectory = $Repo
        if (Test-Path $Icon) { $link.IconLocation = "$Icon,0" }
        $link.Description = $Description
        $link.Save()
        Show-Ok "shortcut: $Path"
    } catch {
        Show-Note "Could not create the shortcut $Path ($($_.Exception.Message))"
    }
}

# The panel window carries the AppUserModel ID "BrianDump.Panel" (briandump/launch.py sets it). A Start menu shortcut with the
# same ID is what Windows uses for the taskbar button and its icon; WScript.Shell cannot set it, so a few lines of C# do.
$AppIdSource = @'
using System;
using System.Runtime.InteropServices;
public static class BrianDumpShortcut {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PropertyKey { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr value; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr bindCtx, int flags, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
    public static int SetAppId(string lnk, string appId) {
        Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IPropertyStore store;
        int hr = SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out store);   // 2 = GPS_READWRITE
        if (hr != 0) return hr;
        PropertyKey key = new PropertyKey();
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");                          // System.AppUserModel.ID
        key.pid = 5;
        PropVariant v = new PropVariant();
        v.vt = 31;                                                                               // VT_LPWSTR
        v.value = Marshal.StringToCoTaskMemUni(appId);
        try { hr = store.SetValue(ref key, ref v); if (hr == 0) hr = store.Commit(); }
        finally { Marshal.FreeCoTaskMem(v.value); Marshal.ReleaseComObject(store); }
        return hr;
    }
}
'@

function Set-ShortcutAppId([string]$Path, [string]$AppId) {
    if ($DryRun) { Show-Would "give the shortcut the AppUserModel ID $AppId (the taskbar icon of the panel window)"; return }
    try {
        if (-not ("BrianDumpShortcut" -as [type])) { Add-Type -TypeDefinition $AppIdSource -ErrorAction Stop }
        $hr = [BrianDumpShortcut]::SetAppId($Path, $AppId)
        if ($hr -ne 0) { Show-Note ("Could not tag the shortcut (0x{0:X8}); only the taskbar icon of the frameless window is affected." -f $hr) }
    } catch {
        Show-Note "Could not tag the shortcut ($($_.Exception.Message)); only the taskbar icon of the frameless window is affected."
    }
}

function Get-ClaudeJson([string[]]$Arguments) {
    # A `claude ... --json` listing as objects (read only); an empty list when it fails
    $raw = & claude @Arguments 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return @() }
    try { $parsed = ($raw -join "`n") | ConvertFrom-Json; return @($parsed) } catch { return @() }
}

function Install-Plugin {
    # This folder holds .claude-plugin/marketplace.json, so it is the marketplace: works offline and keeps the plugin and
    # these scripts on one version. Run again, it refreshes the marketplace and updates the plugin instead.
    $known = @(Get-ClaudeJson @("plugin", "marketplace", "list", "--json") | ForEach-Object { $_.name })
    if ($known -contains "briandump") { & claude plugin marketplace update briandump | Out-Host }
    else { & claude plugin marketplace add $Repo | Out-Host }
    if ($LASTEXITCODE -ne 0) { return $false }
    $installed = @(Get-ClaudeJson @("plugin", "list", "--json") | Where-Object { $_.id -eq $PluginId })
    if ($installed.Count -gt 0) {
        & claude plugin update $PluginId | Out-Host
        if ($LASTEXITCODE -eq 0 -and -not $installed[0].enabled) { & claude plugin enable $PluginId | Out-Host }
    } else {
        & claude plugin install $PluginId | Out-Host
    }
    return ($LASTEXITCODE -eq 0)
}

# ------------------------------------------------------------------ which parts
Write-Host "BrianDump installer" -ForegroundColor Green
Write-Host "Folder: $Repo"
if ($DryRun) { Write-Host "DRY RUN: nothing is installed or changed; each step says what it would do." -ForegroundColor Yellow }

$doPanel = [bool]$Panel; $doF9 = [bool]$F9; $doHooks = [bool]$Hooks; $doStartup = [bool]$Startup
if ($All -or ($DryRun -and -not ($doPanel -or $doF9 -or $doHooks -or $doStartup))) {
    $doPanel = $true; $doF9 = $true; $doHooks = $true; $doStartup = $true
}
# voice.mode decides whether the panel server must run for F9 to work (panel mode, the default) or not (monitor mode)
$PanelMode = $true
$early = Get-BrianDumpSettings
if ($early -and $early.settings -and $early.settings.voice -and ([string]$early.settings.voice.mode).ToLower() -eq "monitor") { $PanelMode = $false }
$WhyStartup = "In panel mode the panel server is what types your F9 words, so it should start when you sign in."
if (-not ($doPanel -or $doF9 -or $doHooks -or $doStartup)) {
    if (-not $Interactive) {
        Write-Host "Say which parts to install: -All, or any of -Panel -F9 -Hooks -Startup (add -DryRun to see the steps first)."
        exit 2
    }
    Write-Host ""
    Write-Host "Which parts do you want? (Enter takes the answer in capitals)"
    $doPanel = Read-YesNo "The side panel: see what Claude is doing, answer its questions?" $true
    $doF9 = Read-YesNo "Hold F9 to talk, F2 for a brain dump (downloads about 700 MB the first time)?" $true
    $doHooks = Read-YesNo "Connect to Claude Code (install the BrianDump plugin)?" $true
    if ($PanelMode) { Write-Host "    ($WhyStartup)" }
    $doStartup = Read-YesNo "Start the panel server every time you sign in?" $PanelMode
}
$parts = @()
if ($doPanel) { $parts += "panel" }
if ($doF9) { $parts += "talk key" }
if ($doHooks) { $parts += "Claude Code plugin" }
if ($doStartup) { $parts += "panel server at sign-in" }
Write-Host ("Installing: " + ($parts -join ", "))

# ------------------------------------------------------------------ 1. Python
Show-Step "Python" "BrianDump is written in Python. The panel needs nothing else; the talk key adds a few packages later."
$pyVersion = Get-PyVersion
if ($pyVersion -and ([version]$pyVersion -ge [version]"3.11")) {
    Show-Ok "Python $pyVersion"
} else {
    if ($pyVersion) { Show-Note "Python $pyVersion is too old: BrianDump needs 3.11 or newer." } else { Show-Note "Python was not found." }
    Write-Host "    Now I'll install Python 3.13 for your user account with winget (no admin rights needed, a minute or two)."
    if ($DryRun) {
        Show-Would "run: winget install -e --id Python.Python.3.13 --scope user"
    } else {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Stop-Setup "winget is not available. Install Python 3.13 from https://www.python.org/downloads/ (keep 'py launcher' ticked), then run this installer again."
        }
        winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements | Out-Host
        Update-PathFromRegistry
        $pyVersion = Get-PyVersion
        if (-not $pyVersion -or ([version]$pyVersion -lt [version]"3.11")) {
            Stop-Setup "Python is installed, but this window cannot see it yet. Close it and double-click 'Install BrianDump.cmd' again."
        }
        Show-Ok "Python $pyVersion"
    }
}
$Pythonw = Get-Pythonw
if (-not $Pythonw) { $Pythonw = "<pythonw.exe next to Python>" }

# ------------------------------------------------------------------ 2. Git for Windows (recommended, never required)
Show-Step "Git for Windows (recommended)" ("Claude Code works without it, but its Bash and Monitor tools need Git Bash (the monitor " +
    "voice mode relies on them). Panel mode works without it.")
$gitLater = "To add it later: winget install --id Git.Git -e"
if (Get-Command git -ErrorAction SilentlyContinue) {
    Show-Ok ((& git --version) -join " ")
} elseif ($env:CLAUDE_CODE_GIT_BASH_PATH -and (Test-Path $env:CLAUDE_CODE_GIT_BASH_PATH)) {
    Show-Ok "Git Bash at $env:CLAUDE_CODE_GIT_BASH_PATH"
} elseif ($DryRun) {
    Show-Note "Git for Windows was not found."
    Show-Would "offer: winget install --id Git.Git -e (yes or no; the install goes on either way)"
} elseif ($Interactive -and (Get-Command winget -ErrorAction SilentlyContinue) -and
    (Read-YesNo "Git for Windows was not found. Install it now with winget (about 300 MB)?" $true)) {
    winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -eq 0) { Update-PathFromRegistry; Show-Ok "Git for Windows installed" }
    else { Show-Note "winget could not install it; the install goes on without it. $gitLater" }
} else {
    Show-Note "Not installed; the install goes on without it. $gitLater"
}

# ------------------------------------------------------------------ 3. Claude Code
Show-Step "Claude Code" "BrianDump is a companion for Claude Code: it shows your session and types your words into it."
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Show-Ok ((& claude --version 2>$null) -join " ")
} else {
    Show-Note "Claude Code was not found (or this window's PATH does not include it yet)."
    Write-Host "    1. Open https://code.claude.com/docs and follow the steps for Windows."
    Write-Host "    2. Open a new terminal, type  claude  and log in."
    Write-Host "    3. Come back and double-click 'Install BrianDump.cmd' again."
    if ($DryRun) { Show-Would "stop here until Claude Code is installed" } else { exit 1 }
}

# ------------------------------------------------------------------ an older install under the old name (bis-kit, up to 0.1.0)
# Only when something of it is found: its task and shortcuts are taken down, what still runs is stopped, and its settings
# folder and your files move to the new names (never over a folder that already exists).
$OldStateDir = Join-Path $env:LOCALAPPDATA "bis-kit"
$OldDataDir = Join-Path $env:USERPROFILE "Documents\bis-kit"
$OldLinks = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Bis-kit.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "bis-kit panel.lnk")
)
$oldTask = Get-ScheduledTask -TaskName $OldTaskName -ErrorAction SilentlyContinue
$oldLinksFound = @($OldLinks | Where-Object { Test-Path -LiteralPath $_ })
$oldEdge = Join-Path $OldStateDir "edge-profile"
$oldProcs = @(Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name = 'msedge.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $cmd = [string]$_.CommandLine
        ($_.Name -like "python*" -and $cmd -match $OldPattern) -or
        ($_.Name -eq "msedge.exe" -and $cmd.IndexOf($oldEdge, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
$oldStateFound = Test-Path -LiteralPath $OldStateDir
$oldDataFound = Test-Path -LiteralPath $OldDataDir
$oldShown = $false
if ($oldTask -or $oldLinksFound.Count -gt 0 -or $oldProcs.Count -gt 0 -or $oldStateFound -or $oldDataFound) {
    $oldShown = $true
    Show-Step "An older bis-kit" ("BrianDump was called bis-kit up to version 0.1.0. Now I'll take down the old task and shortcuts " +
        "and move its settings and your files to the new names.")
    if ($oldTask) {
        if ($DryRun) { Show-Would "stop and remove the task '$OldTaskName'" } else {
            Stop-ScheduledTask -TaskName $OldTaskName -ErrorAction SilentlyContinue
            try { Unregister-ScheduledTask -TaskName $OldTaskName -Confirm:$false -ErrorAction Stop; Show-Ok "removed the task '$OldTaskName'" }
            catch { Show-Note "Could not remove the task '$OldTaskName': $($_.Exception.Message)" }
        }
    }
    foreach ($p in $oldProcs) {
        if ($DryRun) { Show-Would "stop $($p.Name) (pid $($p.ProcessId))"; continue }
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Show-Ok "stopped $($p.Name) (pid $($p.ProcessId))"
    }
    if ($oldProcs.Count -gt 0 -and -not $DryRun) { Start-Sleep -Seconds 2 }   # let Windows release their files before the move
    foreach ($link in $oldLinksFound) {
        if ($DryRun) { Show-Would "delete $link"; continue }
        Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
        Show-Ok "deleted $link"
    }
    $newStateDir = Join-Path $env:LOCALAPPDATA "BrianDump"
    if ($oldStateFound) {
        # read the old data_dir before the move: a folder of your own choosing stays where it is
        $oldCfg = $null
        try { $oldCfg = Get-Content -LiteralPath (Join-Path $OldStateDir "config.json") -Raw -ErrorAction Stop | ConvertFrom-Json } catch { }
        if ($oldCfg -and $oldCfg.data_dir) { $oldDataFound = $false }
        if ($env:BRIANDUMP_HOME) {
            Show-Note "BRIANDUMP_HOME is set, so the old settings folder stays where it is: $OldStateDir"
        } elseif (Test-Path -LiteralPath $newStateDir) {
            Show-Note "Both $OldStateDir and $newStateDir exist: the new one is used. Delete the old one once you no longer need it."
        } elseif ($DryRun) {
            Show-Would "move $OldStateDir to $newStateDir (your settings, logs and voice inbox)"
        } else {
            try { Move-Item -LiteralPath $OldStateDir -Destination $newStateDir -ErrorAction Stop; Show-Ok "moved $OldStateDir to $newStateDir" }
            catch { Show-Note "Could not move $OldStateDir ($($_.Exception.Message)). Close the old panel window and run this again." }
        }
    }
    if ($oldDataFound) {
        $newDataDir = Join-Path $env:USERPROFILE "Documents\BrianDump"
        if ($env:BRIANDUMP_HOME -or $env:BRIANDUMP_DATA) {
            Show-Note "BRIANDUMP_HOME or BRIANDUMP_DATA is set, so your old files stay where they are: $OldDataDir"
        } elseif (Test-Path -LiteralPath $newDataDir) {
            Show-Note "Both $OldDataDir and $newDataDir exist: the new one is used. Move what you want to keep yourself."
        } elseif ($DryRun) {
            Show-Would "move $OldDataDir to $newDataDir (your dumps, reports, agenda and questions)"
        } else {
            try { Move-Item -LiteralPath $OldDataDir -Destination $newDataDir -ErrorAction Stop; Show-Ok "moved $OldDataDir to $newDataDir" }
            catch { Show-Note "Could not move $OldDataDir ($($_.Exception.Message)). Close any file open in it and run this again." }
        }
    }
    if ($env:BISKIT_HOME -or $env:BISKIT_DATA) {
        Show-Note "BISKIT_HOME or BISKIT_DATA is set in your environment: rename it to BRIANDUMP_HOME or BRIANDUMP_DATA."
    }
}
if ($doHooks -and (Get-Command claude -ErrorAction SilentlyContinue)) {
    # the old plugin would light the panel and tell sessions about a voice inbox nobody reads any more
    $oldMarket = @(Get-ClaudeJson @("plugin", "marketplace", "list", "--json") | Where-Object { $_.name -eq $OldPluginName })
    $oldPlugin = @(Get-ClaudeJson @("plugin", "list", "--json") | Where-Object { $_.id -eq $OldPluginId })
    if ($oldMarket.Count -gt 0 -or $oldPlugin.Count -gt 0) {
        if (-not $oldShown) { Show-Step "An older bis-kit" "BrianDump was called bis-kit up to version 0.1.0. Now I'll remove its Claude Code plugin." }
        if ($DryRun) {
            if ($oldPlugin.Count -gt 0) { Show-Would "run: claude plugin uninstall $OldPluginId" }
            if ($oldMarket.Count -gt 0) { Show-Would "run: claude plugin marketplace remove $OldPluginName" }
        } else {
            if ($oldPlugin.Count -gt 0) { & claude plugin uninstall $OldPluginId | Out-Host }
            if ($oldMarket.Count -gt 0) { & claude plugin marketplace remove $OldPluginName | Out-Host }
            Show-Ok "removed the old plugin $OldPluginId"
        }
    }
}

# ------------------------------------------------------------------ 4. settings
Show-Step "Settings" "Your settings live in one file, config.json, in your user folder; running this again keeps your changes."
if (-not (Invoke-Native "py" @($Cli, "config", "--init"))) { Stop-Setup "BrianDump could not write its settings (see the message above)." }
if ($Webview) { Invoke-Native "py" @($Cli, "config", "--set", "window", "webview") | Out-Null }
$Settings = Get-BrianDumpSettings
$StateDir = Join-Path $env:LOCALAPPDATA "BrianDump"
$HostTag = ([string]$env:COMPUTERNAME).ToLower()
if ($Settings) { $StateDir = [string]$Settings.state_dir; $HostTag = [string]$Settings.host }

# ------------------------------------------------------------------ 5-8. the talk key
$TalkKey = "f9"
$DumpKeyName = "f2"
if ($doF9) {
    Show-Step "Python packages for the talk key" ("Now I'll install faster-whisper (speech to text on your PC), sounddevice (the microphone), " +
        "pynput (the keys) and a few helpers: about 250 MB, a few minutes. The panel itself needs none of them.")
    $pipArgs = @("-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "-r", (Join-Path $Repo "requirements.txt"))
    if (-not (Invoke-Native "py" $pipArgs)) { Stop-Setup "pip could not install the packages (see above). Check the internet connection and run this again." }
}
if ($Webview) {
    Show-Step "Frameless window" "pywebview and pythonnet draw the panel in a window without a title bar (WebView2, part of Windows)."
    if (-not (Invoke-Native "py" @("-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "pywebview", "pythonnet"))) {
        Show-Note "pip could not install them: the panel keeps using an Edge window (py briandump.py config --set window edge)."
    }
}
if ($doF9) {
    Show-Step "Microphone and keys" "Pick the microphone the talk key listens to, and the two keys."
    $cur = $null
    if ($Settings) { $cur = $Settings.settings.ptt }
    $curDevice = ""; $curKey = "f9"; $curDump = "f2"; $curModel = "small"
    if ($cur) {
        if ($cur.device) { $curDevice = [string]$cur.device }
        if ($cur.key) { $curKey = [string]$cur.key }
        if ($cur.dump_key) { $curDump = [string]$cur.dump_key }
        if ($cur.model) { $curModel = [string]$cur.model }
    }
    if ($Key -and -not (Test-KeyName $Key)) { Stop-Setup "-Key $Key is not a key BrianDump can use (f1 to f24, scroll_lock, pause, insert, home, end, page_up, page_down)." }
    if ($DumpKey -and -not (Test-KeyName $DumpKey)) { Stop-Setup "-DumpKey $DumpKey is not a key BrianDump can use." }
    $SpeechModel = $curModel
    if ($Model) { $SpeechModel = $Model }
    if ($DryRun) {
        $keepText = "the Windows default"
        if ($curDevice) { $keepText = "keep '$curDevice'" }
        $TalkKey = $curKey; $DumpKeyName = $curDump
        if ($Key) { $TalkKey = $Key.ToLower() }
        if ($DumpKey) { $DumpKeyName = $DumpKey.ToLower() }
        $device = "<your choice>"
        if ($Mic) {
            Show-Would "use the microphone '$Mic'"
            $device = $Mic
            if ($Mic -eq "default") { $device = "null" }
        } elseif ($Interactive) {
            Show-Would "list your microphones (py briandump.py mics) and ask which one to use (Enter = $keepText)"
        } else {
            # no keyboard to ask on (run by a script or by Claude Code): the real run takes the Enter answers too
            $device = "null"
            if ($curDevice) { $device = $curDevice; $keepText = "the microphone '$curDevice'" }
            Show-Would "use $keepText (no keyboard to ask on; -Mic picks another)"
        }
        if ($Interactive) { Show-Would "ask for the talk key (Enter = $TalkKey) and the brain-dump key (Enter = $DumpKeyName)" }
        else { Show-Would "use the talk key $TalkKey and the brain-dump key $DumpKeyName (no keyboard to ask on; -Key and -DumpKey pick others)" }
    } else {
        if ($Mic) {
            $device = $Mic
            if ($Mic -eq "default") { $device = "null" }
        } else {
            $device = Select-Microphone $curDevice
        }
        if ($Key) { $TalkKey = $Key.ToLower() } else {
            Write-Host "    The talk key: hold it, speak, let go. On many laptops F9 needs the Fn key; f8 or scroll_lock work too."
            $TalkKey = Read-KeyName "Talk key" $curKey
        }
        if ($DumpKey) { $DumpKeyName = $DumpKey.ToLower() } else {
            Write-Host "    The brain-dump key: press it once and talk for as long as you like, press it again to stop."
            $DumpKeyName = Read-KeyName "Brain-dump key" $curDump
        }
        if ($TalkKey -eq $DumpKeyName) { Stop-Setup "The talk key and the brain-dump key must be different keys (both are $TalkKey)." }
    }
    $setArgs = @($Cli, "config", "--set", "ptt.device", $device, "--set", "ptt.key", $TalkKey, "--set", "ptt.dump_key", $DumpKeyName,
        "--set", "ptt.model", $SpeechModel)
    if (-not (Invoke-Native "py" $setArgs)) { Stop-Setup "BrianDump could not save the microphone and keys (see above)." }

    $size = $ModelSize[$SpeechModel]
    if (-not $size) { $size = "a few hundred MB" }
    Show-Step "Speech model" ("Now I'll download the speech model '$SpeechModel' ($size, a couple of minutes). " +
        "It lets the talk key understand you without sending audio anywhere.")
    $code = "from briandump.transcribe import Transcriber; print('speech model ready (%.0f s to load)' % Transcriber('$SpeechModel').load())"
    Push-Location $Repo
    $ok = Invoke-Native "py" @("-c", $code)
    Pop-Location
    if (-not $ok) { Stop-Setup "The speech model did not download. Check the internet connection and run this again." }

    Show-Step "The talk-key service" "Now I'll register the '$TaskName' task: it starts the talk key, without a window, at every logon and restarts it if it stops."
    Stop-StrayPtt
    if ($DryRun) {
        Show-Would "register the task '$TaskName': `"$Pythonw`" `"$(Join-Path $Repo 'briandump\ptt_service.pyw')`" (replaces an older one)"
        Show-Would "start it and wait up to 2 minutes for it to say ready ($(Join-Path $StateDir 'state\ptt_status.json'))"
    } else {
        if (-not (Test-Path $Pythonw)) { Stop-Setup "pythonw.exe was not found next to Python ($Pythonw)." }
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        }
        try { Register-PttTask $Pythonw } catch { Stop-Setup "Windows did not accept the task: $($_.Exception.Message)" }
        Show-Ok "registered '$TaskName'"
        $since = Get-Date
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "    Waiting for it to load the speech model (up to 2 minutes)..."
        if (Wait-PttReady (Join-Path $StateDir "state\ptt_status.json") $since) {
            Show-Ok "the talk key is ready"
        } else {
            $log = Join-Path $StateDir ("logs\ptt-{0}.log" -f $HostTag)
            Show-Note "It did not say ready within 2 minutes. The end of its log ($log):"
            if (Test-Path $log) { Get-Content $log -Tail 15 | ForEach-Object { Write-Host "      $_" } }
        }
    }
}

# ------------------------------------------------------------------ 9. Start menu entry
if ($doPanel) {
    Show-Step "Start menu" "A 'BrianDump' entry in the Start menu opens the panel (and starts the panel server when it is not running)."
    $menuLink = Join-Path ([Environment]::GetFolderPath("Programs")) "BrianDump.lnk"
    New-Shortcut $menuLink $Pythonw ('"' + $Cli + '" launch') (Join-Path $Repo "briandump\panel.ico") "BrianDump panel"
    Set-ShortcutAppId $menuLink "BrianDump.Panel"
}

# ------------------------------------------------------------------ 10. panel server at sign-in
$startupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "BrianDump panel.lnk"
if ($doStartup) {
    $why = "A shortcut in your Startup folder runs a small watchdog when you sign in: it starts the panel server, and starts it again if it ever stops."
    if ($PanelMode) { $why = "$why $WhyStartup" }
    Show-Step "Panel server at sign-in" $why
    $watchdog = Join-Path $Repo "briandump\watchdog.pyw"
    New-Shortcut $startupLink $Pythonw ('"' + $watchdog + '"') (Join-Path $Repo "briandump\panel.ico") "BrianDump: keeps the panel server running"
    if ($DryRun) {
        Show-Would "start the watchdog now too, so the panel server is up in about 15 seconds without signing out"
    } elseif (Test-Path $Pythonw) {
        Start-Process -FilePath $Pythonw -ArgumentList ('"' + $watchdog + '"') -WorkingDirectory $Repo   # a second copy exits by itself
        Show-Ok "the watchdog runs now too: the panel server is up in about 15 seconds"
    }
}

# ------------------------------------------------------------------ 11. the Claude Code plugin
if ($doHooks) {
    Show-Step "Claude Code plugin" ("Now I'll install the BrianDump plugin into Claude Code from this folder: a hook that tells each new session " +
        "how your voice arrives, one that lights the panel when Claude needs you, and the /briandump:ask and /briandump:agenda commands.")
    if ($DryRun) {
        Show-Would "run: claude plugin marketplace add `"$Repo`"   (claude plugin marketplace update briandump when it is already there)"
        Show-Would "run: claude plugin install $PluginId   (claude plugin update $PluginId when it is already installed)"
    } elseif (Install-Plugin) {
        Show-Ok "plugin $PluginId installed"
        Write-Host "    Restart any open Claude Code session (type /exit, then claude) so it loads the plugin."
    } else {
        $lines = "/plugin marketplace add $Repo`r`n/plugin install $PluginId"
        try { Set-Clipboard -Value $lines } catch { }
        Show-Note "The plugin step did not finish. In Claude Code, paste these two lines (they are on your clipboard too):"
        Write-Host "      /plugin marketplace add $Repo"
        Write-Host "      /plugin install $PluginId"
    }
    Write-Host "    (From GitHub instead of this folder: claude plugin marketplace add KnockKnockGoose/BrianDump, then claude plugin install $PluginId)"
}

# ------------------------------------------------------------------ 12. proof
Show-Step "Check" "A last look at every part (py briandump.py doctor); each red line has its fix under it."
if ($DryRun) { Show-Would "run: py `"$Cli`" doctor" } else { & py $Cli doctor }

Write-Host ""
if ($doF9 -and $PanelMode -and -not $doStartup -and -not (Test-Path $startupLink)) {
    Show-Note "Nothing starts the panel server when you sign in. $WhyStartup Run this again with -Startup, or open the panel after each sign-in."
}
if ($doF9) {
    $t = $TalkKey.ToUpper(); $d = $DumpKeyName.ToUpper()
    Write-Host "Try it: hold $t and say hello; press $d, talk, press $d again." -ForegroundColor Green
}
if ($doPanel) { Write-Host "The panel: BrianDump in the Start menu, or py `"$Cli`" launch" -ForegroundColor Green }
if ($doPanel -and -not $DryRun -and $Interactive -and (Test-Path $Pythonw)) {
    if (Read-YesNo "Open the panel now?" $true) { Start-Process -FilePath $Pythonw -ArgumentList ('"' + $Cli + '" launch') -WorkingDirectory $Repo }
}
exit 0
