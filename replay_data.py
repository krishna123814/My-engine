"""
Replay Data Layer
==================
Step 1 of Replay feature.

Provides: get_replay_data(asset, start_date, end_date) -> dict
  asset       : 'BANKNIFTY' or 'BTCUSDT'
  start_date  : 'YYYY-MM-DD'  (inclusive)
  end_date    : 'YYYY-MM-DD'  (inclusive)

Returns:
  {
    "base": [[ts_ms, o, h, l, c], ...],          # 1m base candles (raw, for replay engine clock)
    "tfs": {
        "5m":  [[ts_ms,o,h,l,c], ...],
        "15m": [...],
        ...
    },
    "meta": {"asset":..., "start":..., "end":..., "base_interval_min":1}
  }

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

def get_replay_data(asset: str, start_date: str, end_date: str) -> dict:
    asset = asset.upper()
    if asset in ("BANKNIFTY", "BN", "NIFTYBANK"):
        rows_1m = _load_banknifty_1m(start_date, end_date)
        tfs = _build_tfs(rows_1m, BANKNIFTY_TFS, _trading_day_key_ist)
        min_tf = BANKNIFTY_MIN_TF
    elif asset in ("BTCUSDT", "BTC"):
        rows_1m = _fetch_btc_1m(start_date, end_date)
        tfs = _build_tfs(rows_1m, BTC_TFS, _calendar_day_key_utc)
        min_tf = BTC_MIN_TF
    else:
        raise ValueError(f"Unknown asset: {asset}")

    return {
        "base": rows_1m,
        "tfs": tfs,
        "meta": {
            "asset": asset,
            "start": start_date,
            "end": end_date,
            "base_interval_min": 1,
            "min_tf": min_tf,
            "base_count": len(rows_1m),
        },
    }


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) == 4:
        d = get_replay_data(sys.argv[1], sys.argv[2], sys.argv[3])
        print(json.dumps(d["meta"], indent=2))
        for tf, rows in d["tfs"].items():
            print(f"  {tf}: {len(rows)} candles")
    else:
        print("Usage: python replay_data.py BANKNIFTY 2024-01-01 2024-01-31")
