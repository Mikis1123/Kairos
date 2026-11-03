"""Gate 2 D: the mood of a trip and the energy of a track, matched without any settings.

**Trip mood** (`trip_mood`) combines the signals the library already has:
* smiles: mean smile probability of detected faces (smile.py);
* time of day: midday light reads brighter and livelier, dusk and night calmer;
* pace: bursts of many photos per hour are lively, a few contemplative shots are calm;
* vibe: image embeddings (SigLIP when installed, otherwise CLIP) compared with a small bank
  of "lively" vs. "calm" scene prompts. The prompt vectors are computed once in the
  background (`warm_prompt_bank`) and cached, so building a Journey never waits for a model.
Each signal is optional. Mood = weighted mean of the signals present -> energy and
brightness on 0..1.

**Track profile** (`track_profile`): the bundled tracks come from the generator spec. A user
track is measured once from its audio (tempo and beat strength from beat_map, onset
density, loudness, spectral centroid) and cached next to the beat grid.

`rank_tracks` orders tracks by distance in (energy, brightness). A Journey gets the closest
one. Ties break deterministically by trip id, so the same trip always plays the same track.
"""
import json
import math
import os
import threading

import paths

ENERGY_PROMPTS = ["a lively, energetic party with people dancing", "an exciting action shot, people running",
                  "a busy festival crowd, bright and loud", "children playing and laughing outdoors"]
CALM_PROMPTS = ["a calm, quiet landscape at dusk", "a peaceful, empty beach at sunset",
                "a misty forest, still and silent", "a cozy, quiet evening indoors"]
BRIGHT_PROMPTS = ["a sunny day with a bright blue sky", "vivid summer colours in daylight"]
DARK_PROMPTS = ["a dark night scene with city lights", "a moody grey overcast day"]
MAX_EMBEDDINGS = 300
_bank_lock = threading.Lock()


def _bank_path():
    return os.path.join(paths.CACHE_ROOT, "vibe_prompts.json")


