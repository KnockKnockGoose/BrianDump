"""Hold-to-talk plus the dump toggle: the only BrianDump process that touches the microphone.

Hold the talk key (default F9), speak, release: the clip is transcribed on this PC with faster-whisper (the model stays loaded,
so a line costs 1-3 s after release) and lands in <state_dir>/inbox/<stamp>-<host>.txt, where the voice router (panel mode) or
watch_inbox.py (monitor mode) picks it up and types it into your Claude Code session.

Press the dump key (default F2) once and talk for as long as you like (up to dump.max_minutes); press again to stop. The
recording goes to <data_dir>/dumps/<id>/audio.wav with meta.json, then the same loaded model transcribes it in the background
-> transcript.txt. A dump transcript never goes to the inbox: the panel's dump worker turns it into cards.

    py -m briandump.ptt                                  # settings from config.json (ptt.key, ptt.dump_key, ptt.device, ptt.model)
    py -m briandump.ptt --key f8 --device "USB" --model base
    py -m briandump.ptt --list-devices

Why hold-to-talk for the session but a toggle for dumps: a line to Claude is a few seconds and should be gone the moment you let
go; a dump is minutes of thinking out loud with both hands free. One microphone, one recording at a time: the talk key is
ignored (one low beep) while a dump records, and the dump key is ignored while the talk key is held. Talk clips are transcribed
one after another, in the order they were spoken; a dump is transcribed on its own thread, and because the model takes
concurrent calls, a talk clip during a long dump transcription waits for one ~30 s window of it, not for all of it.

Feedback: short high beep on press, low beep on release, double beep when the text has been written; two rising beeps when a
dump starts, two falling when it stops. The status file <state_dir>/state/ptt_status.json (level meter, what is recording) feeds
the panel; the panel's Dump button writes <state_dir>/state/ptt_command.json, which this process polls (one action per nonce;
a command older than COMMAND_MAX_AGE_S, e.g. a click made while this service was down, is dropped, not replayed).

The talk and dump keys are swallowed system-wide while this runs (ptt.suppress_key): a held F9 would otherwise auto-repeat
into the terminal and scroll it, and F2 renames things in other apps. The low-level hook feeds the handlers itself, because
suppressed events never reach pynput's callbacks; the handlers run on a worker thread so the hook returns at once.

Audio is captured at the microphone's native rate and resampled to 16 kHz as each block arrives: a 30-minute dump at 48 kHz
float32 would be ~350 MB in memory, at 16 kHz it is ~115 MB.

Files are written to survive their readers: status and meta files are replaced atomically with retries (Windows refuses a
replace while the panel has the file open), inbox files are created exclusively (a clip in the same second takes the next
one), and a dump store that fails falls back to writing dumps/<id>/ directly: a recording is never lost. One copy runs per
BrianDump home (a named mutex): a second recorder would type every line twice.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import queue
import shutil
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from briandump.config import CONF
from briandump.transcribe import SAMPLE_RATE, Transcriber, normalise, peak_dbfs, resample, rms_dbfs, wav_write

# Guarded: the pure parts (status, levels, commands, the dump hand-off) import without PortAudio or a keyboard hook, which is
# how tests and CI load this module; main() says what is missing.
try:
    import sounddevice as sd
except (ImportError, OSError) as _e:    # not installed, or no PortAudio library
    sd, SD_ERROR = None, f"{type(_e).__name__}: {_e}"
else:
    SD_ERROR = ""
try:
    from pynput import keyboard
except Exception as _e:                 # not installed, or no display to hook (Linux)
    keyboard, KB_ERROR = None, f"{type(_e).__name__}: {_e}"
else:
    KB_ERROR = ""

HOST = CONF.host
MIN_SECONDS = 0.4                   # a shorter talk clip is a slipped finger
MIN_DUMP_SECONDS = 1.0
QUIET_DBFS = -46.0                  # a talk clip peaking below this is hiss: Whisper would invent words
LEVEL_WINDOW_S = 0.25               # the meter: RMS of the last 250 ms
STATUS_FAST_S, STATUS_SLOW_S, COMMAND_POLL_S = 0.25, 2.0, 0.3
COMMAND_MAX_AGE_S = 10              # a command file older than this is logged and deleted, never acted on
DUMP_COMMANDS = ("dump_toggle", "dump_start", "dump_stop")
WM_DOWN, WM_UP = (0x100, 0x104), (0x101, 0x105)   # WM_KEYDOWN / WM_SYSKEYDOWN, WM_KEYUP / WM_SYSKEYUP


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def stamp(t=None):
    return dt.datetime.fromtimestamp(t if t is not None else time.time()).strftime("%Y%m%d-%H%M%S")


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def iso_local(t=None):
    return dt.datetime.fromtimestamp(t if t is not None else time.time()).astimezone().isoformat(timespec="seconds")


# ---------- pure pieces (tests call these with fakes) ----------
def status_dict(*, ready, key, dump_key, device, model, recording=False, mode=None, started=None, level_db=None, peak_db=None,
                transcribing=None, last_error=None, pid=None, ts=None):
    """The ptt_status.json shape (see docs/architecture.md)."""
    return {"pid": pid if pid is not None else os.getpid(), "ts": iso(ts), "ready": bool(ready), "key": key, "dump_key": dump_key,
            "device": device, "model": model, "recording": bool(recording), "mode": mode if recording else None,
            "started": started if recording else None, "level_db": level_db, "peak_db": peak_db, "transcribing": transcribing,
            "last_error": last_error}


def write_json(path, data, tries=20, pause=0.025):
    """Atomic (tmp + os.replace), retried: Windows refuses the replace while another process (the panel) has the file open.
    -> True when written. Never raises: a missed status write is replaced by the next one."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return False
    for i in range(max(1, tries)):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(pause * (1 + i % 2))
        except OSError:
            break
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def read_command(path, seen, allowed=DUMP_COMMANDS, now=None, max_age_s=COMMAND_MAX_AGE_S, log=None):
    """The command file's cmd if it is new, else None. The file is taken (renamed away) before it is read, so a newer command
    written meanwhile is never deleted unread; it is gone afterwards whether or not it is acted on. One action per nonce (a
    command without one is keyed by cmd + t); a command older than max_age_s is logged and dropped, so a click made while
    this service was down can never start a recording later."""
    p = Path(path)
    taken = p.with_name(f"{p.name}.{os.getpid()}.taken")
    try:
        os.replace(p, taken)
    except OSError:
        return None                                    # no command (or the panel is writing it right now: next poll)
    try:
        raw = taken.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    finally:
        try:
            taken.unlink()
        except OSError:
            pass
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    cmd = d.get("cmd")
    key = str(d.get("nonce") or f"{cmd}:{d.get('t')}")
    if cmd not in allowed or key in seen:
        return None
    seen.append(key)
    try:
        t = float(d["t"])
    except (KeyError, TypeError, ValueError):
        t = None
    age = (time.time() if now is None else now) - t if t is not None else 0.0
    if age > max_age_s:
        if log:
            log(f"command file: {cmd} from {age:.0f} s ago ignored (older than {max_age_s:g} s)")
        return None
    return cmd


