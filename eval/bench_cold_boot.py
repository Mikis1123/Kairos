"""Pillar 5 – cold boot: process spawn -> grid firstFrameRendered.

Spawns `python main.py` N times with KAIROS_BOOT_TRACE and reports each milestone (ms since
the OS created the process; boot_trace.py). The first run after a reboot / file-cache flush is
the true "cold" one; later runs are warm-cache launches. Usage:
    python eval/bench_cold_boot.py [runs=5] [--no-splash]
"""
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_once(no_splash=False, timeout=60):
    fd, path = tempfile.mkstemp(suffix=".json", prefix="kairos-boot-")
    os.close(fd)
    os.remove(path)
    env = dict(os.environ, KAIROS_BOOT_TRACE=path)
    if no_splash:
        env["KAIROS_NO_SPLASH"] = "1"
    t0 = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None
    wall = (time.perf_counter() - t0) * 1000
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    os.remove(path)
    data["wall_exit_ms"] = round(wall, 1)
    return data


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    runs = int(args[0]) if args else 5
    no_splash = "--no-splash" in sys.argv
    results = []
    for i in range(runs):
        r = run_once(no_splash)
        if r is None:
            print(f"run {i + 1}: no trace (crash / timeout)")
            continue
        marks = dict(r["marks"])
        results.append(marks)
        print(f"run {i + 1}: " + "  ".join(f"{k}={v:.0f}" for k, v in r["marks"]))
    if not results:
        return 1
    print("\nmedian (ms since process creation):")
    for k in results[0]:
        vals = [m[k] for m in results if k in m]
        print(f"  {k:<16} {statistics.median(vals):7.0f}   (min {min(vals):.0f}, max {max(vals):.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
