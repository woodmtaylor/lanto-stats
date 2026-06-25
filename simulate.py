"""
Simulate four exit strategies for one trade against 1-minute OHLC bars.

A trade is defined by:
    direction : 'long' or 'short'
    entry     : fill price
    stop      : initial stop price (defines 1R = |entry - stop|)
    tps       : [TP1, TP2, TP3]  (TP3 optional; used by trailing)
    bars      : chronological list of bars AFTER entry, each {h,l,c,...}

Returns realized R for each strategy. Conventions:
  * Intrabar tie-break is CONSERVATIVE: if a bar's range contains both the
    stop and a target, the stop is assumed hit first (worst case).
  * Strategy 3 moves the runner's stop to breakeven after TP1 fills.
  * Strategy 4 ('trail') ratchets the stop behind each target reached:
    hit TP1 -> stop to entry; hit TP2 -> stop to TP1; final target TP3.
    (This is one defined trailing rule among several — see notes.)
"""

def _r(direction, entry, risk, price):
    return (price - entry) / risk if direction == "long" else (entry - price) / risk


def _hit_target(direction, bar, level):
    return bar["h"] >= level if direction == "long" else bar["l"] <= level


def _hit_stop(direction, bar, stop):
    return bar["l"] <= stop if direction == "long" else bar["h"] >= stop


def simulate(direction, entry, stop, tps, bars):
    risk = abs(entry - stop)
    if risk == 0:
        return {k: None for k in ("tp1_full", "tp2_full", "tp1half_tp2", "trail")}
    tp1, tp2 = tps[0], tps[1]
    tp3 = tps[2] if len(tps) > 2 and tps[2] is not None else None

    # ---- Strategy 1: full exit at TP1 (stop fixed) ----
    def single_target(tp):
        if tp is None:
            return None
        for b in bars:
            stop_in = _hit_stop(direction, b, stop)
            tp_in = _hit_target(direction, b, tp)
            if stop_in and tp_in:
                return -1.0                      # conservative: stop first
            if stop_in:
                return -1.0
            if tp_in:
                return _r(direction, entry, risk, tp)
        return _r(direction, entry, risk, bars[-1]["c"]) if bars else 0.0  # still open -> mark-to-last

    s1 = single_target(tp1)
    s2 = single_target(tp2)

    # ---- Strategy 3: half at TP1, runner to TP2 with stop -> breakeven ----
    def half_then_runner():
        if tp1 is None or tp2 is None:
            return None
        cur_stop = stop
        i = 0
        # phase 1: to TP1 or stop
        while i < len(bars):
            b = bars[i]
            stop_in = _hit_stop(direction, b, cur_stop)
            tp_in = _hit_target(direction, b, tp1)
            if stop_in and tp_in:
                return -1.0
            if stop_in:
                return -1.0
            if tp_in:
                break
            i += 1
        else:
            return _r(direction, entry, risk, bars[-1]["c"]) if bars else 0.0
        first_half = 0.5 * _r(direction, entry, risk, tp1)
        cur_stop = entry  # breakeven on runner
        # phase 2: runner to TP2 or breakeven
        for b in bars[i + 1:]:
            stop_in = _hit_stop(direction, b, cur_stop)
            tp_in = _hit_target(direction, b, tp2)
            if stop_in and tp_in:
                return first_half + 0.5 * _r(direction, entry, risk, cur_stop)
            if stop_in:
                return first_half + 0.5 * _r(direction, entry, risk, cur_stop)
            if tp_in:
                return first_half + 0.5 * _r(direction, entry, risk, tp2)
        return first_half + 0.5 * _r(direction, entry, risk, bars[-1]["c"]) if bars else first_half

    s3 = half_then_runner()

    # ---- Strategy 4: continuous trail, ratchet behind each target, final TP3 ----
    def trail():
        if tp1 is None:
            return None
        targets = [tp1, tp2] + ([tp3] if tp3 is not None else [])
        targets = [t for t in targets if t is not None]
        stop_levels = ([entry, tp1] + ([tp2] if tp3 is not None else []))[:len(targets)]
        cur_stop = stop
        reached = 0
        for b in bars:
            if _hit_stop(direction, b, cur_stop):
                return _r(direction, entry, risk, cur_stop)
            while reached < len(targets) and _hit_target(direction, b, targets[reached]):
                cur_stop = stop_levels[reached]
                reached += 1
                if reached == len(targets):           # final target reached
                    return _r(direction, entry, risk, targets[-1])
        return _r(direction, entry, risk, bars[-1]["c"]) if bars else 0.0

    s4 = trail()

    return {"tp1_full": s1, "tp2_full": s2, "tp1half_tp2": s3, "trail": s4}
