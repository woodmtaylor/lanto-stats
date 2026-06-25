"""
analytics.py — aggregate performance report for the Lanto tracker.

Builds: full metric suite (per strategy + his realized R), monthly/weekly R and
win rates, by-instrument and by-direction splits, plus two images (equity curves
and a 2x2 dashboard). If WEBHOOK_URL is set it posts an illustrated report to
Discord; otherwise it just writes the PNGs + a text summary into DATA_DIR.

Run directly any time:   python analytics.py
run_pipeline.py invokes it on a cadence (default: Sundays).
"""
import os, csv, json, uuid, io
import urllib.request
from collections import defaultdict, OrderedDict
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DATA = os.environ.get("DATA_DIR") or os.getcwd()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

STRATS = OrderedDict([
    ("R_tp1_full_net", "TP1 full"),
    ("R_tp2_full_net", "TP2 full"),
    ("R_tp1half_tp2_net", "TP1 half+TP2"),
    ("R_trail_net", "Trailing"),
    ("realized_R", "His realized"),
])
PALETTE = {"TP1 full": "#3b82f6", "TP2 full": "#22c55e", "TP1 half+TP2": "#a855f7",
           "Trailing": "#f59e0b", "His realized": "#ef4444"}


def P(name):
    return os.path.join(DATA, name)


def load_rows():
    try:
        rows = list(csv.DictReader(open(P("trades_master.csv"), encoding="utf-8")))
    except FileNotFoundError:
        return []
    rows.sort(key=lambda r: r.get("entry_ts", ""))
    return rows


def series(rows, col):
    """Chronological list of (datetime, R) for trades where col is populated."""
    out = []
    for r in rows:
        v = r.get(col, "")
        if v in ("", None):
            continue
        try:
            dt = datetime.fromisoformat(r["entry_ts"])
            out.append((dt, float(v)))
        except (ValueError, KeyError):
            continue
    return out


def metrics(vals):
    """vals = list of R floats. Returns a dict of summary metrics."""
    if not vals:
        return None
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    cum, peak, maxdd = 0.0, 0.0, 0.0
    for v in vals:
        cum += v
        peak = max(peak, cum)
        maxdd = min(maxdd, cum - peak)
    mcw = mcl = cw = cl = 0
    for v in vals:
        if v > 0:
            cw += 1; cl = 0
        elif v < 0:
            cl += 1; cw = 0
        mcw, mcl = max(mcw, cw), max(mcl, cl)
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    return {
        "n": len(vals), "win": len(wins) / len(vals) * 100,
        "net": sum(vals), "avg": sum(vals) / len(vals),
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "pf": (gross_w / gross_l) if gross_l else float("inf"),
        "best": max(vals), "worst": min(vals),
        "maxdd": maxdd, "mcw": mcw, "mcl": mcl,
    }


def grouped(rows, col, keyfn):
    """net R, win%, n grouped by keyfn(datetime)."""
    g = defaultdict(list)
    for dt, v in series(rows, col):
        g[keyfn(dt)].append(v)
    out = OrderedDict()
    for k in sorted(g):
        vv = g[k]
        out[k] = (sum(vv), sum(1 for x in vv if x > 0) / len(vv) * 100, len(vv))
    return out


def split(rows, col, field):
    g = defaultdict(list)
    for r in rows:
        v = r.get(col, "")
        key = r.get(field, "")
        if v in ("", None) or key in ("", None):
            continue
        try:
            g[key].append(float(v))
        except ValueError:
            continue
    return {k: (sum(v), sum(1 for x in v if x > 0) / len(v) * 100, len(v))
            for k, v in g.items()}


