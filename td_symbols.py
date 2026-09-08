"""
td_symbols.py — Twelve Data se external (non-NSE, non-crypto) symbols ka
historical + live data. Generic registry-based design: NAYA SYMBOL ADD
KARNE KE LIYE bas TD_SYMBOL_REGISTRY mein ek entry add karo — kahin aur
(app.py, chart.html) kuch badalne ki zaroorat NAHI, chahe 1 symbol ho ya
500. app.py aur chart.html dono is registry ko loop karke generically
handle karte hain.

Naya symbol add karne ka process:
    TD_SYMBOL_REGISTRY.append({
        "key": "apple",                # unique short key (chart.html mein
                                        # isi label se dikhega)
        "td_symbol": "AAPL",           # Twelve Data ka EXACT symbol
                                        # (HISTORICAL backfill ke liye)
        "finnhub_symbol": "AAPL",      # Finnhub ka EXACT symbol
                                        # (LIVE WebSocket ke liye) — stocks
                                        # ke liye simple ticker, forex ke
                                        # liye "OANDA:GBP_USD" jaisa format
        "label": "🍎 Apple",            # UI mein dikhne wala naam
        "bucket_min": 130,             # candle-size (minutes) — neeche
                                        # formula dekho
    })

bucket_min kaise nikalein:
    Us instrument ki ACTUAL candle-running time (regular session, koi
    pre/post-market nahi) minutes mein nikalo, 3 se divide karke round
    karo.
    Example: US stock market 9:30AM-4:00PM ET = 6h30m = 390 min → 130m.
    Example: Gold ~23h/din trade hota hai = 1380 min → 460m.

IMPORTANT — service_role Supabase key yahan KABHI store nahi hoti. Upload
karne wala function (upload_fn) caller (app.py) se inject hota hai, taaki
ye module khud kabhi secret na chhue.
"""
import os
import io
import json
import gzip
import time
import math
import datetime
import threading
import requests

# ─────────────────────────────────────────────────────────────────────────
# REGISTRY — yahan naye symbols add karo
# ─────────────────────────────────────────────────────────────────────────
TD_SYMBOL_REGISTRY = [
    # NOTE: raw Dow Jones INDEX symbol ("DJI") is not usable — Twelve Data's
    # own Indices product is still "coming soon" (confirmed via their API:
    # "symbol or figi parameter is missing or invalid" for DJI). Using DIA
    # (SPDR Dow Jones Industrial Average ETF) instead — a normal equity
    # ticker Twelve Data does support, tracks the Dow closely, same NYSE
    # session hours (9:30-4:00 ET = 390 min) so bucket_min=130 stays correct.
    # "finnhub_symbol" — LIVE ticks isi symbol se aate hain (WebSocket,
    # real-time push). "td_symbol" sirf HISTORICAL backfill (Twelve Data,
    # daily background job) ke liye use hota hai. Dono alag providers hain,
    # isliye format bhi alag ho sakta hai per-provider.
    {"key": "dow",  "td_symbol": "DIA",     "label": "📈 Dow Jones",  "bucket_min": 130, "finnhub_symbol": "DIA", "market": "us_stock"},
    # Forex pair — near-24h trade hota hai (Sun 5PM ET se Fri 5PM ET tak,
    # sirf weekend band), isliye bucket_min=460 (~23h/3).
    {"key": "gbpusd", "td_symbol": "GBP/USD", "label": "💷 GBP/USD", "bucket_min": 460, "finnhub_symbol": "OANDA:GBP_USD", "market": "forex"},
    # US stocks (NASDAQ) — same session hours as Dow: 9:30AM-4:00PM ET
    # = 390 min → bucket_min = 130.
    {"key": "apple",  "td_symbol": "AAPL", "label": "🍎 Apple",  "bucket_min": 130, "finnhub_symbol": "AAPL", "market": "us_stock"},
    {"key": "amazon", "td_symbol": "AMZN", "label": "📦 Amazon", "bucket_min": 130, "finnhub_symbol": "AMZN", "market": "us_stock"},
]

_TD_BASE_INTERVAL = "5min"     # saare registry symbols ke liye ek hi base granularity
_TD_HISTORY_YEARS = 2
_TD_FREE_PLAN_CREDITS_PER_MIN = 8   # Twelve Data free-plan budget (1 credit/symbol/call)

_SUPABASE_BASE_DEFAULT = "https://bybiihyjpdxxobxwyajh.supabase.co/storage/v1/object/public/My%20500%20stock"

