#!/usr/bin/env python3
"""Synthesize the "take a break" sound: footsteps, a cup set down, coffee pouring,
two gulps and a satisfied "aaah". Writes sounds/take-a-break.wav next to this file.

Everything is generated from noise and sine waves, so there are no recordings to license.
"""
import sys
import wave
from pathlib import Path

import numpy as np

SR = 44100
rng = np.random.default_rng(7)


def silence(sec):
    return np.zeros(int(SR * sec))


def t_axis(sec):
    return np.arange(int(SR * sec)) / SR


def spectral_shape(x, gain_fn):
    """Filter x by multiplying its spectrum with gain_fn(freqs)."""
    n = len(x)
    pad = 1 << int(np.ceil(np.log2(n * 2)))
    spec = np.fft.rfft(x, pad)
    freqs = np.fft.rfftfreq(pad, 1 / SR)
    return np.fft.irfft(spec * gain_fn(freqs), pad)[:n]


def lowpass(x, fc):
    return spectral_shape(x, lambda f: 1 / np.sqrt(1 + (f / fc) ** 4))


def bandpass(x, lo, hi):
    return spectral_shape(x, lambda f: 1 / np.sqrt(1 + (lo / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / hi) ** 4))


def resonance(f, f0, bw):
    return 1 / (1 + ((f - f0) / (bw / 2)) ** 2)


def place(track, clip, at):
    i = int(SR * at)
    end = min(len(track), i + len(clip))
    track[i:end] += clip[: end - i]


# ---------------------------------------------------------------- footsteps

def footstep(loud):
    t = t_axis(0.22)
    heel_noise = lowpass(rng.standard_normal(len(t)), 350) * np.exp(-t / 0.018)
    heel_body = np.sin(2 * np.pi * 68 * t) * np.exp(-t / 0.045)
    heel = 0.9 * heel_noise / np.max(np.abs(heel_noise)) + 0.7 * heel_body
    toe_t = t_axis(0.08)
    toe = bandpass(rng.standard_normal(len(toe_t)), 900, 3500) * np.exp(-toe_t / 0.012)
    toe = 0.25 * toe / np.max(np.abs(toe))
    step = np.zeros(len(t))
    step += heel
    place(step, toe, 0.075)
    return loud * step


def footsteps(n=6, gap=0.5):
    out = silence(n * gap + 0.3)
    for i in range(n):
        loud = 0.35 + 0.55 * i / (n - 1)  # walking up to the coffee
        jitter = rng.uniform(-0.02, 0.02)
        place(out, footstep(loud), i * gap + jitter + 0.02)
    return out


# ---------------------------------------------------------------- cup and pour

def cup_down():
    t = t_axis(0.5)
    partials = [(2140, 1.0, 0.09), (3470, 0.55, 0.06), (5120, 0.3, 0.04), (780, 0.35, 0.05)]
    clink = sum(a * np.sin(2 * np.pi * f * t) * np.exp(-t / d) for f, a, d in partials)
    thud = lowpass(rng.standard_normal(len(t)), 500) * np.exp(-t / 0.01)
    return 0.22 * clink / np.max(np.abs(clink)) + 0.25 * thud / np.max(np.abs(thud))


def bubble(freq, decay, rise=0.15):
    """One small air bubble in liquid: a short sine whose pitch rises slightly as it decays."""
    bt = t_axis(decay * 5)
    f = freq * (1 + rise * bt / bt[-1])
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-bt / decay) * np.minimum(1, bt / 0.0015)


