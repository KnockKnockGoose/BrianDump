"""The brain-dump store, the worker that turns transcripts into cards, the report writer and file import.

One folder per dump under <data_dir>/dumps/<id>/ (id = local time YYYYMMDD-HHMMSS + 4 hex):

    audio.wav        the recording (ptt.py writes it; an import may bring audio.m4a, .mp3 ...)
    meta.json        status, timings, cost, what went onto the agenda (docs/architecture.md has the shape)
    transcript.txt   Whisper's text
    cards.json       the card deck (cards.py)

plus a readable report at <data_dir>/reports/<id>-<slug>.md. Status: recorded -> transcribing -> transcribed -> thinking
-> ready, or error (message in `error`).

Two processes write here. ptt.py (the recorder) creates a dump as `recorded`, writes audio.wav, flips it to `transcribing`
and later to `transcribed`. The panel server runs a Worker that claims work by writing the next status before doing it,
one dump at a time, oldest first:

    transcribed   at once (an import after a 5 s grace, so a CLI `dump --now` that just imported it gets it first)
    recorded      an import or a re-queued dump after the grace; a recording from the talk key only once it has been
                  untouched for 2 minutes (the recorder died between saving the audio and flipping the status)
    transcribing  after 15 minutes untouched (the recorder died mid-way), or at once if this PC's claimer has exited;
                  the recorder claims its own, and while it runs it gets an hour
    thinking      after 30 minutes untouched, or at once if this PC's claimer has exited (the server stopped mid-call)

Dumps recorded on another PC (a synced Documents folder) are left to that PC. In the sandbox (BRIANDUMP_SANDBOX=1) the
cards always come from the mock processor: cards.py records the `claude -p` call it would have made and spawns nothing.

The thinking step is one `claude -p` call (cards.py) -> cards.json -> the report -> the merge: to-dos go to agenda.md and
questions to questions.md, each with a "(from brain dump 2026-10-18 10:42)" note, deduplicated against the open items
(agenda.py). A failure is retried once; then the dump stays `error` until retry() (the panel's Retry) re-queues it. Only
the cards are essential: a report or agenda write that fails becomes a `warning` on a ready dump.

meta.json is replaced atomically (temp file + os.replace, retried while Windows reports the file in use) and read in one
short call, so the recorder, the worker and the panel never see half a file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
import wave
from pathlib import Path

from briandump import agenda
from briandump.config import CONF

ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")
ACTION_ID_RE = re.compile(r"^[\w-]{1,32}\Z", re.ASCII)
TEXT_EXT = (".txt", ".md")
AUDIO_EXT = (".wav", ".m4a", ".mp3", ".ogg", ".webm", ".flac")
MAX_ATTEMPTS = 2                    # the first try plus one automatic retry
RETRY_PAUSE_S = 3.0
IMPORT_GRACE_S = 5.0
RECORDED_WAIT_S = 120.0
TRANSCRIBING_STALE_S = 15 * 60.0
CLAIMED_STALE_S = 60 * 60.0         # `transcribing` whose claimer still runs: its own, until it has been silent this long
THINKING_STALE_S = 30 * 60.0
SWEEP_EVERY_S = 3600.0
SPEECH_MISSING = "speech model unavailable: install faster-whisper"
_LOCK = threading.RLock()           # read-modify-write of meta.json inside this process
_TRANSCRIBER = None                 # one faster-whisper model per process, built on first need
_T_LOCK = threading.Lock()


class _Permanent(Exception):
    """A failure a second attempt cannot fix (no transcript, no claude on PATH)."""


# ---------- small helpers ----------
def now_iso(t=None):
    """Local time with its UTC offset, to the second: 2026-10-18T10:42:00+02:00."""
    return dt.datetime.fromtimestamp(time.time() if t is None else t).astimezone().isoformat(timespec="seconds")


def _when(value):
    """An aware datetime from an epoch, a datetime or an ISO string (naive = local); None if unreadable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, dt.datetime):
            d = value
        elif isinstance(value, (int, float)):
            d = dt.datetime.fromtimestamp(float(value))
        else:
            d = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.astimezone()
    except (ValueError, OSError, OverflowError):
        return None


