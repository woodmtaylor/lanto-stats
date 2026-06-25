"""
Price engine for ES / NQ 1-minute bars.

Handles the two quirks of the exported CSVs:
  * Different date formats  (ES: 2025-11-5 [Y-M-D] ; NQ: 11/5/2025 [M/D/Y])
  * Bars are stamped in US CENTRAL time (CME exchange time; the daily blank
    hour sits at 16:00 local = the 4-5pm CT Globex maintenance break).
Both are converted to tz-aware UTC so they align with Discord's UTC stamps,
using America/Chicago so the Nov->Jun DST change is handled correctly.
"""

import csv
from datetime import datetime
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

TICK = {"ES": 0.25, "NQ": 0.25}
POINT_VALUE = {"ES": 50.0, "NQ": 20.0}  # $ per point per contract


def _parse_es_date(d):
    y, m, day = d.strip().split("-")
    return int(y), int(m), int(day)


def _parse_nq_date(d):
    m, day, y = d.strip().split("/")
    return int(y), int(m), int(day)


def load_bars(path, symbol):
    """Return list of dicts {ts (UTC datetime), o,h,l,c} sorted ascending."""
    date_parser = _parse_es_date if symbol == "ES" else _parse_nq_date
    bars = []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        # column indices are stable: Date,Time,Open,High,Low,Last,...
        for row in r:
            if len(row) < 6:
                continue
            try:
                y, mo, da = date_parser(row[0])
                hh, mm, ss = row[1].strip().split(":")
                sec = int(float(ss))
                local = datetime(y, mo, da, int(hh), int(mm), sec, tzinfo=CENTRAL)
                ts = local.astimezone(UTC)
                bars.append({
                    "ts": ts,
                    "o": float(row[2]), "h": float(row[3]),
                    "l": float(row[4]), "c": float(row[5]),
                })
            except (ValueError, IndexError):
                continue
    bars.sort(key=lambda b: b["ts"])
    return bars


def window(bars, start_utc, end_utc):
    """Slice bars with start_utc <= ts <= end_utc (both tz-aware UTC)."""
    return [b for b in bars if start_utc <= b["ts"] <= end_utc]


def bar_at(bars, ts_utc):
    """Nearest bar at-or-before ts_utc (the bar 'in progress' at that instant)."""
    prev = None
    for b in bars:
        if b["ts"] > ts_utc:
            break
        prev = b
    return prev