def pour(sec=3.2):
    """Liquid built from many small bubbles, through a cup resonance that rises as it fills."""
    n = int(SR * sec)
    t = t_axis(sec)
    dense = np.zeros(n + SR)
    # Bubble pitch rises a little as the cup fills; most energy stays low (180-800 Hz).
    for _ in range(int(sec * 170)):
        at = rng.uniform(0.0, sec - 0.05)
        p = at / sec
        freq = rng.uniform(180, 520) * (1 + 0.55 * p) * rng.choice([1, 1, 1, 1.6])
        place(dense, rng.uniform(0.2, 1.0) * bubble(freq, rng.uniform(0.006, 0.022)), at)
    dense = dense[:n]
    # A low splashing bed, kept quiet so it doesn't turn into hiss.
    bed = lowpass(rng.standard_normal(n), 900)
    mix = dense / np.max(np.abs(dense)) + 0.18 * bed / np.max(np.abs(bed))
    # Cup cavity: resonance from ~190 Hz rising to ~620 Hz as it fills.
    block, hop = 2048, 512
    win = np.hanning(block)
    out = np.zeros(n + block)
    freqs = np.fft.rfftfreq(block, 1 / SR)
    padded = np.concatenate([mix, np.zeros(block)])
    for start in range(0, n, hop):
        f0 = 190 + 430 * (start / n) ** 1.2
        gain = (0.35 + 1.2 * resonance(freqs, f0, 90) + 0.5 * resonance(freqs, f0 * 2, 160)) / (1 + (freqs / 1800) ** 2)
        seg = np.fft.rfft(padded[start:start + block] * win)
        out[start:start + block] += np.fft.irfft(seg * gain, block) * win
    out = out[:n]
    out *= np.minimum(1, t / 0.12) * np.minimum(1, (sec - t) / 0.3)
    return 0.8 * out / np.max(np.abs(out))


# ---------------------------------------------------------------- gulp

def gulp():
    t = t_axis(0.26)
    f = 270 * np.exp(-t / 0.08) + 95
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR)
    env = np.minimum(1, t / 0.012) * np.exp(-t / 0.07)
    body = lowpass(tone * env, 700)
    click_t = t_axis(0.02)
    click = bandpass(rng.standard_normal(len(click_t)), 600, 2500) * np.exp(-click_t / 0.004)
    out = 0.9 * body / np.max(np.abs(body))
    place(out, 0.3 * click / np.max(np.abs(click)), 0.0)
    return 0.7 * out


# ---------------------------------------------------------------- "aaah"

FORMANTS_A = [(730, 90, 1.0), (1090, 110, 0.5), (2440, 170, 0.25), (3400, 250, 0.08)]


def formant_gain(f):
    return sum(a * resonance(f, fc, bw) for fc, bw, a in FORMANTS_A) + 0.02


def aaah(sec=0.75):
    """One quick, plain "aaah": steady pitch with a slight fall, no vibrato, fast decay."""
    t = t_axis(sec)
    f0 = 128 - 14 * t / sec
    phase = 2 * np.pi * np.cumsum(f0) / SR
    voiced = np.zeros(len(t))
    for k in range(1, 50):
        fk = k * f0
        amp = np.where(fk < 4500, formant_gain(fk) / k ** 1.1, 0.0)
        voiced += amp * np.sin(k * phase)
    voiced /= np.max(np.abs(voiced))
    breath = spectral_shape(rng.standard_normal(len(t)), formant_gain)
    breath /= np.max(np.abs(breath))
    v_env = np.minimum(1, t / 0.03) * np.exp(-t / 0.22)
    b_env = 0.12 * np.exp(-t / 0.05) + 0.05 * np.exp(-t / 0.3)
    out = voiced * v_env + breath * b_env
    out *= np.minimum(1, (sec - t) / 0.08)
    return 0.8 * out / np.max(np.abs(out))


# ---------------------------------------------------------------- assemble

def build():
    parts = [
        (silence(0.35), 0),          # lets Bluetooth audio wake up
        (footsteps(), 0),
        (cup_down(), -0.05),
        (silence(0.15), 0),
        (pour(), 0),
        (silence(0.55), 0),
        (gulp(), 0),
        (silence(0.28), 0),
        (gulp(), 0),
        (silence(0.35), 0),
        (aaah(), 0),
        (silence(0.3), 0),
    ]
    total = sum(len(p) for p, _ in parts) + SR
    track = np.zeros(total)
    pos = 0.0
    for clip, overlap in parts:
        pos += overlap
        place(track, clip, pos)
        pos += len(clip) / SR
    track = track[: int(SR * pos)]
    track *= 0.89 / np.max(np.abs(track))
    return track


def write(path, x):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "sounds" / "take-a-break.wav"
    audio = build()
    write(out, audio)
    print(f"wrote {out} ({len(audio) / SR:.1f} s)")
