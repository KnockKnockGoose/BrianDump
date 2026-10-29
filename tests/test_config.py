"""config.py: documented defaults, the user's file merged over them, paths, and the sandbox recorder."""
import json
import subprocess
import sys

import pytest

from briandump import config
from briandump.config import CONF, Config, deep_merge, strip_doc_keys


def test_defaults_come_from_the_example_without_doc_keys():
    d = config.defaults()
    assert d["port"] == 8770 and d["bind"] == "127.0.0.1" and d["allow"] == []
    assert d["voice"]["mode"] == "panel" and d["voice"]["main_session"] == "Main"
    assert d["ptt"]["key"] == "f9" and d["ptt"]["dump_key"] == "f2"
    assert d["dump"]["processor"] == "claude"
    assert not any(k.startswith("_") for k in d) and not any(k.startswith("_") for k in d["ptt"])


def test_strip_and_merge():
    assert strip_doc_keys({"_c": 1, "a": {"_d": 2, "b": [{"_e": 3, "f": 4}]}}) == {"a": {"b": [{"f": 4}]}}
    base = {"a": 1, "n": {"x": 1, "y": 2}, "l": [1, 2]}
    out = deep_merge(base, {"n": {"y": 3}, "l": [9]})
    assert out == {"a": 1, "n": {"x": 1, "y": 3}, "l": [9]} and base["n"]["y"] == 2


def test_home_fixture_points_everything_at_a_temp_dir(home, tmp_path):
    assert home is CONF and home.sandbox and not home.outward and home.host == "testhost"
    for p in (home.state_dir, home.data_dir, home.inbox, home.state, home.dumps_dir, home.reports_dir, home.agenda_file):
        assert str(p).startswith(str(tmp_path)), p
    assert home.projects == tmp_path / "claude" / "projects" and home.projects.is_dir()
    assert home.agenda_file == home.data_dir / "agenda.md" and home.questions_file == home.data_dir / "questions.md"


