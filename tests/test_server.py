"""Tests for briandump/server.py (the panel server) and the window launchers: transcript parsing, the two inbox writers, the
HTTP API and its guards, the dump endpoints, the port check, and imports without side effects.

    py -m pytest tests/test_server.py -q

Everything runs against conftest's `home` (a throwaway BrianDump home, sandbox on). The API is served in-process on a free
port; the port checks start `py -m briandump.server` on free ports (never the panel's own 8765 / 8770). Transcript lines are
small hand-made copies of the shapes Claude Code writes; dump folders are written by hand per docs/architecture.md.
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from briandump import server

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "briandump" / "fixtures"
HOST = "testhost"                                    # conftest's BRIANDUMP_HOST
WIN = sys.platform == "win32"
only_windows = pytest.mark.skipif(not WIN, reason="Windows only")
DUMP_ID = "20261018-093000-a1b2"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------- helpers ----------
@pytest.fixture(autouse=True)
def fresh_server(monkeypatch):
    """Module state that outlives one request (trackers, known questions, caches) starts empty in every test."""
    for name, value in (("TRACKERS", {}), ("KNOWN_QS", {}), ("_DETAILS", {}), ("_LAST", {}), ("_LOGGED", {}),
                        ("_DUMPS", {"t": 0.0, "dir": None, "rows": []}), ("RELOAD", [0]), ("VOICE", None), ("SESSION", None)):
        monkeypatch.setattr(server, name, value)


@pytest.fixture
def api(home):
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(method, url, body=None, headers=None):
    hdrs, data = dict(headers or {}), None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with OPENER.open(req, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def parsed(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return raw


def get(url, headers=None):
    code, _h, raw = call("GET", url, headers=headers)
    return code, parsed(raw)


def post(url, body=None, headers=None):
    code, _h, raw = call("POST", url, {} if body is None else body, headers=headers)
    return code, parsed(raw)


def inbox_files(home):
    return sorted(home.inbox.glob(f"*-panel-{HOST}.txt"))


def effects(home, kind=None):
    return [e for e in home.side_effect_log() if kind is None or e["kind"] == kind]


def note(stamp, host, text, task="t1"):
    return (f'<task-notification>\n<task-id>{task}</task-id>\n<summary>Monitor event: "voice inbox"</summary>\n'
            f'<event>VOICE {stamp}-{host}: {text}</event>\n</task-notification>')


LINES = [
    {"type": "user", "message": {"content": "sketch the shed floor plan"}, "timestamp": "2026-10-18T15:00:00Z",
     "cwd": "C:/work/garden-shed"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done, the plan is in plan.md."}]},
     "timestamp": "2026-10-18T15:00:05Z"},
    # a monitor-mode event delivered between turns -> a user message carrying the task-notification
    {"type": "user", "message": {"content": note("20261018-150100", HOST, "voice between turns")}, "timestamp": "2026-10-18T15:01:02Z"},
    # a panel line (stem ...-panel-<host>) -> tagged 'panel', not 'voice'
    {"type": "user", "message": {"content": note("20261018-150200", f"panel-{HOST}", "typed in the panel")},
     "timestamp": "2026-10-18T15:02:01Z"},
    # an event absorbed MID-TURN -> logged as an attachment, not a user message
    {"type": "attachment", "attachment": {"type": "queued_command", "timestamp": "2026-10-18T15:03:00Z",
                                          "prompt": note("20261018-150300", HOST, "said while you were busy")},
     "timestamp": "2026-10-18T15:03:01Z"},
    {"type": "user", "isMeta": True, "message": {"content": "meta noise"}, "timestamp": "2026-10-18T15:04:00Z"},
]


def transcript(home, lines, folder="C--work-garden-shed", sid="11111111-2222-3333-4444-555555555555"):
    d = home.projects / folder
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        for e in lines:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")
    return p


def tool_use(tid, name="Read", ts="2026-10-18T15:10:00Z", **inp):
    return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def tool_result(tid, ts="2026-10-18T15:10:01Z", error=False, content="ok"):
    """Key order as Claude Code writes it: the message first, the timestamp last (so a giant result has it in its tail)."""
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": error}]},
            "timestamp": ts}


def make_dump(home, did=DUMP_ID, status="ready", cards=True, text=None, **meta):
    """A dump folder written by hand per docs/architecture.md: meta.json + transcript.txt (+ cards.json)."""
    d = home.dumps_dir / did
    d.mkdir(parents=True, exist_ok=True)
    m = {"id": did, "createdAt": "2026-10-18T09:30:00+00:00", "updatedAt": "2026-10-18T09:31:00+00:00", "durationSec": 94.0,
         "status": status, "error": None, "attempts": 1, "timings": {}, "words": 312, "source": "ptt", "label": None,
         "processor": "mock", "model": None, "cost_usd": None, "report": None, "sent": {}, "agenda_added": [], "questions_added": []}
    m.update(meta)
    (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
    text = (FIXTURES / "sample-transcript.txt").read_text(encoding="utf-8") if text is None else text
    (d / "transcript.txt").write_text(text, encoding="utf-8")
    if cards:
        (d / "cards.json").write_text((FIXTURES / "sample-cards.json").read_text(encoding="utf-8"), encoding="utf-8")
    return d


@pytest.fixture
def real_dumps():
    if server.dumps is None:
        pytest.skip(f"briandump.dumps did not import: {server.WARNINGS.get('dumps')}")
    return server.dumps


def ptt_status(home, age=0.0, **kw):
    st = {"pid": 4242, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age)), "ready": True, "key": "f9",
          "dump_key": "f2", "device": "Test mic", "model": "small", "recording": False, "mode": None, "started": None,
          "level_db": None, "peak_db": None, "transcribing": None, "last_error": None}
    st.update(kw)
    home.ptt_status.write_text(json.dumps(st), encoding="utf-8")
    return st


def free_port():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in (8765, 8770):
            return port


# ---------- the transcript ----------
def test_feed_parses_every_line_kind(home):
    tr = server.Tracker(transcript(home, LINES))
    tr.update()
    got = [(m["who"], m["text"]) for m in tr.feed]
    assert ("typed", "sketch the shed floor plan") in got
    assert ("claude", "Done, the plan is in plan.md.") in got
    assert ("voice", "voice between turns") in got
    assert ("panel", "typed in the panel") in got
    assert ("voice", "said while you were busy") in got          # mid-turn attachment
    assert not any(t == "meta noise" for _, t in got)
    assert len(got) == 5
    assert tr.cwd == "C:/work/garden-shed"


def test_concurrent_updates_never_double_lines(home):
    p = transcript(home, LINES[:2])
    tr = server.Tracker(p)
    tr.update()
    for i in range(40):                                           # new bytes land while 8 pollers race
        transcript(home, [{"type": "assistant", "timestamp": f"2026-10-18T16:00:{i:02d}Z",
                           "message": {"content": [{"type": "text", "text": f"line {i}"}]}}])
        ts = [threading.Thread(target=tr.update) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    texts = [m["text"] for m in tr.feed]
    assert len(texts) == len(set(texts))


MALFORMED = [
    "not json at all",
    "[1, 2, 3]",
    '{"type": "assistant", "message": "a string, not an object"}',
    '{"type": "assistant", "message": {"content": [{"type": "text", "text": 5}, "loose string", null]}}',
    '{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tq", "name": "AskUserQuestion", '
    '"input": {"questions": "x"}}]}}',
    '{"type": "assistant", "message": {"usage": {"input_tokens": "lots"}, "content": []}}',
    '{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": null}, 7]}}',
    '{"type": "attachment", "attachment": "queued_command"}',
    '{"type": "user", "timestamp": 12345, "cwd": ["not", "a", "path"], "message": {"content": {"text": "dict content"}}}',
    '{"truncated": ',
]


def test_huge_and_malformed_lines_never_stop_the_reader(home):
    big = json.dumps(tool_result("t-big", ts="2026-10-18T15:10:09Z", error=True, content="x" * 400_000))   # is_error sits in the tail
    p = transcript(home, [tool_use("t-big", file_path="C:/work/photo.png"), *MALFORMED, big,
                          {"type": "assistant", "timestamp": "2026-10-18T15:11:00Z",
                           "message": {"model": "claude-test", "content": [{"type": "text", "text": "still reading after the odd lines"}],
                                       "usage": {"input_tokens": 1000, "cache_read_input_tokens": 2000, "output_tokens": 50}}}])
    tr = server.Tracker(p)
    tr.update()
    assert [m["text"] for m in tr.feed if m["who"] == "claude"] == ["still reading after the odd lines"]
    assert tr.steps and tr.steps[-1]["text"] == "Reading photo.png" and tr.steps[-1]["error"] is True
    assert tr.steps[-1]["end"] == "2026-10-18T15:10:09Z"
    assert tr.context == 3050 and tr.model == "claude-test"


def test_a_giant_line_written_in_pieces_is_held_as_head_and_tail_only(home):
    p = transcript(home, [tool_use("t-shot", name="mcp__claude-in-chrome__computer", description="take a screenshot")])
    tr = server.Tracker(p)
    tr.update()
    line = json.dumps(tool_result("t-shot", ts="2026-10-18T15:12:00Z", content="A" * 900_000))
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(line[:450_000])                                   # Claude Code is still writing it
    tr.update()
    assert tr.head is not None and len(tr.buf) <= tr.TAIL          # bounded memory, nothing parsed yet
    done = json.dumps({"type": "system", "subtype": "turn_duration", "timestamp": "2026-10-18T15:12:01Z"})
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(line[450_000:] + "\n" + done + "\n")
    tr.update()
    assert tr.steps[-1]["text"] == "Browser: take a screenshot" and tr.steps[-1]["end"] == "2026-10-18T15:12:00Z"
    assert tr.idle and tr.head is None and tr.buf == b""


def test_chunk_boundaries_do_not_change_the_result(home, monkeypatch):
    p = transcript(home, [*LINES, tool_use("t1", name="Bash", command="dir"), tool_result("t1"), *MALFORMED])
    whole = server.Tracker(p)
    whole.update()
    monkeypatch.setattr(server.Tracker, "CHUNK", 7)
    small = server.Tracker(p)
    small.update()
    assert small.feed == whole.feed and small.steps == whole.steps and small.cwd == whole.cwd


def test_describe_takes_the_file_name_from_windows_paths_on_any_os():
    assert server.describe("Edit", {"file_path": "C:\\work\\shed\\plan.md"}) == "Editing plan.md"
    assert server.describe("Read", {"file_path": "/home/someone/plan.md"}) == "Reading plan.md"


def test_describe_names_tools_and_apps():
    assert server.describe("Bash", {"description": "list files"}) == "Bash: list files"
    assert server.describe("Edit", {"file_path": "C:/work/plan.md"}) == "Editing plan.md"
    assert server.describe("mcp__photoshop__photoshop_get_state", {}) == "Photoshop: get state"
    assert server.describe("mcp__after-effects__mcp_aftereffects_applyEffect", {}) == "After Effects: applyEffect"
    assert server.describe("AskUserQuestion", {"questions": ["not a dict"]}) == "Asking you: "
    assert server.describe(None, None) == ""


def test_project_name_comes_from_cwd():
    assert server.project_name("C:/work/garden-shed") == "garden-shed"
    assert server.project_name("C:\\work\\garden-shed\\") == "garden-shed"
    assert server.project_name(str(Path.home())) == "home"
    assert server.project_name("") is None


def test_queued_inbox_shows_unseen_recent_lines_only(home):
    inbox = home.inbox
    (inbox / f"20261018-170000-{HOST}.txt").write_text("already in the feed", encoding="utf-8")
    (inbox / f"20261018-170100-{HOST}.txt").write_text("not picked up yet", encoding="utf-8")
    (inbox / f"20261018-170200-panel-{HOST}.txt").write_text("typed, not picked up", encoding="utf-8")
    old = inbox / f"20261018-090000-{HOST}.txt"
    old.write_text("an hour old", encoding="utf-8")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    (inbox / "20261018-170300-otherhost.txt").write_text("other machine", encoding="utf-8")
    q = server.queued_inbox([{"who": "voice", "text": "already   in the feed", "ts": "x"}])   # whitespace differences ignored
    assert {(m["who"], m["text"]) for m in q} == {("voice", "not picked up yet"), ("panel", "typed, not picked up")}
    assert all(m["queued"] for m in q)


# ---------- /api/state ----------
CONTRACT = {"version", "session", "project", "activity", "steps", "context", "questions", "voice", "ptt", "dumps", "reload"}


def test_state_with_an_empty_claude_dir_is_200(home, api):
    code, st = get(f"{api}/api/state")
    assert code == 200 and CONTRACT <= set(st)
    assert st["session"] is None and st["activity"]["state"] == "idle" and st["steps"] == [] and st["questions"] == []
    for sub in ("projects", "sessions"):                         # ~/.claude without its folders (a fresh install)
        (home.claude_dir / sub).rmdir()
    assert get(f"{api}/api/state")[0] == 200
    assert get(f"{api}/")[0] == 200


def test_state_shape_matches_the_contract(home, api):
    transcript(home, [*LINES, tool_use("t9", name="Grep", pattern="shed"),
                      {"type": "assistant", "timestamp": "2026-10-18T15:20:00Z",
                       "message": {"content": [{"type": "text", "text": "Which base do you prefer, slab or gravel?"}]}}])
    code, st = get(f"{api}/api/state")
    assert code == 200 and CONTRACT <= set(st)
    assert st["version"] == server.__version__ and st["project"] == "garden-shed" and st["session"] == "11111111"
    assert set(st["activity"]) == {"state", "text", "since"} and st["activity"]["state"] == "working"
    assert st["activity"]["text"] == "Searching for shed"
    assert {"used", "limit", "model"} <= set(st["context"]) and st["context"]["limit"] == home.context_limit
    assert {"mode", "target", "main", "queued", "last"} <= set(st["voice"])
    assert {"alive", "ready", "recording", "mode", "started", "seconds", "level_db", "key", "dump_key", "transcribing"} <= set(st["ptt"])
    assert {"processing", "ready", "newest"} <= set(st["dumps"])
    q = next(q for q in st["questions"] if q["source"] == "message")
    assert set(q) >= {"id", "text", "kind", "options", "source", "asked"} and q["text"] == "Which base do you prefer, slab or gravel?"
    assert "feed" not in st


def test_needs_you_turns_the_strip_red_until_the_log_moves_on(home, api):
    p = transcript(home, LINES)
    home.needs_you.write_text(json.dumps({"ts": "2026-10-18T15:30:00Z", "message": "Claude needs your permission to use Bash",
                                          "type": "permission_prompt", "session": p.stem, "transcript": str(p)}), encoding="utf-8")
    assert get(f"{api}/api/state")[1]["activity"] == {"state": "needs", "text": "Claude needs your permission to use Bash",
                                                      "since": "2026-10-18T15:30:00Z"}
    transcript(home, [{"type": "assistant", "timestamp": "2026-10-18T15:31:00.500Z",
                       "message": {"content": [{"type": "text", "text": "ok"}]}}])
    assert get(f"{api}/api/state")[1]["activity"]["state"] != "needs"


def test_a_half_written_status_file_is_no_change(home, api, monkeypatch):
    ptt_status(home)
    assert get(f"{api}/api/state")[1]["ptt"]["alive"] is True
    home.ptt_status.write_text('{"pid": 42, "ts": "2026', encoding="utf-8")          # caught mid-write
    assert get(f"{api}/api/state")[1]["ptt"]["alive"] is True
    real = Path.read_text

    def locked(self, *a, **k):
        if self.name == "ptt_status.json":
            raise PermissionError(5, "Access is denied")                           # the writer is replacing it right now
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", locked)
    code, st = get(f"{api}/api/state")
    assert code == 200 and st["ptt"]["alive"] is True
    home.ptt_status.unlink()                                                       # gone is a real answer
    monkeypatch.setattr(Path, "read_text", real)
    assert get(f"{api}/api/state")[1]["ptt"]["alive"] is False


# ---------- the two inbox writers ----------
def test_answer_lands_in_inbox_as_typed(home, api):
    assert post(f"{api}/api/answer", {"id": "zz", "text": "about 2.4 m"}) == (200, {"ok": True})
    [f] = inbox_files(home)
    assert f.read_text(encoding="utf-8") == "about 2.4 m"
    assert post(f"{api}/api/answer", {"id": "zz", "text": "   "})[0] == 400
    assert post(f"{api}/api/answer", {"id": "zz", "text": "x" * 5000})[0] == 400
    assert len(inbox_files(home)) == 1


def test_same_second_answers_never_overwrite(home, api):
    for i in range(3):
        assert post(f"{api}/api/answer", {"text": f"answer {i}"})[0] == 200
    files = inbox_files(home)
    assert len(files) == 3 and {f.read_text(encoding="utf-8") for f in files} == {"answer 0", "answer 1", "answer 2"}


def test_answering_a_questions_md_item_quotes_it_and_ticks_it(home, api):
    home.questions_file.write_text("# Questions\n\n- [ ] Slab or gravel: which way are you leaning? (from brain dump 2026-10-18 09:30)\n",
                                   encoding="utf-8")
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if q["source"] == "dump")
    assert q["id"].startswith("qmd-") and q["kind"] == "think" and q["text"] == "Slab or gravel: which way are you leaning?"
    assert post(f"{api}/api/answer", {"id": q["id"], "text": "gravel"})[0] == 200
    [f] = inbox_files(home)
    assert f.read_text(encoding="utf-8") == "[BrianDump question: Slab or gravel: which way are you leaning?] gravel"
    assert "- [x] Slab or gravel" in home.questions_file.read_text(encoding="utf-8")
    assert not [q for q in get(f"{api}/api/state")[1]["questions"] if q["source"] == "dump"]


def test_answering_an_asked_question_marks_it_done(home, api):
    home.manual_questions.write_text(json.dumps([{"id": "q1", "text": "Which colour for the door?", "kind": "ask",
                                                  "options": ["green", "grey"], "asked": time.time(), "done": False}]), encoding="utf-8")
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if q["id"] == "q1")
    assert q["options"] == ["green", "grey"] and q["source"] == "manual"
    assert post(f"{api}/api/answer", {"id": "q1", "text": "green"})[0] == 200
    assert inbox_files(home)[0].read_text(encoding="utf-8") == "[panel answer to: Which colour for the door?] green"
    assert json.loads(home.manual_questions.read_text(encoding="utf-8"))[0]["done"] is True


ASKED = [{"type": "assistant", "timestamp": "2026-10-18T15:20:00Z",
          "message": {"content": [{"type": "text", "text": "Should I archive the old docs folder? Say yes or no."}]}}]
SID = "11111111-2222-3333-4444-555555555555"


def live(home, sid=SID, pid=4242, name="Main"):
    """A running session: its record in Claude Code's registry (~/.claude/sessions/<pid>.json)."""
    (home.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": "C:/work", "name": name,
                                                           "entrypoint": "cli", "status": "idle", "startedAt": 1_792_000_000_000}),
                                               encoding="utf-8")


