"""Tests for briandump/ptt.py (hold-to-talk + dump toggle), briandump/list_mics.py and briandump/ptt_service.pyw: the status file, level
math, the command file, the talk-clip path into the inbox (and the router's cross-check of it), the dump hand-off (status
`transcribing` before the stop handler returns, never the inbox, never a lost recording), MiniStore, the single-instance guard.

No keyboard hook, microphone or model: a fake recorder, a fake transcriber and a fake dump store stand in. ptt.py imports
sounddevice and pynput behind guards, so this module loads wherever numpy does.
"""
import json
import re
import subprocess
import sys
import threading
import time
import types
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from briandump import list_mics, ptt, voice  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SR = 16000
STATUS_KEYS = {"pid", "ts", "ready", "key", "dump_key", "device", "model", "recording", "mode", "started", "level_db", "peak_db",
               "transcribing", "last_error"}
META_KEYS = {"id", "createdAt", "updatedAt", "durationSec", "status", "error", "attempts", "timings", "words", "source", "label",
             "processor", "model", "cost_usd", "report", "sent", "agenda_added", "questions_added"}


def sine(seconds=1.0, db=-20.0, freq=440.0):
    t = np.arange(int(seconds * SR)) / SR
    return (10 ** (db / 20) * np.sin(2 * np.pi * freq * t)).astype("float32")


class Key:
    def __init__(self, name):
        self.name = name


class FakeRecorder:
    def __init__(self, audio=None):
        self.audio, self.started, self.name_part, self.starts = audio, 0.0, None, 0

    def start(self):
        self.starts += 1
        self.started = time.time()

    def stop(self):
        return self.audio if self.audio is not None else np.zeros(0, dtype="float32")


class FakeTranscriber:
    model_name = "fake"

    def __init__(self, texts=("hello there",), gate=None, fail=None):
        self.texts, self.gate, self.fail, self.calls = list(texts), gate, fail, []

    def load(self):
        return 0.01

    def transcribe(self, audio, *, vad_retry=True, on_segment=None):
        self.calls.append(len(audio))
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail:
            raise self.fail
        text = self.texts[min(len(self.calls), len(self.texts)) - 1]
        if on_segment:
            on_segment(0.0, 31.0, text)
        return {"text": text, "seconds": len(audio) / SR, "took_s": 0.05, "words": len(text.split())}


class FakeStore:
    """The four dumps.py calls ptt.py makes, on a folder, with the call order recorded."""

    def __init__(self, root, fail_on=None):
        self.root, self.calls, self.meta, self.fail_on = Path(root), [], {}, fail_on

    def create(self, source="ptt", created=None, duration_s=None):
        if self.fail_on == "create":
            raise OSError("store broken")
        did = "20261015-120000-abcd"
        self.calls.append(("create", source, duration_s))
        (self.root / did).mkdir(parents=True, exist_ok=True)
        self.meta[did] = {"id": did, "status": "recorded", "source": source, "durationSec": duration_s}
        return dict(self.meta[did])

    def path(self, did):
        return self.root / did

    def update(self, did, **patch):
        self.calls.append(("update", patch.get("status")))
        self.meta[did].update(patch)
        return dict(self.meta[did])

    def write_transcript(self, did, text):
        self.calls.append(("write_transcript", text))
        (self.root / did / "transcript.txt").write_text(text, encoding="utf-8")


_MADE = []


def make(home, audio=None, transcriber=None, store=None, **kw):
    p = ptt.PTT(["f9"], "f2", None, None, transcriber or FakeTranscriber(), model_name="fake", beeps=False,
                recorder=FakeRecorder(audio), device_name="Test mic", store=store, **kw)
    _MADE.append(p)
    return p


@pytest.fixture(autouse=True)
def _settle_dump_threads():
    """A dump is transcribed on a thread: let it finish inside its own test (its writes and log lines belong there)."""
    yield
    while _MADE:
        p = _MADE.pop()
        p.stop_event.set()
        for th in p.dump_threads:
            th.join(15)


