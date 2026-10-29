"""install.ps1 and uninstall.ps1: they parse, their dry runs change nothing on this machine, they stay plain ASCII with
CRLF line endings (Windows PowerShell 5.1 reads a file without a byte-order mark as ANSI), and no machine-specific detail
leaks into them (generic checks: a per-user path, an e-mail address, an IP address).

Neither script ever runs for real in a test: a second talk-key task would fight the one a developer already runs."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL, UNINSTALL = REPO / "install.ps1", REPO / "uninstall.ps1"
WRAPPERS = [REPO / "Install BrianDump.cmd", REPO / "Uninstall BrianDump.cmd"]
MINE = [INSTALL, UNINSTALL, *WRAPPERS, REPO / "briandump.py", REPO / "briandump" / "cli.py", REPO / "requirements.txt",
        REPO / "requirements-dev.txt", REPO / "tests" / "test_cli.py", REPO / "tests" / "test_doctor.py"]
# Leaks of one machine, without naming anything: an absolute per-user path (%USERPROFILE% and $env: forms are fine), an e-mail
# address (example.com/.org/.net are fine), an IPv4 address other than loopback, "any address" or an RFC 5737 example.
USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/%$<>\"'\s]+[\\/]", re.I)
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
SAFE_IP = re.compile(r"(127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+)")
SAFE_MAIL = re.compile(r"(^|\.)example\.(com|org|net)$", re.I)
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="PowerShell is not installed")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the installer is for Windows")


def run_ps(script, *args, env=None):
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args], capture_output=True,
                          text=True, errors="replace", timeout=300, env=env, stdin=subprocess.DEVNULL, cwd=str(REPO))


def machine_state():
    """What the scripts may change outside their own folders: the two shortcuts and the logon task, under the new names and
    under the old ones (bis-kit, up to 0.1.0), which the installer moves over and the uninstaller removes too."""
    programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    state = {}
    for link in (programs / "BrianDump.lnk", programs / "Startup" / "BrianDump panel.lnk",
                 programs / "Bis-kit.lnk", programs / "Startup" / "bis-kit panel.lnk"):
        state[str(link)] = link.stat().st_mtime if link.exists() else None
    for name in ("BrianDump PTT", "bis-kit PTT"):
        task = subprocess.run(["schtasks", "/query", "/tn", name, "/xml"], capture_output=True)
        state[name] = task.stdout if task.returncode == 0 else None
    return state


# ---------- static ----------
@needs_powershell
@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=lambda p: p.name)
def test_the_scripts_parse(script):
    path = str(script).replace("'", "''")
    cmd = ("$t=$null; $e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{path}', [ref]$t, [ref]$e); $e | ForEach-Object {{ 'line ' + $_.Extent.StartLineNumber + ': ' + $_.Message }}; $e.Count")
    r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, errors="replace",
                       timeout=120)
    lines = r.stdout.strip().splitlines()
    assert lines and lines[-1] == "0", r.stdout + r.stderr


@pytest.mark.parametrize("path", [INSTALL, UNINSTALL, *WRAPPERS], ids=lambda p: p.name)
def test_windows_scripts_are_ascii_with_crlf(path):
    raw = path.read_bytes()
    assert raw.isascii(), "Windows PowerShell 5.1 reads a file without a BOM as ANSI: keep it ASCII"
    assert b"\n" not in raw.replace(b"\r\n", b""), "line endings must be CRLF"


@pytest.mark.parametrize("path", [INSTALL, UNINSTALL], ids=lambda p: p.name)
def test_powershell_5_1_syntax_only(path):
    code = [ln.split("#")[0] for ln in path.read_text(encoding="ascii").splitlines()]
    for op in ("&&", "||", "??", "?."):                   # PowerShell 7 only
        assert not [ln for ln in code if op in ln], op


def leaks(line):
    found = [m.group(0) for m in USER_PATH.finditer(line)]
    found += [m.group(0) for m in EMAIL.finditer(line) if not SAFE_MAIL.search(m.group(1))]
    found += [m.group(1) for m in IPV4.finditer(line) if not SAFE_IP.fullmatch(m.group(1))]
    return found


@pytest.mark.parametrize("path", MINE, ids=lambda p: p.name)
def test_no_machine_specific_paths_addresses_or_ips(path):
    hits = [(n, leak) for n, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) for leak in leaks(ln)]
    assert not hits


def test_the_leak_check_itself():
    assert leaks(r"C:\Users\somebody\notes.txt") and leaks("d:/users/somebody/x") and leaks("write to someone@corp.example.co")
    assert leaks("allow 10.1.2.3") and leaks("http://172.16.0.9:8770/")
    assert not leaks(r"%USERPROFILE%\.cache\huggingface") and not leaks(r"$env:LOCALAPPDATA\BrianDump")
    assert not leaks("http://127.0.0.1:8770/ on 0.0.0.0, e.g. 192.0.2.10") and not leaks("x@example.com")
    assert not leaks("git version 2.50.0.windows.1, Python 3.13.1, build 10.0.26200")


def test_the_wrappers_run_the_script_next_to_them():
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*' in WRAPPERS[0].read_text(encoding="ascii")
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*' in WRAPPERS[1].read_text(encoding="ascii")
    for w in WRAPPERS:
        assert w.read_text(encoding="ascii").rstrip().endswith("pause")


def test_the_names_the_rest_of_briandump_expects():
    install, uninstall = INSTALL.read_text(encoding="ascii"), UNINSTALL.read_text(encoding="ascii")
    for text in (install, uninstall):
        assert '$TaskName = "BrianDump PTT"' in text and '"BrianDump.lnk"' in text and '"BrianDump panel.lnk"' in text
    assert 'briandump\\ptt_service.pyw' in install and 'briandump\\watchdog.pyw' in install and '"BrianDump.Panel"' in install
    assert "& claude plugin marketplace add $Repo" in install and "& claude plugin install $PluginId" in install
    assert '$PluginId = "briandump@briandump"' in install and "KnockKnockGoose/BrianDump" in install
    assert "claude plugin uninstall briandump@briandump" in uninstall and "claude plugin marketplace remove briandump" in uninstall
    # the old names (bis-kit, up to 0.1.0): the installer moves an older install over, the uninstaller removes it too
    for text in (install, uninstall):
        assert '$OldTaskName = "bis-kit PTT"' in text and '"Bis-kit.lnk"' in text and '"bis-kit panel.lnk"' in text
        assert 'Join-Path $env:LOCALAPPDATA "bis-kit"' in text and '"Documents\\bis-kit"' in text
    assert '$OldPluginId = "bis-kit@bis-kit"' in install and "& claude plugin uninstall $OldPluginId" in install
    assert "claude plugin uninstall bis-kit@bis-kit" in uninstall and "claude plugin marketplace remove bis-kit" in uninstall


# ---------- dry runs ----------
@windows_only
@needs_powershell
def test_install_dry_run_changes_nothing(tmp_path):
    home = tmp_path / "never-created"
    env = {**os.environ, "BRIANDUMP_HOME": str(home), "BRIANDUMP_SANDBOX": "1"}
    before = machine_state()
    r = run_ps(INSTALL, "-DryRun", "-All", env=env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    out = r.stdout
    for step in ("Python", "Git for Windows (recommended)", "Claude Code", "Settings", "Python packages for the talk key",
                 "Microphone and keys", "Speech model", "The talk-key service", "Start menu", "Panel server at sign-in",
                 "Claude Code plugin", "Check"):
        assert f"] {step}" in out, step
    assert "(dry run) would" in out and "'BrianDump PTT'" in out and "claude plugin install briandump@briandump" in out
    assert "Try it: hold F9 and say hello; press F2, talk, press F2 again." in out
    assert "Setup stopped" not in out and "Exception" not in r.stderr and "is not recognized" not in r.stderr
    assert not home.exists()
    assert machine_state() == before


@windows_only
@needs_powershell
def test_install_dry_run_offers_the_current_settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text('{"ptt": {"key": "f8", "device": "USB Audio", "model": "base"}}', encoding="utf-8")
    r = run_ps(INSTALL, "-DryRun", "-F9", env={**os.environ, "BRIANDUMP_HOME": str(home), "BRIANDUMP_SANDBOX": "1"})
    assert r.returncode == 0, r.stdout[-3000:]
    assert "use the microphone 'USB Audio' (no keyboard to ask on" in r.stdout and "the talk key f8 and" in r.stdout
    assert "speech model 'base'" in r.stdout
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "BrianDump panel.lnk"
    if not startup.exists():                              # -F9 alone, panel mode, nothing starts the server at sign-in
        assert "Nothing starts the panel server when you sign in" in r.stdout
    assert sorted(p.name for p in home.iterdir()) == ["config.json"]


@windows_only
@needs_powershell
def test_install_without_a_keyboard_takes_the_defaults(tmp_path):
    """Claude Code runs the installer with stdin redirected: nothing may wait for an answer, and every question takes its
    Enter answer (the Windows default microphone, F9, F2, the small model) unless a flag gives another."""
    env = {**os.environ, "BRIANDUMP_HOME": str(tmp_path / "home"), "BRIANDUMP_SANDBOX": "1"}
    r = run_ps(INSTALL, "-All", "-DryRun", env=env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "would ask" not in r.stdout and "and ask which" not in r.stdout and "<your choice>" not in r.stdout
    assert "use the Windows default (no keyboard to ask on; -Mic picks another)" in r.stdout
    assert "config --set ptt.device null --set ptt.key f9 --set ptt.dump_key f2 --set ptt.model small" in r.stdout
    r = run_ps(INSTALL, "-All", "-DryRun", "-Mic", "USB Audio", "-Key", "f8", "-DumpKey", "f7", "-Model", "base", env=env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert 'config --set ptt.device "USB Audio" --set ptt.key f8 --set ptt.dump_key f7 --set ptt.model base' in r.stdout
    assert "Try it: hold F8 and say hello; press F7, talk, press F7 again." in r.stdout


@windows_only
@needs_powershell
def test_install_dry_run_moves_an_older_bis_kit_over(tmp_path):
    """Someone already runs 0.1.0, when BrianDump was called bis-kit: the dry run says it would move the old settings folder
    and the old data folder to the new names, and moves nothing. A data_dir of their own, or a new folder that is already
    there, is left alone."""
    local, user = tmp_path / "local", tmp_path / "user"
    old_state, old_data = local / "bis-kit", user / "Documents" / "bis-kit"
    old_state.mkdir(parents=True)
    old_data.mkdir(parents=True)
    (old_state / "config.json").write_text('{"ptt": {"key": "f8"}}', encoding="utf-8")
    (old_data / "agenda.md").write_text("- [ ] keep me\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BRIANDUMP_", "BISKIT_"))}
    env.update(LOCALAPPDATA=str(local), USERPROFILE=str(user))
    before = machine_state()
    r = run_ps(INSTALL, "-DryRun", "-Panel", "-F9", env=env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "] An older bis-kit" in r.stdout
    assert f"would move {old_state} to {local / 'BrianDump'}" in r.stdout
    assert f"would move {old_data} to {user / 'Documents' / 'BrianDump'}" in r.stdout
    assert not (local / "BrianDump").exists() and not (user / "Documents" / "BrianDump").exists()
    assert sorted(p.name for p in old_state.iterdir()) == ["config.json"] and sorted(p.name for p in old_data.iterdir()) == ["agenda.md"]

    (old_state / "config.json").write_text(json.dumps({"data_dir": str(tmp_path / "mine")}), encoding="utf-8")
    (local / "BrianDump").mkdir()
    r = run_ps(INSTALL, "-DryRun", "-Panel", env=env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert f"Both {old_state} and {local / 'BrianDump'} exist" in r.stdout
    assert "would move" not in r.stdout                   # a data_dir of their own: the old Documents folder is not theirs to move
    assert machine_state() == before


@windows_only
@needs_powershell
def test_uninstall_dry_run_deletes_nothing(tmp_path):
    home = tmp_path / "briandump-home"
    (home / "state").mkdir(parents=True)
    (home / "data").mkdir()
    (home / "config.json").write_text("{}", encoding="utf-8")
    (home / "data" / "agenda.md").write_text("- [ ] keep me\n", encoding="utf-8")
    files = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    before = machine_state()
    for extra in ([], ["-Yes"]):
        r = run_ps(UNINSTALL, "-DryRun", *extra, env={**os.environ, "BRIANDUMP_HOME": str(home), "BRIANDUMP_SANDBOX": "1"})
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
        assert "claude plugin uninstall briandump@briandump" in r.stdout and "claude plugin marketplace remove briandump" in r.stdout
        assert f"except {home / 'data'}" in r.stdout      # the data folder inside this home is spared
        assert ("would delete" if extra else "would ask, then delete") in r.stdout
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == files
    assert machine_state() == before