def test_an_answer_goes_back_to_the_session_that_asked_and_the_card_says_which(home, api):
    docs = "66666666-7777-4888-8999-000000000000"
    transcript(home, ASKED, folder="C--work-docs", sid=docs)
    live(home)
    live(home, docs, 4243, "Docs")
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if "archive" in q["text"])
    assert q["to"] == "Docs"
    assert post(f"{api}/api/answer", {"id": q["id"], "text": "yes"}) == (200, {"ok": True})
    [f] = inbox_files(home)
    assert server.voice.split_meta(f.read_text(encoding="utf-8")) == (
        {"session": docs}, "[panel answer to: Should I archive the old docs folder? Say yes or no.] yes")


def test_an_answer_for_a_session_that_ended_is_refused(home, api):
    transcript(home, ASKED)                                                  # no registry record: the session has ended
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if "archive" in q["text"])
    assert q["to"] == "session ended"
    code, body = post(f"{api}/api/answer", {"id": q["id"], "text": "yes"})
    assert code == 409 and "not running" in body["error"] and inbox_files(home) == []


def test_monitor_mode_sends_an_answer_only_to_the_session_holding_the_voice(home, api):
    home.save_user_value("voice.mode", "monitor")
    transcript(home, ASKED)
    live(home)
    qid = next(q["id"] for q in get(f"{api}/api/state")[1]["questions"] if "archive" in q["text"])
    home.voice_lock.write_text(json.dumps({"session": "someone-else", "pid": 1, "t": time.time()}), encoding="utf-8")
    code, body = post(f"{api}/api/answer", {"id": qid, "text": "yes"})
    assert code == 409 and "monitor mode" in body["error"] and inbox_files(home) == []
    home.voice_lock.write_text(json.dumps({"session": SID, "pid": 1, "t": time.time()}), encoding="utf-8")
    assert post(f"{api}/api/answer", {"id": qid, "text": "yes"})[0] == 200
    [f] = inbox_files(home)                                                  # its watcher relays it to the session that asked
    assert f.read_text(encoding="utf-8") == "[panel answer to: Should I archive the old docs folder? Say yes or no.] yes"


