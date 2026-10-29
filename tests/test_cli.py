"""briandump.py and briandump/cli.py: the commands, each against a throwaway home (the `home` fixture). No model call, nothing
outward: the sandbox is on, and the commands that hand over to another module are checked with a stand-in module."""
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from briandump import __version__, cli

REPO = Path(__file__).resolve().parents[1]
SHIM = REPO / "briandump.py"


def run_shim(*args):
    """briandump.py as a subprocess; it inherits the test's environment (BRIANDUMP_HOME, the sandbox)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(SHIM), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, cwd=str(REPO), env=env)


def same_path(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


# ---------- the shim, help, version ----------
def test_version_through_the_shim(home):
    r = run_shim("version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"BrianDump {__version__}"


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    text = capsys.readouterr().out
    for command in ("launch", "serve", "ptt", "mics", "config", "ask", "agenda", "dump", "doctor", "version"):
        assert f"    {command}" in text, command


def test_no_command_prints_the_help(capsys):
    assert cli.main([]) == 2
    assert "usage: py briandump.py" in capsys.readouterr().out


def test_output_survives_pythonw_and_narrow_code_pages(home, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)            # pythonw: no console at all
    monkeypatch.setattr(sys, "stderr", None)
    assert cli.main(["version"]) == 0
    assert cli.main(["ask", "--list"]) == 0
    assert cli.main(["config", "--set", "port", "abc"]) == 2
    buf = io.BytesIO()
    narrow = io.TextIOWrapper(buf, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", narrow)
    cli.out("mic → Realtek®")                  # the arrow is not in cp1252, the (R) is
    assert buf.getvalue() == "mic ? Realtek®\n".encode("cp1252")


# ---------- config ----------
def test_config_init_from_nothing(home, tmp_path, monkeypatch):
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("BRIANDUMP_HOME", str(fresh))
    r = run_shim("config", "--init")
    assert r.returncode == 0, r.stderr
    raw = json.loads((fresh / "config.json").read_text(encoding="utf-8"))
    assert same_path(raw["install_dir"], REPO) and "_comment" in raw          # the documented example, plus install_dir
    for sub in ("state", "inbox", "logs", "data/dumps", "data/reports"):
        assert (fresh / sub).is_dir(), sub
    assert "new, from config.example.json" in r.stdout
    again = run_shim("config", "--init")
    assert again.returncode == 0 and "kept" in again.stdout
    assert json.loads((fresh / "config.json").read_text(encoding="utf-8")) == raw


def test_config_set_parses_json_and_keeps_plain_text(home, capsys):
    rc = cli.main(["config", "--set", "ptt.key", "f8", "--set", "port", "8771", "--set", "ptt.beeps", "false",
                   "--set", "scale", "1.25", "--set", "ptt.device", "USB Audio", "--set", "allow", '["192.0.2.10"]'])
    assert rc == 0
    raw = json.loads(home.config_file.read_text(encoding="utf-8"))
    assert raw["ptt"]["key"] == "f8" and raw["ptt"]["device"] == "USB Audio" and raw["ptt"]["beeps"] is False
    assert raw["port"] == 8771 and raw["scale"] == 1.25 and raw["allow"] == ["192.0.2.10"]
    assert home.port == 8771 and home.get("ptt.key") == "f8" and "192.0.2.10" in home.allowed     # CONF re-derived in place
    assert cli.main(["config", "--set", "ptt.device", "null", "--set", "ptt.key", "1"]) == 0
    raw = json.loads(home.config_file.read_text(encoding="utf-8"))
    assert raw["ptt"]["device"] is None and raw["ptt"]["key"] == "1"                            # a text setting stays text


@pytest.mark.parametrize("pair, why", [
    (("port", "abc"), "whole number"),
    (("port", "70000"), "1 to 65535"),
    (("scale", "big"), "takes a number"),
    (("ptt.beeps", "maybe"), "true or false"),
    (("voice", "x"), "group of settings"),
    (("allow", "192.0.2.10"), "JSON list"),
    (("_comment", "x"), "not a setting name"),
    (("private_filter", '["(oops"]'), "not a valid pattern"),
])
def test_config_set_refuses_bad_values_and_writes_nothing(home, capsys, pair, why):
    assert cli.main(["config", "--set", "ptt.key", "f8", "--set", *pair]) == 2
    assert why in capsys.readouterr().err
    assert not home.config_file.exists()                  # every pair is checked before any is written


def test_config_unknown_key_is_kept_with_a_note(home, capsys):
    assert cli.main(["config", "--set", "ptt.kye", "f8"]) == 0
    assert "check the spelling" in capsys.readouterr().out


def test_config_show_path_and_open(home, capsys):
    assert cli.main(["config"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["state_dir"] == str(home.state_dir) and shown["settings"]["ptt"]["key"] == "f9" and shown["sandbox"] is True
    assert cli.main(["config", "--path"]) == 0
    assert capsys.readouterr().out.strip() == str(home.config_file)
    assert cli.main(["config", "--open"]) == 0           # sandbox: recorded, not opened
    assert home.side_effect_log()[-1]["kind"] == "open_file" and home.config_file.exists()


def test_config_save_retries_a_file_held_open(home, monkeypatch):
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(str(dst))
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied", str(dst))
        return real(src, dst)

    monkeypatch.setattr(cli.os, "replace", flaky)
    monkeypatch.setattr(cli, "_pause", lambda: None)
    assert cli.main(["config", "--set", "ptt.key", "f8"]) == 0
    assert home.get("ptt.key") == "f8" and len(calls) >= 3


# ---------- ask: questions.json ----------
def test_ask_round_trip_in_the_shape_the_server_reads(home, capsys):
    assert cli.main(["ask", "Paint the shed green or grey?", "--options", "green|grey"]) == 0
    assert capsys.readouterr().out.startswith("q1 is on the BrianDump panel: Paint the shed green or grey?")
    assert cli.main(["ask", "Is", "the", "gutter", "worth", "fixing", "this", "year?", "--kind", "think"]) == 0
    qs = json.loads(home.manual_questions.read_text(encoding="utf-8"))
    assert [set(q) for q in qs] == [{"id", "text", "kind", "options", "asked", "done"}] * 2
    assert qs[0]["options"] == ["green", "grey"] and qs[0]["done"] is False and qs[0]["kind"] == "ask"
    assert qs[1]["id"] == "q2" and qs[1]["text"] == "Is the gutter worth fixing this year?" and qs[1]["kind"] == "think"
    capsys.readouterr()
    assert cli.main(["ask", "--list"]) == 0
    listed = capsys.readouterr().out
    assert "q1 [ask] Paint the shed green or grey?  [green | grey]" in listed and "q2 [think]" in listed
    assert cli.main(["ask", "--done", "1", "--answer", "grey"]) == 0
    first = json.loads(home.manual_questions.read_text(encoding="utf-8"))[0]
    assert first["done"] is True and first["answer"] == "grey" and first["done_at"]
    capsys.readouterr()
    cli.main(["ask", "--list"])
    assert "q1" not in capsys.readouterr().out
    cli.main(["ask", "--list", "--all"])
    assert "q1 [done] Paint the shed green or grey?  [green | grey]  -> grey" in capsys.readouterr().out
    assert cli.main(["ask", "--done", "q9"]) == 1
    assert cli.main(["ask"]) == 2


def test_the_panel_server_reads_what_ask_writes(home):
    server = pytest.importorskip("briandump.server")
    cli.main(["ask", "Black hinges or silver?", "--options", "black|silver"])
    q = server.manual_questions()[0]
    assert (q["id"], q["text"], q["kind"], q["options"], q["done"]) == ("q1", "Black hinges or silver?", "ask", ["black", "silver"], False)
    server.mark_manual_done("q1")                         # a panel answer closes it in the same file
    assert cli.load_questions()[0]["done"] is True


def test_ask_records_the_session_that_asked(home, monkeypatch):
    docs, older = "66666666-7777-4888-8999-000000000000", "77777777-0000-4000-8000-000000000007"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", docs)                       # what Claude Code sets for the commands it runs
    assert cli.main(["ask", "Archive the old docs?"]) == 0
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CLAUDE_PID", "4243")                                 # no session id: the pid's registry record names it
    (home.sessions / "4243.json").write_text(json.dumps({"pid": 4243, "sessionId": older}), encoding="utf-8")
    assert cli.main(["ask", "Rename the folder?"]) == 0
    monkeypatch.delenv("CLAUDE_PID")
    assert cli.main(["ask", "Asked by hand?"]) == 0                          # outside Claude Code: no session
    assert [q.get("session") for q in cli.load_questions()] == [docs, older, None]
    assert cli.asking_session({"CLAUDE_CODE_SESSION_ID": "../../x", "CLAUDE_PID": "12; rm"}) is None


def test_ask_does_not_repeat_an_open_question(home, capsys):
    cli.main(["ask", "Order the blue tiles?"])
    capsys.readouterr()
    assert cli.main(["ask", "order the blue tiles"]) == 0
    assert "q1 is already on the BrianDump panel" in capsys.readouterr().out
    assert len(cli.load_questions()) == 1


def test_ask_keeps_the_file_small(home):
    qs = [{"id": f"q{i}", "text": f"old {i}", "kind": "ask", "options": [], "asked": "2026-01-01T00:00:00", "done": i % 2 == 0}
          for i in range(1, 251)]
    cli.write_json_atomic(home.manual_questions, qs)
    assert cli.main(["ask", "One more?"]) == 0
    kept = cli.load_questions()
    assert len(kept) == 200 and kept[-1]["id"] == "q251"
    assert all(not q["done"] for q in kept[:-1][:50])     # the oldest closed ones went first


def test_ask_retries_while_the_panel_holds_the_file(home, monkeypatch):
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(str(dst))
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied", str(dst))
        return real(src, dst)

    monkeypatch.setattr(cli.os, "replace", flaky)
    monkeypatch.setattr(cli, "_pause", lambda: None)
    assert cli.main(["ask", "Is this saved?"]) == 0
    assert len(calls) == 3 and cli.load_questions()[0]["text"] == "Is this saved?"


def test_ask_gives_up_with_a_clear_message(home, monkeypatch, capsys):
    def locked(src, dst):
        raise PermissionError(5, "Access is denied", str(dst))

    monkeypatch.setattr(cli.os, "replace", locked)
    monkeypatch.setattr(cli, "_pause", lambda: None)
    assert cli.main(["ask", "Will this fail?"]) == 1
    assert "another program kept it open (20 tries)" in capsys.readouterr().err
    assert not home.manual_questions.exists() and not list(home.state.glob("*.tmp"))


# ---------- agenda ----------
def test_agenda_add_list_done_undone(home, capsys):
    pytest.importorskip("briandump.agenda")
    assert cli.main(["agenda", "add", "Order", "more", "sandpaper"]) == 0
    assert "added to the agenda: Order more sandpaper" in capsys.readouterr().out
    assert cli.main(["agenda", "add", "order more sandpaper!"]) == 0
    assert "already on the agenda" in capsys.readouterr().out
    assert "- [ ] Order more sandpaper" in home.agenda_file.read_text(encoding="utf-8")
    assert cli.main(["agenda", "list"]) == 0
    assert "- [ ] Order more sandpaper" in capsys.readouterr().out
    assert cli.main(["agenda", "done", "order more sandpaper"]) == 0
    assert "- [x] Order more sandpaper" in home.agenda_file.read_text(encoding="utf-8")
    capsys.readouterr()
    cli.main(["agenda", "list"])
    out = capsys.readouterr().out
    assert "sandpaper" not in out and "(0 open, 1 done" in out
    cli.main(["agenda", "list", "--all"])
    assert "- [x] Order more sandpaper" in capsys.readouterr().out
    assert cli.main(["agenda", "undone", "Order more sandpaper"]) == 0
    assert "- [ ] Order more sandpaper" in home.agenda_file.read_text(encoding="utf-8")
    assert cli.main(["agenda", "done", "something else entirely"]) == 1
    assert cli.main(["agenda", "add"]) == 2


# ---------- dump ----------
def test_dump_a_transcript_now_with_the_mock_processor(home, tmp_path, capsys):
    pytest.importorskip("briandump.dumps")
    pytest.importorskip("briandump.cards")
    src = tmp_path / "note.txt"
    src.write_text("Fix the gate latch this weekend. Ask the hardware store about longer screws. Maybe paint the shed.", encoding="utf-8")
    assert cli.main(["dump", str(src), "--now", "--processor", "mock"]) == 0
    out = capsys.readouterr().out
    did = out.split("imported as dump ")[1].split(";")[0]
    report = Path(out.split("report: ")[1].splitlines()[0])
    assert report.is_file() and same_path(report.parent, home.reports_dir) and report.name.startswith(did)
    meta = json.loads((home.dumps_dir / did / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ready" and meta["processor"] == "mock" and meta["source"] == "import"


def test_dump_without_now_leaves_it_to_the_panel_server(home, tmp_path, capsys):
    pytest.importorskip("briandump.dumps")
    src = tmp_path / "later.txt"
    src.write_text("Something to think about later.", encoding="utf-8")
    assert cli.main(["dump", str(src), "--label", "later"]) == 0
    did = capsys.readouterr().out.split("imported as dump ")[1].split(":")[0]
    meta = json.loads((home.dumps_dir / did / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "transcribed" and meta["label"] == "later"


def test_dump_refuses_what_it_cannot_import(home, tmp_path, capsys):
    pytest.importorskip("briandump.dumps")
    assert cli.main(["dump", str(tmp_path / "missing.txt")]) == 2
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert cli.main(["dump", str(tmp_path / "empty.txt")]) == 2
    (tmp_path / "notes.pdf").write_text("x", encoding="utf-8")
    assert cli.main(["dump", str(tmp_path / "notes.pdf")]) == 2
    assert "cannot import notes.pdf" in capsys.readouterr().err


# ---------- the commands that hand over to another module ----------
@pytest.mark.parametrize("command, module", [("ptt", "briandump.ptt"), ("serve", "briandump.server"), ("launch", "briandump.launch")])
def test_passthrough_hands_the_arguments_over_untouched(monkeypatch, command, module):
    seen = []
    fake = types.ModuleType(module)
    fake.main = lambda argv=None: seen.append(argv) or 0
    monkeypatch.setitem(sys.modules, module, fake)
    assert cli.main([command, "--help", "--key", "f8"]) == 0
    assert seen == [["--help", "--key", "f8"]]


def test_a_missing_module_says_what_to_do(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "briandump.ptt", None)  # importing it now raises ModuleNotFoundError
    assert cli.main(["ptt"]) == 1
    assert "briandump/ptt.py is missing" in capsys.readouterr().err


def test_mics_text_and_json(monkeypatch, capsys):
    fake = types.ModuleType("briandump.list_mics")
    fake.devices = lambda: [{"index": 1, "name": "Mikrofon (Realtek® Audio)", "default": True},
                            {"index": 4, "name": "Headset", "default": False}]
    monkeypatch.setitem(sys.modules, "briandump.list_mics", fake)
    assert cli.main(["mics"]) == 0
    assert "  1  Mikrofon (Realtek® Audio)   (default)" in capsys.readouterr().out
    assert cli.main(["mics", "--json"]) == 0
    text = capsys.readouterr().out
    assert text.isascii() and json.loads(text)[0]["name"] == "Mikrofon (Realtek® Audio)"   # escaped for PowerShell
    fake.devices = lambda: []
    assert cli.main(["mics"]) == 1
    assert cli.main(["mics", "--json"]) == 0 and json.loads(capsys.readouterr().out.splitlines()[-1]) == []