class Level:
    """RMS and peak (dBFS) of the most recent LEVEL_WINDOW_S of 16 kHz audio: the panel's meter while recording."""

    def __init__(self, window_s=LEVEL_WINDOW_S, rate=SAMPLE_RATE):
        self.n, self.lock = max(1, int(window_s * rate)), threading.Lock()
        self.buf, self.peak = np.zeros(0, dtype="float32"), -100.0

    def reset(self):
        with self.lock:
            self.buf, self.peak = np.zeros(0, dtype="float32"), -100.0

    def push(self, x):
        x = np.asarray(x, dtype="float32").reshape(-1)
        if not x.size:
            return
        with self.lock:
            self.buf = np.concatenate([self.buf, x])[-self.n:]
            self.peak = max(self.peak, peak_dbfs(x))

    def read(self):
        """-> (level_db, peak_db), rounded; (-100, -100) before any audio."""
        with self.lock:
            return round(rms_dbfs(self.buf), 1) if self.buf.size else -100.0, round(self.peak, 1)


# ---------- the dump hand-off ----------
class MiniStore:
    """The four dump-store calls, writing the documented files and meta shape, for when briandump.dumps cannot be imported or
    fails: a recording is never lost because of the store. The real store (briandump.dumps) is used whenever it works."""
    __name__ = "MiniStore"

    def __init__(self, root=None):
        self.root = Path(root or CONF.dumps_dir)

    def path(self, did):
        return self.root / did

    def _meta(self, did):
        try:
            d = json.loads((self.path(did) / "meta.json").read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {"id": did}
        except (OSError, ValueError):
            return {"id": did}

    def create(self, source="ptt", created=None, duration_s=None):
        t = float(created) if isinstance(created, (int, float)) else time.time()
        for _ in range(20):
            did = f"{stamp(t)}-{os.urandom(2).hex()}"
            try:
                self.path(did).mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        meta = {"id": did, "createdAt": iso_local(t), "updatedAt": iso_local(), "durationSec": duration_s, "status": "recorded",
                "error": None, "attempts": 0, "timings": {"transcribe_s": None, "think_s": None}, "words": None, "source": source,
                "label": None, "processor": None, "model": None, "cost_usd": None, "report": None, "sent": {}, "agenda_added": [],
                "questions_added": []}
        if not write_json(self.path(did) / "meta.json", meta):
            raise OSError(f"could not write {self.path(did) / 'meta.json'}")
        return meta

    def update(self, did, **patch):
        meta = self._meta(did)
        for k, v in patch.items():
            meta[k] = {**meta[k], **v} if isinstance(v, dict) and isinstance(meta.get(k), dict) else v
        meta["updatedAt"] = iso_local()
        if not write_json(self.path(did) / "meta.json", meta):
            raise OSError(f"could not write {self.path(did) / 'meta.json'}")
        return meta

    def write_transcript(self, did, text):
        (self.path(did) / "transcript.txt").write_text((text or "").strip() + "\n", encoding="utf-8")


def dump_store():
    """briandump.dumps if it imports (lazily: the recorder starts even without it), else the stand-in."""
    try:
        from briandump import dumps
        return dumps
    except Exception as e:
        log(f"dump store unavailable ({type(e).__name__}: {e}); writing dumps/<id>/ directly")
        return MiniStore()


def _save_into(store, audio, started, seconds):
    """create (recorded) -> audio.wav moved in -> update(transcribing). The slow WAV write happens before the entry exists, so
    the panel's worker never sees a `recorded` ptt entry for longer than one rename."""
    incoming = Path(CONF.dumps_dir) / f".incoming-{os.getpid()}-{threading.get_ident()}.wav"
    wav_write(incoming, audio)
    try:
        try:
            meta = store.create(source="ptt", created=started, duration_s=seconds)
        except TypeError:                                          # a store that derives its own creation time
            meta = store.create(source="ptt", duration_s=seconds)
        did = str(meta["id"])
        folder = Path(store.path(did))
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(incoming), str(folder / "audio.wav"))      # a rename on the same drive
        # claimed by this process: the panel's worker leaves it alone while the recorder runs (however long a slow PC takes)
        store.update(did, status="transcribing", worker={"pid": os.getpid(), "host": CONF.host})
        return did
    finally:
        try:
            incoming.unlink()
        except OSError:
            pass


