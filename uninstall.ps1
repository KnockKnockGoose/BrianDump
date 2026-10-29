<#
.SYNOPSIS
    Remove BrianDump from Windows.

.DESCRIPTION
    Double-click "Uninstall BrianDump.cmd" (it runs this file), or run it from PowerShell:

        powershell -ExecutionPolicy Bypass -File uninstall.ps1           asks before deleting the settings folder
        powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Yes      deletes it without asking
        powershell -ExecutionPolicy Bypass -File uninstall.ps1 -DryRun   says what it would do and changes nothing

    It removes the "BrianDump PTT" logon task, stops BrianDump, deletes the Start menu and Startup shortcuts and, after asking,
    the settings and state folder (%LOCALAPPDATA%\BrianDump: config.json, logs, the voice inbox, the panel window's browser
    profile). It never touches your words: the dumps, reports, agenda and questions in Documents\BrianDump stay where they
    are. The Claude Code plugin is removed from Claude Code itself; the commands are printed at the end.

    BrianDump was called bis-kit up to version 0.1.0. What an older install left under that name (the "bis-kit PTT" task,
    its shortcuts, %LOCALAPPDATA%\bis-kit) is removed the same way.
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$DryRun
)

$Repo = $PSScriptRoot
$Cli = Join-Path $Repo "briandump.py"
$TaskName = "BrianDump PTT"
$OldTaskName = "bis-kit PTT"
$Interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
# BrianDump's own processes: the watchdog, the panel server, the talk key (task or by hand), the panel window helpers;
# then the same under the old names (biskit\, bis-kit.py)
$OursPattern = 'briandump[\\/](watchdog\.pyw|ptt_service\.pyw|frameless\.py|launch\.py)|-m\s+briandump\.(server|ptt|launch)\b|briandump\.py"?\s+(launch|serve|ptt)\b'
$OldPattern = 'biskit[\\/](watchdog\.pyw|ptt_service\.pyw|frameless\.py|launch\.py)|-m\s+biskit\.(server|ptt|launch)\b|bis-kit\.py"?\s+(launch|serve|ptt)\b'

