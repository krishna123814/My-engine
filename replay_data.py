"""
Replay Data Layer
==================
Step 1 of Replay feature.

Provides: get_replay_data(asset, start_date, end_date, sr_lookback=100) -> dict
  asset           : 'BANKNIFTY' or 'BTCUSDT'
  start_date      : 'YYYY-MM-DD'  (inclusive) — actual replay range
  end_date        : 'YYYY-MM-DD'  (inclusive) — actual replay range
  sr_lookback     : int (default 100) — sr_base mein kitne 1m candles chahiye

Returns:
  {
    "base": [[ts_ms, o, h, l, c], ...],   # Hidden replay engine candles (selected date range)
    "sr_base": [[ts_ms, o, h, l, c], ...], # Visible S/R candles (last N candles BEFORE start_date)
    "tfs": {
        "5m":  [[ts_ms,o,h,l,c], ...],
        "15m": [...],
        ...
    },
    "meta": {"asset":..., "start":..., "end":..., "base_interval_min":1, "sr_lookback":100}
  }

Architecture:
  sr_base  → chart par visible hote hain (rolling 100 window), S/R inhi se calculate hota hai
  base     → replay engine ke liye hidden, har candle complete hone par sr_base mein append
             hoti hai aur purani pehli candle hatt jaati hai (rolling window JS mein handle hota hai)

BankNifty source : bn_data_manager.load_bin()  (already saved 1m data)
BTC source       : Binance klines REST API, paginated by date range (no local
                   long-term store yet — fetched fresh per replay request,
                   then resampled in-memory; cheap since Binance allows
                   1000 candles/call and historical range queries).
"""

import datetime
import time
import requests

from bn_data_manager import load_bin

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

# ─── Timeframe definitions per asset ───────────────────────────────────────
# All in MINUTES except the day-multiples which are handled as "N trading/
# calendar days" groupings (since 1d/3d/9d/27d must align to session
# boundaries, not raw minute counts).
BANKNIFTY_TFS = {
    "5m":   {"type": "minute", "mins": 5},
    "15m":  {"type": "minute", "mins": 15},
    "45m":  {"type": "minute", "mins": 45},
    "135m": {"type": "minute", "mins": 135},
    "1d":   {"type": "day",    "days": 1},
    "3d":   {"type": "day",    "days": 3},
    "9d":   {"type": "day",    "days": 9},
    "27d":  {"type": "day",    "days": 27},
}

BTC_TFS = {
    "160m": {"type": "minute", "mins": 160},
    "8h":   {"type": "minute", "mins": 480},
    "1d":   {"type": "day",    "days": 1},
    "3d":   {"type": "day",    "days": 3},
    "9d":   {"type": "day",    "days": 9},
    "27d":  {"type": "day",    "days": 27},
}

BANKNIFTY_MIN_TF = "5m"
BTC_MIN_TF = "160m"


# ─── Date helpers ───────────────────────────────────────────────────────────