def _num(v, nd=1):
    try:
        return None if v is None or isinstance(v, bool) else round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _errmsg(e):
    s = " ".join(str(e).split()) or type(e).__name__
    return s if len(s) <= 500 else s[:499] + "…"


def _log(msg):
    """The worker's log: <state_dir>/logs/dumps.log plus stdout (the server runs under pythonw, without a console)."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        CONF.logs.mkdir(parents=True, exist_ok=True)
        with open(CONF.logs / "dumps.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def _alive(pid):
    """Is process `pid` running? (os.kill(pid, 0) would terminate it on Windows, so the kernel is asked there.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)        # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5                      # access denied: it exists, it is someone else's
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102              # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


# ---------- the store ----------
def valid_id(did):
    return isinstance(did, str) and bool(ID_RE.match(did))


def new_id(now=None):
    """Local time YYYYMMDD-HHMMSS plus 4 random hex digits (two dumps in one second still differ)."""
    d = _when(now) or dt.datetime.now().astimezone()
    return f"{d:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def path(did):
    """The dump's folder. The id is checked first: nothing from outside ever becomes a path unchecked."""
    if not valid_id(did):
        raise ValueError(f"bad dump id: {did!r}")
    return CONF.dumps_dir / did


def _read_json(p):
    try:
        return json.loads(agenda.read_text_retry(p))
    except (OSError, ValueError):
        return None