function Show-Step([string]$Title) { Write-Host ""; Write-Host $Title -ForegroundColor Cyan }
function Show-Done([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Show-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }
function Show-Would([string]$Text) { Write-Host "    (dry run) would $Text" -ForegroundColor DarkGray }

function Test-Under([string]$Path, [string]$Folder) {
    # Is $Path the folder itself or inside it (no matter the case of the letters)?
    $p = $Path.TrimEnd("\") + "\"
    $f = $Folder.TrimEnd("\") + "\"
    return $p.StartsWith($f, [StringComparison]::OrdinalIgnoreCase)
}

function Remove-StateFolder([string]$Dir, [string]$Leaf) {
    # One settings and state folder, after asking (or with -Yes); a data folder inside it is spared
    Show-Step "Settings and state: $Dir"
    $looksOurs = (Split-Path $Dir -Leaf) -eq $Leaf -or (Test-Path -LiteralPath (Join-Path $Dir "config.json"))
    foreach ($keep in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, [Environment]::GetFolderPath("MyDocuments"))) {
        if ($keep -and ($Dir.TrimEnd("\") -ieq ([string]$keep).TrimEnd("\"))) { $looksOurs = $false }
    }
    if ([System.IO.Path]::GetPathRoot($Dir).TrimEnd("\") -ieq $Dir.TrimEnd("\")) { $looksOurs = $false }
    if (-not (Test-Path -LiteralPath $Dir)) {
        Write-Host "    not there"
        return
    }
    if (-not $looksOurs) {
        Show-Note "Left alone: $Dir does not look like a BrianDump folder. Delete it yourself if you are sure."
        return
    }
    Write-Host "    It holds config.json (your settings), the logs, the voice inbox and the panel window's browser profile."
    $dataInside = Test-Under $DataDir $Dir
    $target = $Dir
    if ($dataInside) { $target = "everything in $Dir except $DataDir" }
    $go = [bool]$Yes
    if ($DryRun) {
        if ($go) { Show-Would "delete $target" } else { Show-Would "ask, then delete $target" }
        return
    }
    if (-not $go -and $Interactive) {
        $answer = ([string](Read-Host "    Delete it? [y/N]")).Trim().ToLower()
        $go = ($answer -eq "y" -or $answer -eq "yes")
    }
    if (-not $go) {
        Write-Host "    Kept. (Run with -Yes to delete it without the question.)"
        return
    }
    try {
        if ($dataInside) {
            Get-ChildItem -LiteralPath $Dir -Force | Where-Object { -not (Test-Under $DataDir $_.FullName) } |
                Remove-Item -Recurse -Force -ErrorAction Stop
        } else {
            Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction Stop
        }
        Show-Done "deleted $target"
    } catch {
        Show-Note "Some files could not be deleted ($($_.Exception.Message)). Close the panel window and run this again."
    }
}

Write-Host "BrianDump uninstaller" -ForegroundColor Green
Write-Host "Folder: $Repo"
if ($DryRun) { Write-Host "DRY RUN: nothing is stopped, removed or deleted; each step says what it would do." -ForegroundColor Yellow }

# Where things are: BrianDump itself knows (BRIANDUMP_HOME and the data_dir setting included); otherwise the defaults
$StateDir = Join-Path $env:LOCALAPPDATA "BrianDump"
$DataDir = Join-Path $env:USERPROFILE "Documents\BrianDump"
$OldStateDir = Join-Path $env:LOCALAPPDATA "bis-kit"
$OldDataDir = Join-Path $env:USERPROFILE "Documents\bis-kit"
if (Get-Command py -ErrorAction SilentlyContinue) {
    $raw = & py $Cli config --show 2>$null
    if ($LASTEXITCODE -eq 0 -and $raw) {
        try {
            $info = ($raw -join "`n") | ConvertFrom-Json
            if ($info.state_dir) { $StateDir = [string]$info.state_dir }
            if ($info.data_dir) { $DataDir = [string]$info.data_dir }
        } catch { }
    }
}

# ------------------------------------------------------------------ 1. the logon task (first: it restarts a stopped service)
Show-Step "The talk-key task '$TaskName'"
foreach ($name in @($TaskName, $OldTaskName)) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {
        if ($name -eq $TaskName) { Write-Host "    not installed" }
    } elseif ($DryRun) {
        Show-Would "stop and remove the task '$name'"
    } else {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        try {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
            Show-Done "removed the task '$name'"
        } catch {
            Show-Note "Could not remove the task '$name': $($_.Exception.Message)"
        }
    }
}

# ------------------------------------------------------------------ 2. what still runs
Show-Step "Stopping BrianDump"
$edgeProfiles = @((Join-Path $StateDir "edge-profile"), (Join-Path $OldStateDir "edge-profile"))
$all = @(Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name = 'msedge.exe'" -ErrorAction SilentlyContinue)
$procs = @($all | Where-Object {
        $cmd = [string]$_.CommandLine
        ($_.Name -like "python*" -and ($cmd -match $OursPattern -or $cmd -match $OldPattern)) -or
        ($_.Name -eq "msedge.exe" -and @($edgeProfiles | Where-Object { $cmd.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0)
    } | Sort-Object { if ([string]$_.CommandLine -match 'watchdog') { 0 } else { 1 } })
if ($procs.Count -eq 0) { Write-Host "    nothing of BrianDump is running" }
foreach ($p in $procs) {
    $what = "$($p.Name) (pid $($p.ProcessId))"
    if ($DryRun) { Show-Would "stop $what"; continue }
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Show-Done "stopped $what"
}

# ------------------------------------------------------------------ 3. shortcuts
Show-Step "Shortcuts"
$links = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "BrianDump.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "BrianDump panel.lnk")
)
$oldLinks = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Bis-kit.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "bis-kit panel.lnk")
)
foreach ($link in ($links + @($oldLinks | Where-Object { Test-Path -LiteralPath $_ }))) {
    if (-not (Test-Path -LiteralPath $link)) { Write-Host "    not there: $link"; continue }
    if ($DryRun) { Show-Would "delete $link"; continue }
    Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
    Show-Done "deleted $link"
}

# ------------------------------------------------------------------ 4. settings and state (asks first)
Remove-StateFolder $StateDir "BrianDump"
if ((Test-Path -LiteralPath $OldStateDir) -and ($OldStateDir.TrimEnd("\") -ine $StateDir.TrimEnd("\"))) {
    Remove-StateFolder $OldStateDir "bis-kit"
}

# ------------------------------------------------------------------ 5. the plugin, and what stays
Show-Step "The Claude Code plugin"
Write-Host "    Claude Code removes its own plugins. In a terminal, run:"
Write-Host "      claude plugin uninstall briandump@briandump"
Write-Host "      claude plugin marketplace remove briandump"
Write-Host "    (or inside Claude Code: /plugin uninstall briandump@briandump, then /plugin marketplace remove briandump)"
Write-Host "    An older install named bis-kit, if you had one, has its own plugin:"
Write-Host "      claude plugin uninstall bis-kit@bis-kit"
Write-Host "      claude plugin marketplace remove bis-kit"

Show-Step "What stays"
Write-Host "    Your dumps, reports, agenda and questions stay in $DataDir (BrianDump never deletes them; delete that folder"
Write-Host "    yourself if you no longer want them)."
if (Test-Path -LiteralPath $OldDataDir) { Write-Host "    Files from the older bis-kit install are in ${OldDataDir}: they stay too." }
Write-Host "    The Python packages and the speech model stay installed too: py -m pip uninstall faster-whisper sounddevice pynput"
Write-Host "    pycaw comtypes removes the packages; the model is in %USERPROFILE%\.cache\huggingface."
Write-Host "    This folder ($Repo) can be deleted now."
exit 0
