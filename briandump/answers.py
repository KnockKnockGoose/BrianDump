"""🎤 Answer: arm one open question for your next hold-to-talk clip.

The 🎤 Answer button on a question card arms that question (POST /api/answer/arm). The next F9 clip you START within ARM_S
seconds is typed as `[answer to: "<question>"] <your words>`, into the session that asked the question (Main for a question
from a brain dump), and the question is ticked once the line is delivered. Esc on the panel, a second arm (the last one
wins), the timeout, or that one clip disarm it.

Keystroke rule: the words still come only from your own F9 clip. Only a clip the PTT log vouches for can take the arm;
arming adds the quoted question in front of your words and the tick afterwards, nothing else. The quote is the question's
own text, which the server looks up from the card's id: a question the private filter masks on the panel still works.

State: <state_dir>/state/answer_arm.json, written by the panel server (arm, disarm) and taken by the voice router (panel
mode) or watch_inbox.py (monitor mode):
    {"id", "question", "tick": "md"|"manual"|null, "session": <id>|null, "dump": <id>|null, "armed_at", "expires_at", "nonce"}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid
from pathlib import Path

from briandump.config import CONF, replace_retry

ARM_S = 60              # seconds you have to START answering (press the talk key) after clicking 🎤 Answer
SLACK_S = 1.5           # an inbox stamp is whole seconds, so a clip's computed start can read up to ~1 s early
KEEP_S = 180            # an arm past its window stays this long for a clip that started in time and is still in Whisper
QUOTE_MAX = 200         # the quoted question is cut to this many characters
TICKS = ("md", "manual")


def path():
    return Path(CONF.answer_arm)


def quote(question):
    """The question as the typed line quotes it: one line, no double quotes, at most QUOTE_MAX characters."""
    s = " ".join("".join(ch if ch.isprintable() else " " for ch in str(question or "")).split()).replace('"', "'")
    return s if len(s) <= QUOTE_MAX else s[: QUOTE_MAX - 1].rstrip() + "…"


def prefix(question):
    return f'[answer to: "{quote(question)}"] '


def _write(data):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    replace_retry(tmp, p)


def _load():
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        d["armed_at"], d["expires_at"] = float(d["armed_at"]), float(d["expires_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return d if isinstance(d, dict) and d.get("question") and d.get("nonce") else None


def arm(qid, question, *, tick=None, session=None, dump=None, now=None, seconds=ARM_S):
    """Arm `question` for the next clip started within `seconds`. A second arm replaces the first: the last one wins."""
    now = time.time() if now is None else now
    data = {"id": str(qid), "question": str(question), "tick": tick if tick in TICKS else None,
            "session": str(session) if session else None, "dump": str(dump) if dump else None,
            "armed_at": now, "expires_at": now + seconds, "nonce": uuid.uuid4().hex}
    _write(data)
    return data


def read(now=None):
    """The arm, with "open" = you can still start answering (now < expires_at). None when there is none; an arm KEEP_S past
    its window is removed (a clip started in time has long been taken by then)."""
    now = time.time() if now is None else now
    d = _load()
    if d is None:
        return None
    if now >= d["expires_at"] + KEEP_S:
        disarm(d["nonce"])
        return None
    d["open"] = now < d["expires_at"]
    return d


def disarm(nonce=None):
    """Remove the arm; with `nonce`, only that one (a newer arm is left alone). -> True when one was removed."""
    d = _load()
    if d is None or (nonce and d["nonce"] != nonce):
        return False
    try:
        path().unlink()
    except OSError:
        return False
    return True


def clip_started(stamp, entry):
    """Epoch of the key press behind an inbox clip: its stamp (written after Whisper) minus the clip's length and Whisper's
    time, both from the PTT log line. None without a log line, so an unverified file never takes the arm."""
    if not isinstance(entry, dict):
        return None
    try:
        t = time.mktime(dt.datetime.strptime(str(stamp), "%Y%m%d-%H%M%S").timetuple())
        return t - float(entry.get("seconds") or 0) - float(entry.get("transcribe_s") or 0)
    except (TypeError, ValueError, OverflowError):
        return None


def covers(started, d=None):
    """Does the arm answer a clip pressed at `started` (armed_at - SLACK_S <= started <= expires_at)? Nothing is taken."""
    d = _load() if d is None else d
    return started is not None and d is not None and d["armed_at"] - SLACK_S <= started <= d["expires_at"]


def take(started):
    """The arm, when a clip pressed at `started` answers it (covers); it is removed, so the next clip goes through unchanged.
    None for no arm, a clip from before it (already queued) or one started too late."""
    d = _load()
    return d if covers(started, d) and disarm(d["nonce"]) else None


def tick(armed, words=""):
    """Close the question once its answer is delivered: questions.md (a brain-dump question) or questions.json (`ask`).
    A question from Claude's own message has nothing to tick. Best effort: -> True when a line or an entry was closed."""
    kind, question = (armed or {}).get("tick"), (armed or {}).get("question")
    if kind == "md" and question:
        from briandump import agenda
        try:
            return bool(agenda.set_done(CONF.questions_file, question, True))
        except Exception:  # noqa: BLE001 - the answer is delivered; a tick that fails leaves the card open
            return False
    if kind != "manual":
        return False
    p = Path(CONF.manual_questions)
    try:
        qs = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hit = next((q for q in qs if isinstance(q, dict) and str(q.get("id")) == str(armed.get("id")) and not q.get("done")),
               None) if isinstance(qs, list) else None
    if hit is None:
        return False
    hit.update(done=True, done_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    if words:
        hit["answer"] = str(words)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(qs, indent=2, ensure_ascii=False), encoding="utf-8")
        replace_retry(tmp, p)
    except OSError:
        return False
    return True