def save_dump(audio, started, store, log=log):
    """Hand a finished dump recording to the dump store: audio.wav + meta, flipped to `transcribing` before this returns (so the
    panel's worker leaves it to us). -> (store, dump id). A failing store falls back to MiniStore; if even that fails the WAV
    is kept as <state_dir>/state/dump-unsaved-<stamp>.wav and the error says where."""
    seconds = round(len(audio) / SAMPLE_RATE, 1)
    stores = [store] if isinstance(store, MiniStore) else [store, MiniStore()]
    for st in stores:
        try:
            did = _save_into(st, audio, started, seconds)
            log(f"dump {did}: {seconds:.0f} s saved, transcribing")
            return st, did
        except Exception as e:
            log(f"dump store {getattr(st, '__name__', type(st).__name__)} failed: {type(e).__name__}: {e}")
    path = CONF.state / f"dump-unsaved-{stamp(started)}.wav"
    wav_write(path, audio)
    raise RuntimeError(f"the dump store failed; the recording is kept at {path}")


def transcribe_dump(did, audio, store, transcriber, log=log):
    """The loaded model -> transcript.txt + meta `transcribed` (words, timings.transcribe_s); on failure, or when nothing was
    recognised, meta `error`. Runs on a background thread in the service; returns the transcriber's result (None on error)."""
    seconds = len(audio) / SAMPLE_RATE
    last = [0.0]

    def progress(start, end, text):
        if end - last[0] >= 30:
            last[0] = end
            log(f"dump {did}: {end:.0f}/{seconds:.0f} s")

    try:
        r = transcriber.transcribe(audio, on_segment=progress)
        if not r["text"]:
            raise RuntimeError("no speech recognised in the recording (check the microphone in py briandump.py mics)")
        store.write_transcript(did, r["text"])
        store.update(did, status="transcribed", error=None, words=r["words"], timings={"transcribe_s": r["took_s"]},
                     durationSec=round(seconds, 1))
        log(f"dump {did}: transcribed, {r['words']} words in {r['took_s']:.0f} s")
        return r
    except Exception as e:
        log(f"dump {did}: transcription failed: {type(e).__name__}: {e}")
        try:
            store.update(did, status="error", error=f"{type(e).__name__}: {e}")
        except Exception:
            pass
        return None