def _write_json(p, obj):
    agenda.write_text_atomic(p, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _reserve(when):
    """Create a new, empty dump folder named after `when`; -> its id."""
    CONF.dumps_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        did = new_id(when)
        try:
            path(did).mkdir()
            return did
        except FileExistsError:
            continue
    raise RuntimeError("no free dump id")


def _new_meta(did, when, source, label, duration_s, status):
    return {"id": did, "createdAt": when.isoformat(timespec="seconds"), "updatedAt": now_iso(), "durationSec": _num(duration_s),
            "status": status, "error": None, "attempts": 0, "timings": {}, "words": None, "source": str(source or "ptt"),
            "label": label, "host": CONF.host, "processor": None, "model": None, "cost_usd": None, "report": None, "sent": {},
            "agenda_added": [], "questions_added": []}


def create(source="ptt", label=None, created=None, duration_s=None):
    """A new dump folder with meta.json (status `recorded`). `created` = when the recording started (epoch or datetime)."""
    when = _when(created) or dt.datetime.now().astimezone()
    did = _reserve(when)
    meta = _new_meta(did, when, source, label, duration_s, "recorded")
    _write_json(path(did) / "meta.json", meta)
    return meta


def read_meta(did):
    if not valid_id(did):
        return None
    m = _read_json(path(did) / "meta.json")
    return m if isinstance(m, dict) else None


def update(did, **patch):
    """Merge `patch` into meta.json (a `timings` dict is merged key by key), bump updatedAt, write atomically."""
    p = path(did) / "meta.json"
    with _LOCK:
        meta = _read_json(p)
        if not isinstance(meta, dict):
            raise KeyError(f"no such dump: {did}")
        timings = patch.pop("timings", None)
        meta.update(patch)
        if isinstance(timings, dict):
            old = meta.get("timings") if isinstance(meta.get("timings"), dict) else {}
            meta["timings"] = {**old, **timings}
        meta["updatedAt"] = now_iso()
        _write_json(p, meta)
        return meta


def write_transcript(did, text):
    agenda.write_text_atomic(path(did) / "transcript.txt", str(text or "").strip() + "\n")


def read_transcript(did):
    """The transcript text (stripped), or None when there is none yet."""
    if not valid_id(did):
        return None
    try:
        return agenda.read_text_retry(path(did) / "transcript.txt", errors="replace").strip()
    except OSError:
        return None


def read_cards(did):
    if not valid_id(did):
        return None
    c = _read_json(path(did) / "cards.json")
    return c if isinstance(c, dict) else None


def _audio(did):
    """The dump's recording: audio.wav, else audio.<any known audio extension>; None without one."""
    d = path(did)
    for ext in AUDIO_EXT:
        if (d / f"audio{ext}").is_file():
            return d / f"audio{ext}"
    return None


def _wav_seconds(p):
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return None


def import_file(src_path, label=None):
    """A .txt/.md transcript (-> `transcribed`) or a recording (.wav .m4a .mp3 .ogg .webm .flac, copied to audio.<ext> ->
    `recorded`) as a new dump; -> its id. meta.json is written last, so the worker never sees a half-imported dump."""
    src = Path(src_path)
    ext = src.suffix.lower()
    if ext not in TEXT_EXT + AUDIO_EXT:
        raise ValueError(f"cannot import {src.name}: use a .txt or .md transcript, or audio ({' '.join(AUDIO_EXT)})")
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {src}")
    label = label or src.name
    when = dt.datetime.now().astimezone()
    if ext in TEXT_EXT:
        text = src.read_text(encoding="utf-8-sig", errors="replace").strip()
        if not text:
            raise ValueError(f"{src.name} is empty")
        did = _reserve(when)
        write_transcript(did, text)
        meta = _new_meta(did, when, "import", label, None, "transcribed")
        meta["words"] = len(text.split())
    else:
        did = _reserve(when)
        shutil.copyfile(src, path(did) / f"audio{ext}")
        meta = _new_meta(did, when, "import", label, _wav_seconds(src) if ext == ".wav" else None, "recorded")
    _write_json(path(did) / "meta.json", meta)
    return did


# ---------- listing and the panel's small writes ----------
def summary_line(cards, n=120):
    """The first sentence of the executive summary (at least 20 characters, so "e.g." does not end it), at most n."""
    s = " ".join(str((cards or {}).get("executiveSummary") or "").split())
    m = re.match(r"(.{20,}?[.!?])(?=\s|$)", s)
    s = m.group(1) if m else s
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _row(did, meta, deck):
    c = deck if isinstance(deck, dict) else {}
    return {"id": did, "createdAt": meta.get("createdAt"), "updatedAt": meta.get("updatedAt"), "durationSec": meta.get("durationSec"),
            "status": meta.get("status"), "error": meta.get("error"), "warning": meta.get("warning"), "source": meta.get("source"),
            "label": meta.get("label"), "words": meta.get("words"), "summaryLine": summary_line(c) if c else "",
            "project": c.get("project") or None,
            "counts": {"themes": len(c.get("talkingPoints") or []), "actions": len(c.get("actions") or []),
                       "todos": len(c.get("todos") or []), "questions": len(c.get("nextPrompts") or [])},
            "report": meta.get("report"), "host": meta.get("host")}


def _ids():
    try:
        return sorted(d.name for d in CONF.dumps_dir.iterdir() if d.is_dir() and valid_id(d.name))
    except OSError:
        return []


def _broken_meta(did):
    """Why this dump's meta.json is there but unusable (not JSON, not an object), or None: a missing one is a dump still being
    created, and a locked one is read again on the next poll."""
    try:
        meta = json.loads(agenda.read_text_retry(path(did) / "meta.json"))
    except ValueError as e:
        return f"meta.json cannot be read ({e})"
    except OSError:
        return None
    return None if isinstance(meta, dict) else "meta.json is not a JSON object"


def listing():
    """One row per dump, newest first: id, createdAt, durationSec, status, error, summaryLine, project, counts, report ...
    A dump whose meta.json is broken is listed as an error, so its recording and transcript can still be found."""
    rows = []
    for did in reversed(_ids()):
        meta = read_meta(did)
        if meta is not None:
            rows.append(_row(did, meta, read_cards(did)))
        elif why := _broken_meta(did):
            try:
                created = dt.datetime.strptime(did[:15], "%Y%m%d-%H%M%S").astimezone().isoformat(timespec="seconds")
            except ValueError:
                created = None
            rows.append(_row(did, {"id": did, "status": "error", "createdAt": created, "error": f"{why}; its files are in {path(did)}"},
                             None))
    return rows


def detail(did):
    """meta + `transcript` ("" until there is one) + `cards` (None until there are some); None for an unknown id."""
    meta = read_meta(did)
    if meta is None:
        return None
    return {**meta, "transcript": read_transcript(did) or "", "cards": read_cards(did)}


def retry(did):
    """Re-queue an `error` dump: back to `transcribed` when it has a transcript, else `recorded` when it has audio.
    -> the new meta, or False when there is nothing to retry."""
    with _LOCK:
        meta = read_meta(did)
        if meta is None or meta.get("status") != "error":
            return False
        if read_transcript(did):
            status = "transcribed"
        elif _audio(did) is not None:
            status = "recorded"
        else:
            return False
        return update(did, status=status, error=None, requeued=now_iso())


def mark_sent(did, action_id, text=None):
    """Remember that an action's text went to the session (meta.sent[action id] = time). -> meta, or False."""
    if not ACTION_ID_RE.match(str(action_id or "")):
        return False
    with _LOCK:
        meta = read_meta(did)
        if meta is None:
            return False
        sent = dict(meta["sent"]) if isinstance(meta.get("sent"), dict) else {}
        sent[str(action_id)] = now_iso()
        return update(did, sent=sent)


def _local_stamp(meta):
    """2026-10-18 10:42 from createdAt (in the offset it was recorded in), else from the id; None if neither reads."""
    d = _when((meta or {}).get("createdAt"))
    if d is not None:
        return f"{d:%Y-%m-%d %H:%M}"
    m = re.match(r"([0-9]{4})([0-9]{2})([0-9]{2})-([0-9]{2})([0-9]{2})", str((meta or {}).get("id") or ""))
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}" if m else None