def load_prompt_bank():
    try:
        with open(_bank_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def build_bank_entry(text_model):
    """Prompt-axis vectors for one text model (lively - calm, bright - dark)."""
    import numpy as np

    def axis(pos, neg):
        p = np.mean([_unit(x) for x in text_model.encode(pos)], axis=0)
        n = np.mean([_unit(x) for x in text_model.encode(neg)], axis=0)
        return _unit(p - n).tolist()
    return {"energy": axis(ENERGY_PROMPTS, CALM_PROMPTS), "bright": axis(BRIGHT_PROMPTS, DARK_PROMPTS)}


def warm_prompt_bank(models=None):
    """Computes the missing prompt banks (call off the GUI thread). models = {kind: text model}."""
    with _bank_lock:
        bank = load_prompt_bank()
        if models is None:
            models = {}
            try:
                import ai_engine
                models["clip"] = ai_engine.get_text_model()
                if ai_engine.siglip_available():
                    models["siglip"] = ai_engine.get_siglip_text_model()
            except Exception as e:
                print(f"[Mood] no text model: {e}")
        changed = False
        for kind, model in models.items():
            if kind in bank or model is None:
                continue
            try:
                bank[kind] = build_bank_entry(model)
                changed = True
            except Exception as e:
                print(f"[Mood] prompt bank {kind}: {e}")
        if changed:
            try:
                os.makedirs(os.path.dirname(_bank_path()), exist_ok=True)
                tmp = _bank_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(bank, f)
                os.replace(tmp, _bank_path())
            except OSError:
                pass
        return bank


# ── trip signals ─────────────────────────────────────────────────────────────

def _clamp(x):
    return max(0.0, min(1.0, float(x)))


def daylight(hour):
    """0 at night, 1 around midday (smooth)."""
    return _clamp(0.5 - 0.5 * math.cos((hour - 2.0) / 24.0 * 2 * math.pi)) ** 0.8


def _hours(dates):
    out = []
    for d in dates:
        s = str(d or "")
        if len(s) >= 13 and s[11:13].isdigit():
            out.append(int(s[11:13]) + (int(s[14:16]) / 60.0 if s[14:16].isdigit() else 0.0))
    return out


def pace_energy(dates):
    """Photos per active hour -> 0..1 (1 photo/h calm, 40+/h lively)."""
    import datetime as _d
    stamps = []
    for d in dates:
        try:
            stamps.append(_d.datetime.strptime(str(d)[:16], "%Y-%m-%d %H:%M"))
        except ValueError:
            continue
    if len(stamps) < 3:
        return None
    stamps.sort()
    active = set((s.date(), s.hour) for s in stamps)
    rate = len(stamps) / max(1, len(active))
    return _clamp(math.log1p(rate) / math.log1p(40))


def _vibe(conn, ids, bank):
    """(energy, brightness) from embeddings and the prompt bank, or None."""
    import numpy as np
    from ai_engine import decode_embedding
    for kind, table in (("siglip", "siglip_embeddings"), ("clip", "image_embeddings")):
        entry = bank.get(kind)
        if not entry:
            continue
        try:
            q = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT embedding FROM {table} WHERE media_id IN ({q})", ids).fetchall()
        except Exception:
            continue
        vecs = []
        for (raw,) in rows:
            try:
                v = decode_embedding(raw)
            except Exception:
                continue
            if v is not None and len(v) == len(entry["energy"]):
                vecs.append(_unit(v))
        if len(vecs) < 2:
            continue
        m = np.mean(vecs, axis=0)
        e = float(m @ np.asarray(entry["energy"], dtype=np.float32))
        b = float(m @ np.asarray(entry["bright"], dtype=np.float32))
        # cosine against a prompt axis is small (~ +/-0.05); map to 0..1 around 0.5
        return _clamp(0.5 + e * 8.0), _clamp(0.5 + b * 8.0)
    return None


def trip_mood(conn, event_id, bank=None):
    """{energy, brightness, signals} for an event (0..1 each)."""
    rows = conn.execute("""
        SELECT id, date_taken, media_type FROM media_items
        WHERE event_id = ? AND COALESCE(is_hidden, 0) = 0 AND COALESCE(is_deleted, 0) = 0
        ORDER BY date_taken LIMIT 5000
    """, (int(event_id),)).fetchall() if _has_col(conn, "media_items", "is_deleted") else conn.execute("""
        SELECT id, date_taken, media_type FROM media_items
        WHERE event_id = ? AND COALESCE(is_hidden, 0) = 0 ORDER BY date_taken LIMIT 5000
    """, (int(event_id),)).fetchall()
    ids = [int(r[0]) for r in rows]
    dates = [r[1] for r in rows]
    signals = {}
    hours = _hours(dates)
    if hours:
        light = sum(daylight(h) for h in hours) / len(hours)
        signals["daylight"] = light
    pace = pace_energy(dates)
    if pace is not None:
        signals["pace"] = pace
    if rows:
        videos = sum(1 for r in rows if r[2] == "video") / len(rows)
        signals["motion"] = _clamp(videos * 3)
    try:
        q = ",".join("?" * min(len(ids), 900))
        sm = conn.execute(f"SELECT AVG(smile), COUNT(smile) FROM face_instances WHERE smile IS NOT NULL "
                          f"AND media_id IN ({q})", ids[:900]).fetchone() if ids else None
        if sm and sm[1] and sm[1] >= 2:
            signals["smiles"] = _clamp(sm[0])
    except Exception:
        pass
    bank = load_prompt_bank() if bank is None else bank
    if bank and ids:
        sample = ids if len(ids) <= MAX_EMBEDDINGS else ids[:: max(1, len(ids) // MAX_EMBEDDINGS)][:MAX_EMBEDDINGS]
        vibe = _vibe(conn, sample, bank)
        if vibe:
            signals["vibe_energy"], signals["vibe_bright"] = vibe
    e_parts = [(signals.get("vibe_energy"), 2.0), (signals.get("smiles"), 1.5), (signals.get("pace"), 1.0),
               (signals.get("motion"), 0.5), (signals.get("daylight"), 0.75)]
    b_parts = [(signals.get("daylight"), 2.0), (signals.get("vibe_bright"), 1.5), (signals.get("smiles"), 0.5)]
    return {"energy": _weighted(e_parts), "brightness": _weighted(b_parts), "signals": signals}


def _weighted(parts):
    parts = [(v, w) for v, w in parts if v is not None]
    if not parts:
        return 0.5
    return round(sum(v * w for v, w in parts) / sum(w for _v, w in parts), 4)


def _has_col(conn, table, col):
    try:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))
    except Exception:
        return False


# ── track profile ────────────────────────────────────────────────────────────

def spec_profile(spec):
    """Bundled generator track -> {energy, brightness}."""
    bpm = 60.0 / (spec["chord_s"] / 4.0)
    b = _clamp((spec["brightness"] - 1500) / 1500.0)
    e = _clamp(0.45 * b + 0.3 * _clamp(spec["bells"]) + 0.25 * _clamp((bpm - 20) / 15.0))
    return {"energy": round(e, 4), "brightness": round(b, 4)}


def audio_profile(pcm, sr, beats=None):
    """Measured profile of decoded mono audio (float32) -> {energy, brightness}."""
    import numpy as np
    if pcm is None or pcm.size < sr * 4:
        return {"energy": 0.5, "brightness": 0.5}
    rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64)))))
    loud = _clamp((20 * math.log10(max(rms, 1e-6)) + 30) / 22.0)          # -30 dBFS..-8 dBFS
    n_fft, hop = 2048, 1024
    frames = 1 + (pcm.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(min(frames, 600))[:, None]
    spec = np.abs(np.fft.rfft(pcm[idx] * np.hanning(n_fft), axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    centroid = float((spec * freqs).sum() / max(spec.sum(), 1e-9))
    bright = _clamp((centroid - 600) / 2400.0)
    flux = np.maximum(0, np.diff(np.log1p(100 * spec), axis=0)).sum(axis=1)
    thr = flux.mean() + flux.std()
    onsets = float(((flux[1:-1] > thr) & (flux[1:-1] >= flux[:-2]) & (flux[1:-1] >= flux[2:])).sum())
    rate = onsets / (pcm.size / sr)
    density = _clamp(rate / 4.0)
    tempo = 0.5
    strength = 0.0
    if beats and beats.get("period"):
        tempo = _clamp((60.0 / beats["period"] - 60) / 80.0)
        strength = _clamp(beats.get("strength", 0.5))
    e = 0.35 * loud + 0.3 * density + 0.35 * (tempo * (0.4 + 0.6 * strength))
    return {"energy": round(_clamp(e), 4), "brightness": round(bright, 4)}


def rank_tracks(tracks, mood, salt=0):
    """Tracks sorted best match first (each track needs 'profile')."""
    def dist(t):
        p = t.get("profile") or {"energy": 0.5, "brightness": 0.5}
        return math.hypot(p["energy"] - mood["energy"], 0.6 * (p["brightness"] - mood["brightness"]))
    order = list(range(len(tracks)))
    order.sort(key=lambda i: (round(dist(tracks[i]), 3), (i + int(salt)) % max(1, len(tracks))))
    return [tracks[i] for i in order]