# ---------- audio devices ----------
def pick_device(name_part):
    """The input device whose name contains name_part; MME first (it resamples for us), then WASAPI, then anything."""
    if sd is None:
        return None
    try:
        devices, apis = sd.query_devices(), sd.query_hostapis()
    except Exception:
        return None
    best = None
    for i, d in enumerate(devices):
        if d["max_input_channels"] <= 0 or str(name_part).lower() not in d["name"].lower():
            continue
        api = apis[d["hostapi"]]["name"]
        score = 2 if "MME" in api else 1 if "WASAPI" in api else 0
        if best is None or score > best[0]:
            best = (score, i)
    return best[1] if best else None


def device_info(device):
    if sd is None:
        return None
    try:
        return sd.query_devices(device) if device is not None else sd.query_devices(kind="input")
    except Exception:
        return None


def device_rate(device):
    info = device_info(device)
    try:
        return int(info["default_samplerate"]) if info else SAMPLE_RATE
    except (KeyError, TypeError, ValueError):
        return SAMPLE_RATE


def device_label(device):
    info = device_info(device)
    name = str(info["name"]) if info else "no input device found"
    return name if device is not None else f"{name} (Windows default)"


class Beeper:
    """Tones on the default output through ONE stream kept open (playing silence) for the life of the program: Bluetooth
    headphones take a moment to wake their audio link, so short tones on a freshly opened stream are never heard."""
    SR = 48000

    def __init__(self):
        self.buf, self.lock = np.zeros(0, dtype="float32"), threading.Lock()
        self.stream = sd.OutputStream(samplerate=self.SR, channels=1, dtype="float32", callback=self._fill)
        self.stream.start()

    def _fill(self, out, frames, t, status):
        with self.lock:
            n = min(frames, len(self.buf))
            out[:n, 0] = self.buf[:n]
            out[n:, 0] = 0.0
            self.buf = self.buf[n:]

    def play(self, freq, ms):
        t = np.arange(int(self.SR * ms / 1000)) / self.SR
        ramp = np.minimum(1, np.minimum(t, t[::-1]) / 0.005)          # 5 ms fades, no clicks
        tone = (0.3 * np.sin(2 * np.pi * freq * t) * ramp).astype("float32")
        gap = np.zeros(int(self.SR * 0.05), dtype="float32")        # so double beeps stay separate
        with self.lock:
            self.buf = np.concatenate([self.buf, tone, gap])

    def close(self):
        try:
            self.stream.close()
        except Exception:
            pass