def _date_to_ts_range_ist(start_date: str, end_date: str) -> tuple:
    """Returns (start_ts_ms, end_ts_ms) covering the IST calendar-day range,
    inclusive of end_date's full day."""
    sd = datetime.datetime.strptime(start_date, "%Y-%m-%d")
    ed = datetime.datetime.strptime(end_date, "%Y-%m-%d") + datetime.timedelta(days=1)
    start_ts = int((sd - IST_OFFSET - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    end_ts   = int((ed - IST_OFFSET - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    return start_ts, end_ts


def _date_to_ts_range_utc(start_date: str, end_date: str) -> tuple:
    """Same as above but for UTC-based assets (crypto trades 24/7, no IST session)."""
    sd = datetime.datetime.strptime(start_date, "%Y-%m-%d")
    ed = datetime.datetime.strptime(end_date, "%Y-%m-%d") + datetime.timedelta(days=1)
    start_ts = int((sd - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    end_ts   = int((ed - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    return start_ts, end_ts


# ─── BankNifty: load + filter from existing bin ────────────────────────────

def _load_banknifty_1m(start_date: str, end_date: str) -> list:
    rows = load_bin(auto_download=True)
    if not rows:
        return []
    start_ts, end_ts = _date_to_ts_range_ist(start_date, end_date)
    return [r for r in rows if start_ts <= r[0] < end_ts]


# ─── BankNifty: last N candles BEFORE start_date (for S/R visible window) ──
# _build_sr_tfs() — har TF ke liye last 100 resampled bars return karta hai.
# Yeh Local API path mein use hota hai taaki har chart par SR draw ho sake.
# GitHub fallback mein _load_banknifty_sr_lookback() use hota hai (sirf 1m).

def _load_banknifty_1m_before(start_date: str) -> list:
    """start_date se pehle ke SAARE 1m candles return karo."""
    rows = load_bin(auto_download=True)
    if not rows:
        return []
    start_ts, _ = _date_to_ts_range_ist(start_date, start_date)
    return [r for r in rows if r[0] < start_ts]


def _load_banknifty_sr_lookback(start_date: str, n: int) -> list:
    """start_date se pehle ke exactly N 1m candles (GitHub fallback ke liye)."""
    before = _load_banknifty_1m_before(start_date)
    return before[-n:] if len(before) >= n else before


def _build_sr_tfs(rows_1m_before: list, tf_defs: dict, day_key_fn,
                  sr_bars: int = 100) -> dict:
    """
    Har TF ke liye start_date se PEHLE ke last sr_bars resampled bars return karo.
    JS mein yeh seedha c.bars ke roop mein inject hote hain —
    5m chart pe last 100 5m bars, 15m pe last 100 15m bars, aur 27d pe last 100 27d bars.

    Returns: { "5m": [[ts_ms,o,h,l,c], ...], "15m": [...], "27d": [...], ... }
    """
    result = {}
    for tf_name, tf_def in tf_defs.items():
        if tf_def["type"] == "minute":
            all_bars = _resample_minutes(rows_1m_before, tf_def["mins"])
        else:
            all_bars = _resample_days(rows_1m_before, tf_def["days"], day_key_fn)
        result[tf_name] = all_bars[-sr_bars:] if len(all_bars) >= sr_bars else all_bars
    return result


def _fetch_btc_sr_lookback(start_date: str, n: int) -> list:
    """start_date se pehle ke exactly N 1m candles return karo (BTC/Binance).
    Yeh sr_base ke liye hain — chart par visible honge, S/R inhi se calculate hoga."""
    # N 1m candles = N minutes. Buffer ke saath fetch karo (non-trading gaps ke liye 3x)
    BUFFER_FACTOR = 3
    fetch_minutes = n * BUFFER_FACTOR
    end_ts, _ = _date_to_ts_range_utc(start_date, start_date)
    start_ts  = end_ts - (fetch_minutes * 60_000)

    out = []
    cur = start_ts
    while cur < end_ts and len(out) < n * BUFFER_FACTOR:
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": cur,
                    "endTime": end_ts,
                    "limit": 1000,
                },
                timeout=15,
            ).json()
        except Exception:
            break
        if not isinstance(resp, list) or not resp:
            break
        for k in resp:
            ts = int(k[0])
            if ts < end_ts:
                out.append([ts, float(k[1]), float(k[2]), float(k[3]), float(k[4])])
        last_open = resp[-1][0]
        next_cur  = last_open + 60_000
        if next_cur <= cur:
            break
        cur = next_cur
        if len(resp) < 1000:
            break
        time.sleep(0.05)

    # Last N candles return karo
    return out[-n:] if len(out) >= n else out


# ─── BTC: fetch historical range from Binance ──────────────────────────────

def _fetch_btc_1m(start_date: str, end_date: str) -> list:
    """Paginated fetch of 1m klines from Binance covering [start_date, end_date].
    Capped at ~200k candles (~140 days) to keep replay-load times reasonable;
    longer ranges should be split into multiple smaller loads by the user."""
    start_ts, end_ts = _date_to_ts_range_utc(start_date, end_date)
    MAX_CANDLES = 200_000
    out = []
    cur = start_ts
    # Binance limit=1000 candles/call, 1m candles => ~16.6 hrs/call
    while cur < end_ts and len(out) < MAX_CANDLES:
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": cur,
                    "endTime": end_ts,
                    "limit": 1000,
                },
                timeout=15,
            ).json()
        except Exception:
            break
        if not isinstance(resp, list) or not resp:
            break
        for k in resp:
            # k = [openTime, open, high, low, close, volume, closeTime, ...]
            out.append([int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])])
        last_open = resp[-1][0]
        next_cur = last_open + 60_000  # advance 1 minute past last candle
        if next_cur <= cur:
            break
        cur = next_cur
        if len(resp) < 1000:
            break
        time.sleep(0.1)  # gentle on rate limits
    return out


# ─── Resampling ─────────────────────────────────────────────────────────────

def _resample_minutes(rows_1m: list, mins: int) -> list:
    """Bucket 1m candles into fixed-size minute buckets, aligned to UTC epoch
    minute boundaries (i.e. bucket_start = floor(ts / (mins*60000)) * mins*60000).
    Used for plain intraday TFs (5m,15m,45m,135m,160m,8h)."""
    if not rows_1m:
        return []
    bucket_ms = mins * 60_000
    out = []
    cur_bucket = None
    o = h = l = c = None
    for ts, ro, rh, rl, rc in rows_1m:
        b = (ts // bucket_ms) * bucket_ms
        if b != cur_bucket:
            if cur_bucket is not None:
                out.append([cur_bucket, o, h, l, c])
            cur_bucket = b
            o, h, l, c = ro, rh, rl, rc
        else:
            h = max(h, rh)
            l = min(l, rl)
            c = rc
    if cur_bucket is not None:
        out.append([cur_bucket, o, h, l, c])
    return out


def _trading_day_key_ist(ts_ms: int) -> str:
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000) + IST_OFFSET
    return dt.strftime("%Y-%m-%d")


def _calendar_day_key_utc(ts_ms: int) -> str:
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
    return dt.strftime("%Y-%m-%d")


def _resample_days(rows_1m: list, n_days: int, day_key_fn) -> list:
    """Group 1m rows by calendar/trading day using day_key_fn, then bucket
    consecutive days into groups of n_days (in chronological order of
    distinct days actually present in data — so holidays/weekends don't
    create empty gaps)."""
    if not rows_1m:
        return []
    # group rows by day key, preserving order
    day_groups = {}
    day_order = []
    for r in rows_1m:
        k = day_key_fn(r[0])
        if k not in day_groups:
            day_groups[k] = []
            day_order.append(k)
        day_groups[k].append(r)

    out = []
    for i in range(0, len(day_order), n_days):
        chunk_keys = day_order[i:i + n_days]
        chunk_rows = []
        for k in chunk_keys:
            chunk_rows.extend(day_groups[k])
        if not chunk_rows:
            continue
        ts0 = chunk_rows[0][0]
        o = chunk_rows[0][1]
        h = max(r[2] for r in chunk_rows)
        l = min(r[3] for r in chunk_rows)
        c = chunk_rows[-1][4]
        out.append([ts0, o, h, l, c])
    return out


def _build_tfs(rows_1m: list, tf_defs: dict, day_key_fn) -> dict:
    tfs = {}
    for name, d in tf_defs.items():
        if d["type"] == "minute":
            tfs[name] = _resample_minutes(rows_1m, d["mins"])
        else:
            tfs[name] = _resample_days(rows_1m, d["days"], day_key_fn)
    return tfs


# ─── Public API ─────────────────────────────────────────────────────────────

def get_replay_data(asset: str, start_date: str, end_date: str,
                    sr_lookback: int = 100) -> dict:
    """
    asset        : 'BANKNIFTY' or 'BTCUSDT'
    start_date   : replay range start (YYYY-MM-DD)
    end_date     : replay range end   (YYYY-MM-DD)
    sr_lookback  : kitne 1m candles start_date se pehle chahiye (sr_base ke liye)
                   Default 100. 0 dene par sr_base empty rahega.
    """
    asset = asset.upper()
    sr_lookback = max(0, int(sr_lookback))

    SR_BARS = 100  # Har TF ke liye kitne bars chahiye visible window mein

    if asset in ("BANKNIFTY", "BN", "NIFTYBANK"):
        # Hidden replay engine candles (selected date range)
        rows_1m        = _load_banknifty_1m(start_date, end_date)
        # start_date se pehle ke SAARE 1m candles (sr_tfs ke liye)
        rows_1m_before = _load_banknifty_1m_before(start_date)
        # Har TF ke liye last 100 resampled bars (chart par dikhenge, SR inhi se)
        sr_tfs   = _build_sr_tfs(rows_1m_before, BANKNIFTY_TFS, _trading_day_key_ist, SR_BARS)
        # sr_base: sirf 1m candles (GitHub fallback ke liye backward compatibility)
        sr_base  = rows_1m_before[-sr_lookback:] if sr_lookback > 0 and rows_1m_before else []
        tfs      = _build_tfs(rows_1m, BANKNIFTY_TFS, _trading_day_key_ist)
        min_tf   = BANKNIFTY_MIN_TF

    elif asset in ("BTCUSDT", "BTC"):
        rows_1m  = _fetch_btc_1m(start_date, end_date)
        sr_base  = _fetch_btc_sr_lookback(start_date, sr_lookback) if sr_lookback > 0 else []
        # BTC ke liye sr_tfs: sr_base 1m se resample karo
        sr_tfs   = _build_sr_tfs(sr_base, BTC_TFS, _calendar_day_key_utc, SR_BARS)
        tfs      = _build_tfs(rows_1m, BTC_TFS, _calendar_day_key_utc)
        min_tf   = BTC_MIN_TF

    else:
        raise ValueError(f"Unknown asset: {asset}")

    # sr_tfs_counts: debug ke liye har TF mein kitne bars hain
    sr_tfs_counts = {k: len(v) for k, v in sr_tfs.items()}

    return {
        "base":    rows_1m,     # Hidden: replay engine drives from here
        "sr_base": sr_base,     # 1m candles: GitHub fallback ke liye
        "sr_tfs":  sr_tfs,      # Per-TF last 100 bars: JS mein c.bars ke roop mein inject
        "tfs":     tfs,
        "meta": {
            "asset":             asset,
            "start":             start_date,
            "end":               end_date,
            "base_interval_min": 1,
            "min_tf":            min_tf,
            "base_count":        len(rows_1m),
            "sr_lookback":       sr_lookback,
            "sr_base_count":     len(sr_base),
            "sr_bars":           SR_BARS,
            "sr_tfs_counts":     sr_tfs_counts,
        },
    }


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) >= 4:
        sr_lb = int(sys.argv[4]) if len(sys.argv) == 5 else 100
        d = get_replay_data(sys.argv[1], sys.argv[2], sys.argv[3], sr_lookback=sr_lb)
        print(json.dumps(d["meta"], indent=2))
        print(f"  sr_base : {len(d['sr_base'])} candles (visible S/R window)")
        print(f"  base    : {len(d['base'])} candles (hidden replay engine)")
        for tf, rows in d["tfs"].items():
            print(f"  {tf}: {len(rows)} candles")
    else:
        print("Usage: python replay_data.py BANKNIFTY 2024-01-01 2024-01-31 [sr_lookback=100]")
