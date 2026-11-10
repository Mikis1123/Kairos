"""Hudební podkres Cesty (Cesta 3.0, C6): přibalené skladby a jejich generátor.

Skladby jsou vlastní, vygenerované tímto souborem (deterministicky, bez cizích nahrávek –
žádné licence): pomalé filmové plochy (rozladěné pily s dýchajícím filtrem), basový tón,
řídké zvonkové tóny s ozvěnou a dlouhý prostorový dozvuk. Výstup: assets/music/*.opus
(libopus 112 kb/s, ~2,5 min, začátek i konec v tichu – smyčka i prolnutí jsou měkké).

    python ambient_music.py            # znovu vygenerovat všechny skladby (vyžaduje scipy)

Za běhu jen tracks(): přibalené skladby + vlastní skladby uživatele z USER_MUSIC_DIR
(%APPDATA%/Kairos/music, Gate 2 D: Nastavení › Hudba › Přidat hudbu…).

Sprint 4 – metrum pro Director: akord = jeden takt o BAR dobách (doba = chord_s / BAR), zvonky
padají na osminy a každou dobu nese tichý tlumený úder (těžká doba hlasitěji) – střihy Reels
a příjezdy Cesty na doby jsou tak slyšet. beat_map() vrací mřížku dob: u vlastních skladeb ji
odhadne z nástupů zvuku (beat_detect, tempo autokorelací, fáze hřebenem).
"""
import json
import os
import shutil
import subprocess
import sys

import paths
from tool_timeouts import FFMPEG_GENERATE_TIMEOUT_S

MUSIC_DIR = os.path.join(paths.RESOURCE_DIR, "assets", "music")
# Gate 2 D: the user's own tracks live in the writable data folder (%APPDATA%\Kairos\music) -
# the bundled folder can be read-only in Program Files
USER_MUSIC_DIR = os.path.join(paths.DATA_DIR, "music")
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
BAR = 4                       # dob v taktu (= jeden akord)
PULSE_GAIN = 0.32             # tlumený úder na dobu (těžká doba 1,0×, ostatní 0,45×)


def _listed(folder):
    try:
        return sorted(n for n in os.listdir(folder) if n.lower().endswith(EXTENSIONS)
                      and os.path.isfile(os.path.join(folder, n)))
    except OSError:
        return []


def tracks():
    """[{file, title, path, user}] - bundled tracks first, then the user's own (alphabetical)."""
    titles = {t["file"]: t["title"] for t in TRACKS}
    out = [{"file": n, "title": titles.get(n, os.path.splitext(n)[0]), "path": os.path.join(MUSIC_DIR, n),
            "user": False} for n in _listed(MUSIC_DIR)]
    bundled = {t["file"].lower() for t in out}
    for n in _listed(USER_MUSIC_DIR):
        if os.path.normcase(USER_MUSIC_DIR) == os.path.normcase(MUSIC_DIR) or n.lower() in bundled:
            continue
        out.append({"file": n, "title": os.path.splitext(n)[0], "path": os.path.join(USER_MUSIC_DIR, n),
                    "user": True})
    return out


def user_tracks():
    return [t for t in tracks() if t["user"]]


def add_user_tracks(files):
    """Copies audio files into USER_MUSIC_DIR (originals untouched). Returns the added tracks."""
    os.makedirs(USER_MUSIC_DIR, exist_ok=True)
    added = []
    for src in files:
        name = os.path.basename(src)
        if not name.lower().endswith(EXTENSIONS) or not os.path.isfile(src):
            continue
        stem, ext = os.path.splitext(name)
        target, k = os.path.join(USER_MUSIC_DIR, name), 2
        if os.path.exists(target) and os.path.getsize(target) == os.path.getsize(src):
            continue                                  # the same file again
        while os.path.exists(target):
            target = os.path.join(USER_MUSIC_DIR, f"{stem} ({k}){ext}")
            k += 1
        shutil.copy2(src, target)
        added.append({"file": os.path.basename(target), "title": os.path.splitext(os.path.basename(target))[0],
                      "path": target, "user": True})
    return added


