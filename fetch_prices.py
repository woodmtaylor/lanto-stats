"""
fetch_prices.py — pull rolling 5-day 1-minute ES/NQ from yfinance and write
ES.csv / NQ.csv in the exact format price_engine.py expects (US Central time).

Overwrites each run (rolling window). On failure for a symbol, the OLD file is
left untouched (never wiped), and it exits non-zero so the daily job can log it.

    python fetch_prices.py
"""
import sys, time
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
SYMS = [("ES", "ES=F", "ES.csv"), ("NQ", "NQ=F", "NQ.csv")]


def fetch(ticker, retries=5):
    for a in range(retries):
        try:
            df = yf.download(ticker, period="5d", interval="1m",
                             progress=False, auto_adjust=False)
            if df is not None and len(df) > 0:
                return df
            print(f"  {ticker}: empty response (attempt {a+1})")
        except Exception as e:
            print(f"  {ticker}: attempt {a+1} failed: {e}")
        time.sleep(5 * (a + 1))
    return None


def write_csv(df, path, sym):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    idx = df.index
    idx = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(CENTRAL)
    cols = df[["Open", "High", "Low", "Close", "Volume"]].values
    with open(path, "w", newline="") as f:
        f.write("Date, Time, Open, High, Low, Last, Volume\n")
        for ts, (o, h, l, c, v) in zip(idx, cols):
            if any(pd.isna(x) for x in (o, h, l, c)):
                continue
            date = f"{ts.year}-{ts.month}-{ts.day}" if sym == "ES" else f"{ts.month}/{ts.day}/{ts.year}"
            tm = ts.strftime("%H:%M:%S.000000")
            vol = int(v) if pd.notna(v) else 0
            f.write(f"{date}, {tm}, {o:.2f}, {h:.2f}, {l:.2f}, {c:.2f}, {vol}\n")


def main():
    ok = True
    for sym, ticker, path in SYMS:
        df = fetch(ticker)
        if df is None:
            print(f"{sym}: FAILED — keeping existing {path}")
            ok = False
            continue
        write_csv(df, path, sym)
        print(f"{sym}: wrote {len(df)} bars -> {path}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