# --------------------------------------------------------------------------- #
# Text report
# --------------------------------------------------------------------------- #
def text_report(rows):
    L = [f"LANTO TRACKER — performance report  ({datetime.now(timezone.utc):%Y-%m-%d}Z)",
         f"trades on record: {len(rows)}", ""]
    L.append(f"{'strategy':<14}{'n':>4}{'win%':>6}{'netR':>8}{'avgR':>7}"
             f"{'PF':>6}{'maxDD':>7}{'MCW':>5}{'MCL':>5}")
    for col, name in STRATS.items():
        m = metrics([v for _, v in series(rows, col)])
        if not m:
            continue
        pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
        L.append(f"{name:<14}{m['n']:>4}{m['win']:>6.0f}{m['net']:>+8.1f}{m['avg']:>+7.2f}"
                 f"{pf:>6}{m['maxdd']:>7.1f}{m['mcw']:>5}{m['mcl']:>5}")
    # monthly for his realized
    L += ["", "Monthly (his realized R):"]
    for k, (net, win, n) in grouped(rows, "realized_R", lambda d: d.strftime("%Y-%m")).items():
        L.append(f"  {k}:  net {net:+6.1f}R   win {win:3.0f}%   n={n}")
    # splits
    for label, field in [("By instrument", "instrument"), ("By direction", "direction")]:
        L += ["", f"{label} (his realized R):"]
        for k, (net, win, n) in sorted(split(rows, "realized_R", field).items()):
            L.append(f"  {k:<6} net {net:+6.1f}R   win {win:3.0f}%   n={n}")
    text = "\n".join(L) + "\n"
    with open(P("performance_report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def chart_equity(rows):
    plt.style.use("default")
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=130)
    for col, name in STRATS.items():
        s = series(rows, col)
        if not s:
            continue
        xs = [dt for dt, _ in s]
        cum, c = [], 0.0
        for _, v in s:
            c += v; cum.append(c)
        ax.plot(xs, cum, label=f"{name} ({cum[-1]:+.0f}R)",
                color=PALETTE[name], lw=2.2 if name == "His realized" else 1.6,
                alpha=0.95 if name == "His realized" else 0.8)
    ax.axhline(0, color="#888", lw=0.8)
    ax.set_title("Cumulative R — equity curves", fontsize=13, weight="bold")
    ax.set_ylabel("Cumulative R")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    path = P("equity.png")
    fig.savefig(path); plt.close(fig)
    return path


def chart_dashboard(rows):
    fig, axs = plt.subplots(2, 2, figsize=(11, 8), dpi=130)

    # monthly net R (his realized) + win% overlay
    m = grouped(rows, "realized_R", lambda d: d.strftime("%Y-%m"))
    months = list(m.keys())
    nets = [m[k][0] for k in months]
    wins = [m[k][1] for k in months]
    ax = axs[0, 0]
    bars = ax.bar(months, nets, color=["#22c55e" if x >= 0 else "#ef4444" for x in nets])
    ax.set_title("Monthly net R (his realized)", weight="bold", fontsize=11)
    ax.axhline(0, color="#888", lw=0.8)
    ax.tick_params(axis="x", rotation=45, labelsize=8)

    ax = axs[0, 1]
    ax.plot(months, wins, marker="o", color="#3b82f6")
    ax.set_title("Monthly win % (his realized)", weight="bold", fontsize=11)
    ax.set_ylim(0, 100); ax.axhline(50, color="#888", lw=0.8, ls="--")
    ax.tick_params(axis="x", rotation=45, labelsize=8); ax.grid(True, alpha=0.25)

    # by instrument net R across strategies
    ax = axs[1, 0]
    instrs = sorted({r.get("instrument", "?") for r in rows} & {"ES", "NQ"})
    x = range(len(instrs))
    for i, (col, name) in enumerate(list(STRATS.items())[:2] + [("realized_R", "His realized")]):
        sp = split(rows, col, "instrument")
        ax.bar([xx + i * 0.25 for xx in x], [sp.get(k, (0,))[0] for k in instrs],
               width=0.25, label=name, color=PALETTE[name])
    ax.set_xticks([xx + 0.25 for xx in x]); ax.set_xticklabels(instrs)
    ax.set_title("Net R by instrument", weight="bold", fontsize=11)
    ax.axhline(0, color="#888", lw=0.8); ax.legend(fontsize=8)

    # drawdown underwater (his realized)
    ax = axs[1, 1]
    s = series(rows, "realized_R")
    cum, peak, under, xs = 0.0, 0.0, [], []
    for dt, v in s:
        cum += v; peak = max(peak, cum); under.append(cum - peak); xs.append(dt)
    ax.fill_between(xs, under, 0, color="#ef4444", alpha=0.4)
    ax.set_title("Drawdown underwater (his realized)", weight="bold", fontsize=11)
    ax.set_ylabel("R below peak"); ax.grid(True, alpha=0.25)
    ax.tick_params(axis="x", rotation=45, labelsize=8)

    fig.tight_layout()
    path = P("dashboard.png")
    fig.savefig(path); plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Discord post (multipart with image attachments)
# --------------------------------------------------------------------------- #
def _multipart(payload_json, files):
    boundary = "----lanto" + uuid.uuid4().hex
    body = io.BytesIO()

    def w(s):
        body.write(s.encode() if isinstance(s, str) else s)

    w(f"--{boundary}\r\n")
    w('Content-Disposition: form-data; name="payload_json"\r\n')
    w("Content-Type: application/json\r\n\r\n")
    w(json.dumps(payload_json)); w("\r\n")
    for i, (fname, data) in enumerate(files):
        w(f"--{boundary}\r\n")
        w(f'Content-Disposition: form-data; name="files[{i}]"; filename="{fname}"\r\n')
        w("Content-Type: image/png\r\n\r\n")
        w(data); w("\r\n")
    w(f"--{boundary}--\r\n")
    return boundary, body.getvalue()


def post_report(rows, img_paths):
    if not WEBHOOK_URL:
        return False
    realized = metrics([v for _, v in series(rows, "realized_R")])
    tp1 = metrics([v for _, v in series(rows, "R_tp1_full_net")])
    color = 0x2ecc71 if (realized and realized["net"] >= 0) else 0xe74c3c

    def line(name, m):
        if not m:
            return ""
        pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
        return (f"{name:<13} n={m['n']:>3} win {m['win']:3.0f}% net {m['net']:+6.1f}R "
                f"avg {m['avg']:+.2f} PF {pf} DD {m['maxdd']:.1f}")

    stat_block = "```" + "\n".join(
        l for l in (line(nm, metrics([v for _, v in series(rows, c)]))
                    for c, nm in STRATS.items()) if l) + "```"

    embeds = [
        {"title": "📈 Lanto Tracker — performance report",
         "description": f"{len(rows)} trades on record\n{stat_block}",
         "color": color, "image": {"url": "attachment://equity.png"},
         "timestamp": datetime.now(timezone.utc).isoformat()},
        {"image": {"url": "attachment://dashboard.png"}},
    ]
    files = [(os.path.basename(p), open(p, "rb").read()) for p in img_paths]
    payload = {"username": "Lanto Tracker", "embeds": embeds}
    boundary, body = _multipart(payload, files)
    req = urllib.request.Request(
        WEBHOOK_URL, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "LantoTracker/1.0 (+https://railway.app)"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
        print("analytics: webhook posted", flush=True)
        return True
    except Exception as e:
        print(f"analytics: webhook failed: {e}", flush=True)
        return False


def main():
    rows = load_rows()
    if not rows:
        print("analytics: no trades yet", flush=True)
        return
    text_report(rows)
    imgs = [chart_equity(rows), chart_dashboard(rows)]
    print(f"analytics: built report over {len(rows)} trades -> {', '.join(imgs)}", flush=True)
    post_report(rows, imgs)


if __name__ == "__main__":
    main()
