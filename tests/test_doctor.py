"""The doctor: each check against faked probes (no real claude, task, port or microphone is consulted), the runner's
deadline and crash handling, the real port probes against sockets the test opens, and one real `doctor --json` run."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from briandump import cli

REPO = Path(__file__).resolve().parents[1]
ALL_ROWS = ("BrianDump", "Windows", "Python", "Packages", "Panel window", "Claude Code", "Claude login", "Git for Windows", "Settings",
            "Data folder", "Transcripts", "Claude sessions", "BrianDump plugin", "Speech model", "Microphone", "Talk-key task",
            "Talk-key service", "Panel")


def write_status(home, age, **kw):
    """ptt_status.json as the service writes it, `age` seconds old."""
    st = {"pid": 4242, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age)), "ready": True, "key": "f9",
          "dump_key": "f2", "device": None, "model": "small", "recording": False, "mode": None, "last_error": None, **kw}
    home.ptt_status.parent.mkdir(parents=True, exist_ok=True)
    home.ptt_status.write_text(json.dumps(st), encoding="utf-8")


def write_plugin(home, enabled=None):
    f = home.claude_dir / "plugins" / "installed_plugins.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": 2, "plugins": {"briandump@briandump": [{"scope": "user", "version": "0.1.0"}]}}), encoding="utf-8")
    if enabled is not None:
        (home.claude_dir / "settings.json").write_text(json.dumps({"enabledPlugins": {"briandump@briandump": enabled}}), encoding="utf-8")
    return f


@pytest.fixture
def healthy(home, monkeypatch, tmp_path):
    """A machine where everything is in place: every probe answers like a good install, the files exist in the temp home."""
    bin_dir = tmp_path / "bin"
    tools = {"claude": bin_dir / "claude.exe", "git": bin_dir / "git.exe", "py": bin_dir / "py.exe"}
    answers = {
        "claude --version": (0, "2.1.302 (Claude Code)\n"),
        "claude auth status --json": (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"})),
        "git --version": (0, "git version 2.50.0.windows.1\n"),
        "py": (0, f"3.13.1\n{sys.executable}\n"),
    }

    def fake_run(argv, timeout=cli.RUN_TIMEOUT, encoding="utf-8"):
        name = Path(str(argv[0])).stem.lower()
        return answers["py"] if name == "py" else answers.get(" ".join([name, *map(str, argv[1:])]), (1, "unknown command"))

    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"")
    hf = tmp_path / "hf"
    model = hf / "models--Systran--faster-whisper-small" / "snapshots" / "abc123"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"weights")
    startup = tmp_path / "Startup" / "BrianDump panel.lnk"
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: str(tools[name]) if tools.get(name) else None)
    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "_on_windows", lambda: True)
    monkeypatch.setattr(cli, "_windows_build", lambda: (10, 26100))
    monkeypatch.setattr(cli, "_import_ok", lambda module: (True, ""))
    monkeypatch.setattr(cli, "_dist_version", lambda dist: "1.0")
    monkeypatch.setattr(cli, "_has_module", lambda module: False)
    monkeypatch.setattr(cli, "EDGE", (str(edge),))
    monkeypatch.setattr(cli, "_hf_cache", lambda: hf)
    monkeypatch.setattr(cli, "_model_repo", lambda name: f"Systran/faster-whisper-{name}")
    monkeypatch.setattr(cli, "_mic_list", lambda: [{"index": 1, "name": "Microphone (USB Audio)", "default": True},
                                                   {"index": 3, "name": "Headset Microphone", "default": False}])
    monkeypatch.setattr(cli, "_task_info", lambda name: {"enabled": True, "args": f'"{cli.REPO / "briandump" / "ptt_service.pyw"}"'})
    monkeypatch.setattr(cli, "_registry", lambda: [{"name": "Main", "alive": True, "pid": 11}, {"name": "", "alive": False, "pid": 12}])
    monkeypatch.setattr(cli, "_panel_probe", lambda port: ("briandump", {"version": "0.1.0", "data_dir": str(home.data_dir)}))
    monkeypatch.setattr(cli, "_startup_shortcut", lambda: startup)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home.save_user_value("install_dir", str(cli.REPO))
    write_plugin(home)
    (home.projects / "C--work").mkdir()
    (home.projects / "C--work" / "abc.jsonl").write_text("{}\n", encoding="utf-8")
    write_status(home, age=1)
    return SimpleNamespace(tools=tools, answers=answers, hf=hf, startup=startup)


def rows(checks=None):
    return {c.name: c for c in cli.run_checks(checks)}


def row(check):
    (only,) = cli.run_checks([check])
    return only


# ---------- the whole checklist ----------
def test_a_healthy_machine_is_all_green(healthy, capsys):
    got = rows()
    assert tuple(got) == ALL_ROWS
    assert got["BrianDump"].ok is None and str(cli.REPO) in got["BrianDump"].detail
    for name in ALL_ROWS[1:]:
        assert got[name].ok is True, got[name]
    assert cli.main(["doctor"]) == 0
    text = capsys.readouterr().out
    assert "Everything BrianDump needs is in place." in text and "[FAIL]" not in text and "\x1b[" not in text   # no colour into a pipe


def test_json_output_is_the_list_of_checks(healthy, capsys):
    assert cli.main(["doctor", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in got] == list(ALL_ROWS)
    assert all(set(r) == {"name", "ok", "detail", "fix"} for r in got)


def test_one_failure_makes_the_exit_code_1_and_prints_its_fix(healthy, capsys):
    healthy.tools["claude"] = None
    got = rows()
    assert got["Claude Code"].ok is False and "https://code.claude.com/docs" in got["Claude Code"].fix
    assert got["Claude login"].ok is None                 # nothing to ask without the command
    assert cli.main(["doctor"]) == 1
    text = capsys.readouterr().out
    assert "[FAIL] Claude Code" in text and "fix: install Claude Code" in text and "1 problem:" in text


# ---------- single checks ----------
def test_windows_and_python(healthy, monkeypatch):
    assert "Windows 11 (build 26100)" in row(cli.check_windows).detail
    monkeypatch.setattr(cli, "_windows_build", lambda: None)
    assert row(cli.check_windows).ok is False
    healthy.answers["py"] = (0, "3.10.4\nC:/Python310/python.exe\n")
    r = row(cli.check_python)
    assert r.ok is False and "3.10.4" in r.detail and "winget install" in r.fix
    healthy.tools["py"] = None
    assert "py launcher is missing" in row(cli.check_python).detail


def test_missing_packages_fail_with_the_pip_line(healthy, monkeypatch):
    missing = (False, "ModuleNotFoundError: No module named 'pycaw'")
    monkeypatch.setattr(cli, "_import_ok", lambda m: missing if m == "pycaw" else (True, ""))
    r = row(cli.check_packages)
    assert r.ok is False and "pycaw" in r.detail and "requirements.txt" in r.fix


def test_panel_window(healthy, home, monkeypatch):
    assert row(cli.check_window).ok is True
    monkeypatch.setattr(cli, "EDGE", ())
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: None)
    r = row(cli.check_window)
    assert r.ok is False and "config --set window none" in r.fix
    home.save_user_value("window", "webview")
    assert row(cli.check_window).ok is False              # pywebview missing
    monkeypatch.setattr(cli, "_has_module", lambda module: True)
    assert row(cli.check_window).ok is True


def test_login_states(healthy):
    healthy.answers["claude auth status --json"] = (1, json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}))
    r = row(cli.check_login)
    assert r.ok is False and "claude auth login" in r.fix
    healthy.answers["claude auth status --json"] = (1, json.dumps({"loggedIn": False, "apiProvider": "bedrock"}))
    assert row(cli.check_login).ok is True                # cloud-provider credentials count
    healthy.answers["claude auth status --json"] = (1, "error: unknown command 'auth'")
    assert row(cli.check_login).ok is None                # an older CLI and no .claude.json here: cannot tell


def test_login_never_shows_the_account(healthy):
    healthy.answers["claude auth status --json"] = (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "x@example.com",
                                                                   "orgName": "Example Org"}))
    r = row(cli.check_login)
    assert r.ok is True and "example" not in r.detail.lower()


def test_git_is_recommended_never_required(healthy):
    healthy.tools["git"] = None
    r = row(cli.check_git)
    assert r.ok is None and "Bash and Monitor tools" in r.detail and "panel mode works without it" in r.detail
    assert r.fix.startswith("winget install --id Git.Git -e")
    assert not [c for c in cli.run_checks() if c.ok is False]          # the exit code ignores it


def test_settings_report_a_value_that_cannot_be_used(healthy, home):
    home.save_user_value("port", "eighty")                # written by hand, say: BrianDump runs on 8770 and doctor says why
    r = row(cli.check_config)
    assert r.ok is False and 'port = "eighty"' in r.detail and "using 8770" in r.detail and "config --set" in r.fix


def test_settings_missing_broken_and_moved(healthy, home, tmp_path):
    home.config_file.unlink()
    r = row(cli.check_config)
    assert r.ok is False and r.fix.endswith("config --init")
    home.config_file.write_text("{ not json", encoding="utf-8")
    assert "cannot be read" in row(cli.check_config).detail
    home.config_file.write_text(json.dumps({"port": 8770}), encoding="utf-8")
    assert "no install_dir" in row(cli.check_config).detail
    home.config_file.write_text(json.dumps({"install_dir": str(tmp_path / "gone")}), encoding="utf-8")
    assert "(moved?)" in row(cli.check_config).detail
    other = tmp_path / "other-copy"
    other.mkdir()
    (other / "briandump.py").write_text("", encoding="utf-8")
    home.config_file.write_text(json.dumps({"install_dir": str(other)}), encoding="utf-8")
    assert row(cli.check_config).ok is None              # another working copy: information, not a failure


def test_data_folder(healthy, home):
    assert row(cli.check_data).ok is True and not list(home.data_dir.glob(".briandump-doctor-*"))   # the probe file is gone
    home.save_user_value("data_dir", str(home.state_dir / "not-yet"))
    r = row(cli.check_data)
    assert r.ok is True and "created on first use" in r.detail


def test_transcripts_and_sessions(healthy, home, monkeypatch):
    assert "1 session transcript(s)" in row(cli.check_transcripts).detail
    (home.projects / "C--work" / "abc.jsonl").unlink()
    r = row(cli.check_transcripts)
    assert r.ok is None and "run claude" in r.fix
    assert "'Main' is one of them" in row(cli.check_sessions).detail
    monkeypatch.setattr(cli, "_registry", lambda: [{"name": "other", "alive": True}])
    r = row(cli.check_sessions)
    assert r.ok is None and "claude --name Main" in r.fix
    home.save_user_value("voice.mode", "monitor")
    assert "monitor" in row(cli.check_sessions).detail


def test_plugin_missing_disabled_and_present(healthy, home):
    f = home.claude_dir / "plugins" / "installed_plugins.json"
    f.unlink()
    r = row(cli.check_plugin)
    assert r.ok is False
    assert f'claude plugin marketplace add "{cli.REPO}"' in r.fix and "claude plugin install briandump@briandump" in r.fix
    assert "KnockKnockGoose/BrianDump" in r.fix
    write_plugin(home, enabled=False)
    r = row(cli.check_plugin)
    assert r.ok is False and r.fix == "claude plugin enable briandump@briandump"
    write_plugin(home, enabled=True)
    r = row(cli.check_plugin)
    assert r.ok is True and r.detail == "briandump@briandump 0.1.0 installed"


def test_speech_model_not_downloaded_is_a_tip_not_a_failure(healthy, monkeypatch, tmp_path):
    assert row(cli.check_model).ok is True
    monkeypatch.setattr(cli, "_hf_cache", lambda: tmp_path / "empty-cache")
    r = row(cli.check_model)
    assert r.ok is None and "about 460 MB" in r.detail and "WhisperModel('small')" in r.fix


def test_microphone(healthy, home, monkeypatch):
    assert "Windows default: Microphone (USB Audio)" in row(cli.check_mic).detail
    home.save_user_value("ptt.device", "headset")
    assert row(cli.check_mic).ok is True
    home.save_user_value("ptt.device", "Studio Mic")
    r = row(cli.check_mic)
    assert r.ok is False and "'Studio Mic'" in r.detail and "py briandump.py mics" in r.fix
    monkeypatch.setattr(cli, "_mic_list", lambda: [])
    assert "no microphone found" in row(cli.check_mic).detail

    def broken():
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(cli, "_mic_list", broken)
    assert row(cli.check_mic).ok is None


def test_talk_key_service_fresh_stale_and_loading(healthy, home):
    r = row(cli.check_service)
    assert r.ok is True and "hold F9 to talk, press F2" in r.detail
    write_status(home, age=120)
    r = row(cli.check_service)
    assert r.ok is False and "2 min ago" in r.detail and f"ptt-{home.host}.log" in r.fix and "Start-ScheduledTask" in r.fix
    write_status(home, age=1, ready=False)
    r = row(cli.check_service)
    assert r.ok is None and "loading the speech model" in r.detail
    write_status(home, age=1, ready=False, last_error="no input device")
    assert row(cli.check_service).ok is False
    home.ptt_status.unlink()
    assert row(cli.check_service).ok is False             # a task that never reported in


def test_no_task_fails_with_the_installer_and_quiets_the_service_row(healthy, home, monkeypatch):
    monkeypatch.setattr(cli, "_task_info", lambda name: None)
    write_status(home, age=600)
    got = rows([cli.check_task, cli.check_service])
    assert got["Talk-key task"].ok is False and "Install BrianDump.cmd" in got["Talk-key task"].fix
    assert got["Talk-key service"].ok is None
    monkeypatch.setattr(cli, "_task_info", lambda name: {"enabled": False, "args": ""})
    r = row(cli.check_task)
    assert r.ok is False and "Enable-ScheduledTask" in r.fix
    monkeypatch.setattr(cli, "_task_info", lambda name: {"enabled": True, "args": '"D:\\old copy\\briandump\\ptt_service.pyw"'})
    assert "another folder" in row(cli.check_task).detail


def test_panel_port_answers(healthy, monkeypatch):
    assert "BrianDump 0.1.0 answers on" in row(cli.check_panel).detail
    monkeypatch.setattr(cli, "_panel_probe", lambda port: ("briandump", {"version": "0.1.0", "data_dir": "D:/somewhere/else"}))
    assert row(cli.check_panel).ok is None
    monkeypatch.setattr(cli, "_panel_probe", lambda port: ("other", "pid 4242 (node.exe) answered HTTP 404"))
    r = row(cli.check_panel)
    assert r.ok is False and "node.exe" in r.detail and r.fix.endswith("config --set port 8771")


def test_panel_not_running(healthy, home, monkeypatch):
    monkeypatch.setattr(cli, "_panel_probe", lambda port: ("free", ""))
    r = row(cli.check_panel)                              # panel mode, talk key installed, nothing starts the server
    assert r.ok is False and "types your F9 words" in r.detail and "-Startup" in r.fix and "py briandump.py launch" in r.fix
    healthy.startup.parent.mkdir(parents=True)
    healthy.startup.write_bytes(b"")
    r = row(cli.check_panel)
    assert r.ok is None and "starts it at every sign-in" in r.detail
    healthy.startup.unlink()
    home.save_user_value("voice.mode", "monitor")         # nothing to type: just information
    assert row(cli.check_panel).ok is None
    home.save_user_value("voice.mode", "panel")
    monkeypatch.setattr(cli, "_task_info", lambda name: None)
    assert row(cli.check_panel).ok is None                # no talk key installed: nothing to type either


# ---------- the runner ----------
def test_a_crashing_check_is_reported_not_raised():
    def boom():
        raise RuntimeError("kaput")

    boom.label = "Boom"
    (r,) = cli.run_checks([boom])
    assert r.name == "Boom" and r.ok is None and "kaput" in r.detail and "doctor --json" in r.fix


def test_a_hanging_check_is_cut_off():
    def slow():
        time.sleep(3)
        return True, "late", ""

    slow.label = "Slow"
    t0 = time.monotonic()
    (r,) = cli.run_checks([slow], timeout=0.3)
    assert time.monotonic() - t0 < 2 and r.ok is None and "no answer within" in r.detail


def test_the_checks_run_side_by_side():
    def nap():
        time.sleep(0.4)
        return True, "rested", ""

    checks = []
    for i in range(6):
        def fn():
            return nap()
        fn.label = f"nap{i}"
        checks.append(fn)
    t0 = time.monotonic()
    got = cli.run_checks(checks)
    assert time.monotonic() - t0 < 1.5 and all(c.ok for c in got)    # six 0.4 s checks in parallel, not 2.4 s in a row


# ---------- the real probes, against sockets this test opens ----------
@contextmanager
def tiny_server(status, body, content_type="application/json"):
    payload = (json.dumps(body) if not isinstance(body, str) else body).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_panel_probe_tells_briandump_from_another_program_from_nothing(home):
    with tiny_server(200, {"version": "9.9", "data_dir": "x", "host": "h"}) as port:
        kind, info = cli._panel_probe(port)
        assert kind == "briandump" and info["version"] == "9.9"
    with tiny_server(200, "<html>another app</html>", "text/html") as port:
        kind, info = cli._panel_probe(port)
        assert kind == "other" and "HTTP 200" in info
    with tiny_server(404, {"error": "not found"}) as port:
        assert cli._panel_probe(port)[0] == "other"
    t0 = time.monotonic()
    assert cli._panel_probe(free_port()) == ("free", "")
    assert time.monotonic() - t0 < 1.5                    # no 2 s wait for a refused connection


def test_panel_probe_gives_up_on_a_silent_program(home):
    with socket.socket() as s:                            # accepts connections (the backlog), never answers
        s.bind(("127.0.0.1", 0))
        s.listen(4)
        t0 = time.monotonic()
        kind, info = cli._panel_probe(s.getsockname()[1])
        assert kind == "other" and "no answer within" in info and time.monotonic() - t0 < 4   # 1 s on Windows, 2.5 s elsewhere


@pytest.mark.win32
@pytest.mark.skipif(sys.platform != "win32", reason="the TCP table probe is Windows only")
def test_port_owner_reads_the_listening_table():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        pid, name = cli._port_owner(port)
        assert pid == os.getpid() and name.lower().startswith("python")
    assert cli._port_owner(port) is None


@pytest.mark.win32
@pytest.mark.skipif(sys.platform != "win32", reason="schtasks is Windows only")
def test_task_info_reads_a_real_task_and_a_missing_one():
    assert cli._task_info("BrianDump doctor test: no such task") is None
    info = cli._task_info(r"\Microsoft\Windows\Defrag\ScheduledDefrag")     # present on every Windows 10 and 11
    if info is None:
        pytest.skip("this Windows has no ScheduledDefrag task")
    assert set(info) == {"enabled", "args"} and isinstance(info["enabled"], bool)


# ---------- one real run ----------
def test_a_real_doctor_run_is_fast_and_valid_json(home):
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(REPO / "briandump.py"), "doctor", "--json"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60, cwd=str(REPO), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    took = time.monotonic() - t0
    assert took < 15, f"doctor took {took:.1f} s"
    assert r.returncode in (0, 1), r.stderr
    got = json.loads(r.stdout)
    assert [c["name"] for c in got] == list(ALL_ROWS)
    assert all(c["ok"] in (True, False, None) for c in got)
