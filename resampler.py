# resampler.py
# ─────────────────────────────────────────────────────────────────────────────
# Replay bar resampling + historical/replay split + S/R calculation.
# Live data / WebSocket / REST polling se bilkul alag hai.
#
# Usage (main.py mein):
#   from resampler import resample_bars, split_historical_replay, calc_sr_levels, to_ohlc_output, SUPPORTED_TF
# ─────────────────────────────────────────────────────────────────────────────

# ── BankNifty: bar-count based ───────────────────────────────────────────────
# Market sirf 9:15–15:30 chalta hai → 75 bars/day (5m each).
# Weekends/holidays mein gaps hain isliye timestamp se group karna galat hoga.
# 3d/9d/27d ke liye pehle 1d candles banao, phir unhe group karo —
# taki calendar-aligned boundaries milein (arbitrary file-start grouping nahi).

_BN_TF_BARS: dict[str, int] = {
    "5m":   1,
    "15m":  3,
    "45m":  9,
    "135m": 27,
    "1d":   75,    # 1 trading day = 75 bars of 5m
    "3d":   225,
    "9d":   675,
    "27d":  2025,
}

# Multi-day TFs jo 1d candles se banenge (calendar-aligned)
_BN_MULTIDAY: dict[str, int] = {
    "3d":  3,
    "9d":  9,
    "27d": 27,
}

# ── BTC: timestamp bucket based ──────────────────────────────────────────────
# 24x7 continuous market → koi gap nahi.
# UTC boundary pe snap karo → clean alignment milti hai.

_BTC_TF_SECS: dict[str, int] = {
    "5m":   300,
    "160m": 9_600,
    "8h":   28_800,
    "1d":   86_400,
    "3d":   259_200,
    "9d":   777_600,
    "27d":  2_332_800,
}

# ── S/R rolling window (last N resampled candles) ────────────────────────────
_SR_WINDOW: dict[str, int] = {
    "5m":   500,
    "15m":  300,
    "45m":  200,
    "135m": 150,
    "160m": 150,
    "8h":   120,
    "1d":   200,
    "3d":   100,
    "9d":   60,
    "27d":  40,
}
_SR_WINDOW_DEFAULT = 200