def test_a_dialog_question_is_answered_in_the_terminal_not_from_the_panel(home, api):
    door = {"question": "Which door?", "header": "Door", "multiSelect": False,
            "options": [{"label": "Single", "description": "one leaf"}, {"label": "Double", "description": "two leaves"}]}
    transcript(home, [tool_use("toolu_dialog1", "AskUserQuestion", questions=[door])])
    live(home)
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if q["source"] == "asked")
    assert q["text"] == "Which door?" and q["options"] == ["Single", "Double"]            # shown, for reference
    code, body = post(f"{api}/api/answer", {"id": q["id"], "text": "Double"})
    assert code == 409 and "answer it there" in body["error"] and inbox_files(home) == []


def test_a_masked_option_sends_its_own_words_never_the_placeholder(home, api):
    home.save_user_value("private_filter", ["(?i)zebra"])
    home.manual_questions.write_text(json.dumps([{"id": "q3", "text": "Ship the shed plan now?", "kind": "ask", "done": False,
                                                  "options": ["Ship it", "Hold for the zebra review"],
                                                  "asked": time.strftime("%Y-%m-%dT%H:%M:%S")}]), encoding="utf-8")
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if q["id"] == "q3")
    assert q["options"] == ["Ship it", "(private)"]
    assert post(f"{api}/api/answer", {"id": "q3", "text": "(private)"})[0] == 400              # the placeholder is never typed
    assert post(f"{api}/api/answer", {"id": "q3", "text": "Ship it", "option": 1})[0] == 200   # a stale index: the label wins
    assert post(f"{api}/api/answer", {"id": "q3", "text": "(private)", "option": 1})[0] == 200
    assert [f.read_text(encoding="utf-8") for f in inbox_files(home)] == [
        "[panel answer to: Ship the shed plan now?] Ship it", "[panel answer to: Ship the shed plan now?] Hold for the zebra review"]


