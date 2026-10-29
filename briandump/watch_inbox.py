"""Monitor-mode fallback: print each new hold-to-talk transcript as it lands in the inbox, for a Claude Code session to read.

ptt.py runs as a logon task, so in monitor mode (voice.mode = "monitor") a session only has to watch the inbox. The
SessionStart hook tells it to run, as a background command:

    py "<repo>/briandump/watch_inbox.py" --session <id> --takeover --once      (then again without --takeover after each exit)

Each new <stamp>-<host>.txt (or <stamp>-panel-<host>.txt, typed on the panel) prints one line, "VOICE <stamp>-<host>: <text>",
after its name went into voice_handled.txt, so no other watcher (or the panel's router, later) delivers it again. The same
rules as the router apply: only this host's files, and a voice file without its line in the PTT log is flagged as not verified.
A clip that answers the question armed with the panel's 🎤 Answer is printed with the question quoted in front, and the
question is ticked once the line is printed (voice.answer_line).
--once exits 1.5 s after a batch: a background command has no time cap, and its exit is what wakes the session up.

One owner at a time: the owning session id + a heartbeat live in <state_dir>/state/voice_watcher.json. Every new session arms
this with --takeover, so the NEWEST session gets the voice; an older watcher sees the lock change (it checks right before every
line it prints, too), prints "handed off" and exits. Re-arming without --takeover only reclaims the lock if nobody else holds a
fresh heartbeat.

Panel mode: the panel server's voice router is the single consumer on this PC, so this script prints "handed off" and exits at
once (consuming the same file twice would deliver a line twice). --host all reports every host's files.

Catch-up: on start it also reports transcripts from the last CATCHUP_MIN minutes that no watcher has reported yet.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # runnable as a path too: py briandump/watch_inbox.py

from briandump import answers, voice  # noqa: E402
from briandump.config import CONF  # noqa: E402

CATCHUP_MIN = 15
STALE_S = 30                        # heartbeat older than this = nobody is watching
STABLE_S = 0.4                      # a file is read once its mtime is this old (ptt.py writes the text, then the log line)


class Watcher:
    def __init__(self, session, host=None, inbox=None, lock=None, handled=None, out=print, clock=time.time, sleep=time.sleep):
        self.session, self.host = str(session), str(host or CONF.host).lower()
        self.inbox = Path(inbox or CONF.inbox)
        self.lock_file, self.handled_file = Path(lock or CONF.voice_lock), Path(handled or CONF.voice_handled)
        self.out, self.clock, self.sleep, self.seen = out, clock, sleep, set()

    def kind(self, name):
        """'voice' | 'panel' for a file this watcher reports, else None (logs, clips, other hosts unless --host all)."""
        if self.host == "all":
            parsed = voice.parse_name(name)
            return parsed[1] if parsed else None
        return voice.classify(name, self.host)

    def read_lock(self):
        try:
            d = json.loads(self.lock_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_lock(self):
        voice.write_atomic(self.lock_file, json.dumps({"session": self.session, "pid": os.getpid(), "t": self.clock()}))

    def owner(self):
        return self.read_lock().get("pid") == os.getpid()

    def handed_off(self):
        self.out(f"VOICE-WATCHER handed off to {str(self.read_lock().get('session'))[:12]}; stop re-arming")
        return 0

    def handled(self):
        try:
            return set(self.handled_file.read_text(encoding="utf-8").split())
        except OSError:
            return set()

    def report(self, p, kind):
        """Mark the file handled, then print it: a crash between the two loses the line instead of delivering it twice."""
        raw = p.read_text(encoding="utf-8", errors="replace")
        if kind == "panel":                            # a routing line is never relayed as words
            raw = voice.split_meta(raw)[1]
        stamp, _, host = voice.parse_name(p.name)
        logged = kind != "voice" or voice.ptt_logged(stamp, host, self.inbox, text=raw)
        text = voice.compose(kind, raw, logged)
        self.handled_file.parent.mkdir(parents=True, exist_ok=True)
        with self.handled_file.open("a", encoding="utf-8") as f:
            f.write(p.name + "\n")
        armed = voice.answer_line(stamp, raw, text, host, self.inbox) if kind == "voice" and logged else {}
        if text:
            self.out(f"VOICE {p.stem}: {armed.get('text', text)}")
            if armed.get("answer"):
                answers.tick(armed["answer"], text)
        return bool(text)

    def files(self):
        try:
            return [p for p in sorted(self.inbox.glob("*.txt")) if self.kind(p.name)]
        except OSError:
            return []

    def run(self, takeover=False, once=False):
        lock = self.read_lock()
        try:
            beat = float(lock.get("t") or 0)
        except (TypeError, ValueError):
            beat = 0.0
        if not takeover and lock.get("session") not in (None, self.session) and self.clock() - beat < STALE_S:
            self.out(f"VOICE-WATCHER not started: {str(lock['session'])[:12]} owns the voice")
            return 0
        voice.trim_handled(self.handled_file)
        self.write_lock()
        self.out(f"VOICE-WATCHER armed for session {self.session[:8]} (host: {self.host})")
        last_beat, got = self.clock(), 0.0
        while True:
            now = self.clock()
            if now - last_beat > 5:
                if not self.owner():
                    return self.handed_off()
                self.write_lock()
                last_beat = now
            done = self.handled()                              # another consumer may have taken a file since the last round
            for p in self.files():
                if p.name in self.seen:
                    continue
                if p.name in done:
                    self.seen.add(p.name)
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if now - mt > CATCHUP_MIN * 60:
                    self.seen.add(p.name)                      # old and never reported: skip, do not replay
                    continue
                if now - mt < STABLE_S:
                    continue                                   # still being written: next round
                if not self.owner():
                    return self.handed_off()
                self.seen.add(p.name)
                if self.report(p, self.kind(p.name)):
                    got = now
            if once and got and now - got > 1.5:
                return 0
            self.sleep(0.5)


def main(argv=None):
    ap = argparse.ArgumentParser(description="print hold-to-talk transcripts as VOICE lines (monitor mode)")
    ap.add_argument("--session", default=f"pid{os.getpid()}")
    ap.add_argument("--takeover", action="store_true", help="take the voice from an older session's watcher")
    ap.add_argument("--once", action="store_true", help="exit 1.5 s after delivering a batch (the session re-runs it)")
    ap.add_argument("--host", default=CONF.host, help="only report <stamp>-[panel-]<host>.txt; 'all' = any host")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    def out(line):
        print(line, flush=True)

    if voice.voice_mode() == "panel":
        out("VOICE-WATCHER handed off to the panel voice router (voice.mode is panel); stop re-arming")
        return 0
    return Watcher(args.session, args.host, out=out).run(takeover=args.takeover, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
