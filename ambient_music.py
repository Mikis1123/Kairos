"""Hudební podkres Cesty (Cesta 3.0, C6): přibalené skladby a jejich generátor.

Skladby jsou vlastní, vygenerované tímto souborem (deterministicky, bez cizích nahrávek –
žádné licence): pomalé filmové plochy (rozladěné pily s dýchajícím filtrem), basový tón,
řídké zvonkové tóny s ozvěnou a dlouhý prostorový dozvuk. Výstup: assets/music/*.opus
(libopus 112 kb/s, ~2,5 min, začátek i konec v tichu – smyčka i prolnutí jsou měkké).

    python ambient_music.py            # znovu vygenerovat všechny skladby (vyžaduje scipy)

Za běhu jen tracks(): seznam přibalených souborů. Uživatel může do assets/music přidat
vlastní .opus / .ogg / .mp3 / .m4a / .webm – Cesta je vezme také.
"""
import os
import subprocess
import sys

import paths

MUSIC_DIR = os.path.join(paths.RESOURCE_DIR, "assets", "music")
EXTENSIONS = (".opus", ".ogg", ".webm", ".mp3", ".m4a", ".wav")
SAMPLE_RATE = 48000

# název souboru, tempo akordů (s), akordy (MIDI tóny), barva (horní mez filtru Hz), zvonky
TRACKS = [
    {"file": "kairos_horizon.opus", "title": "Horizont", "chord_s": 9.0, "brightness": 2600, "bells": 0.8,
     "chords": [[50, 57, 62, 66, 69, 76], [47, 54, 59, 62, 66, 73], [43, 50, 55, 59, 62, 69], [45, 52, 57, 61, 64, 71]]},
    {"file": "kairos_dusk.opus", "title": "Soumrak", "chord_s": 10.0, "brightness": 1900, "bells": 0.55,
     "chords": [[45, 52, 57, 60, 64, 71], [41, 48, 53, 57, 60, 67], [48, 55, 60, 64, 67, 74], [43, 50, 55, 59, 62, 69]]},
    {"file": "kairos_north.opus", "title": "Sever", "chord_s": 8.0, "brightness": 2300, "bells": 1.0,
     "chords": [[40, 47, 52, 55, 59, 66], [36, 43, 48, 52, 55, 62], [43, 50, 55, 59, 62, 69], [38, 45, 50, 54, 57, 64]]},
]
TRACK_S = 150.0


def tracks():
    """[{file, title, path}] přibalených (nebo uživatelem přidaných) skladeb, abecedně."""
    try:
        names = sorted(n for n in os.listdir(MUSIC_DIR) if n.lower().endswith(EXTENSIONS))
    except OSError:
        return []
    titles = {t["file"]: t["title"] for t in TRACKS}
    return [{"file": n, "title": titles.get(n, os.path.splitext(n)[0]), "path": os.path.join(MUSIC_DIR, n)}
            for n in names]


# ── generátor (jen pro vývoj; aplikace ho nespouští) ─────────────────────────

def _midi_hz(m):
    return 440.0 * 2 ** ((m - 69) / 12.0)


def _saw(np, f, t, phase, harmonics):
    """Pásmově omezená pila (součet harmonických) – bez aliasingu, měkká."""
    out = np.zeros_like(t)
    for n in range(1, harmonics + 1):
        if f * n > SAMPLE_RATE * 0.45:
            break
        out += np.sin(2 * np.pi * f * n * t + phase * n) / n
    return out


def _envelope(np, n, attack, release):
    env = np.ones(n)
    a, r = int(attack * SAMPLE_RATE), int(release * SAMPLE_RATE)
    if a:
        env[:a] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a))
    if r:
        env[-r:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, r))
    return env


def _reverb_ir(np, rng, seconds=4.2, predelay=0.03):
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    decay = np.exp(-6.9 * t / seconds)              # -60 dB na konci
    irs = []
    for _ in range(2):                              # jiný šum vlevo a vpravo = šířka
        ir = rng.standard_normal(n) * decay
        ir[:int(predelay * SAMPLE_RATE)] = 0
        irs.append(ir / np.sqrt(np.sum(ir ** 2)))
    return irs