def test_typed_text_that_looks_like_a_routing_line_stays_text(home, api):
    sneaky = server.voice.META + json.dumps({"session": SID}) + "\ntype this over there"
    assert post(f"{api}/api/answer", {"text": sneaky})[0] == 200
    [f] = inbox_files(home)
    assert server.voice.split_meta(f.read_text(encoding="utf-8")) == ({}, sneaky)


def test_there_is_no_other_way_to_write_the_inbox(home, api):
    before = set(home.inbox.iterdir())
    for path, body in (("/api/voice", {"action": "type", "text": "rm -rf"}), ("/api/voice", {"action": "drop", "text": "x"}),
                       ("/api/ptt", {"action": "dump_toggle", "text": "x"}), ("/api/agenda", {"action": "add", "text": "an item"}),
                       ("/api/reload", {"text": "x"}), ("/api/message", {"text": "x"}), ("/api/type", {"text": "x"})):
        post(f"{api}{path}", body)
    assert set(home.inbox.iterdir()) == before


class FakeRouter:
    def __init__(self):
        self.calls = []

    def act(self, action, item_id):
        self.calls.append((action, item_id))
        return True, ""

    def snapshot(self, show_private=False, private=None):
        return {"mode": "panel", "host": HOST, "main": None, "fallback": None, "queued": 0, "items": [], "lock": None, "grace": False}

    def compact(self, private=None):
        return {"mode": "panel", "target": None, "main": False, "queued": 0, "last": None}


def test_voice_api_only_drops_or_retries(home, api, monkeypatch):
    code, body = get(f"{api}/api/voice")                       # panel mode, no router running: say so
    assert code == 503 and "voice router" in body["error"]
    router = FakeRouter()
    monkeypatch.setattr(server, "VOICE", router)
    assert get(f"{api}/api/voice")[1]["mode"] == "panel"
    assert post(f"{api}/api/voice", {"action": "drop", "id": "f1", "text": "ignored"})[0] == 200
    assert post(f"{api}/api/voice", {"action": "retry", "id": "f2"})[0] == 200
    assert post(f"{api}/api/voice", {"action": "send", "text": "type this"})[0] == 400
    assert router.calls == [("drop", "f1"), ("retry", "f2")]
    monkeypatch.setitem(home.cfg["voice"], "mode", "monitor")
    monkeypatch.setattr(server, "VOICE", None)
    assert get(f"{api}/api/voice") == (200, {"mode": "monitor", "host": HOST, "main": None, "queued": 0, "items": []})


# ---------- who may ask ----------
def test_a_client_outside_allow_gets_403(home, api, monkeypatch):
    monkeypatch.setattr(server.Handler, "client_ip", lambda self: "10.9.9.9")
    assert get(f"{api}/api/state")[0] == 403
    assert get(f"{api}/")[0] == 403
    assert post(f"{api}/api/answer", {"text": "hello"})[0] == 403
    assert inbox_files(home) == []


def test_desktop_actions_are_for_the_local_page_only(home, api, monkeypatch):
    monkeypatch.setattr(home, "allowed", home.allowed | {"10.0.0.7"})
    monkeypatch.setattr(server.Handler, "client_ip", lambda self: "10.0.0.7")
    assert get(f"{api}/api/state")[0] == 200                     # an allowed machine may look and answer
    for path, body in (("/api/window", {"action": "min"}), ("/api/open-folder", {"what": "reports"}),
                       ("/api/open", {"url": "https://example.com/"}), ("/api/voice", {"action": "open_main"})):
        assert post(f"{api}{path}", body) == (403, {"error": "local page only"}), path
    assert effects(home) == []


def test_other_sites_cannot_post_or_rebind(home, api):
    url = f"{api}/api/answer"
    port = api.rsplit(":", 1)[1]
    assert post(url, {"text": "x"}, headers={"Origin": "https://evil.example"})[0] == 403
    assert post(url, {"text": "x"}, headers={"Origin": "null"})[0] == 403
    assert post(url, {"text": "x"}, headers={"Origin": f"http://localhost:{port}"})[0] == 403        # same port, other origin
    code, _h, _b = call("POST", url, b'{"text": "x"}', headers={"Content-Type": "text/plain"})          # a form-style post
    assert code == 415
    assert get(f"{api}/api/state", headers={"Host": f"evil.example:{port}"})[0] == 403                 # DNS rebinding
    assert post(url, {"text": "x"}, headers={"Host": f"rebind.evil.example:{port}"})[0] == 403
    assert inbox_files(home) == []
    assert post(url, {"text": "same origin"}, headers={"Origin": f"http://127.0.0.1:{port}"})[0] == 200
    assert get(f"{api}/api/state", headers={"Host": f"localhost:{port}"})[0] == 200
    assert get(f"{api}/api/state", headers={"Host": f"{HOST}:{port}"})[0] == 200


def test_the_host_must_be_one_of_this_pcs_own_names_exactly(home, api, monkeypatch):
    port = api.rsplit(":", 1)[1]
    monkeypatch.setattr(server, "os_names", lambda: frozenset({"pc-77", "pc-77.corp.example"}))
    for own in ("pc-77.corp.example", "PC-77", HOST, "[::1]"):                   # the full DNS name Windows records counts too
        assert get(f"{api}/api/state", headers={"Host": f"{own}:{port}"})[0] == 200, own
    for rebound in (f"{HOST}.evil.example", "pc-77.evil.example", "pc-77.corp.example.evil.example", f"x{HOST}", "localhost.evil"):
        where = f"{rebound}:{port}"
        assert get(f"{api}/api/state", headers={"Host": where})[0] == 403, rebound
        assert post(f"{api}/api/answer", {"text": "x"}, headers={"Host": where, "Origin": f"http://{where}"})[0] == 403, rebound
    assert inbox_files(home) == []


def test_bad_bodies_get_a_reply(home, api):
    assert call("POST", f"{api}/api/answer", b"{not json", headers={"Content-Type": "application/json"})[0] == 400
    assert post(f"{api}/api/answer", [1, 2])[0] == 400                     # not an object: treated as {} -> empty
    assert call("POST", f"{api}/api/answer", b"", headers={"Content-Length": "-5"})[0] == 400
    assert post(f"{api}/api/nothing-here", {})[0] == 404


def test_page_headers(home, api):
    code, headers, body = call("GET", f"{api}/")
    assert code == 200 and b"<title>BrianDump \xc2\xb7 SANDBOX</title>" in body                     # sandbox: the window title too
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"
    assert call("GET", f"{api}/favicon.ico")[1]["Content-Type"] == "image/x-icon"


# ---------- outward actions (sandbox: recorded, nothing else) ----------
def boom(*a, **k):
    raise AssertionError("an outward action ran in the sandbox")


def test_open_takes_http_only(home, api, monkeypatch):
    monkeypatch.setattr(server.webbrowser, "open", boom)
    for bad in ("file:///C:/Windows", "javascript:alert(1)", "https://exa mple.com", "ms-settings:privacy", ""):
        assert post(f"{api}/api/open", {"url": bad})[0] == 400, bad
    assert post(f"{api}/api/open", {"url": "https://example.com/x"}) == (200, {"ok": True, "sandbox": True})
    assert [e["url"] for e in effects(home, "open_url")] == ["https://example.com/x"]
    opened = []
    monkeypatch.setattr(home, "sandbox", False)                    # the real path: the default browser
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    assert post(f"{api}/api/open", {"url": "https://example.com/y"}) == (200, {"ok": True})
    assert opened == ["https://example.com/y"]