def note_for(meta):
    """The note that marks agenda / question lines from this dump: (from brain dump 2026-10-18 10:42)."""
    stamp = _local_stamp(meta)
    return f"(from brain dump {stamp})" if stamp else "(from brain dump)"


def add_to_agenda(did, text):
    """The panel's "Add to agenda" on a to-do card: agenda.add with this dump's note; the text is remembered in
    meta.agenda_added (also when it was already on the agenda) so the card shows it as done. -> the added texts."""
    meta = read_meta(did)
    added = agenda.add(CONF.agenda_file, [text], heading=CONF.get("agenda.heading"), suffix=note_for(meta) if meta else None)
    if meta is not None:
        with _LOCK:
            cur = read_meta(did) or meta
            have = [x for x in (cur.get("agenda_added") or []) if isinstance(x, str)]
            if text not in have:
                update(did, agenda_added=have + [text])
    return added


# ---------- the report ----------
def _cell(s):
    return " ".join(str(s or "").split()).replace("|", "\\|")


def _line(s):
    return " ".join(str(s or "").split())


def _duration(sec):
    sec = _num(sec, 0)
    if not sec:
        return ""
    return f"{int(sec)} s" if sec < 60 else f"{round(sec / 60)} min"


def report_markdown(meta, cards, transcript):
    """The dump as one markdown page: summary, themes, actions table, to-dos, questions, transcript."""
    meta, c = meta or {}, cards or {}
    dur = _duration(meta.get("durationSec"))
    out = [f"# Brain dump · {_local_stamp(meta) or meta.get('id', '')}" + (f" ({dur})" if dur else ""), ""]
    if c.get("project"):
        out += [f"**Project:** {_line(c['project'])}", ""]
    out += ["## Summary", "", str(c.get("executiveSummary") or "").strip(), ""]
    themes = [t for t in c.get("talkingPoints") or [] if isinstance(t, dict)]
    if themes:
        out += ["## Themes", ""]
        for t in themes:
            out += [f"### {_line(t.get('theme')) or 'Theme'}", ""] + [f"- {_line(b)}" for b in t.get("bullets") or []] + [""]
    actions = [a for a in c.get("actions") or [] if isinstance(a, dict)]
    if actions:
        out += ["## Proposed actions", "", "| id | Action | Deliverable | Why | Effort |", "|---|---|---|---|---|"]
        out += [f"| {_cell(a.get('id'))} | {_cell(a.get('title'))} | {_cell(a.get('deliverable'))} | {_cell(a.get('why'))} | "
                f"{_cell(a.get('effort'))} |" for a in actions] + [""]
    todos = [_line(t) for t in c.get("todos") or [] if _line(t)]
    if todos:
        out += ["## To-dos", ""] + [f"- [ ] {t}" for t in todos] + [""]
    prompts = [p for p in c.get("nextPrompts") or [] if isinstance(p, dict) and _line(p.get("question"))]
    if prompts:
        out += ["## Questions for next time", ""]
        out += [(f"- **{_line(p['topic'])}:** " if _line(p.get("topic")) else "- ") + _line(p["question"]) for p in prompts] + [""]
    if str(transcript or "").strip():
        out += ["## Transcript", "", str(transcript).strip(), ""]
    calls = meta.get("calls") or 1
    if meta.get("processor") == "mock":
        made = "the mock processor (sample cards, no model call)"
    else:
        made = ("one `claude -p` call" if calls == 1 else f"{calls} `claude -p` calls") + " under your own Claude Code login"
    out += ["---", "", f"Dump `{meta.get('id', '')}`. Made by BrianDump with {made}."]
    return "\n".join(out) + "\n"


