"""Live Photo framing: the clean-aperture parser and one framing (full coded frame) for every surface."""
import os
import struct

import pytest

import video_clap

HERE = os.path.dirname(os.path.abspath(__file__))


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _matrix(rot):
    one = 0x10000
    a, b, c, d = {0: (one, 0, 0, one), 90: (0, one, -one, 0), 180: (-one, 0, 0, -one), 270: (0, -one, one, 0)}[rot]
    return struct.pack(">9i", a, b, 0, c, d, 0, 0, 0, 0x40000000)


def _mov(path, coded=(1920, 1440), clap=(1744, 1308, 0, 0), rot=0, clef=None, video=True):
    tkhd = _box(b"tkhd", bytes([0, 0, 0, 7]) + b"\0" * 36 + _matrix(rot) + struct.pack(">II", coded[0] << 16, coded[1] << 16))
    hdlr = _box(b"hdlr", b"\0" * 4 + b"\0" * 4 + (b"vide" if video else b"soun") + b"\0" * 12 + b"\0")
    children = b""
    if clap:
        cw, ch, ho, vo = clap
        children += _box(b"clap", struct.pack(">IIIIiIiI", cw, 1, ch, 1, ho, 1, vo, 1))
    entry_body = (b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 16 + struct.pack(">HH", *coded) +
                  struct.pack(">II", 0x480000, 0x480000) + b"\0" * 4 + struct.pack(">H", 1) + b"\0" * 32 +
                  struct.pack(">Hh", 24, -1) + children)
    stsd = _box(b"stsd", b"\0" * 4 + struct.pack(">I", 1) + _box(b"hvc1", entry_body))
    stbl = _box(b"stbl", stsd)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", hdlr + minf)
    tapt = _box(b"tapt", _box(b"clef", b"\0" * 4 + struct.pack(">II", clef[0] << 16, clef[1] << 16))) if clef else b""
    trak = _box(b"trak", tkhd + tapt + mdia)
    with open(path, "wb") as f:
        f.write(_box(b"ftyp", b"qt  \0\0\0\0qt  ") + _box(b"mdat", b"\0" * 64) + _box(b"moov", trak))
    return path


def test_iphone_live_clean_aperture(tmp_path):
    c = video_clap.parse(_mov(tmp_path / "a.mov"))
    assert c.coded == (1920, 1440) and c.clean == (1744, 1308)
    assert c.as_tuple() == (0.045833, 0.045833, 0.908333, 0.908333)
    assert abs(c.aspect - 4 / 3) < 1e-9 and c.rotation == 0


@pytest.mark.parametrize("rot,expect", [
    (90, (0.0, 0.25, 0.5, 0.5)),       # coded rect (x .25, y .5, w .5, h .5) seen after a 90 deg turn
    (180, (0.25, 0.0, 0.5, 0.5)),
    (270, (0.5, 0.25, 0.5, 0.5)),
])
def test_rotation_maps_the_rect_into_the_displayed_frame(tmp_path, rot, expect):
    # an off-centre clean aperture: half the frame, centred at (0.5, 0.75) of the coded frame
    c = video_clap.parse(_mov(tmp_path / "r.mov", coded=(1000, 800), clap=(500, 400, 0, 200), rot=rot))
    assert c.rotation == rot
    assert c.as_tuple() == pytest.approx(expect, abs=1e-6)
    assert c.aspect == pytest.approx(0.8 if rot in (90, 270) else 1.25)


def test_tapt_clef_is_the_fallback(tmp_path):
    c = video_clap.parse(_mov(tmp_path / "t.mov", clap=None, clef=(1744, 1308)))
    assert c.clean == (1744, 1308) and c.as_tuple()[2] == pytest.approx(0.908333, abs=1e-6)


def test_no_aperture_means_nothing_to_crop(tmp_path):
    p = _mov(tmp_path / "n.mov", coded=(3840, 2160), clap=(3840, 2160, 0, 0))
    assert video_clap.parse(p).is_identity
    assert video_clap.clean_aperture(str(p)) is None
    assert video_clap.parse(_mov(tmp_path / "s.mov", video=False)) is None


def test_not_a_movie_and_truncated_files_are_safe(tmp_path):
    junk = tmp_path / "j.mov"
    junk.write_bytes(b"\x00\x00\x00\x10moovtrunc")
    assert video_clap.parse(str(junk)) is None
    assert video_clap.clean_aperture(str(tmp_path / "missing.mov")) is None


def test_ffmpeg_args_follow_the_version(monkeypatch):
    import subprocess

    class R:
        def __init__(self, out):
            self.stdout = out

    for out, want in ((b"ffmpeg version 9.0.1-essentials", video_clap.FULL_FRAME_ARGS),
                      (b"ffmpeg version n7.1 Copyright", video_clap.FULL_FRAME_ARGS),
                      (b"ffmpeg version 6.1.1 Copyright", [])):
        video_clap._FFMPEG_OK.clear()
        monkeypatch.setattr(subprocess, "run", lambda *a, _o=out, **k: R(_o))
        assert video_clap.ffmpeg_full_frame_args("ff.exe") == want
    video_clap._FFMPEG_OK.clear()
    assert video_clap.ffmpeg_full_frame_args("") == []


def test_display_decoders_keep_the_full_frame(monkeypatch):
    import card_media
    import scrub_strip
    import video_proxy
    monkeypatch.setattr(video_clap, "ffmpeg_full_frame_args", lambda p: ["-apply_cropping", "codec"])
    for cmd in (scrub_strip._ffmpeg_cmd("x.mov", False, True),
                card_media.ffmpeg_cmd("x.mov", "y.webm", 0, {}),
                video_proxy.ffmpeg_cmd("x.mov", "y.mp4", {})):
        i = cmd.index("-apply_cropping")
        assert cmd[i + 1] == "codec" and i < cmd.index("-i"), "input option, before -i"
    # caches made with the clean-aperture crop are not reused
    assert scrub_strip.STRIP_VERSION >= 3
    assert card_media.CLIP_FRAMING in "|full-frame"


def test_ai_frames_keep_their_geometry():
    # face boxes stored from AI frames refer to the frames as they were cut: no framing change there
    with open(os.path.join(HERE, "video_frames.py"), encoding="utf-8") as f:
        assert "apply_cropping" not in f.read()
