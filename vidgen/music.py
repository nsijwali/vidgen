"""Original soft koto-style background music (no licence or credit needed).

A seeded walk over a Japanese pentatonic scale: plucked strings, a low drone,
a faint breath swell and reverb. Deterministic for a given seed.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SCALES = {
    "hirajoshi": [0, 2, 3, 7, 8],
    "in": [0, 1, 5, 7, 8],
    "yo": [0, 2, 5, 7, 9],
    "iwato": [0, 1, 5, 6, 10],
}
NOTE_OFFSETS = {"C": 0, "C#": 1, "D": 2, "Eb": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "Ab": 8, "A": 9, "Bb": 10, "B": 11}
SR = 44100


def _midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _pluck(freq: float, dur: float, rng: np.random.Generator) -> np.ndarray:
    """Karplus-Strong string, computed one period at a time."""
    period = max(2, int(round(SR / freq)))
    n = int(SR * dur)
    buf = np.convolve(rng.uniform(-1, 1, period), np.ones(3) / 3, "same")
    blocks = n // period + 1
    out = np.empty(blocks * period)
    for b in range(blocks):
        out[b * period:(b + 1) * period] = buf
        buf = 0.498 * (buf + np.roll(buf, -1))
    out = out[:n]
    return out * np.exp(-np.arange(n) / SR * 1.2)


def _pad(freq: float, dur: float, amp: float) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    s = sum(np.sin(2 * np.pi * freq * k * t + k) * a for k, a in [(1, 1.0), (2, 0.25), (3, 0.08)])
    env = np.minimum(1, t / 2.0) * np.minimum(1, np.maximum(dur - t, 0) / 2.5)
    return s * env * amp


def compose(duration: float, bpm: float = 72, root: str = "D", scale: str = "hirajoshi",
            seed: int = 7, density: float = 0.7) -> np.ndarray:
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose from {', '.join(SCALES)}")
    if root not in NOTE_OFFSETS:
        raise ValueError(f"unknown root {root!r}; choose from {', '.join(NOTE_OFFSETS)}")
    rng = np.random.default_rng(seed)
    root_midi = 60 + NOTE_OFFSETS[root]  # octave above middle C region
    pool = [root_midi + 12 * o + s for o in (0, 1) for s in SCALES[scale]] + [root_midi + 24]
    beat = 60.0 / bpm
    mix = np.zeros(int(SR * (duration + 4)))

    def add(x: np.ndarray, t: float) -> None:
        i = int(t * SR)
        mix[i:i + len(x)] += x[: len(mix) - i]

    idx = 2
    t = 0.0
    last_note_t = max(0.0, duration - 3.5)
    while t < last_note_t:
        if t == 0.0 or rng.random() < density:
            add(_pluck(_midi_hz(pool[idx]), 3.5, rng) * 0.55, t)
            if rng.random() < 0.18 and t + beat * 0.5 < last_note_t:  # light grace note
                add(_pluck(_midi_hz(pool[min(idx + 1, len(pool) - 1)]), 2.5, rng) * 0.3, t + beat * 0.5)
        idx = int(np.clip(idx + rng.choice([-2, -1, -1, 0, 1, 1, 2]), 0, len(pool) - 1))
        t += beat * 2
    add(_pluck(_midi_hz(pool[0]), 4.0, rng) * 0.6, last_note_t)  # resolve on the tonic

    add(_pad(_midi_hz(root_midi - 12), duration + 2, 0.10), 0)
    add(_pad(_midi_hz(root_midi - 5), duration + 2, 0.06), 0)
    tt = np.arange(int(SR * duration)) / SR
    air = np.convolve(rng.normal(0, 1, len(tt)), np.ones(60) / 60, "same")
    mix[: len(tt)] += air * 0.075 * np.sin(np.pi * tt / duration) ** 2

    ir_n = int(SR * 2.2)
    ir = rng.normal(0, 1, ir_n) * np.exp(-np.arange(ir_n) / SR * 2.6)
    ir[0] = 0
    size = len(mix) + ir_n
    wet = np.fft.irfft(np.fft.rfft(mix, size) * np.fft.rfft(ir, size), size)[: len(mix)]
    wet /= np.abs(wet).max() + 1e-9
    out = mix / (np.abs(mix).max() + 1e-9) * 0.7 + wet * 0.45
    out /= np.abs(out).max() + 1e-9
    return (out * 0.8)[: int(SR * duration)].astype(np.float32)


def write_wav(path: Path, samples: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return path
