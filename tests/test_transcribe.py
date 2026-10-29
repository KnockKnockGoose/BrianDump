"""Tests for briandump/transcribe.py: the Transcriber around faster-whisper with a fake WhisperModel injected (nothing is
downloaded, no model runs), plus the audio helpers (resample, normalise, levels, wav round trip)."""
import threading
import time
import types

import pytest

np = pytest.importorskip("numpy")

from briandump import transcribe as T  # noqa: E402

SR = T.SAMPLE_RATE


def seg(text, start=0.0, end=2.0):
    return types.SimpleNamespace(text=text, start=start, end=end)


class FakeModel:
    """Stands in for faster_whisper.WhisperModel: records how it was built and every transcribe() call."""
    built = []

    def __init__(self, name, device="cpu", compute_type="int8", script=None):
        FakeModel.built.append((name, device, compute_type))
        self.calls, self.script = [], script or {}

    def transcribe(self, src, **kw):
        self.calls.append({"src": src, **kw})
        texts = self.script.get(kw.get("vad_filter"), [" hello world "])
        return (seg(t, i * 2.0, i * 2.0 + 2.0) for i, t in enumerate(texts)), types.SimpleNamespace(duration=7.5)


def make(**kw):
    script = kw.pop("script", None)
    holder = {}

    def cls(name, device="cpu", compute_type="int8"):
        holder["model"] = FakeModel(name, device, compute_type, script=script)
        return holder["model"]
    t = T.Transcriber(model_cls=cls, **kw)
    return t, holder


def test_load_is_lazy_cached_and_timed():
    FakeModel.built.clear()
    t, holder = make(model="small")
    assert not t.loaded and FakeModel.built == []                           # constructing is free
    s = t.load()
    assert isinstance(s, float) and t.loaded and FakeModel.built == [("small", "cpu", "int8")]
    assert t.load() == s and len(FakeModel.built) == 1                      # loaded once


def test_vad_on_first_then_off_when_empty():
    t, holder = make(script={True: [], False: ["quiet words"]})
    r = t.transcribe(np.zeros(SR * 2, dtype="float32"))
    assert r["text"] == "quiet words" and r["words"] == 2
    assert [c["vad_filter"] for c in holder["model"].calls] == [True, False]


def test_vad_retry_can_be_switched_off_and_found_text_stops_early():
    t, holder = make(script={True: [], False: ["never asked"]})
    assert t.transcribe(np.zeros(SR, dtype="float32"), vad_retry=False)["text"] == ""
    assert [c["vad_filter"] for c in holder["model"].calls] == [True]
    t2, holder2 = make()
    t2.transcribe(np.zeros(SR, dtype="float32"))
    assert [c["vad_filter"] for c in holder2["model"].calls] == [True]     # text on the first pass: no second pass


def test_hotwords_go_in_as_the_initial_prompt():
    t, holder = make(hotwords=("Claude", "Claude Code", "  ", "BrianDump."))
    t.transcribe(np.zeros(SR, dtype="float32"))
    call = holder["model"].calls[0]
    assert call["initial_prompt"] == "Claude, Claude Code, BrianDump."
    assert call["beam_size"] == 5 and call["language"] == "en"
    assert make(hotwords="Claude")[0].prompt == "Claude." and make(hotwords=())[0].prompt is None


def test_language_auto_and_beam_size():
    t, holder = make(language="auto", beam_size=2)
    t.transcribe(np.zeros(SR, dtype="float32"))
    assert holder["model"].calls[0]["language"] is None and holder["model"].calls[0]["beam_size"] == 2
    assert make(language=None)[0].language is None and make(language="de")[0].language == "de"