def remove_user_track(file_name):
    """Deletes a track from the user folder (its own copy; the original was never moved)."""
    path = os.path.join(USER_MUSIC_DIR, os.path.basename(file_name))
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def _track_key(path):
    return f"{os.path.abspath(path)}|{os.path.getsize(path)}|{int(os.path.getmtime(path))}"


def track_profile(track, compute=True):
    """{energy, brightness} 0..1 (mood.py). Bundled: from the generator spec. User: measured
    once from the audio and cached; compute=False returns only a cached value (or None)."""
    import mood
    spec = next((t for t in TRACKS if t["file"] == track.get("file")), None)
    if spec is not None and not track.get("user"):
        return mood.spec_profile(spec)
    path = track.get("path")
    if not path or not os.path.exists(path):
        return None
    key = _track_key(path)
    cache_path = os.path.join(paths.CACHE_ROOT, "track_profiles.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if key in cache or not compute:
        return cache.get(key)
    try:
        pcm = decode_mono(path)
        prof = mood.audio_profile(pcm, DETECT_SR, beat_map(track))
    except Exception as e:
        print(f"[Music] {track.get('file')}: {e}")
        return None
    cache[key] = prof
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass
    return prof


def beat_map(track, compute=True):
    """Mřížka dob skladby pro Director: {period, bar, offset, length} (s), nebo None.
    Přibalené skladby: přesně z generátoru. Vlastní: odhad z nástupů zvuku (uložený v .cache)."""
    spec = next((t for t in TRACKS if t["file"] == track.get("file")), None)
    if spec is not None:
        return {"period": spec["chord_s"] / BAR, "bar": BAR, "offset": 0.0, "length": TRACK_S}
    path = track.get("path")
    if not path or not os.path.exists(path):
        return None
    key = _track_key(path)
    cache_path = os.path.join(paths.CACHE_ROOT, "beats.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if key not in cache:
        if not compute:
            return None                    # Gate 2 D: measured later in the background (warm-up)
        cache[key] = detect_beats(path)
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except OSError:
            pass
    return cache[key]


DETECT_SR = 11025
DETECT_HOP = 256
ONSET_LAG_FRAMES = 3.7      # okno STFT (1024 = 4 kroky) zachytí nástup ~3,7 snímku dřív (změřeno: -85 ms)


def decode_mono(path, seconds=90, ffmpeg_path=None):
    """Prvních seconds s skladby jako mono float32 (ffmpeg) – pro odhad tempa."""
    import numpy as np
    cmd = [ffmpeg_path or paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-t", str(seconds),
           "-i", path, "-ac", "1", "-ar", str(DETECT_SR), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return np.frombuffer(out.stdout, dtype=np.float32)


def onset_envelope(pcm):
    """Spektrální tok (kladné přírůstky log-magnitudy) po DETECT_HOP vzorcích."""
    import numpy as np
    n_fft = 1024
    if pcm.size < n_fft * 4:
        return np.zeros(0, np.float32)
    frames = 1 + (pcm.size - n_fft) // DETECT_HOP
    idx = np.arange(n_fft)[None, :] + DETECT_HOP * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(pcm[idx] * np.hanning(n_fft), axis=1))
    logm = np.log1p(100 * spec)
    flux = np.maximum(0, np.diff(logm, axis=0)).sum(axis=1)
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")   # odečíst pomalý trend
    return np.maximum(flux, 0).astype(np.float32)


def detect_beats(path, pcm=None, bpm_range=(60, 140), bar=BAR):
    """Odhad mřížky dob: tempo = vrchol autokorelace obálky nástupů v rozsahu bpm_range,
    fáze dob = posun s největším součtem obálky na dobách, těžká doba = nejsilnější z bar fází.
    Bez zřetelného rytmu -> {"period": 0} (Director pak střihy na doby nepřichytává)."""
    import numpy as np
    pcm = decode_mono(path) if pcm is None else pcm
    env = onset_envelope(pcm)
    fps = DETECT_SR / DETECT_HOP
    if env.size < fps * 8 or float(env.max(initial=0)) <= 0:
        return {"period": 0.0, "bar": bar, "offset": 0.0, "length": 0.0}
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[e.size - 1:]
    lo, hi = int(fps * 60 / bpm_range[1]), int(fps * 60 / bpm_range[0])
    lag = lo + int(np.argmax(ac[lo:hi + 1]))
    strength = float(ac[lag] / (ac[0] or 1))
    if strength < 0.15:                            # šum ~0,06, plochy bez rytmu ~0,09, údery ≥ 0,7
        return {"period": 0.0, "bar": bar, "offset": 0.0, "length": 0.0}
    if lo < lag < ac.size - 1:                     # vrchol mezi snímky (parabola) – bez driftu za minutu
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        den = a - 2 * b + c
        lag_f = lag + (0.5 * (a - c) / den if den else 0.0)
    else:
        lag_f = float(lag)
    best = None                                    # tempo i fáze společně: hřeben přes jemnou mřížku
    for lag_k in lag_f * (1 + np.linspace(-0.015, 0.015, 31)):
        idx0 = np.arange(int((env.size - 1) / lag_k)) * lag_k
        for p in range(lag):
            idx = np.round(p + idx0).astype(int)
            score = float(env[idx[idx < env.size]].sum())
            if best is None or score > best[0]:
                best = (score, lag_k, p)
    _score, lag_f, phase = best
    idx = np.round(phase + np.arange(int((env.size - 1) / lag_f)) * lag_f).astype(int)
    hits = env[idx[idx < env.size]]
    down = int(np.argmax([hits[k::bar].sum() for k in range(min(bar, hits.size))])) if hits.size else 0
    period = float(lag_f / fps)
    offset = ((phase + ONSET_LAG_FRAMES) / fps + down * period) % (period * bar)
    return {"period": round(period, 4), "bar": bar, "offset": round(float(offset), 4),
            "length": round(pcm.size / DETECT_SR, 3) if pcm.size < DETECT_SR * 89 else 0.0,
            "strength": round(strength, 3)}


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
        eighth = chord_s / BAR / 2
        for _ in range(int(rng.poisson(spec["bells"] * chord_s / 3.0))):
            at = max(eighth, round(rng.uniform(0.5, chord_s) / eighth) * eighth)    # na osminy
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
        # 4) tlumený úder na každou dobu (Sprint 4: střihy na doby jsou slyšet), těžká doba silněji
        beat_s = chord_s / BAR
        for j in range(BAR):
            b0 = s + int(j * beat_s * SAMPLE_RATE)
            bn = min(int(0.9 * SAMPLE_RATE), total - b0)
            if bn <= 0:
                continue
            bt = np.arange(bn) / SAMPLE_RATE
            pitch = f0 * 2 * (1 + 0.5 * np.exp(-bt * 30))           # krátký sestup = měkký „tom“
            thump = np.sin(2 * np.pi * np.cumsum(pitch) / SAMPLE_RATE) * np.exp(-bt * 7) * (1 - np.exp(-bt * 600))
            g = PULSE_GAIN * (1.0 if j == 0 else 0.45)
            left[b0:b0 + bn] += thump * g
            right[b0:b0 + bn] += thump * g
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
        subprocess.run(cmd, input=pcm.tobytes(), check=True, timeout=FFMPEG_GENERATE_TIMEOUT_S)
        made.append(target)
        print(f"{spec['title']}: {target} ({os.path.getsize(target) / 1e6:.1f} MB)")
    return made


if __name__ == "__main__":
    sys.exit(0 if render_all() else 1)