class Recorder:
    """Captures the microphone at its native rate; keeps 16 kHz float32 blocks and feeds the level meter as they arrive."""

    def __init__(self, device, name_part=None, level=None):
        self.device, self.name_part, self.level = device, name_part, level
        self.rate = device_rate(device)
        self.chunks, self.stream, self.started = [], None, 0.0

    def start(self):
        try:
            self._open()
        except sd.PortAudioError:
            # the first press after a reboot or a device switch can fail although the device works a second later:
            # re-scan the audio devices (indices can shift) and try once more
            sd._terminate()
            sd._initialize()
            if self.name_part:
                self.device = pick_device(self.name_part)
            self.rate = device_rate(self.device)
            self._open()

    def _open(self):
        self.chunks, self.started = [], time.time()
        if self.level:
            self.level.reset()
        self.stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="float32", device=self.device,
                                     blocksize=max(64, self.rate // 50), callback=self._block)
        self.stream.start()

    def _block(self, indata, frames, t, status):
        try:
            x = resample(indata[:, 0], self.rate)
            self.chunks.append(x)
            if self.level:
                self.level.push(x)
        except Exception:
            pass

    def seconds(self):
        return sum(len(c) for c in self.chunks) / SAMPLE_RATE

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        chunks, self.chunks = self.chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")


# ---------- keys ----------
def key_name(k):
    """pynput key -> its lower-case name ('f9') or character ('a'); '' when it has neither."""
    name = getattr(k, "name", None) or getattr(k, "char", None)
    return str(name).lower() if name is not None else ""


def key_vk(name):
    """pynput key name -> Windows virtual-key code (None for a key the hook cannot match by name)."""
    k = getattr(keyboard.Key, name, None) if keyboard is not None else None
    try:
        return k.value.vk if k is not None else None
    except AttributeError:
        return None


_MUTEX = []


def single_instance(state_dir=None):
    """One recorder per BrianDump home: a second one (a foreground `py briandump.py ptt` next to the logon task) would record and
    type every line twice. A named mutex, held until the process exits. -> False when another copy holds it."""
    if os.name != "nt":
        return True
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    tag = hashlib.sha1(str(state_dir or CONF.state_dir).lower().encode("utf-8")).hexdigest()[:12]
    h = k.CreateMutexW(None, False, f"Local\\BrianDumpPTT-{tag}")
    if not h:
        return True                                    # cannot tell: run
    if ctypes.get_last_error() == 183:                 # ERROR_ALREADY_EXISTS
        return False
    _MUTEX.append(h)
    return True


# ---------- the service ----------
class PTT:
    def __init__(self, keys, dump_key, device, name_part, transcriber, *, model_name="", beeps=True, keep_last_clip=False,
                 max_minutes=30, status_path=None, command_path=None, inbox=None, store=None, recorder=None, device_name=None):
        self.key_list = [k.lower() for k in keys]
        self.keys, self.dump_key = set(self.key_list), (dump_key or "").lower()
        self.level = Level()
        self.rec = recorder if recorder is not None else Recorder(device, name_part, self.level)
        self.transcriber, self.model_name = transcriber, model_name or getattr(transcriber, "model_name", "")
        self.beeps_on, self.keep_last_clip, self.max_minutes = bool(beeps), bool(keep_last_clip), float(max_minutes or 0)
        self.status_path, self.command_path = Path(status_path or CONF.ptt_status), Path(command_path or CONF.ptt_command)
        self.inbox, self.store = Path(inbox or CONF.inbox), store
        self.lock = threading.RLock()
        self.pressed, self.dump_on, self.dump_key_down = False, False, False
        self.ready, self.transcribing, self.last_error = False, None, None
        self.seen_nonces, self.beeper = deque(maxlen=64), None
        self.device_name = device_name if device_name is not None else device_label(device)
        self.stop_event, self.clips = threading.Event(), queue.Queue()
        self.dump_threads, self.last_cmd, self.next_status = [], 0.0, 0.0

    # feedback
    def beep(self, freq, ms):
        if not self.beeps_on or sd is None:
            return
        try:
            if self.beeper is None:
                self.beeper = Beeper()
            self.beeper.play(freq, ms)
        except Exception:
            pass

    # status
    @property
    def recording(self):
        return self.pressed or self.dump_on

    def status(self):
        level, peak = self.level.read() if self.recording else (None, None)
        mode = "talk" if self.pressed else "dump" if self.dump_on else None
        return status_dict(ready=self.ready, key=",".join(self.key_list), dump_key=self.dump_key, device=self.device_name,
                           model=self.model_name, recording=self.recording, mode=mode,
                           started=iso(self.rec.started) if self.recording else None, level_db=level, peak_db=peak,
                           transcribing=self.transcribing, last_error=self.last_error)

    def write_status(self):
        try:
            return write_json(self.status_path, self.status())
        except Exception:                                  # a status write never takes the recorder down
            return False

    def poll_command(self, now=None):
        cmd = read_command(self.command_path, self.seen_nonces, now=now, log=log)
        if cmd == "dump_toggle":
            self.toggle_dump("panel")
        elif cmd == "dump_start" and not self.dump_on:
            self.start_dump("panel")
        elif cmd == "dump_stop" and self.dump_on:
            self.stop_dump("panel")
        return cmd

    # keys
    def on_press(self, k):
        name = key_name(k)
        if name in self.keys:
            if self.pressed:
                return                                         # auto-repeat
            if self.dump_on:
                self.beep(150, 150)                            # one microphone: a dump is recording
                return
            with self.lock:
                self.pressed = True
                try:
                    self.rec.start()
                except Exception as e:                         # keep listening; the next press may work
                    self.pressed = False
                    self.last_error = f"mic error: {e}"
                    log(f"mic error, try again: {e}")
                    self.beep(150, 150)
                    self.write_status()
                    return
            self.beep(600, 80)
            self.write_status()
            log("listening...")
        elif name and name == self.dump_key:
            if self.dump_key_down:
                return                                         # auto-repeat: a held F2 is one press
            self.dump_key_down = True
            self.toggle_dump("key")

    def on_release(self, k):
        name = key_name(k)
        if name in self.keys:
            if not self.pressed:
                return
            with self.lock:
                self.pressed = False
                audio = self.rec.stop()
            self.beep(300, 80)
            self.write_status()
            self.clips.put(audio)                              # transcribed in order by clip_worker()
        elif name and name == self.dump_key:
            self.dump_key_down = False

    def clip_worker(self):
        """Talk clips one after another, so their inbox files (and the typing) keep the order they were spoken in."""
        while True:
            audio = self.clips.get()
            if audio is None:
                return
            try:
                self.handle(audio)
            except Exception as e:
                self.last_error = f"clip: {type(e).__name__}: {e}"
                log(self.last_error)

    # dumps
    def toggle_dump(self, source="key"):
        if self.dump_on:
            self.stop_dump(source)
        else:
            self.start_dump(source)

    def start_dump(self, source="key"):
        with self.lock:
            if self.dump_on:
                return
            if self.pressed:
                self.beep(150, 150)                            # the talk key is held: one recording at a time
                return
            try:
                self.rec.start()
            except Exception as e:
                self.last_error = f"mic error: {e}"
                log(f"mic error, dump not started: {e}")
                self.beep(150, 150)
                self.write_status()
                return
            self.dump_on = True
        self.beep(500, 90)
        self.beep(750, 90)                                     # rising: a dump is on
        self.write_status()
        log(f"dump started ({source}); press {self.dump_key.upper() or 'the dump key'} again to stop")

    def stop_dump(self, source="key"):
        """Stop, save (audio.wav + meta `transcribing` before this returns), transcribe on a thread. -> the dump id or None."""
        with self.lock:
            if not self.dump_on:
                return None
            started = self.rec.started
            audio = self.rec.stop()
            self.dump_on = False
        self.beep(750, 90)
        self.beep(500, 90)                                     # falling: the dump is off
        seconds = len(audio) / SAMPLE_RATE
        if seconds < MIN_DUMP_SECONDS:
            log(f"dump stopped ({source}): {seconds:.1f} s, discarded")
            self.write_status()
            return None
        audio = normalise(audio)
        try:
            store, did = save_dump(audio, started, self.store or dump_store())
        except Exception as e:
            self.last_error = f"dump not saved: {e}"
            log(self.last_error)
            self.write_status()
            return None
        log(f"dump stopped ({source})")
        self.transcribing = did
        self.write_status()
        th = threading.Thread(target=self._transcribe_dump, args=(did, audio, store), daemon=True, name=f"dump-{did}")
        self.dump_threads.append(th)
        th.start()
        return did

    def _transcribe_dump(self, did, audio, store):
        r = transcribe_dump(did, audio, store, self.transcriber)
        if r is None:
            self.last_error = f"dump {did}: transcription failed"
        if self.transcribing == did:
            self.transcribing = None
        self.write_status()

    # the talk key
    def handle(self, audio):
        seconds = len(audio) / SAMPLE_RATE
        if seconds < MIN_SECONDS:
            log(f"({seconds:.1f} s, ignored)")
            return None
        peak = peak_dbfs(audio)
        if peak < QUIET_DBFS:
            log(f"({seconds:.1f} s, too quiet: peak {peak:.0f} dBFS - check the microphone)")
            self.beep(150, 150)
            return None
        audio = normalise(audio)
        if self.keep_last_clip:
            try:
                wav_write(CONF.state / "last_clip.wav", audio)
            except OSError:
                pass
        try:
            r = self.transcriber.transcribe(audio)
        except Exception as e:
            self.last_error = f"whisper: {type(e).__name__}: {e}"
            log(self.last_error)
            self.beep(150, 150)
            return None
        text = r["text"]
        if not text:
            log(f"({seconds:.1f} s, nothing heard, peak {peak:.0f} dBFS)")
            return None
        path = self.write_inbox(text, seconds, r["took_s"])
        self.beep(750, 60)
        self.beep(750, 60)
        log(f"{seconds:.1f} s audio -> {r['took_s']:.1f} s whisper -> {path.name}\n  > {text}")
        return path

    def write_inbox(self, text, seconds, took_s, t=None):
        """<stamp>-<host>.txt, created exclusively (a second clip in the same second takes the next free second), then the log
        line the voice router cross-checks it against."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        t = time.time() if t is None else t
        for i in range(120):
            st = stamp(t + i)
            path = self.inbox / f"{st}-{HOST}.txt"
            try:
                with open(path, "x", encoding="utf-8") as f:
                    f.write(text + "\n")
                break
            except FileExistsError:
                continue
        else:
            raise OSError("inbox: no free file name in the next 120 s")
        with open(self.inbox / f"log-{HOST}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": st, "seconds": round(seconds, 1), "transcribe_s": round(took_s, 1), "text": text},
                               ensure_ascii=False) + "\n")
        return path

    # housekeeping (main thread)
    def wait_ready(self, load):
        """Run `load` (the model) on a thread while the status file keeps saying alive-but-not-ready (a first run downloads the
        model for minutes). -> (ok, seconds or error text)."""
        out = {}

        def run():
            try:
                out["s"] = load()
            except Exception as e:
                out["e"] = f"{type(e).__name__}: {e}"
        th = threading.Thread(target=run, daemon=True, name="whisper-load")
        th.start()
        while th.is_alive() and not self.stop_event.is_set():
            self.write_status()
            th.join(STATUS_SLOW_S)
        if "e" in out or "s" not in out:
            return False, out.get("e", "stopped while loading")
        return True, out["s"]

    def tick(self, now=None):
        """One pass of the housekeeping loop: the command file, the status file, the dump time limit."""
        now = time.time() if now is None else now
        if now - self.last_cmd >= COMMAND_POLL_S:
            self.last_cmd = now
            try:
                self.poll_command(now)
            except Exception as e:
                log(f"command file: {e}")
        if now >= self.next_status:
            self.write_status()
            self.next_status = now + (STATUS_FAST_S if self.recording else STATUS_SLOW_S)
        if self.dump_on and self.max_minutes and now - self.rec.started >= self.max_minutes * 60:
            log(f"dump reached {self.max_minutes:g} min (dump.max_minutes)")
            self.stop_dump("max_minutes")

    def loop(self):
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(0.05)

    def shutdown(self):
        self.stop_event.set()
        with self.lock:
            if self.dump_on:
                self.stop_dump("shutdown")                     # saved; the panel's worker finishes it if we exit first
            elif self.pressed:
                self.pressed = False
                self.rec.stop()
        self.clips.put(None)
        self.ready = False
        self.write_status()
        if self.beeper:
            self.beeper.close()

    def follow_default_output(self):
        """Reopen the beep stream when Windows' default output changes (a headset switched off -> speakers, or back on).
        PortAudio caches the device list, so it is re-initialised to see the new default; that also drops the mic stream, so
        it only happens between recordings. Needs pycaw + comtypes; without them this thread simply ends."""
        if sd is None:
            return
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities
            comtypes.CoInitialize()
            current = AudioUtilities.GetSpeakers().FriendlyName
        except Exception:
            return
        while not self.stop_event.is_set():
            time.sleep(2)
            try:
                now = AudioUtilities.GetSpeakers().FriendlyName
            except Exception:
                continue                                       # mid-switch: no default device for a moment
            if now == current or self.recording:
                continue
            with self.lock:
                if self.recording:
                    continue
                try:
                    if self.beeper is not None:
                        self.beeper.close()
                    sd._terminate()
                    sd._initialize()
                    if self.rec.name_part:                     # device indices can shift after a re-scan
                        self.rec.device = pick_device(self.rec.name_part)
                    self.rec.rate = device_rate(self.rec.device)
                    self.beeper = Beeper() if self.beeps_on else None
                except Exception:
                    self.beeper = None
            current = now
            log(f"output switched to: {now}")
            time.sleep(0.8)                                    # let a Bluetooth link wake before the confirmation chime
            self.beep(450, 120)


def main(argv=None):
    p = CONF.ptt
    ap = argparse.ArgumentParser(description="BrianDump hold-to-talk + dump recorder")
    ap.add_argument("--key", default=str(p.get("key") or "f9"), help="hold to talk; pynput key names, comma-separated for several (f9,f13)")
    ap.add_argument("--dump-key", default=str(p.get("dump_key") or "f2"), help="press once to start a dump, again to stop ('' = off)")
    ap.add_argument("--device", default=p.get("device") or None, help="part of the microphone's name (default: the Windows default input)")
    ap.add_argument("--model", default=str(p.get("model") or "small"), help="faster-whisper model: tiny | base | small")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--no-suppress", action="store_true", help="let other apps see the talk / dump keys too")
    args = ap.parse_args(argv)

    if args.list_devices:
        from briandump import list_mics
        return list_mics.main([])
    if sd is None or keyboard is None:
        log(f"hold-to-talk needs sounddevice and pynput ({SD_ERROR or KB_ERROR}): py -m pip install -r requirements.txt")
        return 3
    if not single_instance():
        log(f"another BrianDump hold-to-talk is already running for {CONF.state_dir}; this copy exits")
        return 4

    device = pick_device(args.device) if args.device else None
    if args.device and device is None:
        log(f"no input device matching {args.device!r}: using the Windows default input")
    keys = [k.strip().lower() for k in str(args.key).split(",") if k.strip()] or ["f9"]
    dump_key = str(args.dump_key or "").strip().lower()
    if dump_key in keys:
        log(f"the dump key {dump_key} is also a talk key: dumps are off until ptt.dump_key differs")
        dump_key = ""
    for name in keys + ([dump_key] if dump_key else []):
        if not (hasattr(keyboard.Key, name) or len(name) == 1):
            log(f"warning: {name!r} is not a pynput key name (f1..f20, scroll_lock, pause, ...); it will never match")
    suppress = bool(p.get("suppress_key", True)) and not args.no_suppress
    hot = p.get("hotwords") or ()
    transcriber = Transcriber(args.model, hotwords=hot if isinstance(hot, (list, tuple)) else [str(hot)],
                              language=CONF.get("ptt.language", "en"))
    ptt = PTT(keys, dump_key, device, args.device if device is not None else None, transcriber, model_name=args.model,
              beeps=bool(p.get("beeps", True)), keep_last_clip=bool(p.get("keep_last_clip", False)),
              max_minutes=CONF.get("dump.max_minutes", 30))
    CONF.ensure_dirs()
    log(f"mic: {ptt.device_name} | hold {' or '.join(k.upper() for k in keys)} to talk, {dump_key.upper() or 'no key'} toggles a dump | "
        f"whisper {args.model} | inbox {CONF.inbox} | dumps {CONF.dumps_dir} | status {CONF.ptt_status}")

    def on_signal(signum, frame):
        ptt.stop_event.set()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass

    ptt.write_status()
    ptt.beep(0, 1)                                             # open the output stream now: headphones wake while the model loads
    log(f"loading whisper '{args.model}'...")
    ok, took = ptt.wait_ready(transcriber.load)
    if not ok:
        ptt.last_error = f"whisper model '{args.model}' failed to load: {took}"
        log(ptt.last_error)
        ptt.write_status()
        return 2
    log(f"whisper '{args.model}' loaded in {took:.1f} s")
    threading.Thread(target=ptt.follow_default_output, daemon=True, name="output-follower").start()
    threading.Thread(target=ptt.clip_worker, daemon=True, name="clips").start()

    # key events are queued from the hook thread and handled on a worker, so the hook always returns at once
    events = queue.Queue()

    def worker():
        while True:
            kind, k = events.get()
            try:
                (ptt.on_press if kind == "down" else ptt.on_release)(k)
            except Exception as e:
                log(f"key handler: {e}")
    threading.Thread(target=worker, daemon=True, name="keys").start()

    names = keys + ([dump_key] if dump_key else [])
    hot_by_vk = {}
    if suppress:
        for name in names:
            vk = key_vk(name)
            if vk is not None:
                hot_by_vk[vk] = getattr(keyboard.Key, name)
    relevant = set(names)
    listener = None

    def win32_filter(msg, data):
        k = hot_by_vk.get(data.vkCode)
        if k is None:
            return True
        if msg in WM_DOWN:
            events.put(("down", k))
        elif msg in WM_UP:
            events.put(("up", k))
        listener.suppress_event()                              # raises: the key goes nowhere else

    def on_press(k):
        if key_name(k) in relevant:
            events.put(("down", k))

    def on_release(k):
        if key_name(k) in relevant:
            events.put(("up", k))

    # pass the filter only when there is one: pynput keeps an explicit None and would call it
    extra = {"win32_event_filter": win32_filter} if hot_by_vk else {}
    listener = keyboard.Listener(on_press=on_press, on_release=on_release, **extra)
    listener.start()
    waiter = threading.Thread(target=listener.wait, daemon=True)   # the hook is in: from here on the keys work (and are swallowed)
    waiter.start()
    waiter.join(10)                                            # pynput's wait() has no timeout of its own
    if waiter.is_alive():
        log("warning: the keyboard hook did not report ready within 10 s; the keys may not work")
    ptt.ready = True
    ptt.write_status()
    ptt.beep(450, 120)
    ptt.beep(675, 120)                                         # ready chime
    log("ready")
    try:
        ptt.loop()
    except KeyboardInterrupt:
        pass
    finally:
        ptt.shutdown()
        try:
            listener.stop()
        except Exception:
            pass
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
