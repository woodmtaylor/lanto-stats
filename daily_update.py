"""
daily_update.py — incremental scrape / score / append / alert.

Runs each day from run_pipeline.py with cwd = DATA_DIR (the Railway Volume).
Sibling modules (price_engine, simulate, vision_parse) live next to this file
in /app and import normally; all *data* files live in DATA_DIR.

Flow:
  1. Scrape new Discord messages after state['last_message_id'] (bot token),
     download image attachments, append messages to messages_master.jsonl,
     record chart paths in chart_map.json, advance the cursor.
  2. Find entry signals among ALL messages that are not yet in scored_ids and
     that settled >= SETTLE_HOURS ago. (Unscored entries naturally retry until
     their price bars exist — see the price-bars skip below.)
  3. For each: pair with its chart -> vision_parse levels -> simulate four
     exits -> subtract the per-trade cost (R, net) -> parse follow-up text for
     his stated outcome -> append a row to trades_master.csv. Mark the msg_id
     scored either way (a charted+scored trade, or a processed-but-no-trade
     entry), matching the original "scored_ids = processed" convention.
  4. Write stats_summary.txt and (if WEBHOOK_URL set) post a short alert.

Idempotent: a msg_id is processed at most once (scored_ids). A trade whose
price bars are not yet available is skipped WITHOUT being marked scored, so it
is retried on the next run.

CLI:
  python daily_update.py            # normal run
  python daily_update.py --dry-run  # do everything except write files / post
  python daily_update.py --selftest # offline: re-score the newest existing
                                     # trade from the masters to validate the
                                     # simulate+cost+text pipeline (no Discord)
"""

import os
import sys
import re
import csv
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import price_engine as pe
import simulate as sim
import vision_parse as vp

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DATA = os.environ.get("DATA_DIR") or os.getcwd()
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

SETTLE_HOURS = 18            # only score trades whose entry is this old
NO_CHART_GIVEUP_HOURS = 36  # past this, an entry with no chart is marked done
PRICE_WINDOW_GIVEUP_HOURS = 144  # ~6 days: bars older than the rolling window never arrive
CHART_LOOKBACK_MIN = 20     # pair an entry with a chart image up to this long before it
CHART_LOOKAHEAD_MIN = 6     # ...or this long after it
DISCORD_API = "https://discord.com/api/v10"

# Cost model — VERIFIED against trades_master.csv: net = gross - costR, where
# costR = (slippage_pts * point_value + commission) / (risk_pts * point_value).
COST = {"ES": (0.5, 4.0), "NQ": (7.0, 2.0)}  # (slippage_points, commission_$)
POINT_VALUE = {"ES": 50.0, "NQ": 20.0}

STRAT_KEYS = ["tp1_full", "tp2_full", "tp1half_tp2", "trail"]
CSV_COLUMNS = [
    "entry_ts", "msg_id", "instrument", "direction", "entry", "stop",
    "tp1", "tp2", "tp3",
    "R_tp1_full_net", "R_tp2_full_net", "R_tp1half_tp2_net", "R_trail_net",
    "realized_R", "text_tp_hit", "outcome_type", "vis_conf", "chart_date",
]

# Entry-signal patterns: "I am long/short", "Im long/short", "I'm long/short".
ENTRY_RE = re.compile(r"\b(?:i\s*am|i['’`]?m)\s+(long|short)\b", re.IGNORECASE)
THUMB = "\U0001f44d"  # 👍 lone thumbs-up = entry, direction from prior context


def P(name):
    return os.path.join(DATA, name)


def now_utc():
    return datetime.now(timezone.utc)


CENTRAL = ZoneInfo("America/Chicago")


def settle_time(entry_dt):
    """When a trade is considered done: the close (16:00 Central) of the session
    it was entered in. Morning/midday entries settle the SAME day at 4pm CT;
    entries within 30 min of the close, or overnight, roll to the next close."""
    e = entry_dt.astimezone(CENTRAL)
    cutoff = e + timedelta(minutes=30)
    close = cutoff.replace(hour=16, minute=0, second=0, microsecond=0)
    if cutoff > close:
        close = close + timedelta(days=1)
    return close.astimezone(timezone.utc)


def snowflake_for(dt):
    """Discord snowflake id for a given UTC datetime (for time-based cursors)."""
    ms = int(dt.timestamp() * 1000) - 1420070400000  # Discord epoch
    return max(0, ms) << 22


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# State / masters I/O
# --------------------------------------------------------------------------- #
def load_state():
    try:
        s = json.load(open(P("state.json"), encoding="utf-8"))
    except FileNotFoundError:
        s = {}
    s.setdefault("last_message_id", 0)
    s.setdefault("scored_ids", [])
    return s