def compose(spec, seconds=TRACK_S, seed=7):
    import numpy as np
    from scipy import signal
    rng = np.random.default_rng(seed)
    total = int(seconds * SAMPLE_RATE)
    left, right = np.zeros(total), np.zeros(total)
    chord_s, chords = spec["chord_s"], spec["chords"]
    overlap = 3.5
    # 1) plochy: každý akord = rozladěné pily, nástup a doznění přes sebe
    k, start = 0, 0.0
    while start < seconds:
        chord = chords[k % len(chords)]
        dur = min(chord_s + overlap, seconds - start)
        n = int(dur * SAMPLE_RATE)
        t = np.arange(n) / SAMPLE_RATE
        pad_l, pad_r = np.zeros(n), np.zeros(n)
        for j, m in enumerate(chord[1:]):          # bez basu
            f = _midi_hz(m)
            for d, pan in ((-0.07, 0.25), (0.0, 0.5), (0.08, 0.75)):   # rozladění v centech/100
                v = _saw(np, f * 2 ** (d / 12.0), t, rng.uniform(0, 2 * np.pi), 10) * (0.9 if j < 3 else 0.55)
                pad_l += v * np.cos(pan * np.pi / 2)
                pad_r += v * np.sin(pan * np.pi / 2)
        env = _envelope(np, n, 3.2, 4.0)
        s = int(start * SAMPLE_RATE)
        left[s:s + n] += pad_l * env
        right[s:s + n] += pad_r * env
        # 2) bas: sinus kořene o oktávu níž, pomalý nástup
        f0 = _midi_hz(chord[0] - 12)
        bass = (np.sin(2 * np.pi * f0 * t) + 0.25 * np.sin(4 * np.pi * f0 * t)) * _envelope(np, n, 2.5, 4.0) * 1.6
        left[s:s + n] += bass
        right[s:s + n] += bass
        # 3) zvonky: řídké tóny akordu o dvě oktávy výš, každý s exponenciálním dozvukem
        for _ in range(int(rng.poisson(spec["bells"] * chord_s / 3.0))):
            at = rng.uniform(0.5, chord_s)
            m = chord[int(rng.integers(2, len(chord)))] + 12
            f = _midi_hz(m)
            bn = int(min(5.0, dur - at) * SAMPLE_RATE)
            if bn <= 0:
                continue
            bt = np.arange(bn) / SAMPLE_RATE
            bell = (np.sin(2 * np.pi * f * bt) + 0.3 * np.sin(2 * np.pi * f * 2.0 * bt) * np.exp(-bt * 2.5)) \
                * np.exp(-bt * 0.9) * (1 - np.exp(-bt * 400)) * 0.55
            pan = rng.uniform(0.2, 0.8)
            b0 = s + int(at * SAMPLE_RATE)
            b1 = min(total, b0 + bn)
            left[b0:b1] += bell[:b1 - b0] * np.cos(pan * np.pi / 2)
            right[b0:b1] += bell[:b1 - b0] * np.sin(pan * np.pi / 2)
        start += chord_s
        k += 1
    # dýchající filtr: dolní propust, jejíž mez pomalu kolísá (blokově po 0,5 s)
    block = SAMPLE_RATE // 2
    out = []
    for ch in (left, right):
        y = np.zeros_like(ch)
        zi = None
        for b in range(0, total, block):
            tt = b / SAMPLE_RATE
            cutoff = spec["brightness"] * (0.75 + 0.25 * np.sin(2 * np.pi * tt / 37.0))
            sos = signal.butter(2, cutoff, fs=SAMPLE_RATE, output="sos")
            if zi is None:
                zi = np.zeros((sos.shape[0], 2))
            y[b:b + block], zi = signal.sosfilt(sos, ch[b:b + block], zi=zi)
        out.append(y)
    # dlouhý prostor: konvoluce s šumovým dozvukem, mix 45 % mokrého
    irs = _reverb_ir(np, rng)
    wet = [signal.fftconvolve(out[i], irs[i])[:total] for i in range(2)]
    mix = [0.62 * out[i] + 0.45 * wet[i] for i in range(2)]
    stereo = np.stack(mix, axis=1)
    fade = _envelope(np, total, 4.0, 6.0)[:, None]
    stereo *= fade
    stereo /= np.max(np.abs(stereo)) or 1.0
    stereo = np.tanh(stereo * 1.4) / np.tanh(1.4) * 0.7     # měkký limiter, vrchol ≈ -3 dBFS
    return (stereo * 32767).astype(np.int16)


def render_all(out_dir=MUSIC_DIR, ffmpeg_path=None):
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for i, spec in enumerate(TRACKS):
        pcm = compose(spec, seed=11 + i)
        target = os.path.join(out_dir, spec["file"])
        cmd = [ffmpeg_path or paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "-",
               "-c:a", "libopus", "-b:a", "112k", "-metadata", f"title=Kairos – {spec['title']}", target]
        subprocess.run(cmd, input=pcm.tobytes(), check=True)
        made.append(target)
        print(f"{spec['title']}: {target} ({os.path.getsize(target) / 1e6:.1f} MB)")
    return made


if __name__ == "__main__":
    sys.exit(0 if render_all() else 1)
