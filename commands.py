"""
commands.py — pure command logic for the Lanto bot. No Discord here; each
function returns (text, [file_paths]) so it can be unit-tested and reused by the
slash-command layer in bot.py.

Filters are uniform across commands: date range, instrument, direction, outcome,
and (for equity) which strategy series to show. /flag corrections are applied to
the data everywhere, so a manual fix shows up immediately in every view.
"""
import os, csv, json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import analytics as A
import daily_update as D

DATA = os.environ.get("DATA_DIR") or os.getcwd()
SERIES_ALIASES = {
    "tp1": "R_tp1_full_net", "tp2": "R_tp2_full_net",
    "tp1half": "R_tp1half_tp2_net", "trail": "R_trail_net",
    "his": "realized_R", "realized": "realized_R",
}


def P(n):
    return os.path.join(DATA, n)


# --------------------------------------------------------------------------- #
# Loading + corrections
# --------------------------------------------------------------------------- #
def load_corrections():
    return A.load_corrections()


def save_corrections(c):
    json.dump(c, open(P("corrections.json"), "w", encoding="utf-8"), indent=1)


def load_rows():
    return A.load_rows()  # already applies the corrections overlay


def load_messages():
    out = []
    try:
        for line in open(P("messages_master.jsonl"), encoding="utf-8"):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out


def load_state():
    try:
        return json.load(open(P("state.json"), encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_message_id": 0, "scored_ids": []}


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def parse_range(s):
    """Return (start_dt, end_dt) in UTC. Accepts presets or explicit dates."""
    now = datetime.now(timezone.utc)
    if not s or s.lower() == "all":
        return None, None
    s = s.strip().lower()
    presets = {"7d": 7, "14d": 14, "30d": 30, "60d": 60, "90d": 90, "180d": 180}
    if s in presets:
        return now - timedelta(days=presets[s]), now
    if s == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc), now
    if s == "mtd":
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc), now
    if ":" in s:  # explicit from:to
        a, b = s.split(":", 1)
        return (_d(a), _d(b, end=True))
    d = _d(s)  # single date -> that day to now
    return d, now


def _d(s, end=False):
    try:
        dt = datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt + timedelta(days=1) if end else dt
    except ValueError:
        return None


def filter_rows(rows, start=None, end=None, instrument=None, direction=None, outcome=None):
    out = []
    inst = None if not instrument or instrument == "both" else instrument.upper()
    dirn = None if not direction or direction == "both" else direction.lower()
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["entry_ts"])
        except (ValueError, KeyError):
            dt = None
        if start and (not dt or dt < start):
            continue
        if end and (not dt or dt > end):
            continue
        if inst and r.get("instrument", "").upper() != inst:
            continue
        if dirn and r.get("direction", "").lower() != dirn:
            continue
        if outcome and r.get("outcome_type", "") != outcome:
            continue
        out.append(r)
    return out


def _series_cols(series):
    if not series or series == "all":
        return list(A.STRATS.keys())
    cols = []
    for tok in series.replace(" ", "").split(","):
        col = SERIES_ALIASES.get(tok.lower())
        if col and col not in cols:
            cols.append(col)
    return cols or list(A.STRATS.keys())


def _range_label(start, end):
    if not start and not end:
        return "all time"
    return f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"


# --------------------------------------------------------------------------- #
# Commands  -> (text, [files])
# --------------------------------------------------------------------------- #
def cmd_status():
    st = load_state()
    rows = load_rows()
    last = rows[-1]["entry_ts"][:16] if rows else "—"
    corr = len(load_corrections())
    txt = (f"**Status**\n```\n"
           f"trades on record : {len(rows)}\n"
           f"last trade entry : {last}\n"
           f"cursor (msg id)  : {st.get('last_message_id')}\n"
           f"processed ids    : {len(st.get('scored_ids', []))}\n"
           f"manual flags     : {corr}\n"
           f"```")
    return txt, []


