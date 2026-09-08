"""
fyers_history.py — Fyers historical-candle fetchers (BankNifty intraday +
daily, in raw-chunk aur full-load variants) + BTC (Binance) daily/intraday
loaders + generic OHLC normalizer.
"""
import os
import json
import time
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor

from config import IST, _ist_now, BN_DAILY_CACHE, DAILY_CACHE_FILE, DAILY_CACHE_TTL
from credentials import load_creds

def _fyers_history(resolution: str, from_date: str, to_date: str) -> list:
    creds = load_creds()
    if not creds.get("access_token"):
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol":     "NSE:NIFTYBANK-INDEX",
        "resolution": resolution,
        "date_format": "1",
        "range_from": from_date,
        "range_to":   to_date,
        "cont_flag":  "1",
    }
    try:
        res = requests.get(
            "https://api-t1.fyers.in/data/history",
            headers=headers, params=params, timeout=15,
        ).json()
        if res.get("s") == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]]
                    for c in res.get("candles", [])]
    except Exception:
        pass
    return []

def fetch_bn_intraday(interval_mins: int) -> list:
    # Fyers TF ke hisaab se max safe range:
    # 1m  → 10 days, 5m → 30 days, 15m → 60 days, 45m → 90 days
    _days = {1: 10, 5: 30, 15: 60, 45: 90}.get(interval_mins, 30)
    today  = _ist_now().strftime("%Y-%m-%d")
    from_d = (_ist_now() - datetime.timedelta(days=_days)).strftime("%Y-%m-%d")
    return _fyers_history(str(interval_mins), from_d, today)

def _fyers_history_chunk(resolution: str, from_date: str, to_date: str, raise_on_error: bool = False) -> list:
    """Same as _fyers_history but doesn't read creds again (for chunked calls).

    raise_on_error=False (default, existing callers like load_bn_daily()):
    silently returns [] on any failure — kept as-is kyunki wahan chunk-loop
    mein kuch chunks ka legitimately "no_data" hona normal hai (jaise future
    ke half-year mein abhi tak koi candle nahi bana), aur poora loop kisi ek
    chunk ki wajah se crash nahi hona chahiye.

    raise_on_error=True (naya, _append_new_bn_candles() jaisi jagah ke liye):
    agar API call hi fail ho (network/timeout/non-2xx/response "s" != "ok"
    aur != "no_data"), to Exception raise karta hai — taaki caller "koi naya
    candle nahi mila" (genuine) aur "fetch hi fail ho gaya" (fault) mein farak
    kar sake, aur silent false-success na dikhaye."""
    creds = load_creds()
    if not creds.get("access_token"):
        if raise_on_error:
            raise RuntimeError("access_token missing")
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol": "NSE:NIFTYBANK-INDEX", "resolution": resolution,
        "date_format": "1", "range_from": from_date, "range_to": to_date, "cont_flag": "1",
    }
    try:
        resp = requests.get("https://api-t1.fyers.in/data/history",
                           headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        res = resp.json()
        status = res.get("s")
        if status == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]] for c in res.get("candles", [])]
        if status == "no_data":
            return []   # genuinely koi candle nahi (market band / range mein data hi nahi) — ye fail nahi hai
        if raise_on_error:
            raise RuntimeError(f"Fyers history API error: {res}")
        return []
    except Exception:
        if raise_on_error:
            raise
        return []

def load_bn_daily() -> list:
    """Fetch BankNifty daily candles — last 1 year only (live-chart ke liye
    itna hi kaafi hai; poori multi-year history sirf Replay Mode ke .gz data
    se aati hai, wahan is function ka koi role nahi).

    FIX: agar disk pe purana (stale bhi) cache maujood hai, use hamesha
    fallback ke roop mein yaad rakho. Fresh Fyers fetch fail ho ya poori
    tarah khaali aaye (rate-limit / token expiry / network hiccup), to
    empty list return karke chart blank karne ke bajaye purana valid cache
    hi return karo. Isse 1D chart kabhi blank nahi dikhega — worst case
    thoda stale rahega, jab tak fresh fetch phir se successful na ho jaye.
    """
    stale_fallback: list = []
    if os.path.exists(BN_DAILY_CACHE):
        try:
            with open(BN_DAILY_CACHE) as f:
                cache = json.load(f)
            stale_fallback = cache.get("data", [])
            if time.time() - cache.get("ts", 0) < DAILY_CACHE_TTL:
                return stale_fallback
        except Exception:
            pass

    today     = _ist_now()
    today_str = today.strftime("%Y-%m-%d")
    from_str  = (today - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    mid_str   = (today - datetime.timedelta(days=182)).strftime("%Y-%m-%d")

    # 2 chunks (Fyers allows max ~1yr range per call — 2 half-year chunks
    # ke andar hi last-1-year comfortably fit ho jaata hai, in parallel).
    chunks = [(from_str, mid_str), (mid_str, today_str)]

    all_candles: list = []
    seen_times: set  = set()
    with ThreadPoolExecutor(max_workers=2) as _ex:
        _results = list(_ex.map(lambda ch: _fyers_history_chunk("D", ch[0], ch[1]), chunks))
    for chunk in _results:
        for c in chunk:
            if c[0] not in seen_times:
                seen_times.add(c[0])
                all_candles.append(c)

    all_candles.sort(key=lambda x: x[0])

    # Normalize timestamps to 9:15 AM IST of each IST calendar day.
    # Fyers may return midnight UTC or any session-start epoch — normalize to
    # 3:45 AM UTC (= 9:15 AM IST) so chart.html resample() stays consistent.
    _IST_OFF   = 19800          # 5.5 * 3600
    _NSE_OPEN  = 33300          # 9:15 AM = 9*3600+15*60 seconds from IST midnight
    normalized = []
    for c in all_candles:
        t_ms   = int(c[0])
        t_sec  = t_ms // 1000
        ist_sec        = t_sec + _IST_OFF
        ist_midnight   = ist_sec - (ist_sec % 86400)   # IST midnight of that day
        t_fixed        = (ist_midnight - _IST_OFF) + _NSE_OPEN  # 9:15 AM IST in UTC epoch
        normalized.append([t_fixed * 1000] + list(c[1:]))
    all_candles = normalized

    if all_candles:
        try:
            with open(BN_DAILY_CACHE, "w") as f:
                json.dump({"ts": time.time(), "data": all_candles}, f)
        except Exception:
            pass
        return all_candles

    # Fresh fetch poori tarah fail ho gaya (empty) — purana cache hi wapas do
    # taaki 1D chart kabhi blank na dikhe.
    return stale_fallback

# ─── BTC (Binance) ────────────────────────────────────────────────────────────
def fetch_btc(interval: str = "1m", limit: int = 1000) -> list:
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval={interval}&limit={limit}",
            timeout=10,
        ).json()
        return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                 float(x[4]), float(x[5])] for x in r]
    except Exception:
        return []