def _slug(deck):
    themes = (deck or {}).get("talkingPoints") or [{}]
    s = (deck or {}).get("project") or (themes[0].get("theme") if isinstance(themes[0], dict) else "") or ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40].strip("-") or "brain-dump"


def write_report(did):
    """<reports_dir>/<id>-<slug>.md (slug from the project, else the first theme); an older report of the same dump
    under another slug is removed. -> the path."""
    meta = read_meta(did)
    deck = read_cards(did)
    if meta is None or deck is None:
        raise KeyError(f"dump {did} has no cards yet")
    out = CONF.reports_dir / f"{did}-{_slug(deck)}.md"
    agenda.write_text_atomic(out, report_markdown(meta, deck, read_transcript(did)))
    for old in CONF.reports_dir.glob(f"{did}-*.md"):
        if old.name != out.name:
            try:
                old.unlink()
            except OSError:
                pass
    return out


# ---------- transcription (the server's fallback; normally ptt.py transcribes with its own loaded model) ----------
def _make_transcriber():
    """The same model, hotwords and language the recorder uses (ptt.model, ptt.hotwords, ptt.language)."""
    # numpy + faster-whisper: imported only when this process has to transcribe
    from briandump.transcribe import Transcriber
    hot = CONF.get("ptt.hotwords") or ()
    return Transcriber(str(CONF.get("ptt.model", "small") or "small"), hotwords=hot if isinstance(hot, (list, tuple)) else [str(hot)],
                       language=CONF.get("ptt.language", "en"))


def _transcriber():
    global _TRANSCRIBER
    with _T_LOCK:
        if _TRANSCRIBER is None:
            _TRANSCRIBER = _make_transcriber()
        return _TRANSCRIBER


def _claimer():
    return {"pid": os.getpid(), "host": CONF.host}


def _transcribe(did, log):
    """Transcribe a claimed dump (status `transcribing`) -> `transcribed`, or `error`."""
    say = _say(did, log)
    audio = _audio(did)
    if audio is None:
        return update(did, status="error", error="no audio to transcribe", worker=None)
    try:
        model = _transcriber()
        say(f"transcribing {audio.name}")
        r = model.transcribe(str(audio))
    except ImportError:
        return update(did, status="error", error=SPEECH_MISSING, worker=None)
    except Exception as e:  # noqa: BLE001 - a broken file or model is this dump's error, not the worker's
        return update(did, status="error", error=f"transcription failed: {_errmsg(e)}", worker=None)
    text = str(r.get("text") or "").strip()
    if not text:
        return update(did, status="error", error="no speech found in the recording", worker=None)
    write_transcript(did, text)
    patch = {"status": "transcribed", "words": len(text.split()), "timings": {"transcribe_s": _num(r.get("took_s"))}, "worker": None}
    if not (read_meta(did) or {}).get("durationSec") and r.get("seconds"):
        patch["durationSec"] = _num(r.get("seconds"))
    say(f"transcribed: {patch['words']} words")
    return update(did, **patch)


