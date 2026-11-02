"""Gate 0 – měření: identita médií, kontrola dostupnosti, cloud, napájení, migrace.

    python eval/bench_gate0.py [--db KOPIE.db] [--n 20000]

Vše v dočasné složce; skutečná knihovna se jen čte (--db = kopie, jinak paths.DB_PATH read-only).
"""
import argparse
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cloud_files  # noqa: E402
import data_migration  # noqa: E402
import database  # noqa: E402
import governor  # noqa: E402
import media_identity  # noqa: E402
import paths  # noqa: E402
import power  # noqa: E402


def ms(t0):
    return (time.perf_counter() - t0) * 1000


def bench_real(db_src, tmp):
    """Reálná knihovna (kopie DB): kontrola dostupnosti + doplnění otisků."""
    db = os.path.join(tmp, "real.db")
    src = sqlite3.connect(f"file:{db_src}?mode=ro", uri=True)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()
    database.DB_PATH = db
    database.init_db()
    conn = database.get_connection()
    n = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
    t0 = time.perf_counter()
    stats = media_identity.reconcile(conn)
    conn.commit()
    t_rec = ms(t0)
    t0 = time.perf_counter()
    fp = media_identity.backfill_fingerprints(conn, exists=cloud_files.local_ok)
    t_fp = ms(t0)
    t0 = time.perf_counter()
    stats2 = media_identity.reconcile(conn)
    conn.commit()
    t_rec2 = ms(t0)
    conn.close()
    print(f"[skutečná knihovna] {n} položek")
    print(f"  reconcile 1. běh (doplní svazky):  {t_rec:8.1f} ms  ({t_rec / max(n, 1) * 1000:.0f} µs/položka) "
          f"online={stats['online']} offline={stats['offline']} missing={stats['missing']} "
          f"cloud={stats['cloud']} backfilled={stats['backfilled']}")
    print(f"  reconcile další běh:               {t_rec2:8.1f} ms")
    print(f"  otisky 64 kB (xxh3):                {t_fp:8.1f} ms  ({fp} souborů, "
          f"{t_fp / max(fp, 1):.2f} ms/soubor)")
    return {"items": n, "reconcile_ms": t_rec, "reconcile2_ms": t_rec2, "fp_ms_per_file": t_fp / max(fp, 1)}


def bench_synthetic(tmp, n):
    """n položek: 1/2 na odpojeném disku (s GUID), 1/4 přesunuté písmeno, 1/4 lokální."""
    database.DB_PATH = os.path.join(tmp, "syn.db")
    database.init_db()
    lib = os.path.join(tmp, "lib")
    os.makedirs(lib)
    local_n = n // 4
    rows = []
    for i in range(n):
        if i < n // 2:
            rows.append((rf"Q:\Archiv\{i}.jpg", f"{i}.jpg", "\\\\?\\volume{gone}\\", rf"Archiv\{i}.jpg"))
        elif i < n // 2 + local_n:
            p = os.path.join(lib, "moved", f"{i}.jpg")
            rows.append((rf"R:\moved\{i}.jpg", f"{i}.jpg", "\\\\?\\volume{ext}\\", rf"moved\{i}.jpg"))
        else:
            p = os.path.join(lib, f"{i}.jpg")
            rows.append((p, f"{i}.jpg", None, None))
    os.makedirs(os.path.join(lib, "moved"))
    for path, name, guid, rel in rows:
        if guid == "\\\\?\\volume{ext}\\":
            open(os.path.join(lib, rel), "wb").close()
        elif guid is None:
            open(path, "wb").close()
    with database.db_transaction() as conn:
        conn.executemany("INSERT INTO media_items (file_path, file_name, volume_guid, vol_rel_path) "
                         "VALUES (?, ?, ?, ?)", rows)
    vols = {"\\\\?\\volume{ext}\\": [lib + "\\"], "\\\\?\\volume{sys}\\": ["C:\\"]}
    conn = database.get_connection()
    t0 = time.perf_counter()
    st = media_identity.reconcile(conn, volumes=vols)
    conn.commit()
    t = ms(t0)
    t0 = time.perf_counter()
    st2 = media_identity.reconcile(conn, volumes=vols)
    conn.commit()
    t2 = ms(t0)
    conn.close()
    print(f"[syntetická knihovna] {n} položek")
    print(f"  reconcile 1. běh:  {t:8.1f} ms  remapped={st['remapped']} offline={st['offline']} "
          f"online={st['online']} missing={st['missing']}")
    print(f"  reconcile 2. běh:  {t2:8.1f} ms  (vše už přepsané: remapped={st2['remapped']})")
    return {"n": n, "reconcile_ms": t, "reconcile2_ms": t2, "remapped": st["remapped"], "offline": st["offline"]}