def test_window_folder_and_open_main_only_record_in_the_sandbox(home, api, monkeypatch):
    monkeypatch.setattr(server.subprocess, "Popen", boom)
    monkeypatch.setattr(os, "startfile", boom, raising=False)
    transcript(home, [{"type": "user", "message": {"content": "hi"}, "timestamp": "2026-10-18T15:00:00Z", "cwd": str(home.data_dir)}])
    assert post(f"{api}/api/window", {"action": "min"}) == (200, {"ok": True, "sandbox": True})
    assert post(f"{api}/api/window", {"action": "explode"})[0] == 400
    assert post(f"{api}/api/open-folder", {"what": "reports"})[1]["sandbox"] is True
    assert post(f"{api}/api/open-folder", {"what": "../.."})[0] == 400
    code, body = post(f"{api}/api/voice", {"action": "open_main"})
    assert code == 200 and body["ok"] and "sandbox" in body["msg"]
    assert [e["kind"] for e in effects(home)] == ["window", "open_folder", "launch_main"]
    launch = effects(home, "launch_main")[0]
    assert launch["name"] == "Main" and launch["folder"] == str(home.data_dir)     # the newest session's folder


# ---------- auto-open Main at server start ----------
def test_auto_open_main_opens_an_absent_main_once_per_logon(home, monkeypatch):
    monkeypatch.setattr(server.subprocess, "Popen", boom)
    monkeypatch.setattr(server, "session_rows", lambda: [])
    ok, msg = server.auto_open_main(logon="L1")
    assert ok and "sandbox" in msg and len(effects(home, "launch_main")) == 1
    # the user closes Main; the watchdog (or a second launch) restarts the server within the same logon: not reopened
    assert server.auto_open_main(logon="L1") == (False, "already checked in this logon")
    assert len(effects(home, "launch_main")) == 1
    assert json.loads((home.state / "auto_main.json").read_text(encoding="utf-8"))["logon"] == "L1"
    server.auto_open_main(logon="L2")                                         # the next sign-in opens it again
    assert len(effects(home, "launch_main")) == 2


def test_auto_open_main_leaves_a_running_main_alone(home, monkeypatch):
    monkeypatch.setattr(server, "session_rows", lambda: [{"name": "Main", "status": "idle", "host": HOST}])
    ok, msg = server.auto_open_main(logon="L1")
    assert ok and "already open" in msg and effects(home, "launch_main") == []


def test_auto_open_main_off_opens_nothing(home, monkeypatch):
    monkeypatch.setattr(server, "session_rows", lambda: [])
    monkeypatch.setitem(home.cfg["voice"], "auto_open_main", False)
    assert server.auto_open_main(logon="L1") == (False, "voice.auto_open_main is off")
    assert effects(home, "launch_main") == [] and not (home.state / "auto_main.json").exists()


@only_windows
def test_logon_id_is_this_sign_ins_luid():
    lid = server.logon_id()
    assert re.fullmatch(r"[0-9a-f]+-[0-9a-f]{8}", lid) and lid == server.logon_id()


def test_open_main_refuses_an_unsafe_session_name(home, api, monkeypatch):
    monkeypatch.setitem(home.cfg["voice"], "main_session", 'Main" & calc & "')
    code, body = post(f"{api}/api/voice", {"action": "open_main"})
    assert code == 400 and "plain name" in body["msg"] and effects(home) == []


# ---------- agenda ----------
def test_agenda_add_done_undone(home, api):
    added = post(f"{api}/api/agenda", {"action": "add", "text": "Measure the shed space"})
    assert added == (200, {"ok": True, "added": ["Measure the shed space"]})
    assert post(f"{api}/api/agenda", {"action": "add", "text": "measure the  shed space!"})[1]["added"] == []      # deduplicated
    [item] = get(f"{api}/api/agenda")[1]["items"]
    assert item["text"] == "Measure the shed space" and item["done"] is False and re.fullmatch(r"[0-9a-f]{10}", item["id"])
    assert post(f"{api}/api/agenda", {"action": "done", "id": item["id"]}) == (200, {"ok": True})
    assert "- [x] Measure the shed space" in home.agenda_file.read_text(encoding="utf-8")
    assert post(f"{api}/api/agenda", {"action": "undone", "text": "Measure the shed space"})[0] == 200
    assert "- [ ] Measure the shed space" in home.agenda_file.read_text(encoding="utf-8")
    assert post(f"{api}/api/agenda", {"action": "done", "text": "Something never added"})[0] == 404
    assert post(f"{api}/api/agenda", {"action": "delete", "text": "Measure the shed space"})[0] == 400
    assert post(f"{api}/api/agenda", {"action": "add", "text": "x" * 600})[0] == 400


def test_agenda_lists_open_items_first(home, api):
    home.agenda_file.write_text("- [x] Done thing\n- [ ] Open thing (from brain dump 2026-10-18 09:30)\n", encoding="utf-8")
    items = get(f"{api}/api/agenda")[1]["items"]
    assert [(i["base"], i["done"]) for i in items] == [("Open thing", False), ("Done thing", True)]
    assert items[0]["text"] == "Open thing (from brain dump 2026-10-18 09:30)"


# ---------- the private filter ----------
def test_private_filter_masks_what_leaves_the_server(home, api, monkeypatch, real_dumps):
    monkeypatch.setattr(home, "private_re", re.compile(r"lumber yard|leaning", re.I))
    home.agenda_file.write_text("- [ ] Call the lumber yard\n- [ ] Peg the corners\n", encoding="utf-8")
    home.questions_file.write_text("- [ ] Which way are you leaning?\n", encoding="utf-8")
    make_dump(home, text="First I measure it with the long tape. Then I call the lumber yard for a quote. The base is still open.")
    items = get(f"{api}/api/agenda")[1]["items"]
    assert [i["text"] for i in items] == ["(private)", "Peg the corners"]
    assert post(f"{api}/api/agenda", {"action": "done", "id": items[0]["id"], "text": "(private)"})[0] == 200   # by id
    assert "- [x] Call the lumber yard" in home.agenda_file.read_text(encoding="utf-8")
    qs = get(f"{api}/api/state")[1]["questions"]
    assert [q["text"] for q in qs if q["source"] == "dump"] == ["(private)"]
    d = get(f"{api}/api/dumps/{DUMP_ID}")[1]
    assert "lumber yard" not in json.dumps(d).lower()
    assert "(private)" in d["transcript"] and "measure it with the long tape" in d["transcript"]   # one sentence hidden, not all
    assert d["cards"]["todos"][2] == "(private)" and d["cards"]["todos"][0].startswith("Measure")


