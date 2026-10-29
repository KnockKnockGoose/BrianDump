"""Tests for briandump/voice.py (the panel-mode voice router) and briandump/watch_inbox.py (the monitor-mode fallback): inbox
classification (host names with hyphens), the PTT-log cross-check, prefix routing, the gate (including the real trust,
permission and question screens), the pre-Enter re-check, exactly-once FIFO delivery, the handoff grace, single ownership,
act() safety, private masking, 🎤 Answer (an armed question quotes itself in front of the next clip), and an end-to-end run
in the sandbox through the real session reader and FakeWin.
No real console, keyboard or session is touched: every console object here is a fake.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from briandump import answers, sessions, voice, watch_inbox

REPO = Path(__file__).resolve().parents[1]
HOST = "desktop-1a2b3c"                 # Windows default host names carry a hyphen
NOW = 1_792_080_000.0                   # 2026-10-15 ~16:00 UTC, so the 15-min / stable windows are realistic

# The real Claude Code workspace-trust screen: the gate must refuse it.
TRUST_SCREEN = [
    " Accessing workspace:", "", " C:\\Users\\user\\project", "",
    " Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source",
    " project, or work from your team). If not, take a moment to review what's in this folder first.", "",
    " Claude Code'll be able to read, edit, and execute files here.", "", " Security guide", "",
    " \u276f No, exit", "   Yes, I trust this folder", "", " Enter to confirm \u00b7 Esc to cancel"]
IDLE_SCREEN = ["\u276f Read notes.md", "  Done, the heading is blue now.", "", "\u203a Try \"how do I...\"", "", "> "]
WORKING_SCREEN = ["\u276f Editing server.py", "", "  Thinking\u2026 (esc to interrupt)"]
RULE = "\u2500" * 40
# Real 2.1.302 screens: a running turn (spinner, empty input box, status line), an AskUserQuestion and a permission dialog.
BUSY_SCREEN = ["     Reply from 127.0.0.1: bytes=32 time<1ms TTL=128", "     +3 lines (5s)", "     (ctrl+b to run in background)", "",
               "\u273d Flowing\u2026 (8s \u00b7 \u2193 94 tokens)", "",
               RULE + " test-session \u2500", "\u276f", RULE,
               "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 esc to interrupt \u00b7 \u2190 1 agent"
               "      \u25cb low \u00b7 /effort"]
QUESTION_SCREEN = [" \u2610 Color", "", "Do you prefer red or blue?", "", "\u276f 1. Red", "     Answer with red", "  2. Blue",
                   "     Answer with blue", "  3. Type something.", RULE, "  4. Chat about this", "",
                   "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel", RULE + " test-session \u2500"]
PERM_SCREEN = ["  \u23bf  Waiting\u2026", "", " Bash command", " Create directory probe_dir in the working directory", " mkdir probe_dir",
               " Do you want to proceed?", " \u276f 1. Yes", "   2. Yes, and always allow access to C:\\x from this project",
               "   3. Yes, and switch to auto mode \u00b7 auto mode handles these prompts for you", "   4. No", "",
               " Esc to cancel \u00b7 Tab to amend"]


def typed_screen(*lines):
    """BUSY_SCREEN with these lines in its input box (the text as Claude Code wraps it)."""
    return BUSY_SCREEN[:7] + [f"\u276f {lines[0]}", *[f"  {x}" for x in lines[1:]]] + BUSY_SCREEN[8:]


class ScreenWin:
    """A console stand-in: a fixed screen, a fixed keyboard-idle age, and a record of what was typed."""

    def __init__(self, screen=None, idle=10.0, result=(True, "")):
        self.screen, self.idle, self.typed, self.result = screen, idle, [], result

    def input_idle_s(self):
        return self.idle

    def screen_rows(self, pid, rows=30):
        return self.screen

    def type_text(self, pid, text, enter=True):
        self.typed.append((pid, text))
        return self.result


def sess(name, pid=100, status="idle", reg="idle", sid=None, machine=HOST, last="2026-10-15T10:00:00Z", note=""):
    return {"kind": "session", "machine": machine, "name": name, "pid": pid, "id": sid or (name + "-id"),
            "status": status, "reg_status": reg, "last_activity": last, "note": note}


def write(inbox, stamp, text, kind=None, age=10, now=NOW, host=HOST):
    name = f"{stamp}-{kind + '-' if kind else ''}{host}.txt"
    (inbox / name).write_text(text, encoding="utf-8")
    os.utime(inbox / name, (now - age, now - age))             # recent but past STABLE_S
    return name


def log_line(inbox, stamp, text, host=HOST):
    with open(inbox / f"log-{host}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": stamp, "seconds": 1.2, "transcribe_s": 0.6, "text": text}) + "\n")


def deliver_line(tr, text):                                    # the session "receives" the typed line (compact JSON)
    with open(tr, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": text}}, separators=(",", ":")) + "\n")


# ---------- inbox files ----------
def test_classify_only_this_host_and_known_kinds():
    assert voice.classify(f"20261015-120000-{HOST}.txt", HOST) == "voice"
    assert voice.classify(f"20261015-120000-panel-{HOST}.txt", HOST) == "panel"
    assert voice.classify("20261015-120000-icebox.txt", "icebox") == "voice"
    assert voice.classify("20261015-120000-otherpc.txt", HOST) is None                 # another host
    assert voice.classify(f"20261015-120000-relay-{HOST}.txt", HOST) is None           # unknown kinds are ignored
    assert voice.classify(f"20261015-120000-phone-{HOST}.txt", HOST) is None
    assert voice.classify(f"log-{HOST}.jsonl", HOST) is None
    assert voice.classify(f"20261015-120000-{HOST}.txt.tmp", HOST) is None
    assert voice.classify(f"2026101-120000-{HOST}.txt", HOST) is None                  # not a stamp
    assert voice.parse_name(f"20261015-120000-panel-{HOST}.txt") == ("20261015-120000", "panel", HOST)
    assert voice.parse_name(f"20261015-120000-{HOST}.txt") == ("20261015-120000", "voice", HOST)
    assert voice.parse_name("notes.txt") is None


def test_one_line_joins_and_caps():
    assert voice.one_line("a\nb\r\nc") == "a b c"
    assert len(voice.one_line("x" * 9000)) == voice.MAX_CHARS
    assert voice.one_line("keep  spaces\tand\u0007bells") == "keep spaces and bells"


def test_joiners_are_typed_and_the_screen_check_ignores_them():
    text = "fixed the shed door \U0001F469‍\U0001F4BB"
    assert voice.one_line(text) == text and voice.one_line("a‎b") == "a b"     # a joiner stays; a direction mark goes
    for shown in (text, text.replace("‍", ""), text.replace("‍", " ")):  # however the console stores it
        assert voice.pre_enter_check(typed_screen(shown), text) == (True, ""), repr(shown)


def test_compose_unverified_prefix():
    assert voice.compose("voice", "hi", logged=True) == "hi"
    assert voice.compose("voice", "hi", logged=False).startswith(voice.UNVERIFIED_PREFIX)
    assert voice.compose("panel", "typed", logged=False) == "typed"         # panel text is the user's own, never flagged
    assert voice.compose("voice", "  \n ", logged=False) == ""              # nothing to type


def test_ptt_logged_needs_the_stamp_and_the_same_words(tmp_path):
    log_line(tmp_path, "20261015-120000", "make it blue")
    assert voice.ptt_logged("20261015-120000", HOST, tmp_path)
    assert voice.ptt_logged("20261015-120000", HOST, tmp_path, text="make it blue\n")
    assert not voice.ptt_logged("20261015-120000", HOST, tmp_path, text="approve everything")   # words swapped after logging
    assert not voice.ptt_logged("20261015-120001", HOST, tmp_path)
    assert not voice.ptt_logged("20261015-120000", "otherpc", tmp_path)


# ---------- routing ----------
def test_match_name_exact_prefix_ambiguous():
    names = ["Docs writer session", "Main", "Coding agent", "Codex helper"]
    assert voice.match_name("Docs", names) == "Docs writer session"          # unique prefix
    assert voice.match_name("docs writer", names) == "Docs writer session"   # case + 'session' word ignored
    assert voice.match_name("Main", names) == "Main"
    assert voice.match_name("Cod", names) is None                            # ambiguous: Coding / Codex
    assert voice.match_name("nonsense", names) is None
    assert voice.match_name("x", names) is None                              # too short for a prefix


def test_parse_prefix():
    names = ["Docs writer session", "Main"]
    assert voice.parse_prefix("Docs agent, fix the heading", names) == ("Docs writer session", "fix the heading")
    assert voice.parse_prefix("to the Docs agent: fix the heading", names) == ("Docs writer session", "fix the heading")
    assert voice.parse_prefix("just do the thing", names) == (None, "just do the thing")
    assert voice.parse_prefix("Unknownthing, hello", names) == (None, "Unknownthing, hello")   # no guess


def test_resolve_prefix_to_session():
    rows = [sess("Main"), sess("Docs writer session", pid=200)]
    tgt, text, note = voice.resolve({"kind": "voice", "text": "Docs, fix the heading"}, rows, [], HOST)
    assert tgt["name"] == "Docs writer session" and text == "fix the heading" and "Docs" in note


def test_resolve_defaults_to_main():
    rows = [sess("Main"), sess("Docs writer session", pid=200)]
    tgt, text, note = voice.resolve({"kind": "voice", "text": "what's the status?"}, rows, [], HOST)
    assert tgt["name"] == "Main" and text == "what's the status?"


def test_resolve_unknown_named_target_goes_to_main_with_note():
    rows = [sess("Main")]
    tgt, text, note = voice.resolve({"kind": "voice", "text": "Docs agent, hi"}, rows, ["Docs writer session"], HOST)
    assert tgt["name"] == "Main" and text == "Docs agent, hi" and "not running" in note


def test_resolve_fallback_when_no_main():
    rows = [sess("scratch-d1", pid=200, last="2026-10-15T09:00:00Z"), sess("scratch-7f", pid=300, last="2026-10-15T11:00:00Z")]
    tgt, text, note = voice.resolve({"kind": "voice", "text": "hello"}, rows, [], HOST)
    assert tgt["name"] == "scratch-7f" and "Main not running" in note       # the most recently active one


def test_resolve_none_when_nothing_live():
    tgt, text, note = voice.resolve({"kind": "voice", "text": "hello"}, [], [], HOST)
    assert tgt is None and "no interactive session" in note


def test_headless_and_other_host_sessions_are_not_targets():
    rows = [sess("scratch-6f", pid=200, note="headless"), sess("Main", machine="otherpc"),
            sess("stuck-one", pid=300, note="headless · no transcript activity for 12 min")]
    assert voice.most_recent(rows, HOST) is None and voice.find_main(rows, HOST) is None


def test_find_main_by_configured_name():
    rows = [sess("Main"), sess("Desk", pid=200)]
    assert voice.find_main(rows, HOST)["name"] == "Main"
    assert voice.find_main(rows, HOST, "desk")["name"] == "Desk"


# ---------- gate ----------
def test_gate_allows_idle_at_prompt():
    ok, why = voice.gate(sess("Main"), ScreenWin(IDLE_SCREEN, idle=10.0))
    assert ok and why == ""


def test_gate_refuses_trust_screen():
    ok, why = voice.gate(sess("Main"), ScreenWin(TRUST_SCREEN, idle=10.0))
    assert not ok and "prompt" in why                                        # never type at the trust question


def test_gate_refuses_working_busy_recent_input_and_no_prompt():
    assert not voice.gate(sess("Main", status="running"), ScreenWin(IDLE_SCREEN))[0]
    assert not voice.gate(sess("Main", reg="busy"), ScreenWin(IDLE_SCREEN))[0]
    assert not voice.gate(sess("Main"), ScreenWin(IDLE_SCREEN, idle=0.5))[0]        # the user is typing
    assert not voice.gate(sess("Main"), ScreenWin(WORKING_SCREEN, idle=10))[0]      # "esc to interrupt"
    assert not voice.gate(sess("Main"), ScreenWin(["no prompt here", "blah"], idle=10))[0]
    assert not voice.gate(sess("Main"), ScreenWin(QUESTION_SCREEN, idle=10))[0]
    assert not voice.gate(sess("Main"), ScreenWin(PERM_SCREEN, idle=10))[0]


def test_gate_never_types_blind():
    ok, why = voice.gate(sess("Main"), ScreenWin(None, idle=10.0))            # an unreadable screen waits
    assert not ok and "cannot be read" in why


def test_gate_ignores_question_words_in_a_reply_but_not_a_dialog():
    reply = ["\u25cf Hello.", "", "  What do you want to work on?", "", RULE, "\u276f", RULE]
    assert voice.gate(sess("Main"), ScreenWin(reply, idle=10.0))[0]
    dialog = ["\u25cf Bash(rm x)", " \u2502 Do you want to proceed?", " \u2502 \u276f 1. Yes", " \u2502   2. No"]
    assert not voice.gate(sess("Main"), ScreenWin(dialog, idle=10.0))[0]


def test_gate_asks_the_console_object_if_it_can_work_at_all():
    class NotReady(ScreenWin):
        def ready(self):
            return False, "this panel server runs in a console window of its own"
    ok, why = voice.gate(sess("Main"), NotReady(IDLE_SCREEN))
    assert not ok and "console window" in why


# ---------- busy typing: deliver while busy, hold only while the session waits for you ----------
def test_voice_config_busy_typing_and_stale(home):
    assert voice._cfg()[3:] == (True, 120.0)                                 # defaults: busy typing on, 2-minute stale limit
    home.save_user_value("voice.busy_typing", False)                         # the kill switch: idle-only typing
    home.save_user_value("voice.stale_s", 90)
    assert voice._cfg()[3:] == (False, 90.0)
    home.save_user_value("voice.stale_s", 0)                                 # 0 = never stale
    assert voice._cfg()[4] == 0.0
    home.save_user_value("voice.stale_s", "soon")                            # not a number: the default
    assert voice._cfg()[4] == 120.0


def test_gate_without_busy_typing_keeps_the_idle_only_rule():
    assert not voice.gate(sess("Main", status="running", reg="busy"), ScreenWin(BUSY_SCREEN))[0]
    assert not voice.gate(sess("Main"), ScreenWin(BUSY_SCREEN))[0]            # "esc to interrupt" on screen


def test_busy_gate_allows_a_running_turn_at_an_empty_input_line():
    for st in ("running", "thinking"):
        ok, why = voice.gate(sess("Main", status=st, reg="busy"), ScreenWin(BUSY_SCREEN), allow_busy=True)
        assert ok, why
    assert voice.gate(sess("Main"), ScreenWin(IDLE_SCREEN), allow_busy=True)[0]       # idle is still fine


def test_busy_gate_still_refuses_prompts_drafts_and_blind_typing():
    busy = sess("Main", status="running", reg="busy")
    asking = {**sess("Main", status="needs_you", reg="waiting"), "step": "Claude needs your permission to use Bash"}
    ok, why = voice.gate(asking, ScreenWin(BUSY_SCREEN), allow_busy=True)
    assert not ok and why == "Main is waiting for you (Claude needs your permission to use Bash)"
    assert not voice.gate(sess("Main", reg="waiting"), ScreenWin(IDLE_SCREEN), allow_busy=True)[0]
    assert not voice.gate(busy, ScreenWin(TRUST_SCREEN), allow_busy=True)[0]
    assert not voice.gate(busy, ScreenWin(BUSY_SCREEN, idle=0.5), allow_busy=True)[0]            # you are at the keyboard
    ok, why = voice.gate(busy, ScreenWin(typed_screen("half a sentence you are still typ")), allow_busy=True)
    assert not ok and "not empty" in why                                                         # your draft in the box
    ok, why = voice.gate(busy, ScreenWin(None), allow_busy=True)
    assert not ok and "cannot be read" in why                                                    # never blind
    # the dialogs are refused by the SCREEN even when the status lags and still says running / busy
    for screen in (PERM_SCREEN, QUESTION_SCREEN, BUSY_SCREEN[:5] + [" │ Do you want to proceed?", " │ ❯ 1. Yes", " │   2. No"]):
        ok, why = voice.gate(busy, ScreenWin(screen), allow_busy=True)
        assert not ok and "prompt" in why


# ---------- the pre-Enter re-check ----------
def test_pre_enter_check_accepts_the_typed_line_even_when_wrapped():
    line = "make the heading blue and 2 px taller"
    assert voice.pre_enter_check(typed_screen("make the heading blue and 2 px", "taller"), line) == (True, "")


def test_pre_enter_check_withholds_enter_on_a_dialog_or_a_missing_line():
    line = "make the heading blue and 2 px taller"
    ok, why = voice.pre_enter_check(PERM_SCREEN, line)
    assert not ok and "appeared" in why                                      # the race: a dialog popped up while typing
    assert not voice.pre_enter_check(QUESTION_SCREEN, line)[0]
    ok, why = voice.pre_enter_check(BUSY_SCREEN, line)                       # the keys went somewhere else
    assert not ok and "not in the input line" in why
    assert not voice.pre_enter_check(None, line)[0]                          # unreadable: never Enter blind
    assert not voice.pre_enter_check(["just output", "no prompt"], line)[0]


def test_pre_enter_check_does_not_hold_back_your_own_dialog_words():
    line = "make the form submit when I press enter, do you want to see it first? allow once"
    screen = typed_screen("make the form submit when I press enter, do you", "want to see it first? allow once")
    assert voice.pre_enter_check(screen, line) == (True, "")
    # ... but a real dialog still wins, even when the typed text happens to contain its question
    assert not voice.pre_enter_check(PERM_SCREEN, "Do you want to proceed? 1. Yes")[0]


class ProbeWin(voice.Win):
    """voice.Win with the console calls replaced: records the characters written and shows a chosen screen."""

    def __init__(self, screen):
        self.ok, self.attached, self.lock, self.writes, self.screen = True, False, threading.RLock(), [], screen

        class K:
            def FreeConsole(self):
                return 1

            def CloseHandle(self, h):
                return 1
        self.k32 = K()

    def _attach(self, pid):
        return ""

    def _open(self, name):
        return 1

    def _key(self, ch, down):
        return ch

    def _write(self, h, recs):
        self.writes.append("".join(recs[::2]))
        return True

    def _rows_attached(self, rows=30):
        return self.screen


def test_type_text_withholds_enter_when_a_dialog_appears():
    w = ProbeWin(PERM_SCREEN)
    ok, why = w.type_text(1, "yes", pause=0, before_enter=0)
    assert not ok and why.startswith(voice.ENTER_WITHHELD) and "\r" not in "".join(w.writes)


def test_type_text_sends_enter_after_the_words_are_seen():
    line = "make the heading blue"
    w = ProbeWin(typed_screen(line))
    ok, why = w.type_text(1, line, pause=0, before_enter=0)
    assert ok and "".join(w.writes) == line + "\r"


def test_confirm_accepts_a_user_line_or_a_mid_turn_enqueue(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "make it blue"}, separators=(",", ":")) + "\n",
                 encoding="utf-8")
    assert voice.confirm("sid", "make it blue", tries=1, find=lambda s: t, sleep=lambda s: None)
    assert not voice.confirm("sid", "something else", tries=1, find=lambda s: t, sleep=lambda s: None)
    deliver_line(t, "caf\u00e9 au lait")                                      # raw UTF-8, as Claude Code writes it
    assert voice.confirm("sid", "caf\u00e9 au lait", tries=1, find=lambda s: t, sleep=lambda s: None)


# ---------- the Router ----------
@pytest.fixture
def router(tmp_path, monkeypatch):
    inbox, state = tmp_path / "inbox", tmp_path / "state"
    inbox.mkdir()
    state.mkdir()
    monkeypatch.setattr(voice, "ptt_logged", lambda *a, **k: True)           # every clip counts as logged here
    tr = tmp_path / "t.jsonl"
    tr.write_bytes(b"")

    def find(sid):
        return tr if sid == "Main-id" else None

    def make(win, rows, clock, cards=(), grace_passed=True, allow_busy=True, stale_s=120):
        # busy typing and the 2-minute stale rule passed in, so a config file never changes what a test sees
        r = voice.Router(sessions_fn=lambda: rows, cards_fn=lambda: cards, host=HOST, inbox=inbox, state_dir=state, win=win,
                         clock=clock, sleep=lambda s: None, find=find, allow_busy=allow_busy, stale_s=stale_s)
        if grace_passed:
            r.t0 = clock() - voice.HANDOFF_GRACE_S - 1                       # the handoff grace is already over
        return r
    return {"inbox": inbox, "state": state, "tr": tr, "make": make}


def test_router_delivers_once_and_marks_handled(router):
    write(router["inbox"], "20261015-120000", "make it blue")
    win = ScreenWin(IDLE_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])
    r.sleep = lambda s: deliver_line(router["tr"], "make it blue")           # the line lands in the transcript mid-confirm
    r.tick(now[0])
    now[0] += voice.SETTLE_S + 1
    r.tick(now[0])
    assert win.typed == [(100, "make it blue")]                              # typed exactly once
    assert f"20261015-120000-{HOST}.txt" in r.handled()                      # durable marker (shared with watch_inbox)
    item = r.items[0]
    assert item["status"] == "delivered" and item["target"] == "Main" and item["confirmed"]
    r2 = router["make"](win, [sess("Main")], lambda: now[0] + 10)            # a restart must not re-type a handled file
    r2.tick(now[0] + 10)
    assert win.typed == [(100, "make it blue")]


def test_router_kill_switch_queues_until_idle(router):
    # voice.busy_typing = false: the old rule, a busy session is waited for
    write(router["inbox"], "20261015-120100", "hello there")
    win = ScreenWin(BUSY_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main", status="running", reg="busy")], lambda: now[0], allow_busy=False)
    r.tick(now[0])
    assert win.typed == [] and r.items[0]["status"] == "queued" and "running" in r.items[0]["note"]
    now[0] += voice.SETTLE_S + 1
    r.sessions_fn = lambda: [sess("Main", status="idle")]
    win.screen = IDLE_SCREEN
    deliver_line(router["tr"], "hello there")
    r.tick(now[0])
    assert win.typed == [(100, "hello there")] and r.items[0]["status"] == "delivered"


def test_router_claims_nothing_during_the_grace_period(router):
    write(router["inbox"], "20261015-120200", "quick")
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW, grace_passed=False)
    r.t0 = NOW                                                               # just started
    r.tick(NOW + 1)                                                          # an old watcher may still consume this file
    assert not r.items and win.typed == [] and not r.handled()
    assert r.snapshot()["grace"] is True                                     # the panel can say "starting"
    deliver_line(router["tr"], "quick")
    r.tick(NOW + voice.HANDOFF_GRACE_S + 1)
    assert win.typed == [(100, "quick")]


def test_router_old_unconsumed_files_are_not_replayed(router):
    write(router["inbox"], "20261015-010000", "ancient", age=3600)           # older than the 15-min catch-up window
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [] and not r.items


def test_router_waits_for_a_file_still_being_written(router):
    write(router["inbox"], "20261015-120300", "fresh", age=0.1)
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert not r.items                                                       # younger than STABLE_S


def test_router_fifo_order(router):
    write(router["inbox"], "20261015-120400", "first")
    write(router["inbox"], "20261015-120401", "second")
    win = ScreenWin(IDLE_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])
    r.tick(now[0])                                                           # one delivery per settle window, oldest first
    assert [t for _, t in win.typed] == ["first"]
    deliver_line(router["tr"], "first")
    now[0] += voice.SETTLE_S + 1
    r.tick(now[0])
    assert [t for _, t in win.typed] == ["first", "second"]


def test_router_ignores_other_hosts_logs_and_unknown_kinds(router):
    write(router["inbox"], "20261015-120500", "not mine", host="otherpc")
    write(router["inbox"], "20261015-120501", "relayed", kind="relay")
    (router["inbox"] / f"log-{HOST}.jsonl").write_text("{}\n", encoding="utf-8")
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert not r.items and win.typed == []


def test_router_panel_line_is_typed_as_is(router):
    write(router["inbox"], "20261015-120600", "[panel answer to: which colour?] blue", kind="panel")
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [(100, "[panel answer to: which colour?] blue")]


def test_split_meta_reads_a_routing_line_only_when_there_is_one():
    routed = voice.META + '{"session": "Docs-id"}\n[panel answer to: archive it?] yes'
    assert voice.split_meta(routed) == ({"session": "Docs-id"}, "[panel answer to: archive it?] yes")
    assert voice.split_meta("plain words\nsecond line") == ({}, "plain words\nsecond line")
    assert voice.split_meta(voice.META + "not json\nrest") == ({}, voice.META + "not json\nrest")


def test_router_types_an_answer_into_the_session_that_asked(router):
    write(router["inbox"], "20261015-120610", voice.META + '{"session": "Docs-id"}\n[panel answer to: archive it?] yes', kind="panel")
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main", pid=100), sess("Docs", pid=200)], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [(200, "[panel answer to: archive it?] yes")]        # Docs asked: Docs gets it, never Main
    assert r.items[0]["to"] == "Docs-id" and r.items[0]["target"] == "Docs"


def test_router_never_moves_an_answer_on_to_main_when_its_session_is_gone(router):
    write(router["inbox"], "20261015-120620", voice.META + '{"session": "Docs-id"}\n[panel answer to: archive it?] yes', kind="panel")
    write(router["inbox"], "20261015-120621", "carry on")
    win = ScreenWin(IDLE_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])                  # Docs is not running
    r.tick(now[0])
    assert win.typed == [] and r.items[0]["status"] == "queued" and "not running" in r.items[0]["note"]
    now[0] += voice.TARGET_GONE_S + 1                                         # a registry rewrite never fails a line at once
    r.tick(now[0])
    item = r.items[0]
    assert item["status"] == "failed" and "nothing was typed" in item["note"] and item["target"] == "the session that asked"
    deliver_line(router["tr"], "carry on")
    now[0] += 1
    r.tick(now[0])                                                           # the lines behind it move on
    assert win.typed == [(100, "carry on")]
    assert r.act("retry", item["id"]) == (True, "")                          # Retry: Docs again, still never Main
    r.sessions_fn = lambda: [sess("Main"), sess("Docs", pid=200)]
    now[0] += voice.SETTLE_S + 1
    r.tick(now[0])
    assert win.typed[-1] == (200, "[panel answer to: archive it?] yes")


def test_router_withheld_enter_fails_the_line_and_says_nothing_was_approved(router):
    write(router["inbox"], "20261015-120700", "careful now")
    win = ScreenWin(IDLE_SCREEN, result=(False, f"{voice.ENTER_WITHHELD}: a dialog appeared while typing (Do you want to)"))
    now = [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])
    r.tick(now[0])
    item = r.items[0]
    assert item["status"] == "failed" and "Nothing was approved" in item["note"] and "Retry" in item["note"]
    now[0] += voice.SETTLE_S + 1
    r.tick(now[0])
    assert len(win.typed) == 1                                               # a failed line is never retyped on its own


def test_router_restart_mid_typing_does_not_retype(router):
    state_file = router["state"] / "voice_router.json"
    state_file.write_text(json.dumps({"items": [{"id": "x.txt", "status": "typing", "text": "half"}]}), encoding="utf-8")
    r = router["make"](ScreenWin(IDLE_SCREEN), [sess("Main")], lambda: NOW)
    assert r.items[0]["status"] == "failed" and "Retry" in r.items[0]["note"]


def test_router_unverified_voice_file_gets_the_prefix(tmp_path):
    inbox, state = tmp_path / "inbox", tmp_path / "state"
    inbox.mkdir()
    write(inbox, "20261015-120800", "spoken and logged")
    log_line(inbox, "20261015-120800", "spoken and logged")
    write(inbox, "20261015-120801", "dropped in by something else")
    write(inbox, "20261015-120802", "swapped words")
    log_line(inbox, "20261015-120802", "the original words")
    r = voice.Router(lambda: [], host=HOST, inbox=inbox, state_dir=state, win=ScreenWin(IDLE_SCREEN), clock=lambda: NOW,
                     sleep=lambda s: None, find=lambda s: None)
    r.t0 = NOW - voice.HANDOFF_GRACE_S - 1
    r.tick(NOW)
    texts = {i["stamp"]: i["text"] for i in r.items}
    assert texts["20261015-120800"] == "spoken and logged"
    assert texts["20261015-120801"].startswith(voice.UNVERIFIED_PREFIX)
    assert texts["20261015-120802"].startswith(voice.UNVERIFIED_PREFIX)


def test_act_only_drops_or_retries_never_sets_text(router):
    write(router["inbox"], "20261015-120900", "careful")
    win = ScreenWin(PERM_SCREEN)
    r = router["make"](win, [sess("Main", status="needs_you", reg="waiting")], lambda: NOW)   # at a prompt: the line stays queued
    r.tick(NOW)
    iid = r.items[0]["id"]
    assert r.act("inject", iid) == (False, "cannot inject a queued line")    # no arbitrary-text action exists
    assert r.act("drop", iid)[0] and r.items[0]["status"] == "dropped"
    assert r.act("retry", iid)[0] and r.items[0]["status"] == "queued"
    assert r.act("drop", "no-such-id") == (False, "no such line")
    # the only text ever typed is what the inbox file held: act() has no text parameter at all
    assert list(inspect.signature(r.act).parameters) == ["action", "item_id"]
    public = [n for n in dir(voice.Router) if not n.startswith("_")]
    assert not any("text" in n or n.startswith("type") for n in public)      # no typing entry point on the router


def test_private_lines_are_masked_in_snapshot(router):
    write(router["inbox"], "20261015-121000", "call the dentist about the bill")
    win = ScreenWin(PERM_SCREEN)
    r = router["make"](win, [sess("Main", status="needs_you", reg="waiting")], lambda: NOW)   # at a prompt: the line stays queued
    r.tick(NOW)
    snap = r.snapshot(show_private=False, private=re.compile("dentist|bill", re.I))
    assert snap["items"][0]["text"] == "(private line)" and snap["items"][0]["private"]
    assert "dentist" not in json.dumps(snap).lower()
    assert "dentist" in r.snapshot(show_private=True, private=re.compile("dentist", re.I))["items"][0]["text"]
    c = r.compact(private=re.compile("dentist", re.I))
    assert c["mode"] == "panel" and c["queued"] == 1 and c["last"]["text"] == "(private line)" and c["target"] == "Main"
    assert c["events"][0]["kind"] == "queued" and c["events"][0]["text"] == "(private line)" and "dentist" not in json.dumps(c)


# ---------- deliver while busy, hold at a prompt, stale lines, signals ----------
def kinds(r):
    return [e["kind"] for e in r.events]


def enqueue_line(tr, text):                                    # what Claude Code logs for a line typed while a turn runs
    with open(tr, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "sessionId": "Main-id", "content": text},
                           separators=(",", ":")) + "\n")


def test_router_delivers_while_main_is_busy(router):
    write(router["inbox"], "20261019-120000", "also make the heading 2 px taller")
    win = ScreenWin(BUSY_SCREEN)
    r = router["make"](win, [sess("Main", status="thinking", reg="busy")], lambda: NOW)
    r.sleep = lambda s: enqueue_line(router["tr"], "also make the heading 2 px taller")   # queued by Claude Code mid-confirm
    r.tick(NOW)
    assert win.typed == [(100, "also make the heading 2 px taller")]
    item = r.items[0]
    assert item["status"] == "delivered" and item["confirmed"] and "busy" in item["note"]
    assert kinds(r) == ["delivered"] and "after its current step" in r.events[0]["reason"]  # one signal, no "queued" first


def test_router_holds_at_a_permission_prompt_then_delivers(router):
    write(router["inbox"], "20261019-120100", "and check the footer")
    win = ScreenWin(PERM_SCREEN)
    now = [NOW]
    r = router["make"](win, [{**sess("Main", status="needs_you", reg="waiting"), "step": "Claude needs your permission to use Bash"}],
                       lambda: now[0])
    r.tick(now[0])
    assert win.typed == [] and r.items[0]["status"] == "queued" and "waiting for you" in r.items[0]["note"]
    r.sessions_fn = lambda: [sess("Main", status="running", reg="busy")]     # the status lags; the dialog is still up
    for _ in range(3):
        now[0] += 1
        r.tick(now[0])
    assert win.typed == [] and "shows a prompt" in r.items[0]["note"]
    win.screen = BUSY_SCREEN                                                 # you answered it; the turn runs on
    now[0] += 1
    r.tick(now[0])
    assert win.typed == [(100, "and check the footer")] and r.items[0]["status"] == "delivered"
    assert kinds(r) == ["queued", "delivered"]                               # one signal each, none repeated while it waited
    assert "waiting for you" in r.events[0]["reason"] and r.events[0]["target"] == "Main"


def test_router_question_screen_holds_and_nothing_is_typed_at_it(router):
    write(router["inbox"], "20261019-120150", "two")                         # "two" would pick option 2
    win = ScreenWin(QUESTION_SCREEN)
    r = router["make"](win, [sess("Main", status="running", reg="busy")], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [] and r.items[0]["status"] == "queued" and "prompt" in r.items[0]["note"]


def test_router_quick_wait_is_silent_but_a_real_hold_signals_once(router):
    write(router["inbox"], "20261019-120200", "first", age=0.5)              # just spoken; the key release still counts as input
    win = ScreenWin(IDLE_SCREEN, idle=0.5)
    now = [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])
    r.tick(now[0])
    assert win.typed == [] and r.items[0]["note"] == "you are at the keyboard" and kinds(r) == []
    win.idle = 10.0
    now[0] += 1
    r.tick(now[0])
    assert win.typed == [(100, "first")] and kinds(r) == ["delivered"]       # in within HOLD_SIGNAL_S: no "queued" noise
    write(router["inbox"], "20261019-120210", "second", age=0.5, now=now[0])
    write(router["inbox"], "20261019-120211", "third", age=0.5, now=now[0])
    r.sessions_fn = lambda: [sess("Main", status="needs_you", reg="waiting")]
    for _ in range(8):                                                       # blocked for a while: each line signals once
        now[0] += 1
        r.tick(now[0])
    assert kinds(r) == ["delivered", "queued", "queued"]
    assert r.events[2]["reason"].startswith("behind an earlier line")


def test_router_failed_typing_signals_not_sent(router):
    write(router["inbox"], "20261019-120300", "run it")
    win = ScreenWin(BUSY_SCREEN, result=(False, f"{voice.ENTER_WITHHELD}: a dialog appeared while typing (Do you want to)"))
    r = router["make"](win, [sess("Main", status="running", reg="busy")], lambda: NOW)
    r.tick(NOW)
    assert r.items[0]["status"] == "failed" and kinds(r) == ["failed"] and "Nothing was approved" in r.events[0]["reason"]


def test_router_stale_line_needs_your_ok(router):
    write(router["inbox"], "20261019-121000", "make it blue")
    win = ScreenWin(PERM_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main", status="needs_you", reg="waiting")], lambda: now[0])
    r.tick(now[0])
    assert r.items[0]["status"] == "queued"
    now[0] += 115                                                            # 125 s after it was spoken, still blocked
    r.tick(now[0])
    item = r.items[0]
    assert item["status"] == "stale" and "Send anyway or Drop" in item["note"] and "waiting for you" in item["note"]
    r.sessions_fn = lambda: [sess("Main")]                                   # the prompt clears: still not typed on its own
    win.screen = IDLE_SCREEN
    for _ in range(5):
        now[0] += voice.SETTLE_S + 1
        r.tick(now[0])
    assert win.typed == [] and r.items[0]["status"] == "stale" and kinds(r) == ["queued", "stale"]
    c = r.compact()
    assert c["stale"] == 1 and c["queued"] == 0 and c["stale_items"][0]["id"] == item["id"] and r.snapshot()["stale"] == 1


def test_router_old_lines_go_stale_even_when_main_is_free(router):
    # a restart's catch-up (files under 15 min old): a line spoken 5 min ago is not typed on its own, nor does it block
    write(router["inbox"], "20261019-115500", "the old one", age=300)
    write(router["inbox"], "20261019-120000", "the fresh one", age=10)
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert [(i["text"], i["status"]) for i in r.items] == [("the old one", "stale"), ("the fresh one", "delivered")]
    assert win.typed == [(100, "the fresh one")]
    # a state file written before this change (no "born"): the age comes from queued_at, so a restart cannot replay it either
    (router["state"] / voice.CONF.voice_router.name).write_text(json.dumps({"items": [
        {"id": "20261019-110000-x.txt", "kind": "voice", "text": "from before", "status": "queued", "queued_at": voice.iso(NOW - 600)}]}),
        encoding="utf-8")
    r2 = router["make"](win, [sess("Main")], lambda: NOW + 10)
    r2.tick(NOW + 10)
    assert r2.items[0]["status"] == "stale" and win.typed == [(100, "the fresh one")]


def test_router_stale_rule_off_with_zero(router):
    write(router["inbox"], "20261019-115000", "whenever", age=600)
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW, stale_s=0)
    r.tick(NOW)
    assert win.typed == [(100, "whenever")]


def test_router_send_anyway_types_a_stale_line_once(router):
    write(router["inbox"], "20261019-121500", "do the thing", age=200)
    win = ScreenWin(IDLE_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])
    r.tick(now[0])
    iid = r.items[0]["id"]
    assert win.typed == [] and r.items[0]["status"] == "stale"
    assert r.act("send", iid) == (False, "cannot send a stale line")         # no new action: Send anyway is a retry
    assert r.act("retry", iid) == (True, "")
    assert r.items[0]["status"] == "queued" and r.items[0]["forced"] and "sent anyway" in r.items[0]["note"]
    r.sleep = lambda s: deliver_line(router["tr"], "do the thing")
    now[0] += 1
    r.tick(now[0])
    assert win.typed == [(100, "do the thing")] and r.items[0]["status"] == "delivered" and r.items[0]["confirmed"]
    assert kinds(r) == ["stale", "delivered"]
    for _ in range(3):                                                       # exactly once, and never stale again
        now[0] += 300
        r.tick(now[0])
    assert win.typed == [(100, "do the thing")]
    assert r.act("retry", iid) == (False, "cannot retry a delivered line")


def test_router_send_anyway_still_waits_for_a_prompt_to_clear(router):
    write(router["inbox"], "20261019-121600", "after the prompt", age=200)
    win = ScreenWin(PERM_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main", status="needs_you", reg="waiting")], lambda: now[0])
    r.tick(now[0])
    assert r.act("retry", r.items[0]["id"])[0]                              # Send anyway
    for _ in range(4):                                                       # Send anyway never means "type at the dialog"
        now[0] += 200
        r.tick(now[0])
    assert win.typed == [] and r.items[0]["status"] == "queued"
    r.sessions_fn = lambda: [sess("Main", status="running", reg="busy")]
    win.screen = BUSY_SCREEN
    now[0] += 1
    r.tick(now[0])
    assert win.typed == [(100, "after the prompt")]


def test_router_drop_stale_line_is_never_typed(router):
    write(router["inbox"], "20261019-122000", "never mind", age=200)
    win = ScreenWin(IDLE_SCREEN)
    now = [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])
    r.tick(now[0])
    assert r.act("drop", r.items[0]["id"]) == (True, "") and r.items[0]["status"] == "dropped"
    for _ in range(3):
        now[0] += voice.SETTLE_S + 1
        r.tick(now[0])
    assert win.typed == [] and r.items[0]["status"] == "dropped" and r.compact()["stale"] == 0


def test_second_router_stays_passive_while_another_live_one_holds_the_lock(router, monkeypatch):
    write(router["inbox"], "20261015-121100", "only once")
    lock = router["state"] / "voice_watcher.json"
    lock.write_text(json.dumps({"session": voice.OWNER, "pid": 999_999, "t": NOW}), encoding="utf-8")
    monkeypatch.setattr(voice, "proc_alive", lambda pid: True)
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert r.passive and not r.items and win.typed == [] and r.snapshot()["passive"]
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == 999_999   # the owner's lock is left alone
    monkeypatch.setattr(voice, "proc_alive", lambda pid: False)             # the other panel server is gone
    t = NOW + voice.BEAT_S
    r.tick(t)
    assert not r.passive and json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not r.items                                                       # taking over starts a grace period
    deliver_line(router["tr"], "only once")
    r.tick(t + voice.HANDOFF_GRACE_S + 1)
    assert win.typed == [(100, "only once")]


def test_router_takes_the_lock_from_a_watcher(router):
    lock = router["state"] / "voice_watcher.json"
    lock.write_text(json.dumps({"session": "some-session-id", "pid": 999_998, "t": NOW}), encoding="utf-8")
    r = router["make"](ScreenWin(IDLE_SCREEN), [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert not r.passive and json.loads(lock.read_text(encoding="utf-8"))["session"] == voice.OWNER


def test_write_atomic_and_trim_handled(tmp_path):
    p = tmp_path / "sub" / "x.json"
    assert voice.write_atomic(p, "one") and p.read_text(encoding="utf-8") == "one"
    assert voice.write_atomic(p, "two") and p.read_text(encoding="utf-8") == "two"
    assert not list(p.parent.glob("*.tmp"))
    h = tmp_path / "handled.txt"
    h.write_text("".join(f"f{i}.txt\n" for i in range(voice.HANDLED_MAX + 10)), encoding="utf-8")
    assert voice.trim_handled(h)
    lines = h.read_text(encoding="utf-8").split()
    assert len(lines) == voice.HANDLED_MAX // 2 and lines[-1] == f"f{voice.HANDLED_MAX + 9}.txt"
    assert not voice.trim_handled(h)                                         # short enough now


@pytest.mark.win32
@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses os.replace onto an open file; POSIX does not")
def test_write_atomic_retries_while_a_reader_holds_the_file(tmp_path):
    p = tmp_path / "status.json"
    p.write_text("old", encoding="utf-8")
    f = open(p, encoding="utf-8")                                            # a reader without FILE_SHARE_DELETE, like the panel
    threading.Timer(0.15, f.close).start()
    assert voice.write_atomic(p, "new", tries=40)
    assert p.read_text(encoding="utf-8") == "new"


# ---------- Win, FakeWin and the sandbox ----------
def test_router_uses_fakewin_in_the_sandbox(home):
    assert home.sandbox
    assert isinstance(voice.Router(lambda: []).win, voice.FakeWin)


@pytest.mark.win32
@pytest.mark.skipif(sys.platform != "win32", reason="the console calls exist on Windows only")
def test_win_refuses_when_this_process_owns_a_console(monkeypatch):
    w = voice.Win()
    monkeypatch.setattr(w, "_own_console", lambda: True)
    ok, why = w.ready()
    assert not ok and "console window of its own" in why
    assert w.type_text(1, "x") == (False, why)                               # nothing attached, nothing typed
    assert w.screen_rows(1) is None
    monkeypatch.setattr(w, "_own_console", lambda: False)
    assert w.ready() == (True, "")
    assert isinstance(voice.Win()._own_console(), bool)


def test_win_off_windows_is_harmless():
    w = voice.Win.__new__(voice.Win)                                         # what __init__ leaves on a non-Windows system
    w.ok, w.attached, w.lock = False, False, threading.RLock()                # (os.name is never patched: os.kill(pid, 0)
    assert w.ready() == (False, "not Windows") and w.input_idle_s() is None   # terminates a process on Windows)
    assert w.screen_rows(1) is None and w.type_text(1, "x") == (False, "not Windows")
    if os.name != "nt":
        assert voice.Win().ready() == (False, "not Windows")


def _session_fixture(home, sid="11111111-2222-3333-4444-555555555555", pid=4242, name="Main"):
    (home.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": str(home.state_dir), "name": name,
                                                            "entrypoint": "cli", "status": "idle", "startedAt": 1_792_000_000_000}),
                                               encoding="utf-8")
    tr = home.projects / "C--work-project" / f"{sid}.jsonl"
    tr.parent.mkdir(parents=True, exist_ok=True)
    tr.write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in [
        {"type": "user", "message": {"role": "user", "content": "hi"}, "timestamp": "2026-10-15T10:00:00.000Z"},
        {"type": "assistant", "timestamp": "2026-10-15T10:00:01.000Z",
         "message": {"role": "assistant", "model": "claude-test-1", "content": [{"type": "text", "text": "Hello."}]}},
        {"type": "system", "subtype": "turn_duration", "durationMs": 900, "timestamp": "2026-10-15T10:00:02.000Z"}]) + "\n",
        encoding="utf-8")
    return sid, tr


def test_end_to_end_in_the_sandbox(home):
    """inbox file + PTT log line -> real sessions.rows() -> gate -> FakeWin -> transcript -> confirmed; then a dialog that pops
    up while typing withholds Enter (nothing approved), and a busy keyboard keeps the line queued."""
    sid, tr = _session_fixture(home)
    host = home.host
    now = [time.time() + 1]

    def put(text, offset):
        st = time.strftime("%Y%m%d-%H%M%S", time.localtime(now[0] - 100 + offset))
        (home.inbox / f"{st}-{host}.txt").write_text(text + "\n", encoding="utf-8")
        log_line(home.inbox, st, text, host)
        return st

    put("make the header blue", 0)
    r = voice.Router(lambda: sessions.rows(), host=host, clock=lambda: now[0], sleep=lambda s: None)
    assert isinstance(r.win, voice.FakeWin)
    r.t0 = now[0] - voice.HANDOFF_GRACE_S - 1
    r.tick(now[0])
    item = r.items[0]
    assert item["status"] == "delivered" and item["confirmed"] and item["target"] == "Main", item
    assert '"content":"make the header blue"' in tr.read_text(encoding="utf-8")
    fx = [e for e in home.side_effect_log() if e["kind"] == "type_text"]
    assert fx and fx[-1]["text"] == "make the header blue" and fx[-1]["session"] == sid

    (home.state_dir / "dialog_on_type").write_text("", encoding="utf-8")   # a permission dialog pops up mid-typing
    size = tr.stat().st_size
    put("and the footer too", 1)
    now[0] += voice.SETTLE_S + 1
    r.tick(now[0])
    assert r.items[-1]["status"] == "failed" and "Nothing was approved" in r.items[-1]["note"]
    assert tr.stat().st_size == size                                         # nothing reached the session
    assert any(e["kind"] == "enter_withheld" for e in home.side_effect_log())

    (home.state_dir / "dialog_on_type").unlink()
    (home.state_dir / "keyboard_busy").write_text("", encoding="utf-8")
    put("third line", 2)
    now[0] += voice.SETTLE_S + 1
    r.tick(now[0])
    assert r.items[-1]["status"] == "queued" and "keyboard" in r.items[-1]["note"]


def test_voice_mode_reads_the_config(home):
    assert voice.voice_mode() == "panel"
    home.save_user_value("voice.mode", "monitor")
    assert voice.voice_mode() == "monitor"
    home.save_user_value("voice.mode", "something else")
    assert voice.voice_mode() == "panel"


# ---------- watch_inbox.py (monitor mode) ----------
def _watcher(tmp_path, out, clock, sleep, session="sess-1", host=HOST):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    return watch_inbox.Watcher(session, host, inbox, tmp_path / "lock.json", tmp_path / "handled.txt", out=out.append,
                               clock=clock, sleep=sleep), inbox


def test_watcher_reports_this_hosts_lines_once_then_exits(tmp_path):
    t = [time.time()]
    out = []
    w, inbox = _watcher(tmp_path, out, lambda: t[0], lambda s: t.__setitem__(0, t[0] + s))
    write(inbox, "20261015-120000", "first", now=t[0], age=5)
    log_line(inbox, "20261015-120000", "first")
    write(inbox, "20261015-120001", "typed on the panel", kind="panel", now=t[0], age=5)
    write(inbox, "20261015-120002", "not logged", now=t[0], age=5)
    write(inbox, "20261015-120003", "other pc", host="otherpc", now=t[0], age=5)
    write(inbox, "20261015-010000", "too old", now=t[0], age=3600)
    assert w.run(takeover=True, once=True) == 0
    lines = [x for x in out if x.startswith("VOICE ")]
    assert lines == [f"VOICE 20261015-120000-{HOST}: first", f"VOICE 20261015-120001-panel-{HOST}: typed on the panel",
                     f"VOICE 20261015-120002-{HOST}: {voice.UNVERIFIED_PREFIX}not logged"]
    handled = (tmp_path / "handled.txt").read_text(encoding="utf-8").split()
    assert f"20261015-120000-{HOST}.txt" in handled and "20261015-120003-otherpc.txt" not in handled

    class Enough(Exception):
        pass
    calls = []

    def sleep(s):                                                            # a re-armed watcher waits for speech: stop it
        calls.append(s)
        if len(calls) > 4:
            raise Enough
    out.clear()
    w2, _ = _watcher(tmp_path, out, time.time, sleep, session="sess-1")
    with pytest.raises(Enough):
        w2.run(takeover=False, once=True)
    assert not [x for x in out if x.startswith("VOICE ")]                    # nothing is delivered twice


def test_watcher_never_relays_a_routing_line(tmp_path):
    t = [time.time()]
    out = []
    w, inbox = _watcher(tmp_path, out, lambda: t[0], lambda s: t.__setitem__(0, t[0] + s))
    write(inbox, "20261015-120000", voice.META + '{"session": "sess-1"}\n[panel answer to: q?] yes', kind="panel", now=t[0], age=5)
    assert w.run(takeover=True, once=True) == 0
    assert [x for x in out if x.startswith("VOICE ")] == [f"VOICE 20261015-120000-panel-{HOST}: [panel answer to: q?] yes"]


def test_watcher_hands_off_before_reporting_when_the_lock_changes(tmp_path):
    t = [time.time()]
    out = []

    def sleep(s):
        t[0] += s
        if not (tmp_path / "inbox" / f"20261015-120100-{HOST}.txt").exists():
            (tmp_path / "lock.json").write_text(json.dumps({"session": "newer-session", "pid": 999_997, "t": t[0]}), encoding="utf-8")
            write(tmp_path / "inbox", "20261015-120100", "for the new owner", now=t[0], age=5)
            log_line(tmp_path / "inbox", "20261015-120100", "for the new owner")
    w, inbox = _watcher(tmp_path, out, lambda: t[0], sleep)
    assert w.run(takeover=True, once=False) == 0
    assert any("handed off" in x for x in out) and not any(x.startswith("VOICE ") for x in out)
    assert not (tmp_path / "handled.txt").exists() or "20261015-120100" not in (tmp_path / "handled.txt").read_text(encoding="utf-8")


def test_watcher_does_not_start_against_a_fresh_owner(tmp_path):
    out = []
    w, _ = _watcher(tmp_path, out, time.time, lambda s: None)
    (tmp_path / "lock.json").write_text(json.dumps({"session": "other-session", "pid": 999_996, "t": time.time()}), encoding="utf-8")
    assert w.run(takeover=False, once=True) == 0
    assert out and "not started" in out[0]
    assert json.loads((tmp_path / "lock.json").read_text(encoding="utf-8"))["session"] == "other-session"


def test_watch_inbox_hands_off_in_panel_mode(home, capsys):
    assert watch_inbox.main(["--session", "s1", "--once"]) == 0
    assert "handed off to the panel voice router" in capsys.readouterr().out


def test_watch_inbox_script_in_monitor_mode(home):
    """The command the SessionStart hook gives a session, run as a real background-style process."""
    home.save_user_value("voice.mode", "monitor")
    st = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 30))
    (home.inbox / f"{st}-{home.host}.txt").write_text("hello from the key\n", encoding="utf-8")
    log_line(home.inbox, st, "hello from the key", home.host)
    p = subprocess.run([sys.executable, str(REPO / "briandump" / "watch_inbox.py"), "--session", "abc123", "--takeover", "--once"],
                       capture_output=True, timeout=60, env=os.environ.copy())
    out = p.stdout.decode("utf-8", "replace")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert f"VOICE {st}-{home.host}: hello from the key" in out and "armed for session abc123" in out
    assert f"{st}-{home.host}.txt" in home.voice_handled.read_text(encoding="utf-8")


# ---------- 🎤 Answer: an armed question quotes itself in front of the next clip ----------
def epoch(stamp):
    return time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))


def clip(inbox, stamp, text):
    """A spoken clip as ptt.py leaves it: the inbox file and its PTT log line (1.2 s of audio, 0.6 s in Whisper)."""
    write(inbox, stamp, text)
    log_line(inbox, stamp, text)


DUMP_Q = "# Questions\n\n- [ ] Slab or gravel? (from brain dump 2026-10-18 09:30)\n"


def test_router_an_armed_question_takes_the_next_clip_and_is_ticked_once_it_is_delivered(router, home):
    home.questions_file.write_text(DUMP_Q, encoding="utf-8")
    answers.arm("qmd-1", "Slab or gravel?", tick="md", now=epoch("20261015-120700") - 10)
    clip(router["inbox"], "20261015-120700", "gravel, it drains better")
    clip(router["inbox"], "20261015-120705", "and order the stone")
    win, now = ScreenWin(IDLE_SCREEN), [NOW]
    r = router["make"](win, [sess("Main")], lambda: now[0])
    line = '[answer to: "Slab or gravel?"] gravel, it drains better'
    r.sleep = lambda s: deliver_line(router["tr"], line)
    r.tick(now[0])
    assert win.typed == [(100, line)]                                        # the words are the clip's; the arm adds the quote
    assert r.items[0]["status"] == "delivered" and "question ticked" in r.items[0]["note"]
    assert "- [x] Slab or gravel?" in home.questions_file.read_text(encoding="utf-8")
    assert not home.answer_arm.exists()                                      # disarmed after one clip
    r.sleep = lambda s: deliver_line(router["tr"], "and order the stone")
    now[0] += voice.SETTLE_S + 1
    r.tick(now[0])
    assert win.typed[-1] == (100, "and order the stone")                     # the next clip goes to Main unchanged


def test_router_a_clip_with_nothing_armed_goes_to_main_unchanged(router, home):
    answers.arm("qmd-1", "Slab or gravel?", tick="md", now=epoch("20261015-120800") - answers.ARM_S - 30)   # timed out
    clip(router["inbox"], "20261015-120800", "make the header blue")
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [(100, "make the header blue")] and "answer" not in r.items[0]


def test_router_a_clip_already_queued_before_the_arm_is_not_taken(router, home):
    clip(router["inbox"], "20261015-120900", "said before the click")
    answers.arm("qmd-1", "Slab or gravel?", tick="md", now=epoch("20261015-120900") + 5)
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [(100, "said before the click")] and home.answer_arm.exists()


def test_router_only_a_clip_with_its_ptt_log_line_can_take_the_arm(router, home):
    answers.arm("qmd-1", "Slab or gravel?", tick="md", now=epoch("20261015-121000") - 5)
    write(router["inbox"], "20261015-121000", "an inbox file nobody spoke")    # no PTT log line
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [(100, "an inbox file nobody spoke")] and home.answer_arm.exists()


def test_router_an_armed_answer_goes_to_the_session_that_asked_and_never_follows_a_spoken_name(router, home):
    answers.arm("q7", "Archive the old docs?", session="Docs-id", now=epoch("20261015-121100") - 3)
    clip(router["inbox"], "20261015-121100", "Main, yes archive them")
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main", pid=100), sess("Docs", pid=200)], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [(200, '[answer to: "Archive the old docs?"] Main, yes archive them')]
    assert r.items[0]["to"] == "Docs-id" and r.items[0]["target"] == "Docs"


def test_router_two_arms_the_last_one_wins(router, home):
    st = "20261015-121200"
    answers.arm("qmd-1", "Slab or gravel?", tick="md", now=epoch(st) - 20)
    answers.arm("q9", "Which colour for the door?", now=epoch(st) - 10)
    clip(router["inbox"], st, "green")
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert win.typed == [(100, '[answer to: "Which colour for the door?"] green')]


def test_router_a_clip_too_long_for_the_quote_goes_plain_and_keeps_the_arm(router, home, monkeypatch):
    monkeypatch.setattr(voice, "MAX_CHARS", 250)
    answers.arm("qmd-1", "Slab or gravel?", tick="md", now=epoch("20261015-121300") - 5)
    clip(router["inbox"], "20261015-121300", "word " * 20)
    win = ScreenWin(IDLE_SCREEN)
    r = router["make"](win, [sess("Main")], lambda: NOW)
    r.tick(NOW)
    assert win.typed[0][1] == ("word " * 20).strip() and "plain line" in r.items[0]["note"]
    assert home.answer_arm.exists()
    clip(router["inbox"], "20261015-121250", "long " * 50)                   # pressed before the arm: no note about it at all
    r.scan(NOW)
    assert "answer_note" not in r.items[-1]


def test_router_snapshot_marks_an_answer_without_repeating_the_question(router, home):
    home.save_user_value("private_filter", ["(?i)zebra"])
    answers.arm("qmd-1", "Paint the zebra crossing?", tick="md", now=epoch("20261015-121400") - 5)
    clip(router["inbox"], "20261015-121400", "yes, white")
    r = router["make"](ScreenWin(IDLE_SCREEN, idle=0.1), [sess("Main")], lambda: NOW)      # you are typing: it stays queued
    r.tick(NOW)
    [item] = r.snapshot(private=home.private_re)["items"]
    assert item["answer"] is True and item["text"] == "(private line)"
    assert r.items[0]["text"] == '[answer to: "Paint the zebra crossing?"] yes, white'     # the real words go to Claude


def test_watcher_relays_an_armed_answer_with_the_quote_and_ticks_it(tmp_path, home):
    home.questions_file.write_text(DUMP_Q, encoding="utf-8")
    t = [time.time()]
    out = []
    w, inbox = _watcher(tmp_path, out, lambda: t[0], lambda s: t.__setitem__(0, t[0] + s))
    st = time.strftime("%Y%m%d-%H%M%S", time.localtime(t[0] - 5))
    answers.arm("qmd-1", "Slab or gravel?", tick="md", now=epoch(st) - 10)
    write(inbox, st, "gravel", now=t[0], age=2)
    log_line(inbox, st, "gravel")
    assert w.run(takeover=True, once=True) == 0
    assert f'VOICE {st}-{HOST}: [answer to: "Slab or gravel?"] gravel' in out
    assert "- [x] Slab or gravel?" in home.questions_file.read_text(encoding="utf-8")
    assert not home.answer_arm.exists()