def test_user_file_overrides_and_new_keys_keep_defaults(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    (h / "config.json").write_text(json.dumps({"_comment": "x", "port": 9999, "ptt": {"key": "f8"}, "allow": ["10.0.0.5"]}),
                                   encoding="utf-8")
    c = Config(env={"BRIANDUMP_HOME": str(h)})
    assert c.port == 9999 and c.get("ptt.key") == "f8" and c.get("ptt.dump_key") == "f2"
    assert c.allowed == {"127.0.0.1", "::1", "10.0.0.5"}
    assert c.get("no.such.key", "dflt") == "dflt" and c.get("dump.model", "fallback") == "fallback"   # null -> default


def test_broken_user_file_falls_back_to_defaults(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    (h / "config.json").write_text("{ not json", encoding="utf-8")
    c = Config(env={"BRIANDUMP_HOME": str(h)})
    assert c.port == 8770 and c.bind == "127.0.0.1"


def test_a_file_that_cannot_be_used_is_never_written_over(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    broken = '{"port": 8771, "private_filter": ["garden"],}'
    (h / "config.json").write_text(broken, encoding="utf-8")
    c = Config(env={"BRIANDUMP_HOME": str(h)})
    assert c.port == 8770 and "syntax error" in c.load_error and "column" in c.load_error      # the defaults, and the reason
    with pytest.raises(config.ConfigError, match="Nothing was changed"):
        c.save_user_value("ptt.key", "f7")
    (h / "config.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="not a JSON object"):
        c.save_user_value("ptt.key", "f7")
    assert (h / "config.json").read_text(encoding="utf-8") == "[1, 2]"
    (h / "config.json").write_bytes(b'\xef\xbb\xbf{"port": 8772}')                           # a BOM (Notepad, PowerShell 5.1)
    c = Config(env={"BRIANDUMP_HOME": str(h)})
    assert c.port == 8772 and c.load_error is None
    c.save_user_value("ptt.key", "f7")
    assert json.loads((h / "config.json").read_text(encoding="utf-8")) == {"port": 8772, "ptt": {"key": "f7"}}


def test_a_bad_value_gets_its_default_and_a_problem_line(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    bad = {"port": "eighty", "context_limit": -5, "scale": True, "fraction": "most", "voice": ["x"], "allow": "10.0.0.5",
           "private_filter": ["(draft", "dentist", "(?i)zebra"], "dump": {"user_name": "Sam"}}
    (h / "config.json").write_text(json.dumps(bad), encoding="utf-8")
    c = Config(env={"BRIANDUMP_HOME": str(h)})                                   # no exception: every command still starts
    assert (c.port, c.context_limit, c.scale, round(c.fraction, 3)) == (8770, 200_000, 1.0, 0.333)
    assert c.voice["mode"] == "panel" and c.get("voice.main_session") == "Main" and c.allowed == {"127.0.0.1", "::1"}
    assert c.dump["user_name"] == "Sam"                                       # good values next to bad ones are kept
    assert len(c.problems) == 7 and any("port" in p and "eighty" in p for p in c.problems)
    rx = c.private_re                                                         # a bad pattern counts as plain text; (?i) is
    assert rx.search("the (draft plan") and rx.search("ZEBRA crossing") and rx.search("Dentist")   # fine anywhere in the list
    assert not rx.search("timber") and not rx.search("draft")
    good = Config(env={"BRIANDUMP_HOME": str(tmp_path / "fresh")})
    assert good.problems == [] and good.load_error is None


def test_private_filter_masks_and_empty_matches_nothing(tmp_path):
    c = Config(env={"BRIANDUMP_HOME": str(tmp_path)})
    assert not c.private_re.search("anything at all")
    (tmp_path / "config.json").write_text(json.dumps({"private_filter": ["secret project", "dentist"]}), encoding="utf-8")
    c = Config(env={"BRIANDUMP_HOME": str(tmp_path)})
    assert c.private_re.search("Call the DENTIST") and not c.private_re.search("buy timber")


def test_save_user_value_writes_the_file_and_rederives(home):
    home.save_user_value("ptt.key", "scroll_lock")
    raw = json.loads(home.config_file.read_text(encoding="utf-8"))
    assert raw["ptt"]["key"] == "scroll_lock" and "_comment" in raw    # the documented example was copied first
    assert home.get("ptt.key") == "scroll_lock"
    home.save_user_value("port", 8800)
    assert home.port == 8800


def test_save_user_value_keeps_an_explicit_environment(tmp_path):
    c = Config(env={"BRIANDUMP_HOME": str(tmp_path / "x")})
    c.save_user_value("ptt.key", "f8")
    assert c.state_dir == (tmp_path / "x").resolve() and c.get("ptt.key") == "f8"


def test_replace_retry_waits_out_a_locked_target(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("new", encoding="utf-8")
    calls, real = [], config.os.replace

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")      # what Windows says while a reader holds the file
        return real(a, b)

    monkeypatch.setattr(config.os, "replace", flaky)
    config.replace_retry(src, dst, wait=0)
    assert len(calls) == 3 and dst.read_text(encoding="utf-8") == "new"
    monkeypatch.setattr(config.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "denied")))
    with pytest.raises(PermissionError):
        config.replace_retry(src, dst, tries=2, wait=0)


def test_record_only_in_the_sandbox(home, tmp_path):
    assert home.record("open_url", url="https://example.com") is True
    assert home.side_effect_log()[-1]["kind"] == "open_url"
    real = Config(env={"BRIANDUMP_HOME": str(tmp_path / "real")})
    assert real.record("open_url", url="x") is False and not real.side_effects.exists()


def test_model_guard_blocks_claude_print():
    with pytest.raises(RuntimeError, match="must not call the model"):
        subprocess.Popen(["claude", "-p", "hello"])
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok"