# ── Public: supported TF per asset ───────────────────────────────────────────
SUPPORTED_TF: dict[str, list[str]] = {
    "bn":  list(_BN_TF_BARS.keys()),
    "btc": list(_BTC_TF_SECS.keys()),
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_candle(grp: list[dict]) -> dict:
    """Group of raw rows → single OHLC candle dict (internal t/o/h/l/c format)."""
    return {
        "t": grp[0]["t"],
        "o": grp[0]["o"],
        "h": max(r["h"] for r in grp),
        "l": min(r["l"] for r in grp),
        "c": grp[-1]["c"],
    }


def _resample_bn(rows: list[dict], tf: str) -> list[dict]:
    """
    BankNifty resampling:
    - 5m/15m/45m/135m → simple bar-count grouping from 5m data
    - 1d              → group 75 bars (one trading session)
    - 3d/9d/27d       → pehle 1d candles banao, phir N-day groups
                        (calendar-aligned, file-start arbitrary grouping nahi)
    """
    tf = tf.lower()

    # Multi-day: 1d se group karo
    if tf in _BN_MULTIDAY:
        n_days  = _BN_MULTIDAY[tf]
        # Step 1: 5m → 1d
        day_candles = _group_by_count(rows, 75)
        # Step 2: 1d → Nd (aligned from first available trading day)
        return _group_by_count(day_candles, n_days)

    factor = _BN_TF_BARS.get(tf, 0)
    if factor <= 1:
        return rows
    return _group_by_count(rows, factor)


def _group_by_count(rows: list[dict], n: int) -> list[dict]:
    """Simple fixed-count grouping."""
    out = []
    for i in range(0, len(rows), n):
        grp = rows[i : i + n]
        if grp:
            out.append(_make_candle(grp))
    return out


def _resample_btc(rows: list[dict], tf: str) -> list[dict]:
    """BTC: snap each row to UTC time bucket, then aggregate."""
    interval = _BTC_TF_SECS.get(tf.lower(), 0)
    if interval <= 300:
        return rows
    out: list[dict] = []
    bucket_start: int | None = None
    grp: list[dict] = []
    for row in rows:
        bucket = (row["t"] // interval) * interval
        if bucket != bucket_start:
            if grp:
                out.append(_make_candle(grp))
            grp          = [row]
            bucket_start = bucket
        else:
            grp.append(row)
    if grp:
        out.append(_make_candle(grp))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def resample_bars(rows: list[dict], asset: str, tf: str) -> tuple[list[dict], str]:
    """
    Resample raw 5m candle rows to the requested timeframe.

    Parameters
    ----------
    rows  : list of dicts with keys  t, o, h, l, c  (t = UTC epoch seconds)
    asset : "bn"  → BankNifty bar-count / calendar-aligned logic
            "btc" → BTC timestamp-bucket logic
    tf    : timeframe string e.g. "15m", "1d", "160m", "3d"

    Returns
    -------
    (resampled_rows, info_message)
    """
    asset = asset.lower()
    tf    = tf.lower()

    if asset == "bn":
        if tf not in _BN_TF_BARS:
            return rows, f"[resampler] BN: unknown TF '{tf}', returning raw"
        result = _resample_bn(rows, tf)
        msg    = f"[resampler] BN: {len(rows)} 5m → {len(result)} {tf}"

    elif asset == "btc":
        if tf not in _BTC_TF_SECS:
            return rows, f"[resampler] BTC: unknown TF '{tf}', returning raw"
        result = _resample_btc(rows, tf)
        msg    = f"[resampler] BTC: {len(rows)} 5m → {len(result)} {tf}"

    else:
        return rows, f"[resampler] Unknown asset '{asset}', returning raw"

    return result, msg


def split_historical_replay(
    candles: list[dict],
    start_ts: int,
    end_ts: int,
) -> tuple[list[dict], list[dict]]:
    """
    Resampled candles ko do zones mein split karo.

    Parameters
    ----------
    candles  : already resampled candles (t/o/h/l/c format, sorted ascending)
    start_ts : replay start (UTC epoch seconds) — user selected
    end_ts   : replay end   (UTC epoch seconds) — user selected

    Returns
    -------
    (historical, replay)

    historical : candles with t < start_ts
                 S/R calculation ke liye — future leak nahi hoga
    replay     : candles with start_ts <= t <= end_ts
                 Bar-by-bar replay ke liye
    """
    historical = [c for c in candles if c["t"] <  start_ts]
    replay     = [c for c in candles if start_ts <= c["t"] <= end_ts]
    return historical, replay


def calc_sr_levels(
    candles: list[dict],
    tf: str,
    n_levels: int = 5,
) -> dict:
    """
    Rolling window se Support/Resistance levels calculate karo.

    Algorithm:
    - Last N candles ka window lo (TF ke hisaab se)
    - Swing highs (local maxima) → Resistance
    - Swing lows  (local minima) → Support
    - Strength = kitni baar price us level ke paas aaya (touch count)

    Parameters
    ----------
    candles  : candles list jis pe S/R calculate karni hai
               (historical candles, ya historical + replay bars so far)
    tf       : timeframe string — window size decide karne ke liye
    n_levels : kitne S/R levels return karne hain (default 5 each)

    Returns
    -------
    {
        "resistance": [{"price": float, "strength": int}, ...],  # high to low
        "support":    [{"price": float, "strength": int}, ...],  # low to high
        "window_used": int   # kitne candles use hue
    }
    """
    if len(candles) < 3:
        return {"resistance": [], "support": [], "window_used": 0}

    window = _SR_WINDOW.get(tf.lower(), _SR_WINDOW_DEFAULT)
    data   = candles[-window:]  # rolling window — last N bars

    highs  = [c["h"] for c in data]
    lows   = [c["l"] for c in data]
    closes = [c["c"] for c in data]

    # ── Swing detection (3-bar pivot) ────────────────────────────────────────
    # Swing high: middle bar ka high, dono sides se zyada
    # Swing low:  middle bar ka low,  dono sides se kam
    swing_highs: list[float] = []
    swing_lows:  list[float] = []

    for i in range(1, len(data) - 1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            swing_lows.append(lows[i])

    # ── Cluster nearby levels (price range = 0.3% of last close) ─────────────
    last_close = closes[-1] if closes else 1.0
    tolerance  = last_close * 0.003

    def _cluster(levels: list[float]) -> list[dict]:
        if not levels:
            return []
        levels_sorted = sorted(levels)
        clusters: list[list[float]] = []
        cur = [levels_sorted[0]]
        for p in levels_sorted[1:]:
            if p - cur[-1] <= tolerance:
                cur.append(p)
            else:
                clusters.append(cur)
                cur = [p]
        clusters.append(cur)
        result = []
        for cl in clusters:
            avg      = sum(cl) / len(cl)
            strength = len(cl)   # touch count = cluster size
            result.append({"price": round(avg, 2), "strength": strength})
        # Sort by strength desc
        result.sort(key=lambda x: -x["strength"])
        return result

    resistance = _cluster(swing_highs)[:n_levels]
    support    = _cluster(swing_lows)[:n_levels]

    # Resistance: high to low order for chart display
    resistance.sort(key=lambda x: -x["price"])
    # Support: low to high order
    support.sort(key=lambda x: x["price"])

    return {
        "resistance": resistance,
        "support":    support,
        "window_used": len(data),
    }


def to_ohlc_output(rows: list[dict]) -> list[dict]:
    """
    Internal t/o/h/l/c dicts → chart-ready time/open/high/low/close dicts.
    Call this AFTER resample_bars / split, just before sending to client.
    """
    out = []
    for r in rows:
        try:
            out.append({
                "time":  int(r["t"]),
                "open":  round(float(r["o"]), 2),
                "high":  round(float(r["h"]), 2),
                "low":   round(float(r["l"]), 2),
                "close": round(float(r["c"]), 2),
            })
        except Exception:
            pass
    return out
