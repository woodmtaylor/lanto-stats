"""
run_pipeline.py — the single command Railway runs each day.

Order: bootstrap/repair persistent data -> fetch prices -> daily update.
All runtime files live in DATA_DIR (the Railway Volume, default /data) so they
survive restarts. Seed files shipped at the repo root are copied in on first run.

Self-healing: on every run the volume's scored_ids are unioned with the seed's
and the cursor is bumped to at least the seed cursor. This repairs a volume that
was left with a stale/partial cursor (e.g. an interrupted first scrape) WITHOUT
needing any manual flag. Set FORCE_RESEED=1 to additionally overwrite the
masters from the repo seed (rarely needed).
"""
import os, sys, shutil, subprocess, json
from datetime import datetime, timezone

APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", "/data")
SEED = APP  # seed files live at repo root


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
        log("FORCE_RESEED=1 — overwriting volume masters from seed")

    # 1) copy seed files in when missing (or when forced)
    for name in ["state.json", "trades_master.csv", "messages_master.jsonl"]:
        dst, src = os.path.join(DATA, name), os.path.join(SEED, name)
        if (force or not os.path.exists(dst)) and os.path.exists(src):
            shutil.copy(src, dst)
            log(f"{'reseeded' if force else 'bootstrapped'} {name} into volume")

    # 2) self-heal state: union seed scored_ids, never let the cursor regress
    try:
        seed_state = json.load(open(os.path.join(SEED, "state.json"), encoding="utf-8"))
        vol_path = os.path.join(DATA, "state.json")
        vol_state = json.load(open(vol_path, encoding="utf-8"))
        before = len(vol_state.get("scored_ids", []))
        merged = set(map(str, vol_state.get("scored_ids", []))) | \
                 set(map(str, seed_state.get("scored_ids", [])))
        vol_state["scored_ids"] = sorted(merged, key=int)
        new_cursor = max(int(vol_state.get("last_message_id", 0)),
                         int(seed_state.get("last_message_id", 0)))
        if new_cursor != int(vol_state.get("last_message_id", 0)):
            log(f"state repair: cursor bumped to seed ({new_cursor})")
        vol_state["last_message_id"] = new_cursor
        if len(merged) != before:
            log(f"state repair: scored_ids {before} -> {len(merged)} (merged seed)")
        json.dump(vol_state, open(vol_path, "w", encoding="utf-8"))
    except Exception as e:
        log(f"state repair skipped: {e}")

    # 3) chart_map
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
