"""The one speech backend: faster-whisper behind a small class, plus the audio helpers the recorder needs.

    from briandump.transcribe import Transcriber
    t = Transcriber("small", hotwords=("Claude", "Claude Code"))
    t.load()                                   # seconds; lazy, so constructing is free
    t.transcribe(audio_16k)["text"]            # a float32 array at 16 kHz, or a path (faster-whisper decodes files itself)

Why a class: ptt.py keeps ONE model loaded for the whole session (loading takes seconds, a clip then takes 1-3 s), and the
server's dump worker loads one lazily only when it has to transcribe an imported or orphaned dump. Both go through the same
code, so a fix lands in both places.

Why the VAD retry: faster-whisper's voice-activity filter drops quiet speech entirely now and then (a soft first word, a
far mic). The first pass runs with VAD on (it removes hiss that makes Whisper invent words); an empty result is retried once
with VAD off. Hotwords go in as `initial_prompt`: without them "Claude" tends to come out as "cod".

`resample` and `normalise` are plain numpy so tests cover them without a microphone or a model.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000                 # what Whisper wants; microphones are captured at their native rate and resampled
TARGET_PEAK = 0.7                   # normalise() scales the clip so its loudest sample sits here
MAX_GAIN = 100.0                    # ... but never more than this (a near-silent clip is hiss, not speech)


def resample(audio, src_rate, dst_rate=SAMPLE_RATE):
    """Linear interpolation to dst_rate (good enough for speech; no scipy dependency). Always float32, 1-D."""
    audio = np.asarray(audio, dtype="float32").reshape(-1)
    if int(src_rate) == int(dst_rate) or len(audio) == 0:
        return audio
    n_out = int(round(len(audio) * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype="float32")
    x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype("float32")


def peak_dbfs(audio):
    """Peak level in dBFS (0 = full scale); -100 for silence."""
    audio = np.asarray(audio, dtype="float32")
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    return float(20 * np.log10(max(peak, 1e-5)))


def rms_dbfs(audio):
    """RMS level in dBFS; a full-scale sine reads -3, silence -100."""
    audio = np.asarray(audio, dtype="float32")
    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    return float(20 * np.log10(max(rms, 1e-5)))


def normalise(audio, target=TARGET_PEAK, max_gain=MAX_GAIN):
    """Scale so the loudest sample is `target` (a quiet jack mic peaks around -30 dBFS). Gain is capped; silence stays silence."""
    audio = np.asarray(audio, dtype="float32")
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak <= 0.001:
        return audio
    return (audio * min(target / peak, max_gain)).astype("float32")


def wav_write(path, audio, rate=SAMPLE_RATE):
    """16-bit mono PCM; the dump store's audio.wav and the optional last_clip.wav use this."""
    import wave
    audio = np.asarray(audio, dtype="float32")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def wav_read(path):
    """-> (float32 mono array at 16 kHz, seconds). Only PCM wav; other formats go to faster-whisper as a path."""
    import wave
    with wave.open(str(path), "rb") as w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype("float32") / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype="u1").astype("float32") - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported wav sample width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    x = resample(x, rate)
    return x, len(x) / SAMPLE_RATE


class Transcriber:
    """One faster-whisper model, loaded on first use, with the VAD retry and the hotword prompt built in.

    model_cls lets tests inject a fake WhisperModel (nothing is downloaded). Only loading is locked: WhisperModel.transcribe
    keeps no per-call state and CTranslate2 queues concurrent calls itself (faster-whisper documents calls from several
    threads), so an F9 clip that arrives while a long dump is being transcribed waits for one ~30 s window of the dump, not
    for all of it. language=None (or "auto") lets Whisper detect the language."""

    def __init__(self, model="small", hotwords=(), device="cpu", compute_type="int8", language="en", beam_size=5, model_cls=None):
        self.model_name, self.device, self.compute_type = str(model), device, compute_type
        self.language = None if str(language or "auto").lower() == "auto" else str(language)
        self.beam_size = int(beam_size)
        words = [hotwords] if isinstance(hotwords, str) else [str(w) for w in (hotwords or ()) if str(w).strip()]
        self.prompt = (", ".join(w.strip().rstrip(".") for w in words) + ".") if words else None
        self._cls, self._model, self.load_s = model_cls, None, None
        self.lock = threading.Lock()                     # guards load() only

    @property
    def loaded(self):
        return self._model is not None

    def load(self):
        """Load the model (once) and return how many seconds it took; later calls return the cached figure."""
        with self.lock:
            if self._model is None:
                cls = self._cls
                if cls is None:
                    from faster_whisper import WhisperModel  # imported here: a missing package shows up where it is used
                    cls = WhisperModel
                t0 = time.time()
                self._model = cls(self.model_name, device=self.device, compute_type=self.compute_type)
                self.load_s = round(time.time() - t0, 2)
        return self.load_s

    def transcribe(self, audio, *, vad_retry=True, on_segment=None):
        """-> {"text", "seconds", "took_s", "words"}. `audio` is a float32 array at 16 kHz or a file path. `on_segment(start,
        end, text)` is called as segments arrive (progress for a long dump)."""
        self.load()
        if isinstance(audio, (str, Path)):
            src, array_s = str(audio), None
        else:
            src = np.ascontiguousarray(np.asarray(audio, dtype="float32").reshape(-1))
            array_s = len(src) / SAMPLE_RATE
        t0 = time.time()
        text, seconds = "", array_s or 0.0
        for vad in (True, False) if vad_retry else (True,):
            segments, info = self._model.transcribe(src, beam_size=self.beam_size, language=self.language, vad_filter=vad,
                                                    initial_prompt=self.prompt)
            parts = []
            for s in segments:                   # a generator: the work happens here
                t = (getattr(s, "text", "") or "").strip()
                if t:
                    parts.append(t)
                if on_segment:
                    try:
                        on_segment(float(getattr(s, "start", 0) or 0), float(getattr(s, "end", 0) or 0), t)
                    except Exception:
                        pass
            text = " ".join(parts).strip()
            if array_s is None:
                seconds = float(getattr(info, "duration", 0) or 0) or seconds
            if text:
                break
        return {"text": text, "seconds": round(float(seconds), 2), "took_s": round(time.time() - t0, 2), "words": len(text.split())}
