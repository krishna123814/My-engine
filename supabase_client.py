"""
supabase_client.py
-------------------
Thin writer layer between Fyers data and Supabase.
Uses plain REST calls (PostgREST) — no extra SDK dependency needed,
keeps things consistent with the existing `requests`-based app.py.

Credentials are read from environment variables so the secret key
never sits hardcoded in source:

    SUPABASE_URL          -> e.g. https://xxxx.supabase.co
    SUPABASE_SECRET_KEY   -> server-side secret key (NEVER expose to browser)

Set these in Streamlit secrets / .env before running.
"""

import os
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")

REST_BASE = f"{SUPABASE_URL}/rest/v1"

_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}


def _headers(prefer: str = "") -> dict:
    h = dict(_HEADERS)
    if prefer:
        h["Prefer"] = prefer
    return h


def _ok(resp) -> bool:
    return resp.status_code in (200, 201, 204)


# ─────────────────────────────────────────────────────────────────────────────
# Live tick: snapshot (overwrite) + log (append)
# ─────────────────────────────────────────────────────────────────────────────
def upsert_live_tick(symbol: str, ltp: float, prev_close: float) -> bool:
    """Overwrite the single snapshot row for this symbol in live_ticks."""
    if not SUPABASE_KEY:
        return False
    try:
        payload = {
            "symbol":     symbol,
            "ltp":        ltp,
            "prev_close": prev_close,
        }
        resp = requests.post(
            f"{REST_BASE}/live_ticks?on_conflict=symbol",
            headers=_headers("resolution=merge-duplicates"),
            json=payload,
            timeout=5,
        )
        return _ok(resp)
    except Exception:
        return False


def insert_live_tick_log(symbol: str, ltp: float, prev_close: float) -> bool:
    """Append one row to the tick history log."""
    if not SUPABASE_KEY:
        return False
    try:
        payload = {
            "symbol":     symbol,
            "ltp":        ltp,
            "prev_close": prev_close,
        }
        resp = requests.post(
            f"{REST_BASE}/live_ticks_log",
            headers=_headers(),
            json=payload,
            timeout=5,
        )
        return _ok(resp)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Historical candles: bulk upsert
# ─────────────────────────────────────────────────────────────────────────────
def upsert_historical_candles(symbol: str, timeframe: str, candles: list) -> bool:
    """
    candles: list of [epoch_ms, open, high, low, close, volume]
    (this is the exact shape _fyers_history() already returns)
    """
    if not SUPABASE_KEY or not candles:
        return False
    rows = []
    for c in candles:
        try:
            ts_ms = c[0]
            rows.append({
                "symbol":      symbol,
                "timeframe":   timeframe,
                "candle_time": _ms_to_iso(ts_ms),
                "open":        c[1],
                "high":        c[2],
                "low":         c[3],
                "close":       c[4],
                "volume":      c[5] if len(c) > 5 else None,
            })
        except Exception:
            continue
    if not rows:
        return False
    try:
        # batch in chunks of 500 to keep payload size sane
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            resp = requests.post(
                f"{REST_BASE}/historical_candles?on_conflict=symbol,timeframe,candle_time",
                headers=_headers("resolution=merge-duplicates"),
                json=chunk,
                timeout=15,
            )
            if not _ok(resp):
                return False
        return True
    except Exception:
        return False


def _ms_to_iso(ts_ms) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(int(ts_ms) / 1000).isoformat() + "Z"


# ─────────────────────────────────────────────────────────────────────────────
# READ functions — used by the display layer (app.py just shows this data)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_historical_candles(symbol: str, timeframe: str, limit: int = 2000) -> list:
    """Returns candles as [epoch_ms, open, high, low, close, volume], oldest->newest."""
    if not SUPABASE_KEY:
        return []
    try:
        resp = requests.get(
            f"{REST_BASE}/historical_candles",
            headers=_headers(),
            params={
                "symbol":    f"eq.{symbol}",
                "timeframe": f"eq.{timeframe}",
                "select":    "candle_time,open,high,low,close,volume",
                "order":     "candle_time.desc",
                "limit":     str(limit),
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        rows = resp.json()
        rows.sort(key=lambda r: r["candle_time"])  # oldest -> newest
        out = []
        for r in rows:
            ts_ms = _iso_to_ms(r["candle_time"])
            out.append([ts_ms, r["open"], r["high"], r["low"], r["close"], r.get("volume")])
        return out
    except Exception:
        return []


def fetch_latest_live_tick(symbol: str):
    """Returns {'ltp':..., 'prev_close':..., 'updated_at':...} or None."""
    if not SUPABASE_KEY:
        return None
    try:
        resp = requests.get(
            f"{REST_BASE}/live_ticks",
            headers=_headers(),
            params={"symbol": f"eq.{symbol}", "select": "*"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        return rows[0] if rows else None
    except Exception:
        return None


def _iso_to_ms(iso_str: str) -> int:
    import datetime
    s = iso_str.replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(s)
    return int(dt.timestamp() * 1000)