def transcribe(did, log=print):
    """Transcribe a dump in this process (lazily loading one faster-whisper model). -> meta."""
    if read_meta(did) is None:
        raise KeyError(f"no such dump: {did}")
    update(did, status="transcribing", error=None, worker=_claimer())
    return _transcribe(did, log)


# ---------- the thinking step ----------
def _say(did, log):
    return lambda msg: (log or print)(f"dump {did}: {msg}")


def _processor(processor=None):
    """The processor asked for (else dump.processor). In the sandbox cards.process() swaps "claude" for the mock deck and
    records the call it would have made (side_effects.jsonl), and cards.run_claude() refuses to spawn anything."""
    return str(processor or CONF.get("dump.processor", "claude") or "claude")


def _prompt_file():
    v = CONF.get("dump.prompt_file")
    if not v:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(str(v))))
    return p if p.is_absolute() else CONF.state_dir / p


def _merge(did, deck, meta, warnings):
    """To-dos -> agenda.md, questions -> questions.md (deduplicated, with this dump's note); remember what was added."""
    note = note_for(meta)
    todos = [t for t in deck.get("todos") or [] if isinstance(t, str) and t.strip()]
    questions = [p["question"] for p in deck.get("nextPrompts") or [] if isinstance(p, dict) and str(p.get("question") or "").strip()]
    got = {"agenda_added": [], "questions_added": []}
    for key, file, texts, heading in (("agenda_added", CONF.agenda_file, todos, CONF.get("agenda.heading")),
                                      ("questions_added", CONF.questions_file, questions, None)):
        if not texts:
            continue
        try:
            added = set(agenda.add(file, texts, heading=heading, suffix=note))
            got[key] = [t for t in texts if agenda.clean(t) in added]    # as the cards spell them (the panel compares)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{Path(file).name} not updated: {_errmsg(e)}")
    cur = read_meta(did) or meta
    return {k: list(dict.fromkeys([x for x in cur.get(k) or [] if isinstance(x, str)] + v)) for k, v in got.items()}, got


def _think_once(did, meta, processor, say):
    # lazy: the recorder (ptt.py) imports this module and never needs cards
    from briandump import cards
    transcript = read_transcript(did)
    if not transcript:
        raise _Permanent("the transcript is empty")
    stats = {}
    t0 = time.time()
    try:
        deck = cards.process(transcript, recorded_at=meta.get("createdAt"), duration_s=meta.get("durationSec"), processor=processor,
                             log=say, user_name=CONF.get("dump.user_name"), model=CONF.get("dump.model"),
                             effort=CONF.get("dump.effort"), timeout_s=_num(CONF.get("dump.timeout_s", 360), 0) or 360,
                             claude_cmd=str(CONF.get("dump.claude_cmd", "claude") or "claude"), prompt_file=_prompt_file(),
                             stats=stats)
    except FileNotFoundError as e:
        raise _Permanent(_errmsg(e)) from e
    think_s = round(time.time() - t0, 1)
    _write_json(path(did) / "cards.json", deck)
    patch = {"processor": stats.get("processor") or processor, "model": stats.get("model") or CONF.get("dump.model"),
             "cost_usd": stats.get("cost_usd"), "calls": stats.get("calls"), "timings": {"think_s": think_s}}
    if not meta.get("words"):
        patch["words"] = len(transcript.split())
    meta = update(did, **patch)
    warnings = []
    try:
        meta = update(did, report=str(write_report(did)))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"report not written: {_errmsg(e)}")
    merged, new = _merge(did, deck, meta, warnings)
    meta = update(did, status="ready", error=None, warning="; ".join(warnings) or None, worker=None, **merged)
    cost = f", ${meta['cost_usd']:.3f}" if isinstance(meta.get("cost_usd"), (int, float)) else ""
    say(f"ready in {think_s} s{cost}: {len(deck.get('actions') or [])} actions, {len(new['agenda_added'])} new to-dos on the agenda, "
        f"{len(new['questions_added'])} new questions" + (f" (warning: {meta['warning']})" if meta.get("warning") else ""))
    return meta


