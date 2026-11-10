"""Bundled offline world base for the iPhone (Parity sprint, 2026-11-10).

The desktop's world pack (tile_store.WORLD_PACK, z0-6, ~110 MB) is too big to ship inside the app.
This keeps z0..MAX_ZOOM and, inside every vector tile, only the layers the phone's offline style
draws (OfflineMapStyle in MapLibreView.swift: water, landcover, boundary, transportation, ...).
The filter works on the protobuf framing (Tile.layers = field 3, Layer.name = field 1), so the
kept layers are byte-for-byte the originals: no re-encoding, no geometry change.

    python ios/tools/build_world_lite.py [source.mbtiles] [out.mbtiles]
"""
import gzip
import os
import sqlite3
import sys

MAX_ZOOM = 4
KEEP = {"water", "landcover", "boundary", "transportation", "landuse", "park", "waterway"}
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "KairosApp", "Resources",
                           "world-lite.mbtiles")


def varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def fields(buf):
    """Yields (field number, wire type, value start, value end, record start) of one message."""
    i = 0
    while i < len(buf):
        start = i
        key, i = varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            _, i = varint(buf, i)
            yield num, wt, start, i, start
        elif wt == 1:
            i += 8
            yield num, wt, i - 8, i, start
        elif wt == 2:
            n, i = varint(buf, i)
            yield num, wt, i, i + n, start
            i += n
        elif wt == 5:
            i += 4
            yield num, wt, i - 4, i, start
        else:
            raise ValueError(f"wire type {wt}")


def layer_name(layer):
    for num, wt, a, b, _ in fields(layer):
        if num == 1 and wt == 2:
            return layer[a:b].decode("utf-8", "replace")
    return ""


def filter_tile(data, keep=KEEP):
    packed = data[:2] == b"\x1f\x8b"
    raw = gzip.decompress(data) if packed else data
    out = bytearray()
    for num, wt, a, b, start in fields(raw):
        if num == 3 and wt == 2 and layer_name(raw[a:b]) not in keep:
            continue
        out += raw[start:b]
    out = bytes(out)
    return gzip.compress(out, 9) if packed else out


def build(src, dst, max_zoom=MAX_ZOOM):
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    if os.path.exists(dst):
        os.remove(dst)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    d.execute("PRAGMA page_size = 4096")
    d.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    d.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    meta = dict(s.execute("SELECT name, value FROM metadata").fetchall())
    meta.update({"name": "World (lite)", "kairos:title": "World", "minzoom": "0", "maxzoom": str(max_zoom),
                 "description": "Kairos bundled world base: " + ", ".join(sorted(KEEP))})
    d.executemany("INSERT INTO metadata VALUES (?, ?)", sorted(meta.items()))
    before = after = 0
    for z, x, y, data in s.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
                                   "WHERE zoom_level <= ?", (max_zoom,)):
        out = filter_tile(bytes(data))
        before += len(data)
        after += len(out)
        d.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, y, out))
    d.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    d.commit()
    d.execute("VACUUM")
    d.close()
    s.close()
    return before, after


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Kairos", "cache", "map_packs", "world-base.mbtiles")
    dst = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    b, a = build(src, dst)
    print(f"{dst}: tiles {b / 1e6:.1f} MB -> {a / 1e6:.1f} MB, file {os.path.getsize(dst) / 1e6:.1f} MB")
