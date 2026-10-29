"""Tests for briandump/answers.py: 🎤 Answer arms one question for your next talk clip; the clip takes the arm once; the question
is ticked after delivery.

    py -m pytest tests/test_answers.py -q

The router and watch_inbox.py halves are in test_voice.py, the panel endpoints in test_server.py.
"""
import json
import time

from briandump import answers


def epoch(stamp):
    return time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))


def test_the_quote_is_one_line_without_double_quotes_and_capped():
    assert answers.quote('Slab or "gravel"?\nwhich one') == "Slab or 'gravel'? which one"
    long = answers.quote("x" * 500)
    assert len(long) == answers.QUOTE_MAX and long.endswith("…")
    assert answers.prefix("Slab or gravel?") == '[answer to: "Slab or gravel?"] '


def test_a_clip_started_inside_the_window_takes_the_arm_once(home):
    a = answers.arm("qmd-1", "Slab or gravel?", tick="md", now=1000.0)
    assert a["expires_at"] == 1000.0 + answers.ARM_S and home.answer_arm.exists()
    got = answers.take(1005.0)
    assert got["question"] == "Slab or gravel?" and got["tick"] == "md" and got["id"] == "qmd-1"
    assert answers.take(1006.0) is None                                      # one clip only
    assert not home.answer_arm.exists()


def test_a_clip_from_before_the_arm_or_after_the_window_leaves_it_armed(home):
    answers.arm("q1", "Which door?", now=1000.0)
    assert answers.take(990.0) is None                                       # pressed before you armed it (a line already queued)
    assert answers.take(1000.0 + answers.ARM_S + 1) is None                  # pressed after the window closed
    assert answers.take(None) is None                                        # no PTT log line: an unverified file never takes it
    assert home.answer_arm.exists()
    assert answers.take(999.0)                                               # the stamp's whole-second slack
    assert answers.take(1001.0) is None


def test_the_last_arm_wins(home):
    answers.arm("q1", "First?", now=1000.0)
    answers.arm("q2", "Second?", now=1010.0)
    assert answers.read(now=1011.0)["id"] == "q2"
    assert answers.take(1012.0)["question"] == "Second?"
    assert answers.take(1013.0) is None


def test_the_window_closes_but_a_clip_already_started_still_counts(home):
    answers.arm("q1", "Which door?", now=1000.0)
    end = 1000.0 + answers.ARM_S
    assert answers.read(now=end - 1)["open"] is True
    assert answers.read(now=end + 1)["open"] is False                        # the panel stops offering it
    assert answers.take(end - 2)["id"] == "q1"                               # ...but a clip pressed in time, still in Whisper, counts


def test_an_old_arm_is_cleaned_up(home):
    answers.arm("q1", "Which door?", now=1000.0)
    assert answers.read(now=1000.0 + answers.ARM_S + answers.KEEP_S + 1) is None
    assert not home.answer_arm.exists()


def test_disarm_removes_only_the_arm_it_names(home):
    old = answers.arm("q1", "First?", now=1000.0)
    answers.arm("q2", "Second?", now=1001.0)
    assert answers.disarm(old["nonce"]) is False                             # Esc in a window that still shows the old arm
    assert answers.read(now=1002.0)["id"] == "q2"
    assert answers.disarm() is True and answers.read(now=1002.0) is None
    assert answers.disarm() is False


def test_a_broken_arm_file_is_no_arm(home):
    home.answer_arm.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", "[]", '{"question": "x"}', '{"question": "x", "armed_at": "soon", "expires_at": 1, "nonce": "n"}'):
        home.answer_arm.write_text(junk, encoding="utf-8")
        assert answers.read() is None and answers.take(time.time()) is None


def test_clip_started_comes_from_the_stamp_and_the_ptt_log_line():
    st = "20261019-140000"
    assert answers.clip_started(st, {"seconds": 4.0, "transcribe_s": 1.5}) == epoch(st) - 5.5
    assert answers.clip_started(st, None) is None
    assert answers.clip_started("not-a-stamp", {"seconds": 1}) is None


def test_tick_closes_a_questions_md_line(home):
    home.questions_file.write_text("# Questions\n\n- [ ] Slab or gravel? (from brain dump 2026-10-18 09:30)\n", encoding="utf-8")
    assert answers.tick({"tick": "md", "question": "Slab or gravel?"}, "gravel") is True
    assert "- [x] Slab or gravel?" in home.questions_file.read_text(encoding="utf-8")


def test_tick_closes_a_questions_json_entry_with_the_answer(home):
    home.manual_questions.write_text(json.dumps([{"id": "q1", "text": "Which door?", "done": False}]), encoding="utf-8")
    assert answers.tick({"tick": "manual", "id": "q1", "question": "Which door?"}, "the double one") is True
    q = json.loads(home.manual_questions.read_text(encoding="utf-8"))[0]
    assert q["done"] is True and q["answer"] == "the double one" and q["done_at"]
    assert answers.tick({"tick": "manual", "id": "q1", "question": "Which door?"}, "again") is False   # already closed
    assert answers.tick({"tick": None, "question": "Ship it?"}) is False     # a question from Claude's message: nothing to tick