def _think(did, processor, log):
    """Cards for a claimed dump (status `thinking`), with one automatic retry. -> meta (`ready` or `error`)."""
    say = _say(did, log)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        meta = read_meta(did)
        if meta is None:
            raise KeyError(f"no such dump: {did}")
        meta = update(did, status="thinking", error=None, attempts=int(meta.get("attempts") or 0) + 1, worker=_claimer())
        try:
            return _think_once(did, meta, processor, say)
        except Exception as e:  # noqa: BLE001 - every failure ends as this dump's `error`, never the worker's
            msg = _errmsg(e)
            say(f"attempt {attempt} failed: {msg}")
            if attempt >= MAX_ATTEMPTS or isinstance(e, _Permanent):
                return update(did, status="error", error=msg, worker=None)
            update(did, error=msg)
            time.sleep(RETRY_PAUSE_S)


def _busy(meta, now=None):
    """Someone is working on this dump right now (a fresh `transcribing` / `thinking` whose claimer still runs)."""
    st = meta.get("status")
    if st == "transcribing":
        return not _transcribing_stale(meta, now)
    if st != "thinking" or _claimer_gone(meta):
        return False
    return _age(meta, now) < THINKING_STALE_S


def process(did, processor=None, log=print):
    """The thinking step for one dump, in this process (the CLI's `dump --now`): transcribe first if there is only audio,
    then cards -> cards.json -> report -> agenda / questions -> `ready`. Errors end as status `error`. -> meta."""
    meta = read_meta(did)
    if meta is None:
        raise KeyError(f"no such dump: {did}")
    if _busy(meta):
        raise RuntimeError(f"dump {did} is already being processed ({meta.get('status')})")
    if not read_transcript(did):
        if _audio(did) is None:
            return update(did, status="error", error="nothing to process: no transcript and no audio")
        meta = transcribe(did, log=log)
        if meta.get("status") != "transcribed":
            return meta
    update(did, status="thinking", error=None, worker=_claimer())
    return _think(did, _processor(processor), log)


# ---------- the worker ----------
def _age(meta, now=None):
    """Seconds since meta.json was last written (updatedAt; the file's mtime if that is unreadable)."""
    now = time.time() if now is None else now
    d = _when(meta.get("updatedAt"))
    if d is not None:
        return now - d.timestamp()
    try:
        return now - path(meta.get("id")).joinpath("meta.json").stat().st_mtime
    except (OSError, ValueError):
        return 0.0


def _claimer_gone(meta):
    w = meta.get("worker")
    return isinstance(w, dict) and w.get("host") == CONF.host and not _alive(w.get("pid"))


def _transcribing_stale(meta, now=None):
    """A `transcribing` dump nobody is on any more: the process that claimed it on this PC has exited, or it has been silent
    for 15 minutes; an hour when that process still runs (the recorder transcribes a long dump on a slow PC that long, and a
    second model on the same audio would only slow it down)."""
    if _claimer_gone(meta):
        return True
    w = meta.get("worker")
    claimed = isinstance(w, dict) and w.get("host") == CONF.host       # (and alive: not gone)
    return _age(meta, now) > (CLAIMED_STALE_S if claimed else TRANSCRIBING_STALE_S)