def wait_for(cond, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def read_json(path, tries=80):
    """Read a JSON file another thread replaces atomically, the way the panel does: on Windows an open can be refused for a
    moment while the replace happens, so retry briefly (like server.read_shared_json)."""
    for _ in range(tries):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (PermissionError, FileNotFoundError, ValueError):
            time.sleep(0.025)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def settle(p):
    for th in p.dump_threads:
        th.join(30)


# ---------- import guards ----------
def test_ptt_imports_without_portaudio_or_a_keyboard_hook():
    code = ("import sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "class NoPortAudio:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'sounddevice':\n"
            "            raise OSError('PortAudio library not found')\n"
            "sys.meta_path.insert(0, NoPortAudio())\n"
            "sys.modules['pynput'] = None\n"
            "import briandump.ptt as p\n"
            "print(p.sd is None, p.keyboard is None, 'PortAudio' in p.SD_ERROR)\n"
            "print('rc', p.main([]))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "True True True" in out.stdout and "rc 3" in out.stdout and "needs sounddevice and pynput" in out.stdout


# ---------- pure pieces ----------
def test_status_dict_has_the_contract_shape():
    d = ptt.status_dict(ready=True, key="f9", dump_key="f2", device="Mic", model="small", ts=0)
    assert set(d) == STATUS_KEYS and d["ts"] == "1970-01-01T00:00:00Z"
    assert d["recording"] is False and d["mode"] is None and d["started"] is None
    d = ptt.status_dict(ready=True, key="f9", dump_key="f2", device="Mic", model="small", recording=True, mode="dump",
                        started="2026-10-15T10:00:00Z", level_db=-30.0, peak_db=-12.5, transcribing="20261015-120000-abcd")
    assert d["mode"] == "dump" and d["started"] and d["level_db"] == -30.0 and d["transcribing"]


def test_level_math():
    lv = ptt.Level()
    assert lv.read() == (-100.0, -100.0)
    lv.push(np.zeros(SR, dtype="float32"))
    assert lv.read()[0] <= -90                                               # silence
    lv.reset()
    lv.push(sine(1.0, db=0.0))
    level, peak = lv.read()
    assert abs(level - (-3.01)) < 0.1 and abs(peak) < 0.1                    # full-scale sine: RMS -3 dBFS, peak 0 dBFS
    lv.push(np.zeros(int(0.25 * SR), dtype="float32"))                       # the meter shows the last 250 ms only
    assert lv.read()[0] <= -90 and abs(lv.read()[1]) < 0.1                   # ... the peak holds for the recording


def test_read_command_once_per_nonce_and_file_removed(home):
    p, seen = home.ptt_command, []
    p.write_text(json.dumps({"cmd": "dump_toggle", "t": time.time(), "nonce": "n1"}), encoding="utf-8")
    assert ptt.read_command(p, seen) == "dump_toggle" and not p.exists()
    p.write_text(json.dumps({"cmd": "dump_toggle", "t": time.time(), "nonce": "n1"}), encoding="utf-8")
    assert ptt.read_command(p, seen) is None and not p.exists()             # same nonce: acted on once
    p.write_text(json.dumps({"cmd": "dump_stop", "t": time.time(), "nonce": "n2"}), encoding="utf-8")
    assert ptt.read_command(p, seen) == "dump_stop"
    assert ptt.read_command(p, seen) is None                                 # no file


def test_read_command_drops_unknown_malformed_and_stale(home):
    p, seen = home.ptt_command, []
    for raw in ('{"cmd": "type_text", "nonce": "x1", "t": 0}', "{not json", "[1]"):
        p.write_text(raw, encoding="utf-8")
        assert ptt.read_command(p, seen) is None and not p.exists()
    p.write_text(json.dumps({"cmd": "dump_start", "t": time.time() - 3600, "nonce": "old"}), encoding="utf-8")
    assert ptt.read_command(p, seen) is None and not p.exists()             # a click from before this service started
    t = time.time()
    p.write_text(json.dumps({"cmd": "dump_start", "t": t}), encoding="utf-8")   # no nonce: keyed by cmd + t
    assert ptt.read_command(p, seen) == "dump_start"
    p.write_text(json.dumps({"cmd": "dump_start", "t": t}), encoding="utf-8")
    assert ptt.read_command(p, seen) is None
    assert not list(p.parent.glob("*.taken"))


def test_write_json_is_atomic_and_never_raises(tmp_path):
    p = tmp_path / "state" / "s.json"
    assert ptt.write_json(p, {"a": 1}) and json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
    assert not list(p.parent.glob("*.tmp"))
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert ptt.write_json(blocker / "sub" / "s.json", {"a": 1}) is False    # a folder that cannot exist: False, no exception
    assert ptt.write_json(p, {"bad": object()}) is False


@pytest.mark.win32
@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses os.replace onto an open file; POSIX does not")
def test_write_json_retries_while_the_panel_reads(tmp_path):
    p = tmp_path / "ptt_status.json"
    p.write_text("{}", encoding="utf-8")
    f = open(p, encoding="utf-8")
    threading.Timer(0.15, f.close).start()
    assert ptt.write_json(p, {"ready": True}, tries=40)
    assert json.loads(p.read_text(encoding="utf-8")) == {"ready": True}


def test_resample_and_normalise():
    x = sine(1.0, db=-6.0)
    up = ptt.resample(np.repeat(x, 3), 48000)
    assert len(up) == SR and up.dtype == np.float32
    assert len(ptt.resample(np.zeros(0), 48000)) == 0
    assert ptt.resample(x, SR) is not None and len(ptt.resample(x, SR)) == SR
    quiet = sine(1.0, db=-30.0)
    assert abs(float(np.abs(ptt.normalise(quiet)).max()) - 0.7) < 1e-3       # a quiet mic is lifted to 0.7
    hiss = (np.ones(SR) * 0.002).astype("float32")
    assert float(np.abs(ptt.normalise(hiss)).max()) <= 0.2 + 1e-6           # gain capped at 100x
    silence = np.zeros(SR, dtype="float32")
    assert not ptt.normalise(silence).any()


def test_key_helpers(monkeypatch):
    assert ptt.key_name(Key("F9")) == "f9" and ptt.key_name(types.SimpleNamespace(char="a")) == "a"
    assert ptt.key_name(object()) == ""
    monkeypatch.setattr(ptt, "keyboard", None)
    assert ptt.key_vk("f9") is None


# ---------- the talk key ----------
def test_talk_clip_lands_in_the_inbox_and_passes_the_routers_cross_check(home):
    p = make(home, audio=sine(1.5, db=-24.0), transcriber=FakeTranscriber(["make the heading blue"]))
    p.on_press(Key("f9"))
    assert p.pressed and p.status()["mode"] == "talk" and p.status()["recording"]
    p.on_release(Key("f9"))
    assert not p.pressed
    path = p.handle(p.clips.get_nowait())
    assert path.read_text(encoding="utf-8") == "make the heading blue\n"
    assert voice.classify(path.name, home.host) == "voice"
    stamp = path.name[:15]
    assert voice.ptt_logged(stamp, home.host, home.inbox, text=path.read_text(encoding="utf-8"))
    entry = json.loads((home.inbox / f"log-{home.host}.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["at"] == stamp and entry["text"] == "make the heading blue" and entry["seconds"] == 1.5
    assert not (home.state / "last_clip.wav").exists()                      # keep_last_clip is off by default


def test_short_quiet_and_empty_clips_are_ignored(home):
    p = make(home, transcriber=FakeTranscriber([""]))
    assert p.handle(sine(0.2)) is None                                       # a slipped finger
    assert p.handle(sine(1.0, db=-60.0)) is None                             # hiss
    assert p.handle(sine(1.0, db=-20.0)) is None                             # nothing recognised
    assert not list(home.inbox.glob("*.txt"))


def test_keep_last_clip_writes_a_wav(home):
    p = make(home, transcriber=FakeTranscriber(["hi"]), keep_last_clip=True)
    p.handle(sine(1.0))
    with wave.open(str(home.state / "last_clip.wav"), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2)


def test_inbox_files_never_collide(home):
    p = make(home)
    t = time.time()
    a = p.write_inbox("one", 1.0, 0.1, t=t)
    b = p.write_inbox("two", 1.0, 0.1, t=t)                                   # the same second: takes the next one
    assert a != b and a.read_text(encoding="utf-8") == "one\n" and b.read_text(encoding="utf-8") == "two\n"
    stamps = [json.loads(ln)["at"] for ln in (home.inbox / f"log-{home.host}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert stamps == [a.name[:15], b.name[:15]]


def test_clips_are_typed_in_the_order_they_were_spoken(home):
    p = make(home, transcriber=FakeTranscriber(["first", "second", "third"]))
    for _ in range(3):
        p.clips.put(sine(1.0))
    p.clips.put(None)
    p.clip_worker()
    files = sorted(home.inbox.glob("*.txt"))
    assert [f.read_text(encoding="utf-8").strip() for f in files] == ["first", "second", "third"]


def test_talk_key_auto_repeat_and_the_dump_key_toggles_once_per_press(home):
    p = make(home, audio=sine(2.0))
    p.on_press(Key("f9"))
    p.on_press(Key("f9"))                                                    # auto-repeat
    assert p.rec.starts == 1
    p.on_release(Key("f9"))
    p.on_press(Key("f2"))
    p.on_press(Key("f2"))                                                    # a held F2 is one press
    assert p.dump_on and p.rec.starts == 2
    p.on_release(Key("f2"))
    p.on_press(Key("a"))                                                     # other keys do nothing
    assert p.dump_on


# ---------- the dump toggle ----------
def test_dump_stop_hands_over_before_returning_and_never_touches_the_inbox(home, tmp_path):
    gate = threading.Event()
    store = FakeStore(tmp_path / "dumps")
    p = make(home, audio=sine(3.0, db=-30.0), transcriber=FakeTranscriber(["a dump about the garden shed"], gate=gate), store=store)
    p.start_dump("key")
    st = p.status()
    assert p.dump_on and st["recording"] and st["mode"] == "dump" and st["started"] and st["level_db"] is not None
    p.on_press(Key("f9"))                                                    # one microphone: the talk key is refused
    assert not p.pressed
    did = p.stop_dump("key")
    assert did == "20261015-120000-abcd" and not p.dump_on
    assert store.meta[did]["status"] == "transcribing"                       # before stop_dump returned
    assert [c[0] for c in store.calls] == ["create", "update"] and store.calls[1][1] == "transcribing"
    assert read_json(home.ptt_status)["transcribing"] == did
    with wave.open(str(store.path(did) / "audio.wav"), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2) and abs(w.getnframes() - 3 * SR) < 10
        peak = np.abs(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")).max() / 32767
    assert abs(peak - 0.7) < 0.01                                            # the quiet recording was normalised
    assert not list(home.dumps_dir.glob(".incoming-*")) and not list((tmp_path / "dumps").glob(".incoming-*"))
    gate.set()
    assert wait_for(lambda: store.meta[did]["status"] == "transcribed")
    meta = store.meta[did]
    assert meta["words"] == 6 and meta["timings"] == {"transcribe_s": 0.05} and meta["durationSec"] == 3.0
    assert (store.path(did) / "transcript.txt").read_text(encoding="utf-8") == "a dump about the garden shed"
    assert wait_for(lambda: p.transcribing is None)
    assert not list(home.inbox.glob("*"))                                    # a dump transcript never goes to the inbox


def test_a_short_dump_is_discarded(home, tmp_path):
    store = FakeStore(tmp_path / "dumps")
    p = make(home, audio=sine(0.5), store=store)
    p.start_dump()
    assert p.stop_dump() is None and store.calls == []


def test_a_failing_store_never_loses_the_recording(home):
    p = make(home, audio=sine(2.0), transcriber=FakeTranscriber(["kept anyway"]), store=FakeStore(home.dumps_dir, fail_on="create"))
    p.start_dump()
    did = p.stop_dump()
    assert did and re.match(r"^\d{8}-\d{6}-[0-9a-f]{4}$", did)
    folder = home.dumps_dir / did
    assert (folder / "audio.wav").exists()
    settle(p)
    meta = read_json(folder / "meta.json")
    assert meta["status"] == "transcribed"
    assert set(meta) >= META_KEYS and meta["source"] == "ptt" and meta["words"] == 2
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "kept anyway\n"


def test_transcription_errors_and_silence_mark_the_dump_as_error(tmp_path):
    did = "20261015-120000-abcd"
    for transcriber, why in ((FakeTranscriber(fail=RuntimeError("model gone")), "model gone"), (FakeTranscriber([""]), "no speech")):
        store = FakeStore(tmp_path / why.replace(" ", "-"))
        store.create()
        assert ptt.transcribe_dump(did, sine(1.0), store, transcriber, log=lambda m: None) is None
        assert store.meta[did]["status"] == "error" and why in store.meta[did]["error"]
        assert not (store.path(did) / "transcript.txt").exists()
    broken = FakeStore(tmp_path / "broken")                                  # an update that fails is swallowed, not raised
    assert ptt.transcribe_dump(did, sine(1.0), broken, FakeTranscriber(fail=RuntimeError("x")), log=lambda m: None) is None


def test_transcribe_dump_reports_progress(tmp_path):
    store = FakeStore(tmp_path)
    store.create()
    lines = []
    ptt.transcribe_dump("20261015-120000-abcd", sine(40.0), store, FakeTranscriber(["long one"]), log=lines.append)
    assert any("31/40 s" in ln for ln in lines) and any("transcribed" in ln for ln in lines)


def test_command_file_toggles_the_dump(home, tmp_path):
    store = FakeStore(tmp_path / "dumps")
    p = make(home, audio=sine(2.0), store=store)
    home.ptt_command.write_text(json.dumps({"cmd": "dump_toggle", "t": time.time(), "nonce": "a1"}), encoding="utf-8")
    assert p.poll_command() == "dump_toggle" and p.dump_on
    home.ptt_command.write_text(json.dumps({"cmd": "dump_start", "t": time.time(), "nonce": "a2"}), encoding="utf-8")
    p.poll_command()
    assert p.dump_on and p.rec.starts == 1                                   # start while on: nothing new
    home.ptt_command.write_text(json.dumps({"cmd": "dump_toggle", "t": time.time(), "nonce": "a3"}), encoding="utf-8")
    p.poll_command()
    assert not p.dump_on and store.calls and store.calls[0][0] == "create"


def test_max_minutes_stops_the_dump(home, tmp_path):
    store = FakeStore(tmp_path / "dumps")
    p = make(home, audio=sine(2.0), store=store, max_minutes=1)
    p.start_dump()
    p.rec.started = 1_000.0                                                  # a fake clock: tick(now) decides
    p.tick(1_000.0 + 59)
    assert p.dump_on and not store.calls
    p.tick(1_000.0 + 61)
    assert not p.dump_on and store.calls[0][0] == "create"
    settle(p)


def test_tick_polls_the_command_file_and_paces_the_status(home, tmp_path, monkeypatch):
    p = make(home, audio=sine(2.0), store=FakeStore(tmp_path / "dumps"))
    writes = []
    monkeypatch.setattr(p, "write_status", lambda: writes.append(p.recording))
    p.tick(100.0)
    p.tick(100.1)                                                            # idle: one status write per 2 s
    assert writes == [False]
    home.ptt_command.write_text(json.dumps({"cmd": "dump_toggle", "t": time.time(), "nonce": "k1"}), encoding="utf-8")
    p.tick(100.4)
    assert p.dump_on and not home.ptt_command.exists()
    p.tick(102.5)
    p.tick(102.6)
    p.tick(102.8)                                                            # recording: every 0.25 s
    assert writes[-2:] == [True, True] and len(writes) >= 3
    p.stop_dump()
    settle(p)


def test_a_stale_command_is_logged_and_never_starts_a_recording(home, monkeypatch):
    lines = []
    monkeypatch.setattr(ptt, "log", lines.append)
    p = make(home, audio=sine(2.0))
    now = time.time()
    home.ptt_command.write_text(json.dumps({"cmd": "dump_toggle", "t": now - 11, "nonce": "late"}), encoding="utf-8")
    assert p.poll_command(now) is None and not p.dump_on and not home.ptt_command.exists()
    assert any("ignored" in ln and "dump_toggle" in ln for ln in lines)
    home.ptt_command.write_text(json.dumps({"cmd": "dump_toggle", "t": now - 5, "nonce": "fresh"}), encoding="utf-8")
    assert p.poll_command(now) == "dump_toggle" and p.dump_on                # within COMMAND_MAX_AGE_S: acted on


def test_status_file_and_shutdown(home, tmp_path):
    p = make(home, audio=sine(2.0), store=FakeStore(tmp_path / "dumps"))
    p.ready = True
    assert p.write_status()
    st = read_json(home.ptt_status)
    assert set(st) == STATUS_KEYS and st["ready"] and st["key"] == "f9" and st["dump_key"] == "f2" and st["device"] == "Test mic"
    p.start_dump()
    p.shutdown()                                                             # a dump that is on is saved, not lost
    settle(p)
    st = read_json(home.ptt_status)
    assert st["ready"] is False and st["recording"] is False and p.store.calls[0][0] == "create"


def test_wait_ready_keeps_the_status_alive_while_the_model_loads(home):
    p = make(home)
    released = threading.Event()

    def slow_load():
        assert released.wait(10)
        return 1.25
    threading.Timer(0.3, released.set).start()
    assert p.wait_ready(slow_load) == (True, 1.25)
    assert read_json(home.ptt_status)["ready"] is False
    ok, err = p.wait_ready(lambda: (_ for _ in ()).throw(OSError("no model")))
    assert not ok and "no model" in err


def test_ministore_meta_shape_and_merge(tmp_path):
    s = ptt.MiniStore(tmp_path)
    meta = s.create(source="ptt", created=1_792_080_000.0, duration_s=12.5)
    assert set(meta) == META_KEYS and meta["status"] == "recorded" and re.match(r"^\d{8}-\d{6}-[0-9a-f]{4}$", meta["id"])
    m = s.update(meta["id"], status="transcribed", timings={"transcribe_s": 3.0}, words=40)
    assert m["timings"] == {"transcribe_s": 3.0, "think_s": None} and m["words"] == 40
    s.write_transcript(meta["id"], "  words  ")
    assert (tmp_path / meta["id"] / "transcript.txt").read_text(encoding="utf-8") == "words\n"


def test_dump_store_falls_back_when_dumps_cannot_import(monkeypatch):
    import briandump
    monkeypatch.setitem(sys.modules, "briandump.dumps", None)                  # the import fails ...
    monkeypatch.delattr(briandump, "dumps", raising=False)                      # ... even when an earlier test imported it
    assert isinstance(ptt.dump_store(), ptt.MiniStore)


@pytest.mark.win32
@pytest.mark.skipif(sys.platform != "win32", reason="named mutex")
def test_single_instance_per_home(tmp_path):
    assert ptt.single_instance(tmp_path / "home-a")
    assert not ptt.single_instance(tmp_path / "home-a")                     # a second recorder for the same home
    assert ptt.single_instance(tmp_path / "home-b")


# ---------- list_mics.py ----------
def fake_sounddevice(default_input=1):
    devs = [{"name": "Microphone (USB Audio)", "max_input_channels": 1, "hostapi": 0},           # MME (truncated names)
            {"name": "Microphone (USB Audio)", "max_input_channels": 1, "hostapi": 1},           # WASAPI
            {"name": "Headset Microphone (Wireless Headset)", "max_input_channels": 1, "hostapi": 1},
            {"name": "Headset Microphone (Wireless Headset)", "max_input_channels": 1, "hostapi": 1},   # duplicate
            {"name": "Speakers (USB Audio)", "max_input_channels": 0, "hostapi": 1}]
    apis = [{"name": "MME", "default_input_device": 0}, {"name": "Windows WASAPI", "default_input_device": default_input}]

    def query_devices(device=None, kind=None):
        return devs[0] if kind == "input" else devs
    return types.SimpleNamespace(query_devices=query_devices, query_hostapis=lambda: apis,
                                 default=types.SimpleNamespace(device=(0, 4)))


def test_list_mics_wasapi_names_deduplicated_one_default(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice())
    rows = list_mics.devices()
    assert [r["name"] for r in rows] == ["Microphone (USB Audio)", "Headset Microphone (Wireless Headset)"]
    assert [r["default"] for r in rows] == [True, False] and rows[0]["index"] == 1


def test_list_mics_without_portaudio(monkeypatch, capsys):
    class NoPortAudio:
        def find_spec(self, name, path=None, target=None):
            if name == "sounddevice":
                raise OSError("PortAudio library not found")
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setattr(sys, "meta_path", [NoPortAudio(), *sys.meta_path])
    assert list_mics.devices() == []
    assert list_mics.main(["--json"]) == 1 and capsys.readouterr().out.strip() == "[]"


def test_list_mics_prints_one_line_per_mic(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice())
    assert list_mics.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["  1  Microphone (USB Audio)   (default)", "  2  Headset Microphone (Wireless Headset)"]
    assert list_mics.main(["--json"]) == 0 and json.loads(capsys.readouterr().out)[0]["default"] is True


# ---------- ptt_service.pyw ----------
def test_service_wrapper_logs_to_the_state_folder(home):
    """The logon task runs ptt_service.pyw hidden: whatever happens goes to <state_dir>/logs/ptt-<host>.log."""
    p = subprocess.run([sys.executable, str(REPO / "briandump" / "ptt_service.pyw"), "--no-such-flag"], capture_output=True,
                       timeout=60)
    assert p.returncode == 2 and not p.stdout                               # argparse refused the flag; nothing on a console
    log = (home.logs / f"ptt-{home.host}.log").read_text(encoding="utf-8")
    assert "=== BrianDump PTT start" in log and "--no-such-flag" in log
