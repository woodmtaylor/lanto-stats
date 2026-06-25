"""
validate_export.py — turn a Discrub HTML chat export into a trade ledger and
(optionally) score the bot's text classifier against a truth file.

Usage:
    python validate_export.py path/to/export_dir            # build ledger -> ledger.csv
    python validate_export.py export.zip                    # zip is auto-extracted
    python validate_export.py export_dir --truth truth.csv  # also report accuracy

It reuses daily_update's real classifier/window logic, so the numbers reflect
exactly what the live bot would label. Charts are listed (not read) so you can
eyeball vision separately. Set OUTCOME_LLM=1 (+ ANTHROPIC_API_KEY) to score with
the LLM extractor instead of regex.

truth.csv columns (any subset): entry_ts, outcome_type, realized_R
  entry_ts must match the ledger's entry_ts (the export timestamp string).
"""
import os, sys, re, html, csv, glob, json, zipfile, tempfile
from datetime import datetime, timezone, timedelta
import daily_update as d


def parse_export(root):
    pages = sorted(glob.glob(os.path.join(root, "**", "lanto-page-*.html"), recursive=True))
    if not pages:
        pages = sorted(glob.glob(os.path.join(root, "**", "*.html"), recursive=True))

    def clean(s):
        s = re.sub(r"<br\s*/?>", "\n", s)
        return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()

    msgs = []
    for page in pages:
        t = open(page, encoding="utf-8").read()
        for b in re.split(r'(?=<div class="message" id="msg-)', t):
            mid = re.search(r'data-message-id="(\d+)"', b)
            if not mid:
                continue
            ts = re.search(r'<span class="timestamp">([^<]+)</span>', b)
            titles = [clean(x) for x in re.findall(r'class="embed-title"[^>]*>(.*?)</div>', b, re.S)]
            descs = [clean(x) for x in re.findall(r'class="embed-description"[^>]*>(.*?)</div>', b, re.S)]
            fields = [clean(x) for x in re.findall(r'class="embed-field[^"]*"[^>]*>(.*?)</div>', b, re.S)]
            body = re.search(r'<div class="message-text">(.*?)</div>', b, re.S)
            mtext = clean(body.group(1)) if body else ""
            imgs = sorted(set(re.findall(r'src="(media/(?:embed-images|attachments)/[^"]+)"', b)))
            embed = "\n".join(x for x in titles + descs + fields if x)
            text = (embed + ("\n" + mtext if mtext else "")).strip()
            msgs.append({"id": mid.group(1), "ts_raw": ts.group(1) if ts else "",
                         "text": text, "imgs": imgs})
    # dedupe, timestamp, sort
    uniq = {m["id"]: m for m in msgs}
    msgs = list(uniq.values())
    for m in msgs:
        try:
            dt = datetime.strptime(m["ts_raw"], "%m/%d/%Y %I:%M %p").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
        m["dt"] = dt
        m["timestamp"] = dt.isoformat() if dt else ""
    msgs = [m for m in msgs if m["dt"]]
    msgs.sort(key=lambda m: m["dt"])
    return msgs


def build_ledger(msgs):
    rows, last = [], None
    for idx, m in enumerate(msgs):
        if not d.is_entry(m["text"]):
            continue
        et = m["dt"]
        mdir = d.entry_direction(m, msgs, idx)
        if last and last[1] == mdir and (et - last[0]).total_seconds() <= d.MERGE_WINDOW_MIN * 60:
            last = (et, mdir)
            continue  # re-entry of same trade
        last = (et, mdir)
        w = d.outcome_window_text(m, msgs, idx)
        llm = d.llm_outcome(w)
        if llm is not None:
            r, tp, ot = llm
            src = "llm"
        else:
            r, tp, ot = d.classify_outcome(w, 2)
            src = "regex"
        rows.append({"entry_ts": m["ts_raw"], "msg_id": m["id"], "direction": mdir or "?",
                     "charts": ";".join(os.path.basename(x) for x in m["imgs"]),
                     "outcome_type": ot, "text_tp_hit": tp,
                     "realized_R": "" if r is None else r, "src": src,
                     "window_snippet": w[:160].replace("\n", " ")})
    return rows


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    path = sys.argv[1]
    truth_path = None
    if "--truth" in sys.argv:
        truth_path = sys.argv[sys.argv.index("--truth") + 1]

    root = path
    if path.endswith(".zip"):
        root = tempfile.mkdtemp()
        with zipfile.ZipFile(path) as z:
            z.extractall(root)

    msgs = parse_export(root)
    print(f"parsed {len(msgs)} messages, {sum(1 for m in msgs if m['imgs'])} with charts")
    rows = build_ledger(msgs)
    print(f"identified {len(rows)} trades "
          f"(scored via {'LLM' if rows and rows[0]['src']=='llm' else 'regex'})\n")

    out = "ledger.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["entry_ts", "msg_id", "direction", "charts", "outcome_type",
                            "text_tp_hit", "realized_R", "src", "window_snippet"])
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['entry_ts']:>20}  {r['direction']:5} {r['outcome_type']:<11} "
              f"R={str(r['realized_R']):<6} charts={r['charts'][:40]}")
    print(f"\nwrote {out}")

    if truth_path:
        truth = {}
        for t in csv.DictReader(open(truth_path)):
            truth[t["entry_ts"]] = (t.get("outcome_type", ""), t.get("realized_R", ""))
        okt = okr = tot = 0
        print("\n--- accuracy vs truth ---")
        for r in rows:
            if r["entry_ts"] not in truth:
                continue
            ot_t, r_t = truth[r["entry_ts"]]
            tot += 1
            tt = (r["outcome_type"] == ot_t)
            try:
                rr = (r_t == "" and r["realized_R"] == "") or \
                     abs(float(r_t) - float(r["realized_R"])) < 0.01
            except (ValueError, TypeError):
                rr = (str(r_t) == str(r["realized_R"]))
            okt += tt
            okr += rr
            if not (tt and rr):
                print(f"  MISS {r['entry_ts']}: got {r['outcome_type']}/{r['realized_R']} "
                      f"vs truth {ot_t}/{r_t}")
        if tot:
            print(f"\noutcome_type {okt}/{tot}={okt/tot*100:.0f}%   "
                  f"realized_R {okr}/{tot}={okr/tot*100:.0f}%")


if __name__ == "__main__":
    main()