def load_btc_daily() -> list:
    """Fetch BTC daily candles — last 1 year only (live-chart ke liye itna
    hi kaafi hai; poori multi-year history sirf Replay Mode ke .gz data se
    aati hai, wahan is function ka koi role nahi). Binance ek hi call mein
    1000 daily candles tak de deta hai, isliye 365 din ka data 1 hi request
    mein aa jaata hai — koi chunking/loop ki zaroorat nahi."""
    today_str = _ist_now().strftime("%Y-%m-%d")
    if os.path.exists(DAILY_CACHE_FILE):
        try:
            with open(DAILY_CACHE_FILE) as f:
                c = json.load(f)
            data = c.get("data", [])
            cache_ok = time.time() - c.get("ts", 0) < DAILY_CACHE_TTL
            if cache_ok and data:
                last_ts = data[-1][0] // 1000
                last_date = datetime.datetime.utcfromtimestamp(last_ts).strftime("%Y-%m-%d")
                if last_date < today_str:
                    cache_ok = False
            if cache_ok:
                return data
        except Exception:
            pass

    today   = _ist_now()
    from_ms = int((today - datetime.timedelta(days=365)).replace(
        tzinfo=datetime.timezone.utc).timestamp() * 1000)
    to_ms   = int(today.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) + 86400000

    all_candles: list = []
    seen_times: set   = set()
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval=1d&startTime={from_ms}&endTime={to_ms}&limit=1000",
            timeout=15,
        ).json()
        if isinstance(r, list):
            for x in r:
                ts = int(x[0])
                if ts not in seen_times:
                    seen_times.add(ts)
                    all_candles.append([ts, float(x[1]), float(x[2]),
                                        float(x[3]), float(x[4]), float(x[5])])
    except Exception:
        pass

    all_candles.sort(key=lambda x: x[0])

    # FIX: agar Binance fetch fail ho gaya ya khaali aaya (rate-limit, network
    # hiccup — jo rapid sv1↔sv2↔sv3 switching ke baad common hai kyunki
    # thode hi second mein multiple daily-klines calls chali jaati hain),
    # to khaali [] return karke daily+ panels (1D/3D/9D/27D) ko crash mat
    # karo. Iske bajaye purana cached data hi wapas de do (chahe stale ho) —
    # kam-se-kam history dikhti rahegi jab tak agla successful fetch na ho
    # jaaye. Sirf tabhi khaali [] jaayega jab disk par pehle se koi cache
    # hi kabhi nahi bani.
    if not all_candles:
        try:
            if os.path.exists(DAILY_CACHE_FILE):
                with open(DAILY_CACHE_FILE) as f:
                    _stale = json.load(f).get("data", [])
                if _stale:
                    return _stale
        except Exception:
            pass
        return all_candles   # sach mein kabhi cache nahi bani — tab hi []

    try:
        with open(DAILY_CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "data": all_candles}, f)
    except Exception:
        pass
    return all_candles

# ─── OHLC converter ───────────────────────────────────────────────────────────
def to_ohlc(bars: list) -> list:
    out = []
    for b in bars:
        try:
            out.append({
                "time":   int(b[0]) // 1000,
                "open":   round(float(b[1]), 2),
                "high":   round(float(b[2]), 2),
                "low":    round(float(b[3]), 2),
                "close":  round(float(b[4]), 2),
                "volume": round(float(b[5]), 2) if len(b) > 5 else 0,
            })
        except Exception:
            continue
    return out

# ─── Fyers WebSocket (DataSocket) — live BankNifty ticks ─────────────────────
_ws_thread_started = False