def test_array_input_is_float32_16k_and_timed_by_length():
    t, holder = make()
    r = t.transcribe([0.0] * (SR * 3))                                       # a plain list is accepted too
    src = holder["model"].calls[0]["src"]
    assert isinstance(src, np.ndarray) and src.dtype == np.float32 and src.flags["C_CONTIGUOUS"] and src.ndim == 1
    assert r == {"text": "hello world", "seconds": 3.0, "took_s": r["took_s"], "words": 2} and r["took_s"] >= 0


def test_file_input_goes_to_the_model_as_a_path(tmp_path):
    t, holder = make()
    p = tmp_path / "clip.m4a"
    p.write_bytes(b"not really audio")
    r = t.transcribe(p)
    assert holder["model"].calls[0]["src"] == str(p) and r["seconds"] == 7.5   # duration from the decoder's info
    assert t.transcribe(str(p))["seconds"] == 7.5


def test_on_segment_progress_and_a_broken_callback_is_ignored():
    t, _ = make(script={True: ["one", "two", "three"]})
    seen = []
    r = t.transcribe(np.zeros(SR, dtype="float32"), on_segment=lambda s, e, txt: seen.append((s, e, txt)))
    assert seen == [(0.0, 2.0, "one"), (2.0, 4.0, "two"), (4.0, 6.0, "three")] and r["text"] == "one two three"
    t.transcribe(np.zeros(SR, dtype="float32"), on_segment=lambda *a: 1 / 0)


def test_concurrent_calls_overlap_instead_of_queueing_behind_a_long_one():
    """A talk clip during a long dump transcription: only load() is locked, so the model sees both calls at once."""
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class SlowModel(FakeModel):
        def transcribe(self, src, **kw):
            def gen():
                with lock:
                    state["now"] += 1
                    state["max"] = max(state["max"], state["now"])
                time.sleep(0.3)
                with lock:
                    state["now"] -= 1
                yield seg("x")
            return gen(), types.SimpleNamespace(duration=1.0)

    t = T.Transcriber(model_cls=lambda *a, **k: SlowModel(*a, **k))
    t.load()
    threads = [threading.Thread(target=t.transcribe, args=(np.zeros(SR, dtype="float32"),)) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert state["max"] == 2


def test_levels_resample_normalise_and_wav_round_trip(tmp_path):
    t = np.arange(SR) / SR
    full = np.sin(2 * np.pi * 440 * t).astype("float32")
    assert abs(T.rms_dbfs(full) + 3.01) < 0.05 and abs(T.peak_dbfs(full)) < 0.01
    assert T.rms_dbfs(np.zeros(10)) == -100.0 and T.peak_dbfs(np.zeros(0)) == -100.0
    assert len(T.resample(np.zeros(44100), 44100)) == SR and len(T.resample(np.zeros(8000), 8000)) == SR
    ramp = np.linspace(-1, 1, 48000).astype("float32")
    r = T.resample(ramp, 48000)
    assert len(r) == SR and abs(float(r[0]) + 1) < 1e-6 and float(r[-1]) > 0.99   # linear interpolation keeps the shape
    assert abs(float(np.abs(T.normalise(full * 0.01)).max()) - 0.7) < 1e-3
    p = tmp_path / "x" / "a.wav"
    T.wav_write(p, full * 0.5)
    back, seconds = T.wav_read(p)
    assert seconds == 1.0 and np.allclose(back, full * 0.5, atol=1 / 16000)
    T.wav_write(tmp_path / "loud.wav", full * 3.0)                           # clipped, not wrapped around
    loud, _ = T.wav_read(tmp_path / "loud.wav")
    assert float(loud.max()) <= 1.0 and float(loud.min()) >= -1.0


def test_wav_read_stereo_and_other_rates(tmp_path):
    import wave
    p = tmp_path / "st.wav"
    frames = (np.column_stack([np.full(48000, 0.5), np.full(48000, -0.5)]) * 32767).astype("<i2")
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(frames.tobytes())
    x, seconds = T.wav_read(p)
    assert seconds == 1.0 and len(x) == SR and abs(float(x.mean())) < 1e-3   # channels averaged