def _job(meta, now):
    """What the worker should do with this dump now: "think", "transcribe" or None."""
    host = meta.get("host")
    if host and host != CONF.host:
        return None                                  # recorded on another PC (synced folder): that PC's worker owns it
    st, age = meta.get("status"), _age(meta, now)
    quick = meta.get("source") == "import" or bool(meta.get("requeued"))
    if st == "transcribed":
        return "think" if not quick or age >= IMPORT_GRACE_S else None
    if st == "recorded":
        return "transcribe" if (quick and age >= IMPORT_GRACE_S) or age > RECORDED_WAIT_S else None
    if st == "transcribing":
        return "transcribe" if _transcribing_stale(meta, now) else None
    if st == "thinking" and (age > THINKING_STALE_S or _claimer_gone(meta)):
        return "think" if read_transcript(meta.get("id")) else "transcribe"
    return None


def _claim(did, seen, status):
    """Take a dump: write `status` only if nobody wrote meta.json since `seen` was read. True = it is ours."""
    with _LOCK:
        cur = read_meta(did)
        if cur is None or (cur.get("status"), cur.get("updatedAt")) != (seen.get("status"), seen.get("updatedAt")):
            return False
        update(did, status=status, error=None, worker=_claimer())
        return True


class Worker:
    """The panel server's dump worker: a daemon thread that runs tick() every `interval` seconds. tick() claims the
    oldest dump that needs work (see the module docstring), does it, and returns the ids it touched (at most one)."""

    def __init__(self, interval=2.0, processor=None, log=None):
        self.interval, self.processor, self.log = float(interval), processor, log or _log
        self._stop, self._thread, self._last_sweep = threading.Event(), None, 0.0

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="BrianDump dumps", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                self.log(f"dump worker: {_errmsg(e)}")
            self._stop.wait(self.interval)

    def _sweep(self, now):
        days = CONF.get("dump.delete_audio_after_days")
        if isinstance(days, bool) or not isinstance(days, (int, float)) or now - self._last_sweep < SWEEP_EVERY_S:
            return
        self._last_sweep = now
        try:
            n = sweep_audio(days, now=now)
            if n:
                self.log(f"deleted {n} recording(s) older than {days:g} days (dump.delete_audio_after_days)")
        except Exception as e:  # noqa: BLE001
            self.log(f"audio sweep: {_errmsg(e)}")

    def tick(self, now=None):
        now = time.time() if now is None else float(now)
        self._sweep(now)
        processor = _processor(self.processor)               # the sandbox still gets mock cards (cards.process)
        for did in _ids():
            meta = read_meta(did)
            job = _job(meta, now) if meta else None
            if job is None or not _claim(did, meta, "transcribing" if job == "transcribe" else "thinking"):
                continue
            try:
                if job == "transcribe":
                    meta = _transcribe(did, self.log)
                    if meta.get("status") == "transcribed" and _claim(did, meta, "thinking"):
                        _think(did, processor, self.log)
                else:
                    _think(did, processor, self.log)
            except Exception as e:  # noqa: BLE001
                self.log(f"dump {did}: {_errmsg(e)}")
                try:
                    update(did, status="error", error=_errmsg(e), worker=None)
                except Exception:  # noqa: BLE001 - the folder may be gone
                    pass
            return [did]
        return []


def sweep_audio(days, now=None):
    """Delete the recordings of dumps older than `days` (dump.delete_audio_after_days) that already have a transcript;
    transcript, cards and report stay. -> how many files were deleted."""
    if isinstance(days, bool):
        return 0
    try:
        days = float(days)
    except (TypeError, ValueError):
        return 0
    if days < 0:
        return 0
    now = time.time() if now is None else now
    deleted = 0
    for did in _ids():
        meta = read_meta(did)
        if meta is None or meta.get("status") in ("recorded", "transcribing") or not read_transcript(did):
            continue
        created = _when(meta.get("createdAt"))
        if created is None or now - created.timestamp() < days * 86400:
            continue
        gone = 0
        for ext in AUDIO_EXT:
            try:
                (path(did) / f"audio{ext}").unlink()
                gone += 1
            except OSError:                              # not there, or in use (next sweep)
                pass
        if gone:
            deleted += gone
            update(did, audioDeleted=now_iso())
    return deleted