# in-memory cache: {key: [rows]}  (rows = base 5-min granularity, raw)
_TD_CACHE: dict = {}
# _TD_CACHE ab do sources se likha jaata hai — Twelve Data ka periodic REST
# poll (slow, backup) aur Finnhub ka WebSocket (fast, primary live). Dono
# alag threads se aa sakte hain, isliye ek simple lock taaki list read-
# modify-write beech mein interleave na ho (list corrupt/duplicate rows).
_TD_CACHE_LOCK = threading.Lock()
# Jo keys Finnhub tick se abhi-abhi update hui hain (last push ke baad se) —
# app.py ka live-pusher isse consume karke sirf UNHI symbols ka fresh data
# browser ko bhejta hai, poori registry har second scan nahi karni padti.
_TD_LIVE_DIRTY: set = set()


def _log(msg: str, level: str = "info", log_fn=None):
    if log_fn:
        try:
            log_fn(f"[td_symbols] {msg}", level=level)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
# Twelve Data API wrappers
# ─────────────────────────────────────────────────────────────────────────
def _td_normalize_values(values: list) -> list:
    """Twelve Data 'values' list (timezone=UTC ke saath maanga gaya) ko
    {"t":epoch_utc,"o","h","l","c"} rows mein badalta hai."""
    rows = []
    for v in values or []:
        try:
            dt = datetime.datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S")
            t = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
            rows.append({
                "t": t,
                "o": float(v["open"]), "h": float(v["high"]),
                "l": float(v["low"]),  "c": float(v["close"]),
            })
        except Exception:
            continue
    return rows


def _td_call_time_series(td_symbol: str, api_key: str, interval: str = _TD_BASE_INTERVAL,
                          start_date: str = None, end_date: str = None,
                          outputsize: int = None, timeout: int = 30) -> "tuple[bool, str, list]":
    """Ek Twelve Data time_series API call (single symbol). Fail ho to
    (False, error_msg, [])."""
    params = {"symbol": td_symbol, "interval": interval, "timezone": "UTC", "apikey": api_key}
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"] = end_date
    if outputsize: params["outputsize"] = outputsize
    try:
        resp = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=timeout)
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return False, data.get("message", "unknown Twelve Data error"), []
        values = data.get("values", []) if isinstance(data, dict) else []
        return True, "ok", _td_normalize_values(values)
    except Exception as e:
        return False, str(e), []