def cmd_stats(start=None, end=None, instrument=None, direction=None, series="tp1"):
    rows = filter_rows(load_rows(), start, end, instrument, direction)
    if not rows:
        return f"No trades in {_range_label(start, end)}.", []
    cols = _series_cols(series)
    lines = [f"{'strategy':<13}{'n':>4}{'win%':>6}{'netR':>8}{'avgR':>7}{'PF':>6}{'DD':>7}{'MCW':>4}{'MCL':>4}"]
    for col in cols:
        name = A.STRATS.get(col, col)
        m = A.metrics([v for _, v in A.series(rows, col)])
        if not m:
            continue
        pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
        lines.append(f"{name:<13}{m['n']:>4}{m['win']:>6.0f}{m['net']:>+8.1f}"
                     f"{m['avg']:>+7.2f}{pf:>6}{m['maxdd']:>7.1f}{m['mcw']:>4}{m['mcl']:>4}")
    flt = _filter_label(instrument, direction)
    return f"**Stats — {_range_label(start, end)}{flt}**\n```\n" + "\n".join(lines) + "\n```", []


def cmd_equity(start=None, end=None, instrument=None, direction=None,
               series="tp1,tp1half,trail", by_date=True):
    rows = filter_rows(load_rows(), start, end, instrument, direction)
    if not rows:
        return f"No trades in {_range_label(start, end)}.", []
    cols = _series_cols(series)
    title = f"Equity — {_range_label(start, end)}"
    path = A.chart_equity(rows, cols=cols, out_name="cmd_equity.png",
                          title=title, by_date=by_date)
    return f"**{title}**{_filter_label(instrument, direction)}", [path]


