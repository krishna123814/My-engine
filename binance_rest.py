"""
binance_rest.py — Binance spot/options REST helpers: server time, request
signing, spot balance/price, daily kline fetch (full + incremental),
nearest option lookup, option premium/greeks.
"""
import time
import hmac
import json
import hashlib
import datetime
import requests
from urllib.parse import urlencode

from config import IST, BINANCE_BASE_URL, BINANCE_EAPI_URL, BTC_1D_SYMBOL
from network_proxy import _get_proxy_dict
from storage import _supabase_upload
from market_data import _SV3_CACHE, _sv3_fetch_symbol_from_supabase, _sv3_load_all_from_supabase

def _binance_server_time() -> int:
    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/time",
            proxies=_get_proxy_dict(),
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("serverTime", int(time.time() * 1000))
    except Exception:
        pass
    return int(time.time() * 1000)

def _binance_sign(params: dict, secret_key: str) -> str:
    params["timestamp"] = _binance_server_time()
    params["recvWindow"] = 10000
    qs = urlencode(params, doseq=True)
    sig = hmac.new(secret_key.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"

def _binance_call(base: str, path: str, params: dict, api_key: str,
                   signed: bool = False, secret_key: str = None):
    headers = {"X-MBX-APIKEY": api_key} if api_key else {}
    if signed:
        qs = _binance_sign(params.copy(), secret_key)
    else:
        qs = urlencode(params, doseq=True)
    url = f"{base}{path}"
    if qs:
        url = f"{url}?{qs}"
    try:
        r = requests.get(url, headers=headers, proxies=_get_proxy_dict(), timeout=20)
        ct = r.headers.get("Content-Type", "")
        if "application/json" in ct:
            data = r.json()
            if r.status_code == 200:
                return True, data
            code = data.get("code", r.status_code)
            msg = data.get("msg", "Unknown error")
            return False, f"Error {code}: {msg}"
        text = r.text.strip()
        if "<html" in text.lower():
            return False, f"HTTP {r.status_code}: Binance returned HTML instead of JSON (endpoint may be unavailable for your account/region)"
        return False, f"HTTP {r.status_code}: {text[:500]}"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except requests.exceptions.ConnectionError:
        return False, "Connection error"
    except Exception as e:
        return False, str(e)

def binance_get_spot_balance(api_key: str, secret_key: str):
    ok, data = _binance_call(BINANCE_BASE_URL, "/api/v3/account", {}, api_key, signed=True, secret_key=secret_key)
    if not ok:
        return False, data
    balances = [
        b for b in data.get("balances", [])
        if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0
    ]
    return True, balances

def binance_get_spot_price(symbol: str = "BTCUSDT"):
    ok, data = _binance_call(BINANCE_BASE_URL, "/api/v3/ticker/price", {"symbol": symbol}, "", signed=False)
    if not ok:
        return None, data
    return float(data["price"]), None

# ─── SV3 BTC 1D history (Binance public klines) ────────────────────────────
# SV3 (Long Term Replay) ke "3M/9M/27M/81M/243M" jaise long timeframes ke
# liye BTC ko yahan bhi ek "symbol" ki tarah treat karte hain — bilkul
# Nifty500 stocks jaisa hi per-symbol daily JSON, Supabase par. Farak sirf
# itna hai ki data-source Fyers nahi, Binance hai (public klines endpoint,
# koi API-key/login zaroori nahi) — isliye ye pipeline Nifty500 ke
# incremental-update loop se poori tarah ALAG/independent hai, taaki galti
# se BTC ko Fyers se fetch karne ki koshish na ho (aur na hi Nifty500 loop
# BTC ko touch kare).
#
# NOTE: Binance is HF Space se direct blocked hai (jaisa upar _test_proxy
# comments mein hai) — isliye ye saare calls _binance_call() ke through
# jaate hain, jo already proxy (_get_proxy_dict()) use karta hai.
BTC_1D_SYMBOL         = "BTCUSDT"
BTC_1D_FILENAME       = f"{BTC_1D_SYMBOL}.json"
_BTC_LISTING_START_MS = 1502928000000   # 2017-08-17 00:00:00 UTC — Binance par BTCUSDT listing ki date

def _binance_klines_daily_chunk(start_ms: int, end_ms: int) -> list:
    """Binance /api/v3/klines se ek chunk (max 1000 daily candles) — public
    endpoint, signing nahi chahiye. Candle 'openTime' Binance mein already
    UTC-midnight par align hoti hai (24x7 market, koi session/IST offset
    nahi) — isliye seedha t=openTime_sec use karte hain, jo _sv2_resample_
    btc_daily() ke 'UTC calendar-day' convention se bhi match karta hai."""
    ok, data = _binance_call(
        BINANCE_BASE_URL, "/api/v3/klines",
        {"symbol": BTC_1D_SYMBOL, "interval": "1d",
         "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        "", signed=False,
    )
    if not ok or not isinstance(data, list):
        return []
    out = []
    for k in data:
        # k = [openTime, open, high, low, close, volume, closeTime, ...]
        try:
            out.append({
                "t": int(k[0]) // 1000,
                "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]),
            })
        except Exception:
            continue
    return out

def _fetch_btc_full_daily_binance() -> list:
    """BTCUSDT ka POORA available daily history — listing (17-Aug-2017) se
    aaj tak, Binance klines se 1000-candle (~1000-din) chunks mein
    paginate karke. Proxy zaroori hai (SV2 jaisa hi)."""
    now_ms   = int(time.time() * 1000)
    chunk_ms = 1000 * 86400 * 1000   # ek chunk = 1000 din
    all_rows: dict = {}
    start = _BTC_LISTING_START_MS
    while start < now_ms:
        end   = min(start + chunk_ms - 1, now_ms)
        chunk = _binance_klines_daily_chunk(start, end)
        for r in chunk:
            all_rows[r["t"]] = r
        if not chunk:
            start = end + 1
            continue
        last_t_ms = chunk[-1]["t"] * 1000
        if last_t_ms < start:   # safety: progress na ho to infinite-loop se bacho
            break
        start = last_t_ms + 86_400_000
        time.sleep(0.2)   # Binance rate-limit ke liye halka gap
    return sorted(all_rows.values(), key=lambda r: r["t"])

def _fetch_btc_incremental_daily_binance(from_date: str, to_date: str) -> list:
    """Sirf gap (last-saved-date+1 se aaj tak) fetch karta hai — daily
    auto-update ke liye, poora history dobara nahi khinchta. Nifty500 wale
    _fetch_symbol_incremental_daily() jaisa hi role, bas Binance-based."""
    start_ms = int(datetime.datetime.strptime(from_date, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    end_ms   = int(datetime.datetime.strptime(to_date, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) + 86_399_000
    if start_ms > end_ms:
        return []
    all_rows: dict = {}
    cur      = start_ms
    chunk_ms = 1000 * 86400 * 1000
    while cur <= end_ms:
        end   = min(cur + chunk_ms - 1, end_ms)
        chunk = _binance_klines_daily_chunk(cur, end)
        for r in chunk:
            all_rows[r["t"]] = r
        if not chunk:
            break
        cur = chunk[-1]["t"] * 1000 + 86_400_000
        time.sleep(0.2)
    return sorted(all_rows.values(), key=lambda r: r["t"])

def _btc_sv3_build_and_upload_full() -> tuple[bool, str]:
    """ONE-TIME bootstrap: poora BTCUSDT 1D history Binance se khinch ke
    Supabase par BTCUSDT.json bana deta hai — SV3 ke per-symbol format
    (Nifty500 stocks jaisa hi), taaki _sv3_fetch_symbol_from_supabase("BTCUSDT")
    /_build_sv3_data("BTCUSDT") bina kisi extra change ke seedha kaam kar jayein."""
    rows = _fetch_btc_full_daily_binance()
    if not rows:
        return False, "Binance se BTC 1D data nahi mila — proxy check karo (Proxy Settings → Test button)."
    ok, msg = _supabase_upload(BTC_1D_FILENAME, json.dumps(rows).encode("utf-8"), "application/json")
    if ok:
        _SV3_CACHE.clear()
        try:
            _sv3_load_all_from_supabase.clear()
        except Exception:
            pass
        return True, f"BTC 1D history ({len(rows)} candles, {BTC_1D_FILENAME}) Supabase par upload ho gayi."
    return False, msg

def _btc_sv3_incremental_update() -> tuple[bool, str]:
    """Daily gap-fill: existing BTCUSDT.json + Binance ke naye missing din
    merge karke wapas upload — Nifty500 wale _nifty500_incremental_update
    jaisa hi pattern, bas Binance-based aur Fyers-login se poori tarah
    independent (isliye Nifty500 loop mein bilkul touch nahi kiya gaya)."""
    existing  = _sv3_fetch_symbol_from_supabase(BTC_1D_SYMBOL) or []
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if existing:
        last_t    = max(r["t"] for r in existing)
        last_date = datetime.datetime.utcfromtimestamp(last_t).date()
        from_d    = (last_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if from_d > today_str:
            return True, "BTC 1D already up-to-date."
        new_rows = _fetch_btc_incremental_daily_binance(from_d, today_str)
    else:
        new_rows = _fetch_btc_full_daily_binance()
    if not new_rows:
        return True, "BTC 1D — koi naya candle nahi mila (already up-to-date ya Binance se kuch nahi aaya)."
    merged_map = {r["t"]: r for r in existing}
    for r in new_rows:
        merged_map[r["t"]] = r
    merged = sorted(merged_map.values(), key=lambda r: r["t"])
    ok, msg = _supabase_upload(BTC_1D_FILENAME, json.dumps(merged).encode("utf-8"), "application/json")
    if ok:
        _SV3_CACHE.clear()
        try:
            _sv3_load_all_from_supabase.clear()
        except Exception:
            pass
        return True, f"BTC 1D update: {len(merged)} total candles ({len(new_rows)} naye/updated)."
    return False, msg


def binance_get_nearest_option(api_key: str, btc_price: float, side: str = "CALL"):
    ok, data = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/exchangeInfo", {}, api_key, signed=False)
    if not ok:
        return None, data
    symbols = data.get("optionSymbols", [])
    candidates = []
    for s in symbols:
        if s.get("underlying", "") != "BTCUSDT":
            continue
        if s.get("side", "").upper() != side.upper():
            continue
        try:
            strike = float(s.get("strikePrice"))
        except Exception:
            continue
        candidates.append({
            "symbol": s.get("symbol"),
            "strikePrice": strike,
            "expiryDate": s.get("expiryDate"),
            "side": s.get("side"),
            "distance": abs(strike - btc_price),
        })
    if not candidates:
        return None, f"No BTCUSDT {side} option found"
    candidates.sort(key=lambda x: x["distance"])
    return candidates[0], None

def binance_get_option_premium(api_key: str, option_symbol: str):
    ok, data = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/mark", {"symbol": option_symbol}, api_key, signed=False)
    if not ok:
        return None, data
    row = data[0] if isinstance(data, list) and data else data
    return {
        "symbol": row.get("symbol", option_symbol),
        "markPrice": row.get("markPrice"),
        "bidIV": row.get("bidIV"),
        "askIV": row.get("askIV"),
        "markIV": row.get("markIV"),
        "delta": row.get("delta"),
        "gamma": row.get("gamma"),
        "theta": row.get("theta"),
        "vega": row.get("vega"),
    }, None