def bench_reconnect(tmp, n=500):
    """Přesun + přejmenování n souborů -> znovupřipojení skenerem (bez exiftoolu)."""
    import scanner
    database.DB_PATH = os.path.join(tmp, "rc.db")
    database.init_db()
    src_dir = os.path.join(tmp, "src")
    os.makedirs(src_dir)
    s = scanner.MediaScannerThread(None, files=[])
    files = []
    for i in range(n):
        p = os.path.join(src_dir, f"IMG_{i:04d}.JPG")
        with open(p, "wb") as f:
            f.write(b"\xff\xd8" + os.urandom(80_000))
        files.append(p)
    with database.db_transaction() as conn:
        cur = conn.cursor()
        for i, p in enumerate(files):
            s._insert_item(cur, {"DateTimeOriginal": f"2024:05:01 10:{i // 60 % 60:02d}:{i % 60:02d}"}, p,
                           os.path.getsize(p), os.path.getmtime(p))
    dst_dir = os.path.join(tmp, "Přejmenovaná složka")
    os.replace(src_dir, dst_dir)
    moved = []
    for i in range(n):
        a = os.path.join(dst_dir, f"IMG_{i:04d}.JPG")
        b = os.path.join(dst_dir, f"dovolená_{i:04d}.jpg")
        os.replace(a, b)
        moved.append(b)
    t0 = time.perf_counter()
    conn = database.get_connection()
    missing = media_identity.MissingIndex.load(conn)
    conn.close()
    to_process, _, _ = s._classify(moved, {}, missing)
    t_cls = ms(t0)
    with database.db_transaction() as conn:
        cur = conn.cursor()
        for i, (real, dbp, size, mtime, cands, fp) in enumerate(to_process):
            s._insert_item(cur, {"DateTimeOriginal": f"2024:05:01 10:{i // 60 % 60:02d}:{i % 60:02d}"},
                           dbp, size, mtime, cands=cands, fp=fp, missing=missing)
    t_all = ms(t0)
    conn = database.get_connection()
    total = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
    conn.close()
    print(f"[znovupřipojení] {n} souborů přesunuto + přejmenováno")
    print(f"  připojeno: {s.reconnected}/{n}, položek v DB: {total} (duplicity: {total - n})")
    print(f"  otisk + párování: {t_cls:.1f} ms, celkem se zápisem: {t_all:.1f} ms "
          f"({t_all / n:.2f} ms/soubor, bez exiftoolu)")
    return {"n": n, "reconnected": s.reconnected, "duplicates": total - n, "ms": t_all}


def bench_cloud_and_power(tmp):
    p = os.path.join(tmp, "x.jpg")
    open(p, "wb").write(b"x" * 1000)
    k = 5000
    t0 = time.perf_counter()
    for _ in range(k):
        cloud_files.local_ok(p)
    t_cloud = ms(t0) / k * 1000
    g = governor.Governor(quiet_s=0)
    power.get().override = power.PowerState()
    t0 = time.perf_counter()
    for _ in range(k):
        g.checkpoint(heavy=True)
    t_ac = ms(t0) / k * 1000
    power.get().override = None
    t0 = time.perf_counter()
    for _ in range(200):
        power.read_system()
    t_read = ms(t0) / 200 * 1000
    st = power.read_system()
    print("[cloud + napájení]")
    print(f"  cloud_files.local_ok:          {t_cloud:6.1f} µs/soubor (atributy, bez otevření dat)")
    print(f"  governor.checkpoint (síť):     {t_ac:6.1f} µs (režie Gate 0 na položku)")
    print(f"  GetSystemPowerStatus:          {t_read:6.1f} µs  ({st})")
    pol = {}
    for name, state in (("síť", power.PowerState()),
                        ("baterie 60 %", power.PowerState(True, 60, False, True)),
                        ("baterie 15 %", power.PowerState(True, 15, False, True)),
                        ("Spořič", power.PowerState(True, 80, True, True))):
        pol[name] = (power.decide(state, "throttle", True), power.decide(state, "throttle", False))
    print("  politika throttle (těžká, lehká):", pol)
    work = 0.5
    duty = work / (work + power.HEAVY_PAUSE_S)
    print(f"  baterie: těžká položka ~{work:.1f} s + pauza {power.HEAVY_PAUSE_S} s -> střída {duty:.0%} "
          f"(+ EcoQoS vlákna)")
    return {"cloud_us": t_cloud, "checkpoint_us": t_ac, "power_read_us": t_read}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=paths.DB_PATH if os.path.exists(paths.DB_PATH)
                    else os.path.join(paths.LEGACY_DIR, "photos.db"))
    ap.add_argument("--n", type=int, default=20000)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="kairos_bench_g0_")
    try:
        if os.path.exists(args.db):
            bench_real(args.db, tmp)
        bench_synthetic(tmp, args.n)
        bench_reconnect(tmp)
        bench_cloud_and_power(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