def cmd_trades(start=None, end=None, instrument=None, direction=None,
               limit=15, as_csv=False, series="tp1"):
    rows = filter_rows(load_rows(), start, end, instrument, direction)
    if not rows:
        return f"No trades in {_range_label(start, end)}.", []
    rows = sorted(rows, key=lambda r: r.get("entry_ts", ""))
    if as_csv:
        path = P("cmd_trades.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        return f"{len(rows)} trades in {_range_label(start, end)} (CSV attached).", [path]

    cols = _series_cols(series)
    short = {"R_tp1_full_net": "TP1", "R_tp2_full_net": "TP2",
             "R_tp1half_tp2_net": "TP½", "R_trail_net": "trl", "realized_R": "his"}
    shown = rows[-limit:]
    header = f"{'date':<11}{'sym':<4}{'dir':<6}" + "".join(f"{short[c]:>7}" for c in cols) + "  outcome"
    lines = [header]
    for r in shown:
        def g(k):
            v = r.get(k, "")
            return f"{float(v):+.2f}" if v not in ("", None) else "—"
        lines.append(f"{r.get('entry_ts','')[:10]:<11}{r.get('instrument','?'):<4}"
                     f"{r.get('direction','?'):<6}" + "".join(f"{g(c):>7}" for c in cols) +
                     f"  {r.get('outcome_type','?')}")
    head = f"**Trades — last {len(shown)} of {len(rows)} in {_range_label(start, end)}**"
    return head + "\n```\n" + "\n".join(lines) + "\n```", []


def cmd_review(limit=15):
    """Surface trades most likely mis-scored: low vis_conf or unclear outcome."""
    rows = sorted(load_rows(), key=lambda r: r.get("entry_ts", ""))
    flagged = [r for r in rows
               if r.get("vis_conf", "") == "low" or r.get("outcome_type", "") == "unclear"]
    if not flagged:
        return "Nothing needs review — no low-confidence or unclear trades.", []
    shown = flagged[-limit:]
    lines = [f"{'date':<11}{'sym':<4}{'dir':<6}{'conf':<7}{'outcome':<10}  msg_id"]
    for r in shown:
        lines.append(f"{r.get('entry_ts','')[:10]:<11}{r.get('instrument','?'):<4}"
                     f"{r.get('direction','?'):<6}{r.get('vis_conf','?'):<7}"
                     f"{r.get('outcome_type','?'):<10}  {r.get('msg_id','')}")
    head = f"**Review queue — {len(flagged)} flagged (showing {len(shown)})**\n" \
           f"Use `/flag` to correct; corrections override the bot and feed `/validate`."
    return head + "\n```\n" + "\n".join(lines) + "\n```", []


def cmd_coverage(start=None, end=None):
    """Detection funnel over a range: entries he posted vs scored vs missed."""
    msgs = load_messages()
    st = load_state()
    scored = set(map(str, st.get("scored_ids", [])))
    traded = {r.get("msg_id") for r in A.load_rows()}
    posted = scored_n = traded_n = 0
    for m in msgs:
        try:
            dt = datetime.fromisoformat(m.get("timestamp", ""))
        except ValueError:
            dt = None
        if start and (not dt or dt < start):
            continue
        if end and (not dt or dt > end):
            continue
        if not D.is_entry(m.get("text", "")):
            continue
        posted += 1
        if m["id"] in scored:
            scored_n += 1
        if m["id"] in traded:
            traded_n += 1
    miss = posted - scored_n
    txt = (f"**Coverage — {_range_label(start, end)}**\n```\n"
           f"entry signals posted : {posted}\n"
           f"processed by bot      : {scored_n}\n"
           f"became scored trades  : {traded_n}\n"
           f"unprocessed/missed    : {miss}\n"
           f"```")
    return txt, []


def cmd_flag(msg_id, **fields):
    """Record a manual correction (ground truth) for a trade."""
    msg_id = str(msg_id)
    valid = {"outcome_type", "realized_R", "instrument", "direction",
             "entry", "stop", "tp1", "tp2", "tp3", "text_tp_hit"}
    fix = {k: v for k, v in fields.items() if k in valid and v not in (None, "")}
    if not fix:
        return "No valid fields given. Correctable: " + ", ".join(sorted(valid)), []
    c = load_corrections()
    rec = c.get(msg_id, {})
    rec.update(fix)
    rec["_truth"] = True
    rec["_ts"] = datetime.now(timezone.utc).isoformat()
    c[msg_id] = rec
    save_corrections(c)
    return f"✅ Flagged `{msg_id}`: " + ", ".join(f"{k}={v}" for k, v in fix.items()) \
           + "\nApplied to all views and used as ground truth for `/validate`.", []


def cmd_audit(msg_id=None):
    """Show what the bot sees for an entry: its text, paired chart, and a LIVE
    vision read. The fastest way to see why a trade did/didn't score."""
    import vision_parse as V
    msgs = load_messages()
    if not msg_id:
        traded = {r.get("msg_id") for r in A.load_rows()}
        ents = [m for m in msgs if D.is_entry(m.get("text", "")) and m["id"] not in traded]
        if not ents:
            return "No recent un-scored entries to audit.", []
        m = sorted(ents, key=lambda x: x.get("timestamp", ""))[-1]
    else:
        m = next((x for x in msgs if x["id"] == str(msg_id)), None)
        if not m:
            return f"Message `{msg_id}` not found in the store.", []

    cmap = {}
    try:
        cmap = json.load(open(P("chart_map.json"), encoding="utf-8"))
    except Exception:
        pass
    idx = next((i for i, x in enumerate(msgs) if x["id"] == m["id"]), None)
    # use the SAME self-healing pairing the scorer uses (rejects avatars, searches
    # neighbors), so audit reflects the real chart, not a stale mapping
    chart = D.find_chart(m, msgs, idx, cmap) if idx is not None else cmap.get(m["id"])
    chart_full = P(chart) if chart else None
    exists = bool(chart_full and os.path.exists(chart_full))

    out = [f"**Audit `{m['id']}`**  ({m.get('timestamp','')[:16]})",
           f"detected as entry: {D.is_entry(m.get('text',''))}",
           f"chart used: {chart or '—'}  | file present: {exists}",
           "```", (m.get("text", "")[:300] or "(no text)"), "```"]

    files = []
    if exists:
        files = [chart_full]
        try:
            ctx = D.context_around(msgs, idx) if idx is not None else m.get("text", "")
            v = V.read_chart(chart_full, ctx[:1600])
            out.append("**live vision result:**\n```\n" + json.dumps(v, indent=1)[:700] + "\n```")
        except Exception as e:
            out.append(f"vision raised: {e}")
    else:
        out.append("_No chart-like image found near this entry — that's why it didn't score._")
    return "\n".join(out), files


def _filter_label(instrument, direction):
    bits = []
    if instrument and instrument != "both":
        bits.append(instrument.upper())
    if direction and direction != "both":
        bits.append(direction.lower())
    return f"  ·  {', '.join(bits)}" if bits else ""