# ---------- dumps ----------
class SpyDumps:
    """Records every call: the id checks must happen before the store is asked for anything."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call_(*a, **k):
            self.calls.append((name, a))
            return None
        return call_


BAD_IDS = ["..%2F..%2Fconfig.json", "..", "20261018-0930-a1b2", "20261018-093000-A1B2", "20261018-093000-a1b2x",
           "20261018-093000-a1b2%00", "..%5C..%5Cstate", "x" * 300]


def test_dump_ids_are_checked_before_the_store_is_touched(home, api, monkeypatch):
    spy = SpyDumps()
    monkeypatch.setattr(server, "dumps", spy)
    for bad in BAD_IDS:
        assert get(f"{api}/api/dumps/{bad}")[0] == 400, bad
        for act in ("retry", "send", "agenda"):
            assert post(f"{api}/api/dumps/{bad}/{act}", {"action_id": "a1", "text": "x"})[0] == 400, (bad, act)
    assert get(f"{api}/api/dumps/../../state/x")[0] == 404
    assert post(f"{api}/api/dumps/{DUMP_ID}/delete", {})[0] == 404
    for aid in ("../../x", "a" * 40, "a/b", "", "a1\n", "a1\r\n", "ａ１"):        # "$" would let the newline through; \w non-ASCII
        assert post(f"{api}/api/dumps/{DUMP_ID}/send", {"action_id": aid, "text": "x"})[0] == 400, repr(aid)
    assert post(f"{api}/api/answer", {"id": "qmd-0123456789", "text": "an answer", "dump": DUMP_ID + "\n"})[0] == 200
    assert spy.calls == [] and inbox_files(home)[0].read_text(encoding="utf-8") == "an answer"
    # a raw newline cannot travel in a URL path, so the path checks are pinned on the patterns themselves
    assert server.DUMP_ID_RE.match(DUMP_ID) and not server.DUMP_ID_RE.match(DUMP_ID + "\n")
    assert server.ACTION_ID_RE.match("a1") and not server.ACTION_ID_RE.match("a1\n")


def test_dump_list_and_detail(home, api, real_dumps):
    make_dump(home)
    make_dump(home, "20261018-101500-c3d4", status="error", cards=False, error="claude -p timed out", attempts=2)
    code, rows = get(f"{api}/api/dumps")
    assert code == 200 and [r["id"] for r in rows] == ["20261018-101500-c3d4", DUMP_ID]          # newest first
    ready = rows[1]
    assert ready["status"] == "ready" and ready["counts"] == {"themes": 3, "actions": 3, "todos": 3, "questions": 5}
    assert ready["summaryLine"] and rows[0]["error"] == "claude -p timed out"
    code, d = get(f"{api}/api/dumps/{DUMP_ID}")
    assert code == 200 and d["cards"]["executiveSummary"].startswith("The user is planning a small garden shed")
    assert len(d["questions"]) == 5 and all(q["id"].startswith("qmd-") for q in d["questions"])
    assert d["todo_added"] == [False, False, False] and "the shed again" in d["transcript"]
    assert get(f"{api}/api/dumps/20261018-111111-ffff")[0] == 404
    st = get(f"{api}/api/state")[1]["dumps"]
    assert st["ready"] == 1 and st["errors"] == 1 and st["newest"]["id"] == "20261018-101500-c3d4"


def test_send_to_claude_writes_what_the_user_edited(home, api, real_dumps):
    make_dump(home)
    edited = "Summarise the permit rules for small sheds, one page, plain words"
    code, body = post(f"{api}/api/dumps/{DUMP_ID}/send", {"action_id": "a1", "text": edited})
    assert code == 200 and body["ok"] and body["sent"]
    [f] = inbox_files(home)
    assert f.read_text(encoding="utf-8") == edited
    assert "a1" in get(f"{api}/api/dumps/{DUMP_ID}")[1]["sent"]
    assert post(f"{api}/api/dumps/{DUMP_ID}/send", {"action_id": "a9", "text": edited})[0] == 404       # not in this dump
    assert post(f"{api}/api/dumps/{DUMP_ID}/send", {"action_id": "a1", "text": ""})[0] == 400
    assert len(inbox_files(home)) == 1


def test_add_to_agenda_takes_the_dumps_own_words(home, api, real_dumps):
    make_dump(home)
    code, body = post(f"{api}/api/dumps/{DUMP_ID}/agenda", {"index": 0, "text": "(private)"})
    assert code == 200 and body["added"] == ["Measure the shed space with the long tape and peg the corners"]
    assert "Measure the shed space with the long tape" in home.agenda_file.read_text(encoding="utf-8")
    assert post(f"{api}/api/dumps/{DUMP_ID}/agenda", {"index": 0})[1]["added"] == []                  # deduplicated
    assert post(f"{api}/api/dumps/{DUMP_ID}/agenda", {"text": "(private)"})[0] == 400


def test_retry_requeues_a_failed_dump(home, api, real_dumps):
    make_dump(home, "20261018-101500-c3d4", status="error", error="claude -p timed out", attempts=2)
    code, body = post(f"{api}/api/dumps/20261018-101500-c3d4/retry")
    assert code == 200 and body["ok"]
    assert get(f"{api}/api/dumps/20261018-101500-c3d4")[1]["status"] != "error"


# ---------- PTT ----------
def test_dump_toggle_needs_a_live_service(home, api):
    code, body = post(f"{api}/api/ptt", {"action": "dump_toggle"})
    assert code == 200 and body["ok"] is False and "not running" in body["msg"]
    assert not home.ptt_command.exists()
    ptt_status(home, age=30)                                        # a stale file is not a running service
    assert post(f"{api}/api/ptt", {"action": "dump_toggle"})[1]["ok"] is False and not home.ptt_command.exists()
    ptt_status(home)
    code, body = post(f"{api}/api/ptt", {"action": "dump_toggle"})
    assert code == 200 and body["ok"] is True
    cmd = json.loads(home.ptt_command.read_text(encoding="utf-8"))
    assert cmd["cmd"] == "dump_toggle" and cmd["nonce"] == body["nonce"] and abs(cmd["t"] - time.time()) < 10
    assert post(f"{api}/api/ptt", {"action": "start_recording_forever"})[0] == 400


def test_ptt_state_follows_the_status_file(home, api):
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 75))
    ptt_status(home, recording=True, mode="dump", started=started, level_db=-21.5, peak_db=-6.0)
    code, p = get(f"{api}/api/ptt")
    assert code == 200 and p["alive"] and p["recording"] and p["mode"] == "dump" and p["level_db"] == -21.5
    assert 73 <= p["seconds"] <= 80
    ptt_status(home, age=60, recording=True, mode="dump", started=started)
    p = get(f"{api}/api/ptt")[1]
    assert p["alive"] is False and p["recording"] is False and p["seconds"] == 0
    ptt_status(home, age=-3600)                                    # stamped an hour ahead (the clock was set back): not alive
    assert get(f"{api}/api/ptt")[1]["alive"] is False


def test_ptt_field_names_match_the_ptt_writer(home, api):
    ptt = pytest.importorskip("briandump.ptt", reason="ptt.py needs numpy, sounddevice and pynput")
    st = ptt.status_dict(ready=True, key="f9", dump_key="f2", device="Test mic", model="small", recording=True, mode="dump",
                         started=ptt.iso(time.time() - 5), level_db=-30.0, peak_db=-12.0)
    home.ptt_status.write_text(json.dumps(st), encoding="utf-8")
    p = get(f"{api}/api/ptt")[1]
    assert p["alive"] and p["mode"] == "dump" and p["level_db"] == -30.0 and 3 <= p["seconds"] <= 8


# ---------- Windows file contention ----------
@pytest.mark.win32
@only_windows
def test_atomic_write_rides_out_a_reader(home):
    target = home.state / "contended.json"
    target.write_text("{}", encoding="utf-8")
    holder = open(target, encoding="utf-8")                      # Python opens without FILE_SHARE_DELETE: replace must wait
    try:
        tmp = home.state / "probe.tmp"
        tmp.write_text("x", encoding="utf-8")
        with pytest.raises(PermissionError):
            os.replace(tmp, target)
        threading.Timer(0.15, holder.close).start()
        server.atomic_write(target, '{"ok": true}')
    finally:
        holder.close()
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    with open(target, encoding="utf-8"):
        with pytest.raises(server.Busy):
            server.atomic_write(target, "{}", tries=3)
    assert not list(home.state.glob("contended.json.*.tmp"))


# ---------- a sibling module that did not import ----------
def test_a_missing_sibling_gives_503_and_the_page_still_loads(home, api, monkeypatch):
    monkeypatch.setattr(server, "dumps", None)
    monkeypatch.setattr(server, "agenda", None)
    monkeypatch.setattr(server, "reports", None)
    monkeypatch.setitem(server.WARNINGS, "dumps", "ImportError: test")
    monkeypatch.setitem(server.WARNINGS, "agenda", "SyntaxError: test")
    monkeypatch.setitem(server.WARNINGS, "reports", "NameError: test")
    assert get(f"{api}/")[0] == 200
    code, st = get(f"{api}/api/state")
    assert code == 200 and st["dumps"]["unavailable"] == "ImportError: test"
    code, body = get(f"{api}/api/dumps")
    assert code == 503 and "briandump.dumps" in body["error"] and "ImportError: test" in body["error"]
    assert get(f"{api}/api/dumps/{DUMP_ID}")[0] == 503
    assert post(f"{api}/api/dumps/{DUMP_ID}/send", {"action_id": "a1", "text": "x"})[0] == 503
    assert get(f"{api}/api/agenda")[0] == 503
    assert post(f"{api}/api/agenda", {"action": "add", "text": "x"})[0] == 503
    code, body = get(f"{api}/api/reports")
    assert code == 503 and "briandump.reports" in body["error"] and "NameError: test" in body["error"]
    assert get(f"{api}/api/reports/0123456789ab")[0] == 503
    assert post(f"{api}/api/reports/0123456789ab/read", {})[0] == 503
    m = get(f"{api}/api/machine")[1]
    assert {"host", "scale", "window", "sandbox", "version", "data_dir", "config_file"} <= set(m)
    assert set(m["missing"]) >= {"agenda", "dumps", "reports"} and any("ImportError: test" in w for w in m["warnings"])


def test_the_server_imports_without_its_siblings(home):
    code = ("import sys\nfor m in ('agenda', 'dumps', 'voice', 'sessions', 'reports'): sys.modules['briandump.' + m] = None\n"
            "import briandump.server as s\nprint(sorted(s.WARNINGS))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "['agenda', 'dumps', 'reports', 'sessions', 'voice']"


# ---------- the port ----------
def run_server(*args, timeout=60):
    return subprocess.run([sys.executable, "-m", "briandump.server", *args], cwd=REPO, capture_output=True, text=True, timeout=timeout)


def test_a_port_held_by_another_program_stops_the_server(home):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        r = run_server("--port", str(port))
    assert r.returncode == server.EXIT_PORT_IN_USE
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert lines == [f"port {port} is in use by another program; pick another with `py briandump.py config --set port {port + 1}`"]
    assert "BrianDump panel on" not in r.stdout


def test_a_port_held_by_another_panel_is_named(home):
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        r = run_server("--port", str(port))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert r.returncode == server.EXIT_PORT_IN_USE and f"port {port} is in use by another BrianDump panel" in r.stderr


def test_two_servers_never_share_a_port(home):
    first = server.Server(("127.0.0.1", 0), server.Handler)
    try:
        with pytest.raises(OSError):
            server.Server(("127.0.0.1", first.server_address[1]), server.Handler)
    finally:
        first.server_close()
    assert server.Server.allow_reuse_address is (not WIN)


def test_the_server_process_starts_and_answers(home):
    home.config_file.write_text(json.dumps({"dump": {"processor": "mock", "claude_cmd": "briandump-test-no-such-command"}}),
                                encoding="utf-8")
    port = free_port()
    out = home.state_dir / "server-process.log"
    with open(out, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen([sys.executable, "-m", "briandump.server", "--port", str(port)], cwd=REPO, stdout=logf,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            info, deadline = None, time.time() + 30
            while time.time() < deadline:
                info = server.read_shared_json(home.state / "server.json")
                if isinstance(info, dict) and info.get("pid") == proc.pid:
                    break
                assert proc.poll() is None, out.read_text(encoding="utf-8")
                time.sleep(0.1)
            assert info and info["port"] == port and info["sandbox"] is True
            code, st = get(f"http://127.0.0.1:{port}/api/state")
            assert code == 200 and CONTRACT <= set(st)
            assert get(f"http://127.0.0.1:{port}/api/machine")[1]["sandbox"] is True
        finally:
            proc.kill()
            proc.wait(timeout=10)


# ---------- the launchers: imports without side effects, the pure parts ----------
def load_watchdog():
    path = REPO / "briandump" / "watchdog.pyw"
    loader = importlib.machinery.SourceFileLoader("briandump_watchdog_under_test", str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


def test_launcher_modules_import_without_side_effects(home):
    import briandump.frameless as frameless
    import briandump.launch as launch
    wd = load_watchdog()
    assert launch.user32 is None and frameless._keep == {} and callable(wd.main)
    assert effects(home) == [] and not (home.state / "window.json").exists()
    assert launch.TITLE == frameless.TITLE == "BrianDump · SANDBOX"
    r = subprocess.run([sys.executable, str(REPO / "briandump" / "frameless.py")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "usage:" in r.stderr                  # its command line still parses, and nothing opened


def test_launch_geometry():
    from briandump import launch
    assert launch.panel_rect((0, 0, 1920, 1040), 1 / 3) == (1280, 0, 640, 1040)
    assert launch.app_rect((0, 0, 1920, 1040), 1 / 3) == (0, 0, 1280, 1040)
    assert launch.panel_rect((-2560, 0, 2560, 1400), 0.25) == (-640, 0, 640, 1400)
    assert launch.is_primary((0, 0, 1920, 1040)) and not launch.is_primary((1920, 0, 1920, 1040))
    assert launch._ps_quote("C:\\Users\\O'Neil\\x") == "'C:\\Users\\O''Neil\\x'"


def test_launch_in_the_sandbox_records_the_window(home, monkeypatch):
    from briandump import launch
    monkeypatch.setattr(launch, "server_state", lambda port=None: "up")
    monkeypatch.setattr(launch.subprocess, "Popen", boom)
    assert launch.main([]) == 0
    [e] = effects(home, "open_window")
    assert e["url"] == f"http://127.0.0.1:{home.port}/" and e["window"] == home.window
    monkeypatch.setattr(launch, "server_state", lambda port=None: "other")
    with pytest.raises(SystemExit, match="is in use by another program"):
        launch.main([])


def test_frameless_keeps_the_panel_on_its_origin(home, monkeypatch):
    from briandump import frameless
    monkeypatch.setattr(frameless.webbrowser, "open", boom)
    o = frameless.origin("http://127.0.0.1:8770/")
    assert o == "http://127.0.0.1:8770"
    assert not frameless.leaves_panel("http://127.0.0.1:8770/api/state", o)
    assert frameless.leaves_panel("https://example.com/docs", o)
    assert frameless.leaves_panel("http://127.0.0.1:87701/", o)
    assert not frameless.leaves_panel("about:blank", o)
    assert frameless.to_browser("file:///C:/Windows") is False and frameless.to_browser("javascript:x") is False
    assert frameless.to_browser("https://example.com/docs") is True
    assert [e["url"] for e in effects(home, "open_url")] == ["https://example.com/docs"]

    class R:
        left, top, right, bottom = 100, 100, 500, 900
    assert frameless.hit_code(101, 500, R) == 10 and frameless.hit_code(499, 899, R) == 17 and frameless.hit_code(300, 500, R) is None


def test_watchdog_sees_a_listener(home):
    wd = load_watchdog()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert wd.up(port) is True
    assert wd.up(port) is False


# ---------- 🎤 Answer: arm a question for your next talk clip ----------
SLAB = "# Questions\n\n- [ ] Slab or gravel: which way are you leaning? (from brain dump 2026-10-18 09:30)\n"


def dump_q(api):
    return next(q for q in get(f"{api}/api/state")[1]["questions"] if q["source"] == "dump")


def test_arming_a_question_shows_on_the_panel_until_you_cancel(home, api):
    home.questions_file.write_text(SLAB, encoding="utf-8")
    q = dump_q(api)
    code, body = post(f"{api}/api/answer/arm", {"id": q["id"]})
    assert code == 200 and body["ok"] and body["armed"]["id"] == q["id"]
    armed = get(f"{api}/api/state")[1]["armed"]
    assert armed["text"] == "Slab or gravel: which way are you leaning?" and 55 <= armed["left"] <= 60 and not armed["listening"]
    assert inbox_files(home) == []                                           # arming types nothing
    assert post(f"{api}/api/answer/disarm", {"nonce": armed["nonce"]}) == (200, {"ok": True, "disarmed": True})
    assert get(f"{api}/api/state")[1]["armed"] is None and not home.answer_arm.exists()


def test_an_arm_times_out_after_a_minute(home, api):
    home.questions_file.write_text(SLAB, encoding="utf-8")
    q = dump_q(api)
    server.answers.arm(q["id"], q["text"], tick="md", now=time.time() - server.answers.ARM_S - 1)
    assert get(f"{api}/api/state")[1]["armed"] is None


def test_the_last_arm_wins_on_the_panel(home, api):
    home.questions_file.write_text(SLAB + "- [ ] Which lumber yard? (from brain dump 2026-10-18 09:30)\n", encoding="utf-8")
    qs = [q for q in get(f"{api}/api/state")[1]["questions"] if q["source"] == "dump"]
    for q in qs:
        assert post(f"{api}/api/answer/arm", {"id": q["id"]})[0] == 200
    assert get(f"{api}/api/state")[1]["armed"]["id"] == qs[-1]["id"]


def test_an_answer_still_being_recorded_keeps_the_arm_on_the_panel(home, api):
    home.questions_file.write_text(SLAB, encoding="utf-8")
    q = dump_q(api)
    server.answers.arm(q["id"], q["text"], tick="md", now=time.time() - server.answers.ARM_S - 5)   # the window closed 5 s ago
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - server.answers.ARM_S // 2 - 5 - 3))
    ptt_status(home, recording=True, mode="talk", started=started)          # ...but you pressed the key in time
    armed = get(f"{api}/api/state")[1]["armed"]
    assert armed["listening"] is True and armed["id"] == q["id"]
    ptt_status(home, recording=True, mode="talk", started=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1)))
    assert get(f"{api}/api/state")[1]["armed"] is None                       # a clip pressed after the window does not count


def test_arming_refuses_a_dialog_an_unknown_id_and_an_ended_session(home, api):
    door = {"question": "Which door?", "header": "Door", "multiSelect": False,
            "options": [{"label": "Single", "description": "one leaf"}, {"label": "Double", "description": "two leaves"}]}
    transcript(home, [tool_use("toolu_dialog2", "AskUserQuestion", questions=[door])])
    live(home)
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if q["source"] == "asked")
    code, body = post(f"{api}/api/answer/arm", {"id": q["id"]})
    assert code == 409 and "answer it there" in body["error"]
    assert post(f"{api}/api/answer/arm", {"id": "qmd-nope"})[0] == 404
    assert not home.answer_arm.exists()


def test_arming_a_question_from_a_session_that_ended_is_refused(home, api):
    transcript(home, ASKED)                                                  # no registry record: the session has ended
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if "archive" in q["text"])
    code, body = post(f"{api}/api/answer/arm", {"id": q["id"]})
    assert code == 409 and "not running" in body["error"] and not home.answer_arm.exists()


def test_an_armed_session_question_remembers_who_asked(home, api):
    docs = "66666666-7777-4888-8999-000000000000"
    transcript(home, ASKED, folder="C--work-docs", sid=docs)
    live(home)
    live(home, docs, 4243, "Docs")
    q = next(q for q in get(f"{api}/api/state")[1]["questions"] if "archive" in q["text"])
    assert post(f"{api}/api/answer/arm", {"id": q["id"]})[0] == 200
    assert get(f"{api}/api/state")[1]["armed"]["to"] == "Docs"
    assert json.loads(home.answer_arm.read_text(encoding="utf-8"))["session"] == docs


def test_a_private_question_arms_by_id_and_keeps_its_own_words_off_the_panel(home, api):
    home.save_user_value("private_filter", ["(?i)zebra"])
    home.questions_file.write_text("# Questions\n\n- [ ] Paint the zebra crossing white? (from brain dump 2026-10-18 09:30)\n",
                                   encoding="utf-8")
    q = dump_q(api)
    assert q["text"] == "(private)"
    assert post(f"{api}/api/answer/arm", {"id": q["id"]})[0] == 200
    assert get(f"{api}/api/state")[1]["armed"]["text"] == "(private)"
    assert json.loads(home.answer_arm.read_text(encoding="utf-8"))["question"] == "Paint the zebra crossing white?"


def test_a_dump_deck_question_can_be_armed(home, api, real_dumps):
    make_dump(home)
    d = get(f"{api}/api/dumps/{DUMP_ID}")[1]
    q = d["questions"][0]
    assert post(f"{api}/api/answer/arm", {"id": q["id"], "dump": DUMP_ID})[0] == 200
    a = json.loads(home.answer_arm.read_text(encoding="utf-8"))
    assert a["tick"] == "md" and a["dump"] == DUMP_ID and a["question"].startswith("What did the measurement show")


def test_arm_then_speak_end_to_end_in_the_sandbox(home, api):
    """The real pieces: the page arms a brain-dump question; a clip lands with its PTT log line; the router (the sandbox's
    FakeWin and the real session reader) types the quote plus the words into Main; the question is ticked."""
    from briandump import sessions, voice
    home.questions_file.write_text(SLAB, encoding="utf-8")
    sid, pid = "11111111-2222-3333-4444-555555555555", 4242
    (home.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": str(home.state_dir), "name": "Main",
                                                           "entrypoint": "cli", "status": "idle", "startedAt": 1_792_000_000_000}),
                                               encoding="utf-8")
    tr = transcript(home, [{"type": "user", "message": {"role": "user", "content": "hi"}, "timestamp": "2026-10-15T10:00:00.000Z"},
                           {"type": "system", "subtype": "turn_duration", "durationMs": 900, "timestamp": "2026-10-15T10:00:02.000Z"}],
                    folder="C--work-project", sid=sid)
    q = dump_q(api)
    assert post(f"{api}/api/answer/arm", {"id": q["id"]})[0] == 200
    st = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() + 3))   # a clip pressed just after the click
    (home.inbox / f"{st}-{home.host}.txt").write_text("gravel, it drains better\n", encoding="utf-8")
    with open(home.inbox / f"log-{home.host}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": st, "seconds": 1.2, "transcribe_s": 0.6, "text": "gravel, it drains better"}) + "\n")
    t = [time.time() + 10]
    r = voice.Router(lambda: sessions.rows(), host=home.host, clock=lambda: t[0], sleep=lambda s: None)
    r.t0 = t[0] - voice.HANDOFF_GRACE_S - 1
    r.tick(t[0])
    item = r.items[0]
    line = '[answer to: "Slab or gravel: which way are you leaning?"] gravel, it drains better'
    assert item["status"] == "delivered" and item["target"] == "Main" and item["text"] == line, item
    assert json.dumps(line)[1:-1] in tr.read_text(encoding="utf-8")
    assert "- [x] Slab or gravel" in home.questions_file.read_text(encoding="utf-8")
    state = get(f"{api}/api/state")[1]
    assert state["armed"] is None and not [x for x in state["questions"] if x["source"] == "dump"]
