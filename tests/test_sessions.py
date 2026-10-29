"""Tests for briandump/sessions.py: Claude Code's live-session registry + transcript tail -> status rows.

Registry files and transcripts are fixtures in a temp ~/.claude (the `home` fixture); the process table is a stub.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from briandump import sessions

SID = "0a1b2c3d-1111-2222-3333-444455556666"


def reg(home, pid, sid=SID, name=None, entrypoint="cli", status="idle", **extra):
    d = {"pid": pid, "sessionId": sid, "cwd": "C:\\work\\project", "entrypoint": entrypoint, "status": status,
         "startedAt": 1_792_000_000_000, "version": "2.1.302", "messagingSocketPath": "\\\\.\\pipe\\x", **extra}
    if name:
        d["name"] = name
    (home.sessions / f"{pid}.json").write_text(json.dumps(d), encoding="utf-8")
    return d


def transcript(home, sid=SID, lines=(), project="C--work-project"):
    p = home.projects / project / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines), encoding="utf-8")
    return p


def user(text, ts="2026-10-15T10:00:00.000Z"):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts}


def assistant(ts="2026-10-15T10:00:01.000Z", tools=(), text="ok", model="claude-opus-5-5", usage=True):
    content = [{"type": "text", "text": text}] + [{"type": "tool_use", "id": tid, "name": name, "input": inp} for tid, name, inp in tools]
    msg = {"role": "assistant", "model": model, "content": content}
    if usage:
        msg["usage"] = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 5, "output_tokens": 20}
    return {"type": "assistant", "timestamp": ts, "message": msg}


def result(tid, ts="2026-10-15T10:00:02.000Z"):
    block = {"type": "tool_result", "tool_use_id": tid, "content": "done"}
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [block]}}


def turn_end(ts="2026-10-15T10:00:03.000Z"):
    return {"type": "system", "subtype": "turn_duration", "durationMs": 1000, "timestamp": ts}


def rows(home, alive=lambda pid: True, now=None):
    return sessions.rows(now=now, sessions_dir=home.sessions, projects_dir=home.projects, needs_you=home.needs_you, host="testhost",
                         tails={}, alive=alive)


def test_idle_session_row_has_the_router_fields(home):
    reg(home, 4242, name="Main")
    transcript(home, lines=[user("hi"), assistant(), turn_end()])
    (r,) = rows(home)
    assert r["status"] == "idle" and r["kind"] == "session" and r["id"] == SID and r["name"] == "Main" and r["pid"] == 4242
    assert r["machine"] == "testhost" and r["reg_status"] == "idle" and r["project"] == "project" and r["note"] == ""
    assert r["model"] == "opus" and r["context"] == 1035 and r["last_activity"] and r["started"].endswith("Z")


def test_unnamed_session_is_named_by_its_id(home):
    reg(home, 4242)
    transcript(home, lines=[user("hi"), assistant(), turn_end()])
    assert rows(home)[0]["name"] == SID[:8]


def test_running_and_stuck(home):
    reg(home, 4242, name="Main", status="busy")
    p = transcript(home, lines=[user("go"), assistant(tools=[("t1", "Bash", {"description": "list files", "command": "ls"})])])
    (r,) = rows(home)
    assert r["status"] == "running" and r["step"] == "Bash: list files" and not r["stuck"] and r["reg_status"] == "busy"
    old = time.time() - 20 * 60
    os.utime(p, (old, old))
    (r,) = rows(home)
    assert r["status"] == "running" and r["stuck"] and "no transcript activity" in r["note"]


def test_tool_result_and_turn_end_make_it_idle_again(home):
    reg(home, 4242, name="Main")
    transcript(home, lines=[user("go"), assistant(tools=[("t1", "Read", {"file_path": "C:\\a\\b.py"})]), result("t1"),
                            assistant(ts="2026-10-15T10:00:04.000Z"), turn_end("2026-10-15T10:00:05.000Z")])
    assert rows(home)[0]["status"] == "idle"


def test_thinking_while_a_turn_runs_without_open_calls(home):
    reg(home, 4242, name="Main")
    transcript(home, lines=[user("go")])                                     # fresh, no reply yet
    assert rows(home)[0]["status"] == "thinking"
    assert rows(home, now=time.time() + 600)[0]["status"] == "idle"          # silent for > 5 min: idle


def test_needs_you_from_an_open_question_or_the_hook(home):
    reg(home, 4242, name="Main")
    transcript(home, lines=[user("go"), assistant(tools=[("q1", "AskUserQuestion", {"questions": []})])])
    (r,) = rows(home)
    assert r["status"] == "needs_you" and r["step"] == "Asking you a question"
    reg(home, 4243, sid="99999999-1111-2222-3333-444455556666", name="Other")
    transcript(home, sid="99999999-1111-2222-3333-444455556666", lines=[user("go"), assistant(), turn_end()])
    home.needs_you.write_text(json.dumps({"ts": "2026-10-15T10:00:09.000Z", "message": "Claude needs your permission",
                                          "type": "permission_prompt", "session": "99999999-1111-2222-3333-444455556666"}),
                              encoding="utf-8")
    other = next(r for r in rows(home) if r["name"] == "Other")
    assert other["status"] == "needs_you" and other["step"] == "Claude needs your permission"


def test_needs_you_flag_rules(home):
    p = home.needs_you
    p.write_text(json.dumps({"ts": "2026-10-15T10:00:00.500Z", "type": "permission_prompt", "session": SID}), encoding="utf-8")
    assert sessions.needs_you_for(SID, "2026-10-15T10:00:00.100Z", p)       # transcript not past it yet (same second)
    assert sessions.needs_you_for(SID, "2026-10-15T10:00:00.900Z", p) is None   # the transcript moved on
    assert sessions.needs_you_for("someone-else", None, p) is None
    p.write_text(json.dumps({"ts": "2026-10-15T10:00:00.500Z", "type": "idle_prompt", "session": SID}), encoding="utf-8")
    assert sessions.needs_you_for(SID, None, p) is None
    p.write_text("[1, 2]", encoding="utf-8")
    assert sessions.needs_you_for(SID, None, p) is None


def test_registry_waiting_status_is_needs_you(home):
    reg(home, 4242, name="Main", status="waiting")
    transcript(home, lines=[user("go"), assistant(), turn_end()])
    assert rows(home)[0]["status"] == "needs_you"


def test_headless_note_survives_a_stuck_note(home):
    reg(home, 4242, entrypoint="sdk-cli")
    p = transcript(home, lines=[user("go"), assistant(tools=[("t1", "Bash", {"command": "sleep 999"})])])
    assert rows(home)[0]["note"] == "headless"
    old = time.time() - 30 * 60
    os.utime(p, (old, old))
    note = rows(home)[0]["note"]
    assert note.startswith("headless") and "no transcript activity" in note    # still never a voice target


def test_dead_and_malformed_registry_entries_are_skipped(home):
    reg(home, 4242, name="Main")
    reg(home, 4243, sid="99999999-1111-2222-3333-444455556666", name="Crashed")
    (home.sessions / "4244.json").write_text("{not json", encoding="utf-8")
    (home.sessions / "4245.json").write_text("[1, 2, 3]", encoding="utf-8")
    (home.sessions / "4246.json").write_text(json.dumps({"pid": 4246}), encoding="utf-8")          # no sessionId
    reg(home, 4247, sid="../../etc/*", name="Glob")                                                  # never a glob pattern
    reg(home, 4248, sid="88888888-1111-2222-3333-444455556666", name="Odd", startedAt="not a number")
    transcript(home, lines=[user("hi"), assistant(), turn_end()])
    names = sorted(r["name"] for r in rows(home, alive=lambda pid: pid != 4243))
    assert names == ["Main", "Odd"]                                          # one odd record never hides the others


def test_key_files_are_never_opened(home, monkeypatch):
    reg(home, 4242, name="Main")
    (home.sessions / "4242.0123456789abcdef.key").write_text("peer secret", encoding="utf-8")
    (home.sessions / "4242.extra.json").write_text(json.dumps({"sessionId": "x"}), encoding="utf-8")
    opened = []
    real = type(home.sessions).read_text

    def spy(self, *a, **k):
        opened.append(self.name)
        return real(self, *a, **k)
    monkeypatch.setattr(type(home.sessions), "read_text", spy)
    found = sessions.read_registry(home.sessions, alive=lambda pid: True)
    assert [d["pid"] for d in found] == [4242] and "messagingSocketPath" not in found[0]
    assert opened == ["4242.json"]                                           # not the .key file, not the two-dot name


def test_tail_skips_big_lines_but_keeps_their_tool_ids(home):
    reg(home, 4242, name="Main")
    big = {"type": "user", "timestamp": "2026-10-15T10:00:02.000Z",
           "message": {"content": [{"type": "tool_result", "tool_use_id": "shot1", "content": "x" * (sessions.BIG + 10)}]}}
    transcript(home, lines=[user("go"), assistant(tools=[("shot1", "computer", {})]), big, turn_end()])
    assert rows(home)[0]["status"] == "idle"


def test_tail_reads_incrementally_and_restarts_on_truncation(home):
    p = transcript(home, lines=[user("go"), assistant(tools=[("t1", "Bash", {"command": "x"})])])
    tl = sessions.Tail(p)
    tl.update()
    assert [c["name"] for c in tl.open_calls()] == ["Bash"] and not tl.idle
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(result("t1")) + "\n" + json.dumps(turn_end()))   # the last line is still being written
    tl.update()
    assert not tl.open_calls() and not tl.idle
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n")
    tl.update()
    assert tl.idle
    p.write_text(json.dumps(user("new")) + "\n", encoding="utf-8")         # replaced by a shorter file
    tl.update()
    assert not tl.idle and tl.offset == p.stat().st_size


def test_tail_survives_odd_lines(home):
    p = transcript(home, lines=[{"type": "assistant", "message": {"usage": {"input_tokens": None, "output_tokens": "x"}}},
                                {"type": "assistant", "message": "not a dict"}, [1, 2], user("go"), turn_end()])
    tl = sessions.Tail(p)
    tl.update()
    assert tl.idle


def test_title_summary_and_sidechain(home):
    reg(home, 4242, name="Main")
    transcript(home, lines=[user("go"), {"type": "ai-title", "aiTitle": "Fix the heading"},
                            {"type": "system", "subtype": "away_summary", "content": "Working on the heading."},
                            {"type": "assistant", "isSidechain": True,
                             "message": {"content": [{"type": "tool_use", "id": "s1", "name": "Bash"}]}},
                            assistant(), turn_end()])
    (r,) = rows(home)
    assert r["title"] == "Fix the heading" and r["summary"] == "Working on the heading." and r["status"] == "idle"


def test_find_transcript_cache_is_per_projects_folder(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "proj").mkdir(parents=True)
        (root / "proj" / f"{SID}.jsonl").write_text("", encoding="utf-8")
    assert sessions.find_transcript(SID, a).parent.parent == a
    assert sessions.find_transcript(SID, b).parent.parent == b             # not the cached path from the other folder
    assert sessions.find_transcript("*", a) is None and sessions.find_transcript("", a) is None


def test_pid_alive_in_the_sandbox_uses_the_fixture_table(home):
    assert home.sandbox
    assert sessions.pid_alive(4242)                                          # no procs.json: trust the fixture registry
    (home.state_dir / "procs.json").write_text(json.dumps({"alive": [4242]}), encoding="utf-8")
    assert sessions.pid_alive(4242) and not sessions.pid_alive(4243) and not sessions.pid_alive(None)


def test_os_pid_alive_uses_the_real_process_table():
    assert sessions.os_pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not sessions.os_pid_alive(p.pid)
    assert not sessions.os_pid_alive(None) and not sessions.os_pid_alive("x") and not sessions.os_pid_alive(-5)


@pytest.mark.win32
@pytest.mark.skipif(sys.platform != "win32", reason="process creation times via GetProcessTimes")
def test_a_reused_pid_is_not_the_session():
    started_long_ago_ms = (time.time() - 3 * 3600) * 1000                  # the session started before this process existed
    assert not sessions.os_pid_alive(os.getpid(), started_long_ago_ms)
    assert sessions.os_pid_alive(os.getpid(), time.time() * 1000)           # started after this process: it is the session


def test_helpers():
    assert sessions.short_model("claude-opus-5-5") == "opus" and sessions.short_model("sonnet") == "sonnet"
    assert sessions.short_model(None) is None
    assert sessions.project_name("C:\\Users\\someone\\Code\\BrianDump\\") == "BrianDump"
    assert sessions.project_name("/home/someone", home="/home/someone") == "home" and sessions.project_name("") == ""
    assert sessions.epoch("2026-10-15T10:00:00Z") == sessions.epoch("2026-10-15T10:00:00+00:00")
    assert sessions.epoch("garbage") is None and sessions.epoch(None) is None
    assert sessions.mins(59) == "59 s" and sessions.mins(120) == "2 min" and sessions.mins(3700) == "1 h 1 min"
    assert sessions.describe("Edit", {"file_path": "C:\\a\\server.py"}) == "Edit server.py"
    assert sessions.describe("Agent", {"description": "review"}) == "Agent: review"
    assert sessions.block_text([{"type": "text", "text": "a"}, {"type": "image"}]) == "a"