def _td_fetch_full_history(td_symbol: str, api_key: str, years: int = _TD_HISTORY_YEARS,
                            chunk_days: int = 15, log_fn=None) -> list:
    """Poori history chunks mein fetch karta hai (ek call mein Twelve Data
    limited rows deta hai — free plan par output size cap hai). chunk_days
    conservative (15) rakha hai taaki 24h-continuous symbols (jaise Gold)
    bhi ek single call mein row-cap se neeche rahein (15 din * ~288
    bars/din(5-min) ≈ 4320, 5000 ki limit se safe neeche)."""
    today = datetime.datetime.now(datetime.timezone.utc)
    start_limit = today - datetime.timedelta(days=years * 365)
    all_rows, seen = [], set()
    end = today
    while end > start_limit:
        start = max(end - datetime.timedelta(days=chunk_days), start_limit)
        ok, msg, rows = _td_call_time_series(
            td_symbol, api_key,
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if ok:
            for r in rows:
                if r["t"] not in seen:
                    seen.add(r["t"])
                    all_rows.append(r)
        else:
            _log(f"{td_symbol} chunk {start.date()}..{end.date()} fail: {msg}", "warn", log_fn)
        end = start - datetime.timedelta(seconds=1)
        # Free-plan budget is _TD_FREE_PLAN_CREDITS_PER_MIN calls/minute (every
        # call costs a credit, even a failed one) — 0.3s was way too fast
        # (≈200 calls/min), so most chunks past the first ~8 were silently
        # rate-limited away during any first-time full-history backfill.
        time.sleep(60.0 / _TD_FREE_PLAN_CREDITS_PER_MIN + 0.5)
    all_rows.sort(key=lambda r: r["t"])
    return all_rows


def _td_bucket_rows(rows: list, bucket_min: int) -> list:
    """5-min base rows ko N-consecutive-row buckets mein group karta hai
    (N = bucket_min // 5), Lightweight-Charts-ready format mein
    ({"time","open","high","low","close"}).

    Session-gaps (raat/weekend) apne-aap sahi rehte hain kyunki Twelve
    Data sirf ACTUAL-trading rows deta hai — band ghanton ke liye koi
    filler row nahi hoti, isliye simple index-chunking hi (BankNifty jaisi
    session-anchor date-math ke bina) session-aligned bucket bana deti
    hai. Occasional 1-bar drift ho sakta hai holidays/DST ke aas-paas —
    analysis ke liye acceptable hai."""
    n = max(1, bucket_min // 5)
    out = []
    for i in range(0, len(rows), n):
        chunk = rows[i:i + n]
        if not chunk:
            continue
        out.append({
            "time":  chunk[0]["t"],
            "open":  chunk[0]["o"],
            "high":  max(r["h"] for r in chunk),
            "low":   min(r["l"] for r in chunk),
            "close": chunk[-1]["c"],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# Storage (gzip local + Supabase) — BN/BTC .gz jaisa hi pattern
# ─────────────────────────────────────────────────────────────────────────
def _td_local_path(key: str) -> str:
    d = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(d, f"td_{key}_5m.gz")


def _td_supabase_filename(key: str) -> str:
    return f"td_{key}_5m.gz"


def _td_gzip_bytes(rows: list) -> bytes:
    payload = json.dumps({"data": rows}).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(payload)
    return buf.getvalue()


def _td_save_local(key: str, rows: list) -> None:
    try:
        with open(_td_local_path(key), "wb") as f:
            f.write(_td_gzip_bytes(rows))
    except Exception:
        pass


def _td_load_from_supabase(key: str, supabase_base: str = _SUPABASE_BASE_DEFAULT) -> list:
    url = f"{supabase_base}/{_td_supabase_filename(key)}"
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with gzip.open(io.BytesIO(resp.content), "rb") as f:
            data = json.load(f)
        return data.get("data", []) if isinstance(data, dict) else data
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────
# LIVE ticks (Finnhub WebSocket se) — Twelve Data se sirf HISTORICAL data
# aata hai (daily backfill), live price-updates yahan se merge hote hain.
# ─────────────────────────────────────────────────────────────────────────
def td_apply_live_tick(key: str, price: float, epoch_ts: float = None) -> None:
    """Finnhub WS se aaya ek live trade-price ko current 5-min (BASE
    INTERVAL) row mein merge karta hai — taaki td_get_bucketed() turant
    naya/updated candle dikhaye, Twelve Data ke periodic REST poll ka
    wait kiye bina. Historical rows (Supabase se load hui) already
    _TD_CACHE mein hain (td_all_bucketed() startup par call hota hai) —
    ye function sirf SABSE AAKHRI (abhi chal rahe) 5-min bucket ko
    update/append karta hai, purani history ko chhedta nahi.

    5-min bucket boundary UTC epoch ke hisaab se align hoti hai (jaise
    Twelve Data ke apne 5-min bars bhi align hote hain), taaki dono
    sources ke rows seamlessly ek hi timeline mein baith jaayein."""
    if price is None:
        return
    try:
        price = float(price)
    except (TypeError, ValueError):
        return
    if epoch_ts is None:
        epoch_ts = time.time()
    bucket_start = int(epoch_ts // 300) * 300   # 5-min = 300s align
    with _TD_CACHE_LOCK:
        rows = _TD_CACHE.setdefault(key, [])
        if rows and rows[-1]["t"] == bucket_start:
            r = rows[-1]
            r["h"] = max(r["h"], price)
            r["l"] = min(r["l"], price)
            r["c"] = price
        elif rows and rows[-1]["t"] > bucket_start:
            # Late/out-of-order tick (clock-skew ya reconnect ke turant
            # baad) — purane bucket ko overwrite mat karo, chup-chaap
            # ignore karo.
            return
        else:
            rows.append({"t": bucket_start, "o": price, "h": price, "l": price, "c": price})
        _TD_LIVE_DIRTY.add(key)


def td_pop_dirty_keys() -> list:
    """Jo keys pichli baar consume kiye jaane ke baad se live-tick se
    update hui hain, unki list return karke dirty-set clear kar deta hai.
    app.py ka _live_data_pusher isse har run pe call karta hai — sirf
    unhi symbols ka fresh data browser ko bhejta hai jinme actually kuch
    naya aaya hai."""
    with _TD_CACHE_LOCK:
        keys = list(_TD_LIVE_DIRTY)
        _TD_LIVE_DIRTY.clear()
        return keys


def td_reset_cache(key: str = None) -> None:
    with _TD_CACHE_LOCK:
        if key:
            _TD_CACHE.pop(key, None)
        else:
            _TD_CACHE.clear()


def td_load_symbol(key: str, supabase_base: str = _SUPABASE_BASE_DEFAULT) -> list:
    """Cached read — process ke andar dobara-dobara Supabase se na
    maangna pade. td_reset_cache() se clear ho sakta hai."""
    with _TD_CACHE_LOCK:
        if key in _TD_CACHE:
            return list(_TD_CACHE[key])
    rows = _td_load_from_supabase(key, supabase_base)
    with _TD_CACHE_LOCK:
        # Agar itne mein (Supabase fetch ke dauraan) live-tick ne already
        # kuch rows daal diye hon (race), unhe overwrite mat karo — merge.
        existing = _TD_CACHE.get(key, [])
        merged_map = {r["t"]: r for r in rows}
        for r in existing:
            merged_map[r["t"]] = r
        _TD_CACHE[key] = sorted(merged_map.values(), key=lambda r: r["t"])
        return list(_TD_CACHE[key])


def td_get_bucketed(key: str, supabase_base: str = _SUPABASE_BASE_DEFAULT) -> list:
    """Ready-to-render (LWC format) candles — already bucketed us symbol
    ke apne calculated bucket_min par."""
    entry = next((e for e in TD_SYMBOL_REGISTRY if e["key"] == key), None)
    if not entry:
        return []
    rows = td_load_symbol(key, supabase_base)
    return _td_bucket_rows(rows, entry["bucket_min"])


def td_all_bucketed(supabase_base: str = _SUPABASE_BASE_DEFAULT) -> dict:
    """SAARE registry symbols ka ready-to-render data — {key: {label,
    bucket_min, candles:[...]}}. chart.html mein isi tarah inject hota
    hai — registry mein jitne bhi symbols hon, ye sabko cover karta hai."""
    out = {}
    for e in TD_SYMBOL_REGISTRY:
        out[e["key"]] = {
            "label": e["label"],
            "bucket_min": e["bucket_min"],
            "market": e.get("market"),   # "forex" | "us_stock" — chart.html
                                          # isse sahi trading-hours logic
                                          # chunta hai (NSE hours nahi).
            "candles": td_get_bucketed(e["key"], supabase_base),
        }
    return out


def td_symbol_list_for_ui() -> list:
    """Halka summary (candles ke bina) — dropdown/list building ke liye
    kaafi hai agar poore candles abhi nahi chahiye."""
    return [{"key": e["key"], "label": e["label"], "bucket_min": e["bucket_min"]}
            for e in TD_SYMBOL_REGISTRY]


# ─────────────────────────────────────────────────────────────────────────
# Update (historical backfill + daily incremental gap-fill)
# ─────────────────────────────────────────────────────────────────────────
def td_update_symbol(key: str, api_key: str, upload_fn, supabase_base: str = _SUPABASE_BASE_DEFAULT,
                      log_fn=None) -> "tuple[bool, str]":
    """Naya/missing data fetch karke Supabase par push karta hai. Pehli
    baar (koi existing data nahi) poori history fetch hoti hai; baad mein
    sirf gap (last-saved-candle se ab tak) fill hota hai.

    upload_fn: (filename, bytes, content_type) -> (ok, msg) — app.py ka
    _supabase_upload yahan caller se pass hota hai; service-key is module
    mein kabhi store nahi hoti."""
    entry = next((e for e in TD_SYMBOL_REGISTRY if e["key"] == key), None)
    if not entry:
        return False, f"Registry mein '{key}' nahi mila."
    if not api_key:
        return False, "TWELVEDATA_API_KEY secret nahi mila."

    existing = td_load_symbol(key, supabase_base)
    if existing:
        last_t = max(r["t"] for r in existing)
        start_dt = datetime.datetime.utcfromtimestamp(last_t + 1).replace(tzinfo=datetime.timezone.utc)
        end_dt = datetime.datetime.now(datetime.timezone.utc)
        if start_dt >= end_dt:
            return True, f"{key}: already up-to-date ({len(existing)} rows)."
        ok, msg, new_rows = _td_call_time_series(
            entry["td_symbol"], api_key,
            start_date=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if not ok:
            return False, f"{key}: gap-fetch fail: {msg}"
        merged_map = {r["t"]: r for r in existing}
        for r in new_rows:
            merged_map[r["t"]] = r
        merged = sorted(merged_map.values(), key=lambda r: r["t"])
    else:
        _log(f"{key}: pehli baar — poori {_TD_HISTORY_YEARS}-saal history fetch ho rahi hai "
             f"(isme kaafi time lag sakta hai).", "info", log_fn)
        merged = _td_fetch_full_history(entry["td_symbol"], api_key, log_fn=log_fn)
        if not merged:
            return False, f"{key}: initial history fetch fail (0 rows)."

    _td_save_local(key, merged)
    ok, msg = upload_fn(_td_supabase_filename(key), _td_gzip_bytes(merged), "application/gzip")
    if ok:
        _TD_CACHE[key] = merged
    return ok, f"{key}: {len(merged)} rows total. {msg}"


def td_update_all(api_key: str, upload_fn, supabase_base: str = _SUPABASE_BASE_DEFAULT,
                   log_fn=None) -> "tuple[bool, str]":
    """SAARE registry symbols ko update karta hai — registry mein jitne
    bhi hon (2 ho ya 500), ye loop khud-ba-khud sabko cover karta hai. Naya
    symbol add karne par isme koi change NAHI chahiye."""
    results = []
    for entry in TD_SYMBOL_REGISTRY:
        ok, msg = td_update_symbol(entry["key"], api_key, upload_fn, supabase_base, log_fn)
        results.append((entry["key"], ok, msg))
        _log(msg, "ok" if ok else "err", log_fn)
        time.sleep(0.5)
    ok_count = sum(1 for _, ok, _ in results if ok)
    fails = "; ".join(f"{k}: {m}" for k, ok, m in results if not ok)
    summary = f"td_update_all: {ok_count}/{len(results)} symbols updated." + (f" FAILS: {fails}" if fails else "")
    return ok_count == len(results), summary


# ─────────────────────────────────────────────────────────────────────────
# Live refresh (~60s cadence, auto-scales interval as registry grows)
# ─────────────────────────────────────────────────────────────────────────
def td_recommended_poll_interval_sec() -> int:
    """Registry jitni badi hogi utna hi live-poll interval khud-ba-khud
    badhta hai — taaki free-plan (8 credit/min) kabhi cross na ho.
    2 symbols → 60s. 50 symbols → ~7min. 500 symbols → ~63min.
    (1 credit = 1 symbol per API call, chahe outputsize kuch bhi ho.)"""
    n = max(1, len(TD_SYMBOL_REGISTRY))
    return max(60, int(math.ceil(n / _TD_FREE_PLAN_CREDITS_PER_MIN) * 60))


def td_live_poll_batch(api_key: str, log_fn=None) -> dict:
    """SAARE registry symbols ka latest chhota window (last ~10 bars)
    fetch karke in-memory cache (_TD_CACHE) mein merge karta hai — Supabase
    par write NAHI karta (wo sirf daily background job karta hai). Free-
    plan rate-limit (8 credit/min) respect karne ke liye batch khud-ba-khud
    max 8-symbols-per-call mein chunk hoti hai. Return: {key: True/...}
    jo symbols update hue unki list."""
    if not api_key:
        return {}
    updated = {}
    MAX_PER_CALL = _TD_FREE_PLAN_CREDITS_PER_MIN
    for i in range(0, len(TD_SYMBOL_REGISTRY), MAX_PER_CALL):
        batch = TD_SYMBOL_REGISTRY[i:i + MAX_PER_CALL]
        symbols_csv = ",".join(e["td_symbol"] for e in batch)
        try:
            resp = requests.get("https://api.twelvedata.com/time_series", params={
                "symbol": symbols_csv, "interval": _TD_BASE_INTERVAL,
                "timezone": "UTC", "outputsize": 10, "apikey": api_key,
            }, timeout=20)
            data = resp.json()
        except Exception as e:
            _log(f"live-poll batch fail: {e}", "warn", log_fn)
            continue
        for entry in batch:
            key = entry["key"]
            if len(batch) == 1 and isinstance(data, dict) and "values" in data:
                sym_data = data
            elif isinstance(data, dict):
                sym_data = data.get(entry["td_symbol"])
            else:
                sym_data = None
            if not isinstance(sym_data, dict):
                continue
            new_rows = _td_normalize_values(sym_data.get("values", []))
            if not new_rows:
                continue
            with _TD_CACHE_LOCK:
                existing = _TD_CACHE.get(key, [])
                merged_map = {r["t"]: r for r in existing}
                for r in new_rows:
                    merged_map[r["t"]] = r
                _TD_CACHE[key] = sorted(merged_map.values(), key=lambda r: r["t"])
            updated[key] = True
        time.sleep(0.2)
    return updated
