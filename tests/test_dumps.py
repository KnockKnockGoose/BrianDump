"""dumps.py: the store, the worker's claim rules, the pipeline with the mock processor, failures and Retry, the report,
listing, the audio sweep, and meta.json writes under concurrent readers. No model calls, no real speech model."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

from briandump import agenda, cards, dumps
from briandump.config import CONF

TRANSCRIPT = (CONF.fixtures / "sample-transcript.txt").read_text(encoding="utf-8")
LATER = 60.0                                   # seconds ahead: past the import grace, well short of the stale limits


@pytest.fixture
def store(home, monkeypatch):
    monkeypatch.setattr(dumps, "RETRY_PAUSE_S", 0)
    monkeypatch.setattr(dumps, "_TRANSCRIBER", None)
    return home


class FakeTranscriber:
    def __init__(self, text):
        self.text, self.calls = text, []

    def transcribe(self, audio, **kw):
        self.calls.append(str(audio))
        return {"text": self.text, "seconds": 1.0, "took_s": 0.5, "words": len(self.text.split())}


def make_wav(p, seconds=1.0, rate=16000):
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return p


def import_text(tmp_path, text=TRANSCRIPT, name="desk.txt"):
    src = tmp_path / name
    src.write_text(text, encoding="utf-8")
    return dumps.import_file(src)


def later():
    return time.time() + LATER


# ---------- the store ----------
def test_ids_are_checked_before_any_path(store):
    did = dumps.new_id(dt.datetime(2026, 10, 18, 10, 42, 5))
    assert re.fullmatch(r"20261018-104205-[0-9a-f]{4}", did) and dumps.ID_RE.match(did)
    assert dumps.path(did) == store.dumps_dir / did
    for bad in ("../x", "..\\x", "20261018-104205-ab12/..", "20261018-104205-ab12\n", "20261018-104205-AB12", "20261018-104205-ab1",
                "\uff12" + "0261018-104205-ab12", "", None, "20261018-104205-ab12.json", "C:\\20261018-104205-ab12"):
        assert not dumps.valid_id(bad)
        with pytest.raises(ValueError):
            dumps.path(bad)
        assert dumps.read_meta(bad) is None and dumps.detail(bad) is None and dumps.read_transcript(bad) is None
        assert dumps.read_cards(bad) is None and dumps.retry(bad) is False and dumps.mark_sent(bad, "a1") is False


def test_create_update_and_read_like_the_recorder(store):
    t = time.time() - 30
    meta = dumps.create(source="ptt", created=t, duration_s=12.34)          # ptt.save_dump's call
    did = meta["id"]
    assert did.startswith(dt.datetime.fromtimestamp(t).strftime("%Y%m%d-%H%M%S"))
    assert (meta["status"], meta["source"], meta["durationSec"], meta["attempts"], meta["host"]) == ("recorded", "ptt", 12.3, 0, store.host)
    created = dt.datetime.fromisoformat(meta["createdAt"])
    assert created.utcoffset() is not None and abs(created.timestamp() - t) < 1
    assert dumps.path(did).is_dir() and json.loads((dumps.path(did) / "meta.json").read_text(encoding="utf-8")) == meta
    dumps.update(did, status="transcribing")
    dumps.write_transcript(did, "  words from the recorder \n")
    m = dumps.update(did, status="transcribed", words=4, timings={"transcribe_s": 1.5}, durationSec=12.3)
    m = dumps.update(did, timings={"think_s": 2.0})
    assert m["timings"] == {"transcribe_s": 1.5, "think_s": 2.0} and m["status"] == "transcribed"
    assert dumps.read_meta(did) == m and dumps.read_transcript(did) == "words from the recorder" and dumps.read_cards(did) is None
    assert dumps.create(source="ptt", created=t)["id"] != did                 # same second, another id
    with pytest.raises(KeyError):
        dumps.update("20200101-000000-0000", status="x")


def test_import_file(store, tmp_path):
    did = import_text(tmp_path)
    meta = dumps.read_meta(did)
    assert (meta["status"], meta["source"], meta["label"]) == ("transcribed", "import", "desk.txt")
    assert meta["words"] == len(TRANSCRIPT.split()) and dumps.read_transcript(did) == TRANSCRIPT.strip()
    wav = make_wav(tmp_path / "talk.wav", seconds=2.5)
    aid = dumps.import_file(wav, label="desk mic")
    m = dumps.read_meta(aid)
    assert (m["status"], m["durationSec"], m["label"]) == ("recorded", 2.5, "desk mic")
    assert (dumps.path(aid) / "audio.wav").read_bytes() == wav.read_bytes()
    (tmp_path / "x.mp3").write_bytes(b"ID3")
    assert (dumps.path(dumps.import_file(tmp_path / "x.mp3")) / "audio.mp3").is_file()
    (tmp_path / "bom.md").write_bytes("\ufeff# notes\nshed things".encode("utf-8"))
    assert dumps.read_transcript(dumps.import_file(tmp_path / "bom.md")) == "# notes\nshed things"
    with pytest.raises(ValueError, match="cannot import"):
        dumps.import_file(tmp_path / "notes.docx")
    (tmp_path / "empty.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        dumps.import_file(tmp_path / "empty.md")
    with pytest.raises(FileNotFoundError):
        dumps.import_file(tmp_path / "missing.txt")
    assert len(dumps.listing()) == 4                                         # failed imports leave nothing behind


# ---------- the pipeline ----------
def test_worker_turns_an_import_into_cards_report_agenda_and_questions(store, tmp_path):
    did = import_text(tmp_path)
    w = dumps.Worker()
    assert w.tick() == []                                     # an import waits out the grace (a CLI --now may own it)
    assert w.tick(now=later()) == [did]
    meta = dumps.read_meta(did)
    deck = cards.mock_cards()
    assert (meta["status"], meta["error"], meta["processor"], meta["attempts"], meta["worker"]) == ("ready", None, "mock", 1, None)
    assert meta["timings"]["think_s"] >= 0 and dumps.read_cards(did) == deck
    report = Path(meta["report"])
    assert report.parent == store.reports_dir and report.name == f"{did}-garden-shed.md"
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Brain dump · ") and "## Summary" in text and "| a1 |" in text and "mock processor" in text
    note = dumps.note_for(meta)
    assert re.fullmatch(r"\(from brain dump \d{4}-\d{2}-\d{2} \d{2}:\d{2}\)", note)
    agenda_md = store.agenda_file.read_text(encoding="utf-8")
    assert agenda_md.startswith("# Agenda\n") and all(f"- [ ] {t} {note}\n" in agenda_md for t in deck["todos"])
    assert meta["agenda_added"] == deck["todos"]
    assert meta["questions_added"] == [p["question"] for p in deck["nextPrompts"]]
    assert [i["base"] for i in agenda.read(store.questions_file)] == meta["questions_added"]
    assert "ready in" in (store.logs / "dumps.log").read_text(encoding="utf-8")
    assert w.tick(now=later()) == []                                       # a second tick has nothing to do
    assert dumps.read_meta(did)["updatedAt"] == meta["updatedAt"]


def test_dedupe_against_the_agenda_and_across_dumps(store, tmp_path):
    store.agenda_file.write_text("# Mine\n\n- [ ] measure the shed space with the long tape and peg the corners!\n", encoding="utf-8")
    todos = cards.mock_cards()["todos"]
    first = dumps.process(import_text(tmp_path), log=None)
    assert first["status"] == "ready" and first["agenda_added"] == todos[1:]       # the first was already on the agenda
    before = (store.agenda_file.read_bytes(), store.questions_file.read_bytes())
    second = dumps.process(import_text(tmp_path, name="again.txt"), log=None)
    assert second["status"] == "ready" and second["agenda_added"] == [] and second["questions_added"] == []
    assert (store.agenda_file.read_bytes(), store.questions_file.read_bytes()) == before
    assert store.agenda_file.read_text(encoding="utf-8").startswith("# Mine\n\n- [ ] measure the shed space")


def test_agenda_heading_from_the_config(store, tmp_path, monkeypatch):
    monkeypatch.setitem(store.cfg["agenda"], "heading", "From dumps")
    store.agenda_file.write_text("# Notes\n\n## Work\n- [ ] something else\n", encoding="utf-8")
    dumps.process(import_text(tmp_path), log=None)
    assert [i["base"] for i in agenda.read(store.agenda_file, "From dumps")] == cards.mock_cards()["todos"]
    assert agenda.read(store.agenda_file, "Work")[0]["text"] == "something else"


def test_a_failure_is_retried_once_then_retry_requeues(store, tmp_path, monkeypatch):
    calls = []

    def broken(*a, **k):
        calls.append(1)
        raise RuntimeError("claude -p timed out after 360 s")
    monkeypatch.setattr(cards, "process", broken)
    did = import_text(tmp_path)
    w = dumps.Worker()
    assert w.tick(now=later()) == [did]
    meta = dumps.read_meta(did)
    assert len(calls) == 2 and (meta["status"], meta["attempts"], meta["worker"]) == ("error", 2, None)
    assert meta["error"] == "claude -p timed out after 360 s"
    assert w.tick(now=later()) == []                                      # an error waits for the user
    monkeypatch.setattr(cards, "process", lambda *a, **k: cards.mock_cards())
    m = dumps.retry(did)
    assert m["status"] == "transcribed" and m["error"] is None and m["requeued"]
    assert dumps.retry(did) is False                                      # only an error dump is retried
    assert w.tick(now=later()) == [did]
    assert (dumps.read_meta(did)["status"], dumps.read_meta(did)["attempts"]) == ("ready", 3)


def test_permanent_failures_are_not_retried(store, tmp_path, monkeypatch):
    calls = []

    def missing(*a, **k):
        calls.append(1)
        raise cards.ClaudeNotFound("Claude Code (`claude`) was not found on PATH")
    monkeypatch.setattr(cards, "process", missing)
    did = import_text(tmp_path)
    m = dumps.process(did, log=None)
    assert len(calls) == 1 and m["status"] == "error" and "not found on PATH" in m["error"] and m["attempts"] == 1
    blank = dumps.create()["id"]
    dumps.write_transcript(blank, "")
    dumps.update(blank, status="transcribed")
    dumps.Worker().tick(now=later())
    assert dumps.read_meta(blank)["error"] == "the transcript is empty" and dumps.read_meta(blank)["attempts"] == 1


def test_the_sandbox_worker_never_runs_claude(store, tmp_path, monkeypatch):
    monkeypatch.setitem(store.cfg["dump"], "processor", "claude")

    def boom(*a, **k):
        raise AssertionError("claude -p in the sandbox")
    monkeypatch.setattr(cards, "run_claude", boom)
    did = import_text(tmp_path)
    assert dumps.Worker().tick(now=later()) == [did]
    assert (dumps.read_meta(did)["status"], dumps.read_meta(did)["processor"]) == ("ready", "mock")
    assert "mock processor" in Path(dumps.read_meta(did)["report"]).read_text(encoding="utf-8")
    assert [e["kind"] for e in store.side_effect_log()] == ["claude_p"]        # what would have happened, recorded
    assert dumps.process(import_text(tmp_path, name="b.txt"), processor="claude", log=None)["processor"] == "mock"
    assert len(store.side_effect_log()) == 2


# ---------- the claim rules (the recorder runs in another process) ----------
def test_claim_rules_for_the_recorders_dumps(store, monkeypatch):
    fake = FakeTranscriber("words from the recorder")
    monkeypatch.setattr(dumps, "_make_transcriber", lambda: fake)
    w = dumps.Worker()
    rec = dumps.create(source="ptt")["id"]                     # just created: the recorder is still saving audio.wav
    make_wav(dumps.path(rec) / "audio.wav")
    now = time.time()
    assert w.tick(now=now) == [] and w.tick(now=now + 100) == []
    assert w.tick(now=now + dumps.RECORDED_WAIT_S + 5) == [rec]           # 2 minutes untouched: orphaned, transcribe it here
    m = dumps.read_meta(rec)
    assert m["status"] == "ready" and dumps.read_transcript(rec) == "words from the recorder"
    assert (m["words"], m["durationSec"], m["timings"]["transcribe_s"]) == (4, 1.0, 0.5)
    assert fake.calls == [str(dumps.path(rec) / "audio.wav")]

    busy = dumps.create(source="ptt")["id"]
    make_wav(dumps.path(busy) / "audio.wav")
    dumps.update(busy, status="transcribing")                  # the recorder is transcribing it
    assert w.tick(now=time.time() + 600) == []
    assert w.tick(now=time.time() + dumps.TRANSCRIBING_STALE_S + 5) == [busy]
    assert dumps.read_meta(busy)["status"] == "ready"

    done = dumps.create(source="ptt")["id"]
    dumps.write_transcript(done, "the recorder finished")
    dumps.update(done, status="transcribed")
    assert w.tick() == [done]                                  # transcribed by the recorder: claimed at once
    assert dumps.read_meta(done)["status"] == "ready"


def test_stale_claims_and_other_pcs(store, tmp_path):
    w = dumps.Worker()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone = import_text(tmp_path, name="gone.txt")
    dumps.update(gone, status="thinking", worker={"pid": dead.pid, "host": store.host})    # its server stopped mid-call
    assert dumps._alive(os.getpid()) and not dumps._alive(dead.pid) and not dumps._alive(None)
    assert w.tick() == [gone] and dumps.read_meta(gone)["status"] == "ready"

    live = import_text(tmp_path, name="live.txt")
    dumps.update(live, status="thinking", worker={"pid": os.getpid(), "host": store.host})
    assert w.tick(now=later()) == []
    with pytest.raises(RuntimeError, match="already being processed"):
        dumps.process(live)
    assert w.tick(now=time.time() + dumps.THINKING_STALE_S + 5) == [live]

    other = import_text(tmp_path, name="other.txt")
    dumps.update(other, host="another-pc")                     # a synced Documents folder: that PC's worker owns it
    assert w.tick(now=later()) == []


def test_missing_speech_model_and_empty_audio(store, tmp_path, monkeypatch):
    def missing():
        raise ModuleNotFoundError("No module named 'faster_whisper'")
    monkeypatch.setattr(dumps, "_make_transcriber", missing)
    aid = dumps.import_file(make_wav(tmp_path / "a.wav"))
    assert dumps.Worker().tick(now=later()) == [aid]
    m = dumps.read_meta(aid)
    assert m["status"] == "error" and m["error"] == dumps.SPEECH_MISSING == "speech model unavailable: install faster-whisper"
    monkeypatch.setattr(dumps, "_make_transcriber", lambda: FakeTranscriber(""))
    assert dumps.retry(aid)["status"] == "recorded"
    dumps.Worker().tick(now=later())
    assert dumps.read_meta(aid)["error"] == "no speech found in the recording"
    monkeypatch.setattr(dumps, "_TRANSCRIBER", FakeTranscriber("now it hears you"))
    dumps.retry(aid)
    assert dumps.Worker().tick(now=later()) == [aid] and dumps.read_meta(aid)["status"] == "ready"


def test_the_fallback_transcriber_follows_the_ptt_settings(store, monkeypatch):
    pytest.importorskip("numpy")                                  # briandump.transcribe needs it; constructing loads no model
    monkeypatch.setitem(store.cfg["ptt"], "model", "base")
    monkeypatch.setitem(store.cfg["ptt"], "hotwords", ["Gravel", "Slab"])
    monkeypatch.setitem(store.cfg["ptt"], "language", "de")
    t = dumps._make_transcriber()
    assert (t.model_name, t.language, t.prompt, t.loaded) == ("base", "de", "Gravel, Slab.", False)
    monkeypatch.setitem(store.cfg["ptt"], "language", "auto")
    assert dumps._make_transcriber().language is None
    monkeypatch.delitem(store.cfg["ptt"], "language")
    assert dumps._make_transcriber().language == "en"


def test_process_in_process_like_the_cli(store, tmp_path, monkeypatch):
    monkeypatch.setattr(dumps, "_make_transcriber", lambda: FakeTranscriber("spoken words here"))
    aid = dumps.import_file(make_wav(tmp_path / "a.wav", seconds=3))
    lines = []
    m = dumps.process(aid, processor="mock", log=lines.append)
    assert m["status"] == "ready" and dumps.read_transcript(aid) == "spoken words here" and m["durationSec"] == 3.0
    assert all(ln.startswith(f"dump {aid}: ") for ln in lines) and any("ready in" in ln for ln in lines)
    empty = dumps.create()["id"]
    assert dumps.process(empty, log=None)["error"] == "nothing to process: no transcript and no audio"
    with pytest.raises(KeyError):
        dumps.process("20200101-000000-0000")


# ---------- listing, detail, the panel's writes ----------
def test_listing_newest_first_with_summary_and_counts(store, tmp_path):
    old = dumps.create(created=time.time() - 3600)["id"]
    new = import_text(tmp_path)
    dumps.process(new, log=None)
    rows = dumps.listing()
    assert [r["id"] for r in rows] == [new, old]
    r = rows[0]
    assert (r["status"], r["project"], r["error"]) == ("ready", "Garden shed", None)
    assert r["summaryLine"].startswith("The user is planning a small garden shed") and len(r["summaryLine"]) <= 120
    assert r["counts"] == {"themes": 3, "actions": 3, "todos": 3, "questions": 5} and r["report"]
    assert {"id", "createdAt", "durationSec", "status", "error", "summaryLine", "project", "counts", "report"} <= set(r)
    assert rows[1]["summaryLine"] == "" and rows[1]["counts"]["actions"] == 0
    d = dumps.detail(new)
    assert d["transcript"] == TRANSCRIPT.strip() and d["cards"] == cards.mock_cards() and d["status"] == "ready"
    assert dumps.detail(old)["transcript"] == "" and dumps.detail(old)["cards"] is None


def test_a_dump_the_running_recorder_claimed_is_left_to_it(store):
    did = dumps.create(source="ptt")["id"]
    make_wav(dumps.path(did) / "audio.wav")
    dumps.update(did, status="transcribing", worker={"pid": os.getpid(), "host": store.host})   # the recorder, still running
    meta, now = dumps.read_meta(did), time.time()
    assert dumps._job(meta, now + dumps.TRANSCRIBING_STALE_S + 60) is None and dumps._busy(meta, now + 600)
    assert dumps._job(meta, now + dumps.CLAIMED_STALE_S + 60) == "transcribe"                 # silent for an hour: stuck
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dumps.update(did, worker={"pid": dead.pid, "host": store.host})                            # the recorder exited mid-way
    assert dumps._job(dumps.read_meta(did), time.time()) == "transcribe"


def test_listing_shows_a_broken_meta_as_an_error_but_not_a_dump_being_created(store):
    broken = store.dumps_dir / "20261018-095000-a006"
    broken.mkdir(parents=True)
    (broken / "meta.json").write_text("[1, 2]", encoding="utf-8")
    (store.dumps_dir / "20261018-095100-a007").mkdir()                        # no meta.json yet: a dump still being created
    rows = dumps.listing()
    assert [r["id"] for r in rows] == ["20261018-095000-a006"]
    r = rows[0]
    assert r["status"] == "error" and "not a JSON object" in r["error"] and str(broken) in r["error"]
    assert r["createdAt"].startswith("2026-10-18T09:50:00") and r["counts"]["actions"] == 0


def test_summary_line():
    assert dumps.summary_line({"executiveSummary": "The user wants e.g. a shed by spring.  More text here."}) == \
        "The user wants e.g. a shed by spring."
    long = "word " * 60
    assert len(dumps.summary_line({"executiveSummary": long})) == 120 and dumps.summary_line({}) == ""


def test_mark_sent_persists(store, tmp_path):
    did = import_text(tmp_path)
    dumps.process(did, log=None)
    m = dumps.mark_sent(did, "a2", "Draft a cutting list")
    assert set(m["sent"]) == {"a2"} and dt.datetime.fromisoformat(m["sent"]["a2"])
    dumps.mark_sent(did, "a1", "x")
    assert set(dumps.read_meta(did)["sent"]) == {"a1", "a2"}
    assert dumps.mark_sent(did, "../evil", "x") is False and dumps.mark_sent("20200101-000000-0000", "a1", "x") is False


def test_add_to_agenda_from_a_dump(store, tmp_path):
    did = import_text(tmp_path)
    assert dumps.add_to_agenda(did, "Buy a long tape measure") == ["Buy a long tape measure"]
    note = dumps.note_for(dumps.read_meta(did))
    assert f"- [ ] Buy a long tape measure {note}\n" in store.agenda_file.read_text(encoding="utf-8")
    assert dumps.add_to_agenda(did, "buy a long tape measure") == []
    assert dumps.read_meta(did)["agenda_added"] == ["Buy a long tape measure", "buy a long tape measure"]


# ---------- the report ----------
def test_report_markdown_layout():
    meta = {"id": "20261018-104200-ab12", "createdAt": "2026-10-18T10:42:00+02:00", "durationSec": 361, "processor": "claude", "calls": 1}
    deck = cards.mock_cards()
    deck["actions"][0]["why"] = "pipes | in text"
    md = dumps.report_markdown(meta, deck, "the words")
    lines = md.splitlines()
    assert lines[0] == "# Brain dump · 2026-10-18 10:42 (6 min)" and lines[2] == "**Project:** Garden shed"
    assert [ln for ln in lines if ln.startswith("## ")] == ["## Summary", "## Themes", "## Proposed actions", "## To-dos",
                                                           "## Questions for next time", "## Transcript"]
    assert "### Measuring the space" in lines and "| id | Action | Deliverable | Why | Effort |" in lines and "pipes \\| in text" in md
    assert "- [ ] Measure the shed space with the long tape and peg the corners" in lines
    assert "- **Pricing:** What did the lumber yard quote for the frame, and do they deliver?" in lines
    assert lines[-1] == "Dump `20261018-104200-ab12`. Made by BrianDump with one `claude -p` call under your own Claude Code login."
    assert "\u2014" not in md
    small = {"executiveSummary": "s", "talkingPoints": [], "actions": [], "nextPrompts": []}
    two = dumps.report_markdown({**meta, "calls": 2, "durationSec": 40}, small, "")
    assert two.splitlines()[0].endswith("(40 s)") and "2 `claude -p` calls" in two and "## Transcript" not in two
    assert "## Themes" not in two and "mock processor" in dumps.report_markdown({**meta, "processor": "mock"}, small, "")


def test_write_report_replaces_the_old_slug(store, tmp_path):
    did = import_text(tmp_path)
    first = Path(dumps.process(did, log=None)["report"])
    deck = dumps.read_cards(did)
    deck["project"] = "Tool shed (v2)"
    dumps._write_json(dumps.path(did) / "cards.json", deck)
    second = dumps.write_report(did)
    assert second.name == f"{did}-tool-shed-v2.md" and second.is_file() and not first.exists()


# ---------- the audio sweep ----------
def test_sweep_deletes_only_old_transcribed_audio(store):
    def dump(age_days, status, transcript):
        did = dumps.create(created=time.time() - age_days * 86400)["id"]
        make_wav(dumps.path(did) / "audio.wav")
        if transcript:
            dumps.write_transcript(did, transcript)
        dumps.update(did, status=status)
        return did
    old, fresh = dump(10, "ready", "words"), dump(1, "ready", "words")
    untranscribed, failed = dump(10, "recorded", None), dump(10, "error", None)
    assert dumps.sweep_audio(7) == 1
    assert not (dumps.path(old) / "audio.wav").exists() and dumps.read_meta(old)["audioDeleted"]
    assert dumps.read_transcript(old) == "words"
    for keep in (fresh, untranscribed, failed):
        assert (dumps.path(keep) / "audio.wav").exists()
    for off in (None, True, "x", -1):
        assert dumps.sweep_audio(off) == 0


def test_worker_sweeps_at_most_hourly(store, monkeypatch):
    calls = []
    monkeypatch.setattr(dumps, "sweep_audio", lambda days, now=None: calls.append(days) or 0)
    w = dumps.Worker()
    w.tick(now=50_000.0)
    assert calls == []                                                   # delete_audio_after_days is null: keep everything
    monkeypatch.setitem(store.cfg["dump"], "delete_audio_after_days", 7)
    for t in (50_000.0, 50_060.0, 53_601.0):
        w.tick(now=t)
    assert calls == [7, 7]


# ---------- concurrency ----------
def test_meta_survives_concurrent_readers(store):
    did = dumps.create()["id"]
    meta_file = dumps.path(did) / "meta.json"
    stop, errors = threading.Event(), []

    def reader():                                                          # the panel's listing and detail
        while not stop.is_set():
            try:
                if dumps.read_meta(did) is None or not dumps.listing():
                    errors.append("meta.json unreadable")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            time.sleep(0.001)

    def outsider():                                                        # a plain reader (another process) holding it open
        while not stop.is_set():
            try:
                with open(meta_file, encoding="utf-8") as f:
                    json.loads(f.read())
            except OSError:
                pass
            except ValueError:
                errors.append("half-written meta.json")
            time.sleep(0.001)
    threads = [threading.Thread(target=f, daemon=True) for f in (reader, reader, outsider)]
    for t in threads:
        t.start()
    try:
        for n in range(150):
            dumps.update(did, words=n, timings={f"t{n % 5}": n})
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert errors == []
    m = dumps.read_meta(did)
    assert m["words"] == 149 and m["timings"] == {"t0": 145, "t1": 146, "t2": 147, "t3": 148, "t4": 149}
    assert not [p.name for p in dumps.path(did).iterdir() if p.name.endswith(".tmp")]


def test_worker_thread_runs_and_stops(store):
    did = dumps.create()["id"]
    dumps.write_transcript(did, "a few words")
    dumps.update(did, status="transcribed")
    w = dumps.Worker(interval=0.05).start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and dumps.read_meta(did)["status"] != "ready":
            time.sleep(0.05)
    finally:
        w.stop()
    assert dumps.read_meta(did)["status"] == "ready" and not w._thread.is_alive()