def save_state(state):
    json.dump(state, open(P("state.json"), "w", encoding="utf-8"))


def load_messages():
    msgs = []
    try:
        with open(P("messages_master.jsonl"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    msgs.append(json.loads(line))
    except FileNotFoundError:
        pass
    msgs.sort(key=lambda m: m["timestamp"])
    return msgs


def append_messages(new_msgs):
    """Update-or-insert messages by id, so a re-scrape can backfill text that an
    earlier run stored empty (e.g. before embeds were parsed)."""
    existing = {}
    try:
        with open(P("messages_master.jsonl"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    m = json.loads(line)
                    existing[m["id"]] = m
    except FileNotFoundError:
        pass
    for m in new_msgs:
        rec = {"id": m["id"], "timestamp": m["timestamp"], "text": m["text"]}
        # keep the richer (non-empty) text if a re-scrape now has content
        if m["id"] in existing and not rec["text"]:
            rec["text"] = existing[m["id"]].get("text", "")
        existing[m["id"]] = rec
    ordered = sorted(existing.values(), key=lambda r: int(r["id"]))
    tmp = P("messages_master.jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in ordered:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, P("messages_master.jsonl"))


def load_chart_map():
    try:
        return json.load(open(P("chart_map.json"), encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_chart_map(cm):
    json.dump(cm, open(P("chart_map.json"), "w", encoding="utf-8"))


def trades_count():
    try:
        with open(P("trades_master.csv"), encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except FileNotFoundError:
        return 0


def append_trade_row(row):
    exists = os.path.exists(P("trades_master.csv"))
    with open(P("trades_master.csv"), "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not exists:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------------------- #
# Discord scrape  (only this section needs network; untested offline)
# --------------------------------------------------------------------------- #
def discord_get(path):
    req = urllib.request.Request(
        DISCORD_API + path,
        headers={"Authorization": f"Bot {DISCORD_TOKEN}",
                 "User-Agent": "LantoTracker (https://example, 1.0)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def fetch_new_messages(after_id):
    """Return raw Discord messages with id > after_id, ascending."""
    if not (DISCORD_TOKEN and CHANNEL_ID):
        log("  scrape skipped: DISCORD_TOKEN/CHANNEL_ID not set")
        return []
    out, cursor = [], int(after_id)
    for _ in range(60):                           # hard cap: <=6000 msgs/run
        try:
            batch = discord_get(
                f"/channels/{CHANNEL_ID}/messages?after={cursor}&limit=100")
        except urllib.error.HTTPError as e:
            if e.code == 429:                     # rate limited: wait, then retry
                try:
                    wait = json.loads(e.read()).get("retry_after", 1.0)
                except Exception:
                    wait = 1.0
                log(f"  rate limited; waiting {wait:.1f}s")
                time.sleep(min(float(wait) + 0.2, 10))
                continue                          # retry SAME cursor
            log(f"  Discord HTTP {e.code}: {e.read()[:200]!r}")
            break
        except Exception as e:
            log(f"  Discord error: {e}")
            break
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))    # API returns newest-first
        for m in batch:
            # Lanto's calls often arrive as forwarded EMBEDS: the real text is in
            # the embed (title/description/fields), not the plain 'content'.
            parts = [m.get("content", "") or ""]
            for e in m.get("embeds", []) or []:
                parts.append(e.get("title", "") or "")
                parts.append(e.get("description", "") or "")
                for fld in e.get("fields", []) or []:
                    parts.append(fld.get("name", "") or "")
                    parts.append(fld.get("value", "") or "")
            m["text"] = "\n".join(p for p in parts if p).strip()
        out.extend(batch)
        cursor = int(batch[-1]["id"])             # advance past newest seen
        if len(batch) < 100:
            break
        time.sleep(0.4)                            # be polite to the API
    return out


def _image_urls(m):
    """Chart-candidate image URLs on a message. Pulls attachments and embed
    IMAGES (not thumbnails — those are avatars/icons), and digs into forwarded
    message_snapshots where Lanto's real charts live."""
    urls = []

    def from_obj(obj):
        for att in obj.get("attachments", []) or []:
            ct = (att.get("content_type") or "")
            fn = att.get("filename", "")
            if ct.startswith("image/") or fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                urls.append((att.get("url"), fn))
        for e in obj.get("embeds", []) or []:
            img = e.get("image") or {}          # NOT 'thumbnail' (that's the avatar)
            u = img.get("url") or img.get("proxy_url")
            if u:
                urls.append((u, u.split("?")[0].rsplit("/", 1)[-1]))

    from_obj(m)
    for snap in m.get("message_snapshots", []) or []:
        from_obj(snap.get("message") or {})
    return [(u, fn) for (u, fn) in urls if u]


def _chart_dims(path):
    """(w, h) if the file is a decodable image, else None."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _is_chart_like(path):
    """A real TradingView chart is large; avatars/icons (e.g. 256x256) are not."""
    d = _chart_dims(path)
    return bool(d and min(d) >= 400 and max(d) >= 800)


def download_charts(raw_msgs, chart_map, dry):
    """Download candidate images; map each message to its LARGEST chart-like
    image (filtering out avatar/icon thumbnails)."""
    os.makedirs(P("charts"), exist_ok=True)
    saved = 0
    for m in raw_msgs:
        best, best_area = None, 0
        for i, (url, fn) in enumerate(_image_urls(m)):
            ext = (fn.rsplit(".", 1)[-1] if "." in fn else "png").lower()
            if ext not in ("png", "jpg", "jpeg", "webp"):
                ext = "png"
            local = os.path.join("charts", f"{m['id']}_{i}.{ext}")
            full = P(local)
            if dry:
                if not best:
                    best = local  # best-effort in dry mode (no dims available)
                continue
            if not os.path.exists(full):
                try:
                    dreq = urllib.request.Request(
                        url, headers={"User-Agent": "Mozilla/5.0 (LantoTracker/1.0)"})
                    with urllib.request.urlopen(dreq, timeout=60) as resp:
                        data = resp.read()
                    with open(full, "wb") as fh:
                        fh.write(data)
                    saved += 1
                except Exception as e:
                    log(f"  chart download failed for {m['id']}: {e}")
                    continue
            d = _chart_dims(full)
            if d and min(d) >= 400 and max(d) >= 800:   # chart-like, not an avatar
                area = d[0] * d[1]
                if area > best_area:
                    best, best_area = local, area
        if best:
            chart_map[m["id"]] = best
    if saved:
        log(f"  downloaded {saved} new image(s)")
    return chart_map


# --------------------------------------------------------------------------- #
# Entry detection + chart pairing
# --------------------------------------------------------------------------- #
def parse_ts(s):
    return datetime.fromisoformat(s)


def entry_direction(msg, msgs, idx):
    """Direction from this message's text, else from prior context (for 👍)."""
    mo = ENTRY_RE.search(msg["text"])
    if mo:
        return mo.group(1).lower()
    for j in range(idx - 1, max(-1, idx - 8), -1):
        mo = ENTRY_RE.search(msgs[j]["text"])
        if mo:
            return mo.group(1).lower()
    return None


def is_entry(text):
    return bool(ENTRY_RE.search(text)) or (THUMB in text and len(text.strip()) <= 3)


def context_around(msgs, idx, before=6):
    """Context for vision: the few messages BEFORE the entry (where he states the
    setup levels — 'Target PDL 29,262', 'Breakeven at 5m gap fill 29,464'), the
    entry line itself, then the immediate follow-ups."""
    start = max(0, idx - before)
    pre = [msgs[j]["text"] for j in range(start, idx) if msgs[j].get("text", "").strip()]
    post = outcome_window_text(msgs[idx], msgs, idx)
    blocks = []
    if pre:
        blocks.append("SETUP CONTEXT (his messages just before entry):\n" + "\n".join(pre[-before:]))
    blocks.append("ENTRY: " + msgs[idx].get("text", ""))
    if post.strip():
        blocks.append("AFTER ENTRY:\n" + post)
    return "\n\n".join(blocks).strip()


def find_chart(entry_msg, msgs, idx, chart_map):
    """Closest chart image within the look-back/ahead window around the entry."""
    if entry_msg["id"] in chart_map:
        return chart_map[entry_msg["id"]]
    et = parse_ts(entry_msg["timestamp"])
    best, best_gap = None, None
    lo = et - timedelta(minutes=CHART_LOOKBACK_MIN)
    hi = et + timedelta(minutes=CHART_LOOKAHEAD_MIN)
    for j in range(max(0, idx - 12), min(len(msgs), idx + 12)):
        mid = msgs[j]["id"]
        if mid not in chart_map:
            continue
        t = parse_ts(msgs[j]["timestamp"])
        if not (lo <= t <= hi):
            continue
        gap = abs((t - et).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = chart_map[mid], gap
    return best


# --------------------------------------------------------------------------- #
# Outcome text parsing  (RECONSTRUCTED — verify against your conventions)
# --------------------------------------------------------------------------- #
R_NUM_RE = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+))\s*R\b", re.IGNORECASE)
R_MAX = 30.0  # plausible |R| ceiling; bigger = a misread price/typo, ignore


def _r_candidates(text):
    """All plausible R figures with metadata, best-first. Filters out prices
    and grouped-number fragments (e.g. '29,302'), and de-prioritizes day-total
    phrasing ('-2R day')."""
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    out = []
    for mo in R_NUM_RE.finditer(text):
        raw = mo.group(1)
        # reject if part of a comma-grouped number like 29,302  (preceded by ,digit or digit,)
        pre = text[max(0, mo.start() - 1)]
        if pre == "," or (pre.isdigit()):
            continue
        # reject cumulative brags: "over 25R", "25R+", "+76RR"
        ctx_before = text[max(0, mo.start() - 6):mo.start()].lower()
        ctx_after = text[mo.end():mo.end() + 2]
        if "over" in ctx_before or ctx_after.startswith("+") or ctx_after.startswith("R"):
            continue
        try:
            val = float(raw.replace(",", "."))
        except ValueError:
            continue
        if abs(val) > R_MAX:          # a price or typo, not an R result
            continue
        tail = text[mo.end():mo.end() + 6].lower()
        is_day = tail.lstrip().startswith("day")   # "-2R day" = day total, deprioritize
        out.append((is_day, mo.start(), val))
    # prefer non-day, earliest
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def parse_realized_r(text):
    """His stated R (best single figure). Handles comma decimals ('-0,5R') and
    bare leading decimals ('.5R'). Rejects prices/day-totals. NOTE: sign is
    often implied by context ('jabbed w/ 0.5R' = -0.5R) and is corrected in
    classify_outcome; for full fidelity enable OUTCOME_LLM=1."""
    c = _r_candidates(text)
    return c[0][2] if c else None


def classify_outcome(window_text, tps_present):
    """
    Return (realized_R, text_tp_hit, outcome_type) from the follow-up text.

    Corpus-informed (see notes): he writes "breakeven stops" as a habitual
    risk note in ~half of ALL outcomes, so it is only a LOW-priority fallback.
    The strong win signal is "smashed"; stops are hardest and lean on an
    explicit negative R or stop-out language.
    """
    t = window_text.lower()
    realized = parse_realized_r(window_text)

    win_hit = any(k in t for k in
                  ("smashed", "full take profit", "full tp", "all out",
                   "took profit", "tp filled", "tp hit", "hit tp", "tapped tp",
                   "target hit", "hit target", "reached target", "tps hit"))
    stop_hit = any(k in t for k in
                   ("stopped", "stop hit", "stopped out", "jabbed out", "jabbed",
                    "chopped out", "took the loss", "stop out", "stop-out"))
    breakeven_hit = any(k in t for k in
                        ("breakeven", "break even", "break-even", "moved to be",
                         "b/e", "be stop", "risk free", "risk-free"))
    # broader loss context for SIGN correction only (not for outcome_type)
    loss_ctx = stop_hit or any(k in t for k in ("papercut", "paper cut"))

    # sign-from-context: a positive number written under loss language is a loss
    if realized is not None and realized > 0 and loss_ctx and not win_hit:
        realized = -realized

    # when a win is explicitly signalled, the realized figure is the POSITIVE
    # one (a re-entry after a small loss states both, e.g. "-0.5R ... +2R smashed")
    if win_hit:
        pos = [v for (_, _, v) in _r_candidates(window_text) if v > 0]
        if pos:
            realized = max(pos)

    def which_tp():
        if "tp3" in t or "third target" in t:
            return "tp3"
        if "tp2" in t or "second target" in t:
            return "tp2"
        if "tp1" in t or "first target" in t:
            return "tp1"
        if any(k in t for k in ("full take profit", "full tp", "all out", "smashed")):
            return "tp3" if tps_present >= 3 else ("tp2" if tps_present >= 2 else "tp1")
        return "tp1"

    # 1) explicit win language -> tp_hit
    if win_hit and (realized is None or realized >= 0):
        return realized, which_tp(), "tp_hit"

    # 2) explicit R number present
    if realized is not None:
        if realized < 0 or stop_hit:
            return realized, "none", "stopped" if stop_hit else "explicit_r"
        if realized > 0 and any(k in t for k in (" tp", "target")):
            return realized, which_tp(), "tp_hit"
        return realized, "none", "explicit_r"

    # 3) stop-out language without a number
    if stop_hit:
        return None, "none", "stopped"

    # 4) generic target language without an explicit hit word
    if any(k in t for k in ("hit tp", "target reached", "took profit")):
        return None, which_tp(), "tp_hit"

    # 5) breakeven habit phrase as last-resort signal
    if breakeven_hit:
        return None, "none", "breakeven"

    return None, "none", "unclear"


def llm_outcome(window_text):
    """
    OPTIONAL high-fidelity outcome extraction (OUTCOME_LLM=1). Uses the same
    Anthropic key/model as vision_parse to read his freeform follow-up text and
    return his discretionary result. Far better than regex on phrasing like
    'jabbed w/ 0.5R' (=-0.5R) or revised day-totals. Falls back to regex on any
    error so a live run never breaks because of this.

    Returns (realized_R|None, text_tp_hit, outcome_type) or None to signal
    'use the regex classifier instead'.
    """
    if os.environ.get("OUTCOME_LLM") != "1" or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if not window_text.strip():
        return None
    prompt = (
        "You are reading a futures trader's freeform follow-up messages about ONE "
        "trade he just entered. Report HIS realized outcome for THIS trade.\n"
        "Return ONLY JSON: {\"realized_R\": number or null, "
        "\"text_tp_hit\": \"none\"|\"tp1\"|\"tp2\"|\"tp3\", "
        "\"outcome_type\": \"tp_hit\"|\"breakeven\"|\"stopped\"|\"explicit_r\"|\"unclear\"}.\n"
        "Rules:\n"
        "- 'jabbed'/'jabbed out'/'stopped'/'chopped out' = stopped; if no number is "
        "stated this is a full stop = realized_R -1.0. If a number is stated, R is "
        "NEGATIVE even if written without a minus ('jabbed w/ 0.5R' = -0.5).\n"
        "- 'smashed'/'full take profit'/'target smashed' = tp_hit; use the stated "
        "POSITIVE R (e.g. 'loss wiped + 1R profit ... +2R smashed' -> realized_R 2).\n"
        "- 'papercut' = a small DISCRETIONARY loss he chose to take, outcome_type "
        "'explicit_r' (NOT 'stopped'), realized_R -0.5 unless he states otherwise.\n"
        "- 'breakeven'/'B/E'/'breakeven stops'/'risk free' with no profit/loss stated "
        "= breakeven, realized_R null. He says 'breakeven stops' habitually; that alone "
        "is not an outcome. BUT if he scratches the trade / goes to cash / says the "
        "setup died with no profit or loss, treat that as breakeven, not unclear.\n"
        "- IGNORE numbers that are PRICES ('29,302', '7,641'), POSITION SIZE on the "
        "entry ('I AM LONG, 0.5R' = he risked 0.5R, not a result), DAY/WEEK TOTALS "
        "('-2R day', 'our week: ...'), and CUMULATIVE BRAGS ('over 25R+', '+76RR YTD').\n"
        "- Prefer his PERSONAL/discretionary result if he gives both personal and "
        "'consistency/capped' numbers (e.g. '+1.1R personal / +1.8R consistency' -> 1.1).\n"
        "- Only use 'unclear' if he genuinely never resolves the trade (e.g. 'TP, b/e "
        "or SL here, hard to call').\n\n"
        "Messages:\n" + window_text[:1800])
    body = json.dumps({
        "model": os.environ.get("MODEL", "claude-opus-4-8"),
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json",
                 "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        txt = "".join(b.get("text", "") for b in data.get("content", [])
                      if b.get("type") == "text")
        txt = re.sub(r"^```json|```$", "", txt.strip(), flags=re.M).strip()
        o = json.loads(txt)
        tp = o.get("text_tp_hit") or "none"
        ot = o.get("outcome_type") or "unclear"
        rr = o.get("realized_R")
        return (None if rr is None else float(rr)), tp, ot
    except Exception as e:
        log(f"  outcome LLM failed ({e}); using regex")
        return None


MERGE_WINDOW_MIN = 35  # a same-direction re-entry within this is the SAME trade


def outcome_window_text(entry_msg, msgs, idx):
    """His messages after this entry, up to the SETTLE_HOURS horizon OR the next
    DISTINCT trade (whichever comes first). A same-direction entry signal within
    MERGE_WINDOW_MIN is treated as a re-entry of the same trade and absorbed, so
    a split entry ('I AM LONG' posted twice) doesn't truncate the window."""
    et = parse_ts(entry_msg["timestamp"])
    hi = et + timedelta(hours=SETTLE_HOURS)
    my_dir = entry_direction(entry_msg, msgs, idx)
    chunks = []
    for j in range(idx + 1, len(msgs)):
        t = parse_ts(msgs[j]["timestamp"])
        if t > hi:
            break
        if is_entry(msgs[j]["text"]):
            same_dir = entry_direction(msgs[j], msgs, j) == my_dir
            quick = (t - et).total_seconds() <= MERGE_WINDOW_MIN * 60
            if not (same_dir and quick):
                break  # a genuinely new/opposite trade begins — stop here
        if msgs[j]["text"].strip():
            chunks.append(msgs[j]["text"])
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def cost_r(instrument, risk_pts):
    slip, comm = COST[instrument]
    pv = POINT_VALUE[instrument]
    return (slip * pv + comm) / (risk_pts * pv)


def fmt(x):
    if x is None or x == "":
        return ""
    return f"{x:.3f}" if isinstance(x, float) else str(x)


def score_entry(entry_msg, msgs, idx, chart_map, bars_cache, dry):
    """
    Returns ("trade", row_dict) | ("nochart", None) | ("nobars", None) | ("skip", None).
    "nobars" means retry next run (do NOT mark scored). The others are terminal.
    """
    chart = find_chart(entry_msg, msgs, idx, chart_map)
    if not chart:
        return "nochart", None
    chart_path = P(chart)
    if not os.path.exists(chart_path):
        return "nochart", None

    ctx = context_around(msgs, idx)[:1600]
    v = vp.read_chart(chart_path, ctx)
    if not v or v.get("error") or v.get("entry") is None or v.get("stop") is None:
        log(f"  vision unusable for {entry_msg['id']}: {v.get('error') if v else 'no data'}")
        return "nochart", None

    instrument = v.get("instrument")
    if instrument not in ("ES", "NQ"):
        instrument = "NQ" if float(v["entry"]) > 12000 else "ES"
    direction = (v.get("direction") or
                 entry_direction(entry_msg, msgs, idx) or "long")
    entry = float(v["entry"])
    stop = float(v["stop"])
    tp1 = v.get("tp1")
    tp2 = v.get("tp2")
    tp3 = v.get("tp3")
    tps = [tp1, tp2, tp3]
    risk = abs(entry - stop)

    # price bars for this symbol, from entry forward to settle horizon
    if instrument not in bars_cache:
        try:
            bars_cache[instrument] = pe.load_bars(P(f"{instrument}.csv"), instrument)
        except FileNotFoundError:
            bars_cache[instrument] = []
    bars = bars_cache[instrument]
    et = parse_ts(entry_msg["timestamp"]).astimezone(timezone.utc)
    after = pe.window(bars, et, et + timedelta(hours=SETTLE_HOURS))
    if not after:
        return "nobars", None  # price data not present yet -> retry next run

    sims = sim.simulate(direction, entry, stop, tps, after)

    if risk > 0:
        cr = cost_r(instrument, risk)
        net = {k: (None if sims[k] is None else sims[k] - cr) for k in STRAT_KEYS}
    else:
        net = {k: None for k in STRAT_KEYS}  # degenerate (entry==stop)

    wtext = outcome_window_text(entry_msg, msgs, idx)
    llm = llm_outcome(wtext)
    if llm is not None:
        realized, tp_hit, otype = llm
    else:
        realized, tp_hit, otype = classify_outcome(
            wtext, sum(1 for x in tps if x is not None))

    row = {
        "entry_ts": entry_msg["timestamp"],
        "msg_id": entry_msg["id"],
        "instrument": instrument,
        "direction": direction,
        "entry": fmt(entry), "stop": fmt(stop),
        "tp1": fmt(float(tp1)) if tp1 is not None else "",
        "tp2": fmt(float(tp2)) if tp2 is not None else "",
        "tp3": fmt(float(tp3)) if tp3 is not None else "",
        "R_tp1_full_net": fmt(net["tp1_full"]),
        "R_tp2_full_net": fmt(net["tp2_full"]),
        "R_tp1half_tp2_net": fmt(net["tp1half_tp2"]),
        "R_trail_net": fmt(net["trail"]),
        "realized_R": "" if realized is None else fmt(realized),
        "text_tp_hit": tp_hit,
        "outcome_type": otype,
        "vis_conf": v.get("confidence", "low"),
        "chart_date": v.get("chart_date") or "",
    }
    return "trade", row


# --------------------------------------------------------------------------- #
# Stats summary  (reconstructed format; tune freely)
# --------------------------------------------------------------------------- #
def _strategy_stats(rows):
    """Return [(label, n, win%, net_R, avg_R), ...] for the four strategies plus
    his stated R. Shared by the text summary and the Discord embed."""
    out = []
    labels = [("R_tp1_full_net", "TP1 full"), ("R_tp2_full_net", "TP2 full"),
              ("R_tp1half_tp2_net", "TP1½+TP2"), ("R_trail_net", "Trailing"),
              ("realized_R", "His realized")]
    for col, name in labels:
        vals = [float(r[col]) for r in rows if r.get(col) not in ("", None)]
        if not vals:
            continue
        wins = sum(1 for v in vals if v > 0)
        out.append((name, len(vals), wins / len(vals) * 100, sum(vals), sum(vals) / len(vals)))
    return out


def write_stats_summary():
    try:
        rows = list(csv.DictReader(open(P("trades_master.csv"), encoding="utf-8")))
    except FileNotFoundError:
        return ""
    lines = [f"Lanto tracker — {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z",
             f"trades recorded: {len(rows)}", ""]
    for name, n, win, net, avg in _strategy_stats(rows):
        lines.append(f"{name:<13} n={n:>3}  win={win:4.0f}%  net={net:+6.1f}R  avg={avg:+.3f}R")
    text = "\n".join(lines) + "\n"
    with open(P("stats_summary.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


def _fmt_trade_line(r):
    """One compact line per new trade for the Discord report."""
    def g(k):
        v = r.get(k, "")
        return f"{float(v):+.2f}" if v not in ("", None) else "—"
    instr = r.get("instrument", "?")
    direction = r.get("direction", "?")
    his = r.get("realized_R", "")
    his_s = f" · his {float(his):+.2f}R" if his not in ("", None) else ""
    return (f"`{r.get('entry_ts','')[:10]}` **{instr} {direction}** · "
            f"TP1 {g('R_tp1_full_net')} / TP2 {g('R_tp2_full_net')} / "
            f"trail {g('R_trail_net')}{his_s} · _{r.get('outcome_type','?')}_")


def post_webhook(new_rows, status=""):
    if not WEBHOOK_URL:
        return
    try:
        rows = list(csv.DictReader(open(P("trades_master.csv"), encoding="utf-8")))
    except FileNotFoundError:
        rows = []

    # color: green if the day's new trades net positive, red if negative, gray if none
    day_net = sum(float(r["realized_R"]) for r in new_rows
                  if r.get("realized_R") not in ("", None))
    color = 0x9aa0a6 if not new_rows else (0x2ecc71 if day_net >= 0 else 0xe74c3c)

    if new_rows:
        desc = "\n".join(_fmt_trade_line(r) for r in new_rows[:12])
        title = f"📊 Lanto Tracker — {len(new_rows)} new trade(s)"
    else:
        desc = "No new settled trades today."
        title = "📊 Lanto Tracker — quiet day"

    stat_lines = [f"{name:<13} n={n:>3}  win {win:3.0f}%  net {net:+6.1f}R"
                  for name, n, win, net, avg in _strategy_stats(rows)]
    fields = [{"name": "Mechanical strategies + his realized (net of costs)",
               "value": "```" + "\n".join(stat_lines) + "```", "inline": False}]

    embed = {"title": title, "description": desc[:4000], "color": color,
             "fields": fields,
             "footer": {"text": status or f"{len(rows)} trades on record"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    payload = {"username": "Lanto Tracker", "embeds": [embed]}
    try:
        req = urllib.request.Request(
            WEBHOOK_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "LantoTracker/1.0 (+https://railway.app)"})
        urllib.request.urlopen(req, timeout=30).read()
        log("  webhook posted")
    except Exception as e:
        log(f"  webhook failed: {e}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(dry=False):
    state = load_state()
    scored = set(state["scored_ids"])
    chart_map = load_chart_map()

    # 1) scrape. RESCRAPE_DAYS re-fetches a recent window (one-time backfill) so
    # messages stored before embeds were parsed get their text/charts refreshed.
    after = state["last_message_id"]
    rescrape = os.environ.get("RESCRAPE_DAYS")
    if rescrape:
        try:
            cutoff = now_utc() - timedelta(days=float(rescrape))
            after = min(int(after), snowflake_for(cutoff))
            log(f"  RESCRAPE_DAYS={rescrape}: re-fetching from {cutoff:%Y-%m-%d}")
        except ValueError:
            pass
    raw = fetch_new_messages(after)
    if raw:
        log(f"  fetched {len(raw)} message(s)")
        chart_map = download_charts(raw, chart_map, dry)
        if not dry:
            append_messages(raw)
            state["last_message_id"] = max(int(state["last_message_id"]),
                                           max(int(m["id"]) for m in raw))
            save_chart_map(chart_map)

    # 2) candidate entries from the full corpus, not yet processed
    msgs = load_messages()
    now = datetime.now(timezone.utc)

    # During a RESCRAPE, re-attempt entries we previously marked done WITHOUT
    # producing a trade (e.g. give-ups from when vision was broken). Only those
    # in the rescrape window and not already traded, so we don't churn old data.
    if rescrape and not dry:
        try:
            win_start = now_utc() - timedelta(days=float(rescrape))
            traded = set()
            try:
                with open(P("trades_master.csv"), encoding="utf-8") as f:
                    traded = {r["msg_id"] for r in csv.DictReader(f)}
            except FileNotFoundError:
                pass
            freed = 0
            for m in msgs:
                if m["id"] in scored and m["id"] not in traded and is_entry(m["text"]):
                    try:
                        if parse_ts(m["timestamp"]).astimezone(timezone.utc) >= win_start:
                            scored.discard(m["id"]); freed += 1
                    except (ValueError, KeyError):
                        pass
            if freed:
                log(f"  rescrape: re-attempting {freed} previously-skipped entr(ies)")
        except ValueError:
            pass

    bars_cache = {}
    new_rows, scored_now, gaveup = [], [], 0
    last_entry = None  # (ts, direction) of the most recently handled entry

    for idx, m in enumerate(msgs):
        if m["id"] in scored or not is_entry(m["text"]):
            continue
        et = parse_ts(m["timestamp"]).astimezone(timezone.utc)
        age_h = (now - et).total_seconds() / 3600
        if now < settle_time(et):
            continue  # the trade's Central session hasn't closed yet
        mdir = entry_direction(m, msgs, idx)
        if last_entry and last_entry[1] == mdir and \
                (et - last_entry[0]).total_seconds() <= MERGE_WINDOW_MIN * 60:
            scored_now.append(m["id"])   # re-entry of the prior trade: don't double-count
            last_entry = (et, mdir)
            continue
        last_entry = (et, mdir)
        kind, row = score_entry(m, msgs, idx, chart_map, bars_cache, dry)
        if kind == "trade":
            new_rows.append(row)
            scored_now.append(m["id"])
            log(f"  scored {m['id']}  {row['instrument']} {row['direction']}  "
                f"TP1full {row['R_tp1_full_net']}R  ({row['outcome_type']})")
        elif kind == "nochart":
            if age_h >= NO_CHART_GIVEUP_HOURS:
                scored_now.append(m["id"])  # processed, no trade (matches the 77 historical)
                gaveup += 1
        elif kind == "nobars":
            if age_h >= PRICE_WINDOW_GIVEUP_HOURS:
                scored_now.append(m["id"])  # bars will never arrive (older than window)
                gaveup += 1
                log(f"  {m['id']} too old for price window — giving up")
            else:
                log(f"  {m['id']} pending price bars — will retry next run")

    # 3) persist
    if not dry:
        for r in new_rows:
            append_trade_row(r)
        scored.update(scored_now)
        state["scored_ids"] = sorted(scored, key=int)
        save_state(state)

    summary = write_stats_summary() if not dry else "(dry-run: stats not written)"
    status = (f"cursor {state['last_message_id']} · {trades_count()} trades · "
              f"{len(scored)} processed")
    log(f"  new trades: {len(new_rows)} | newly processed: {len(scored_now)} "
        f"(of which no-chart give-ups: {gaveup})")
    if not dry:
        post_webhook(new_rows, status)
    log("daily_update complete")
    return new_rows


def selftest():
    """Offline: re-score the newest existing trade from the masters and compare
    the mechanical R columns. Exercises simulate + cost + text parsing without
    touching Discord or writing anything."""
    msgs = load_messages()
    by_id = {m["id"]: i for i, m in enumerate(msgs)}
    trades = list(csv.DictReader(open(P("trades_master.csv"), encoding="utf-8")))
    cm = load_chart_map()  # historical charts likely absent -> we test text+cost math only
    log("self-test: cost model + text classifier on the 3 newest trades")
    for tr in trades[-3:]:
        # cost-model check (independent of vision)
        try:
            entry, stop = float(tr["entry"]), float(tr["stop"])
            instrument = tr["instrument"]
            risk = abs(entry - stop)
            cr = cost_r(instrument, risk) if risk else 0.0
            log(f"  {tr['msg_id']} {instrument} risk={risk:g}pts  costR={cr:.3f}  "
                f"(stored TP1full={tr['R_tp1_full_net']})")
        except Exception as e:
            log(f"  {tr['msg_id']}: {e}")
        # text classifier check
        i = by_id.get(tr["msg_id"])
        if i is not None:
            wt = outcome_window_text(msgs[i], msgs, i)
            tps_present = sum(1 for k in ("tp1", "tp2", "tp3") if tr[k])
            r, tp, ot = classify_outcome(wt, tps_present)
            mark = "OK" if ot == tr["outcome_type"] else "DIFFERS"
            log(f"      classifier -> {ot}/{tp}/realized={r}  | stored "
                f"{tr['outcome_type']}/{tr['text_tp_hit']}/{tr['realized_R']}  [{mark}]")
    log("self-test done (mechanical R math is exact; text labels are best-effort)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run(dry="--dry-run" in sys.argv)
