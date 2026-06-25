"""
run_pipeline.py — the single command Railway runs each day.

Order: bootstrap persistent data (first run only) -> fetch prices -> daily update.
All runtime files live in DATA_DIR (the Railway Volume, default /data) so they
survive restarts. Seed files shipped at the repo root are copied in once.

Set FORCE_RESEED=1 to OVERWRITE the volume's state/masters from the repo seed on
the next run (use once to repair a bad cursor, then REMOVE the variable).
"""
import os, sys, shutil, subprocess, json
from datetime import datetime, timezone

APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", "/data")
SEED = APP  # seed files live at repo root (Option B)


def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(DATA, "pipeline.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def bootstrap():
    os.makedirs(DATA, exist_ok=True)
    force = os.environ.get("FORCE_RESEED") == "1"
    if force:
        log("FORCE_RESEED=1 — overwriting volume state/masters from seed")
    for name in ["state.json", "trades_master.csv", "messages_master.jsonl"]:
        dst, src = os.path.join(DATA, name), os.path.join(SEED, name)
        if (force or not os.path.exists(dst)) and os.path.exists(src):
            shutil.copy(src, dst)
            log(f"{'reseeded' if force else 'bootstrapped'} {name} into volume")
    cm = os.path.join(DATA, "chart_map.json")
    if force or not os.path.exists(cm):
        json.dump({}, open(cm, "w"))
        log("initialized empty chart_map.json")


def run(script):
    log(f"running {script}")
    rc = subprocess.run([sys.executable, os.path.join(APP, script)], cwd=DATA).returncode
    log(f"{script} exit code {rc}")
    return rc


def main():
    log("=== pipeline start ===")
    bootstrap()
    if run("fetch_prices.py") != 0:
        log("WARNING: price fetch failed; trades lacking bars will retry next run")
    run("daily_update.py")
    log("=== pipeline done ===")


if __name__ == "__main__":
    main()
