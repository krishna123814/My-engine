"""
live_engine.py — Phase 3 refactor: extracted from app.py.

Ye module poora "live-data engine" hai jo pehle app.py ke andar ek bade
block mein tha (Binance option-chain WebSocket engine — mark/trade/spot
threads + watchdog, Finnhub WebSocket, Fyers option-chain background
cache, Fyers WebSocket engine + REST fallback poller, market-depth
caching, aur BNHistoryAPI ka internal Tornado HTTP server registration).

ZERO BEHAVIOR CHANGE — pure code move + import re-wiring, jaisa Phase 1-2
modularization mein config.py/live_state.py/etc ke liye kiya gaya tha.
Koi logic nahi badla, sirf location.

app.py ab isse import karta hai: _ensure_binance_threads,
_ensure_finnhub_ws_thread, _ensure_fyers_threads, _get_live_payload,
_register_api_route, get_cached_binance_option_chain_payload,
get_cached_option_chain_payload, refresh_market_depth_cache,
_MD_PUSHER_DEBUG.
"""
import json
import time
import threading
import datetime
import requests
import streamlit as st
import td_symbols as _td
import replay_symbols as _replay

from config import BN_LIVE_FILE, _ist_now
from startup_log import _slog, _slog_exception
from credentials import load_creds, _get_binance_creds
from candle_state import _CANDLE, _CANDLE_LOCK, _update_candle_ltp, _set_candle_from_bar
from live_state import (
    _LIVE, _LIVE_LOCK, _LAST_TICK_JS, _LAST_TICK_LOCK,
    _BN_FEED_DEBUG, _BN_FEED_DEBUG_LOCK,
)
from network_proxy import _get_ws_proxy
from login_session import _token_monitor_loop
from fyers_history import _fyers_history
from storage import (
    _local_state_load_all, _local_state_save,
    FINNHUB_API_KEY, REPLAY_SUPABASE_URL, REPLAY_SUPABASE_ANON_KEY,
)
from binance_rest import _binance_call, binance_get_spot_price
from market_data import _build_sv3_bulk_all_stocks, _sv3_symbol_list, save_sv3_last_symbol
from broker_meta import (
    fyers_get_option_chain, BINANCE_EAPI_URL,
    OC_FILE, _OC_CACHE, _OC_DEBUG, _OC_LOCK, _OC_TTL,
)

try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None

BINANCE_OC_FILE          = "binance_optionchain.json"
BINANCE_OC_STRIKE_WINDOW = 20     # ATM ke dono taraf, HAR expiry ke liye itni strikes
                                    # (frontend "Strikes" dropdown max option se match — user
                                    # jitni bhi maange, 20 tak, usi window se serve hota hai;
                                    # frontend usmein se apni choice slice karta hai)
BINANCE_OC_META_TTL      = 600    # exchangeInfo (strikes/expiries) refresh — 10 min
BINANCE_OC_TICKER_TTL    = 5      # 24hr ticker snapshot (OI/vol/chg%) refresh — 5 sec
BINANCE_SPOT_STALE_SEC   = 10     # spot WS tick se purana ho to REST se fresh price lo
BINANCE_MARK_STALE_SEC   = 15     # mark-price stream se 15 sec tak koi bhi tick na aaye to stale
BINANCE_TRADE_STALE_SEC  = 30     # trade stream sparse hoti hai, isliye thoda zyada margin

BINANCE_WS_MARK_URL  = "wss://fstream.binance.com/market/stream?streams=btcusdt@optionMarkPrice"
BINANCE_WS_TRADE_URL = "wss://fstream.binance.com/public/stream?streams=btcusdt@optionTrade"
BINANCE_WS_SPOT_URL  = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# symbol -> {mark,bid,ask,last,delta,gamma,theta,vega,iv,oi,chg,chgp,volume,vol_cum,ts}
#
# FIX (same class of bug as _PROXY_CACHE, isi pipeline ke saath saath): ye
# saare _BN_* stores pehle plain dict literals the. Streamlit har rerun par
# poori file top-se-bottom dobara chalata hai — background threads (WS
# mark/trade/spot loops, meta loop, ticker loop, payload loop, watchdog) jo
# bhi likhte the, agla hi rerun use khaali/default pe reset kar deta tha.
# Isi wajah se "Expiries loaded: 0", "Meta last try: abhi tak try nahi hua",
# "Payload/Ticker/Meta loop: never started" aur "Mark/Trade/Spot WS:
# DISCONNECTED — last tick never" hamesha dikhta tha, chahe threads
# background mein sahi se data fetch/receive kar rahe hon. @st.cache_resource
# har store ko process-wide singleton banata hai — function sirf pehli baar
# chalta hai, baaki sab reruns (isi rerun ke andar bhi) wahi ek object wapas
# paate hain, jab tak process khud restart na ho.
@st.cache_resource
def _get_bn_live_quotes() -> dict:
    return {}

@st.cache_resource
def _get_bn_live_lock() -> threading.Lock:
    return threading.Lock()

_BN_LIVE_QUOTES = _get_bn_live_quotes()
_BN_LIVE_LOCK   = _get_bn_live_lock()

@st.cache_resource
def _get_bn_spot_price() -> dict:
    return {"price": None, "ts": 0.0, "source": None}   # source: "ws" | "rest"

@st.cache_resource
def _get_bn_spot_lock() -> threading.Lock:
    return threading.Lock()

_BN_SPOT_PRICE = _get_bn_spot_price()
_BN_SPOT_LOCK  = _get_bn_spot_lock()

# exchangeInfo se banaya gaya static-ish structure: expiry_epoch(ms) -> [{strike, ce_symbol, pe_symbol}]
@st.cache_resource
def _get_bn_chain_meta() -> dict:
    return {"expiries": [], "by_expiry": {}, "ts": 0.0}

@st.cache_resource
def _get_bn_chain_meta_lock() -> threading.Lock:
    return threading.Lock()

_BN_CHAIN_META = _get_bn_chain_meta()
_BN_CHAIN_META_LOCK = _get_bn_chain_meta_lock()

# ── Meta-fetch ka EXACT last outcome — pehle _bn_refresh_option_meta() ka
# (ok, msg) return value silently discard ho jaata tha (_binance_oc_meta_bg_loop
# use kabhi padhta hi nahi tha), isliye "Expiries loaded: 0" dikhta tha par
# WHY fail hua ye kahin visible nahi tha. Ab har attempt (success ya fail,
# dono) ka result yahan RAM mein store hota hai, aur debug panel (chart.html)
# tak bhi bheja jaata hai. ──────────────────────────────────────────────────
@st.cache_resource
def _get_bn_meta_last_result() -> dict:
    return {"ok": None, "msg": "abhi tak try nahi hua", "ts": 0.0}

@st.cache_resource
def _get_bn_meta_last_result_lock() -> threading.Lock:
    return threading.Lock()

_BN_META_LAST_RESULT = _get_bn_meta_last_result()
_BN_META_LAST_RESULT_LOCK = _get_bn_meta_last_result_lock()

@st.cache_resource
def _get_bn_ticker_last() -> dict:
    return {"ts": 0.0}

_BN_TICKER_LAST = _get_bn_ticker_last()

# ── Background-thread heartbeats — agar in mein se koi thread kisi
# unhandled exception se silently mar jaaye (Python thread crash ho jaaye
# to koi global alert nahi milta), to bhi frontend debug turant flag kar
# sake ki fulaan loop ruk gaya hai. Har loop apni iteration ke saath yahan
# apna timestamp likhta rehta hai — agar wo bahut purana ho jaaye, thread
# dead maano. ────────────────────────────────────────────────────────────
@st.cache_resource
def _get_bn_thread_heartbeat() -> dict:
    return {"meta": 0.0, "ticker": 0.0, "payload": 0.0}

_BN_THREAD_HEARTBEAT = _get_bn_thread_heartbeat()

@st.cache_resource
def _get_bn_ws_state() -> dict:
    return {
        "mark_connected":  False,
        "trade_connected": False,
        "spot_connected":  False,
        "last_error":      None,
        # Global "last message received" timestamps — per-symbol staleness track
        # karna mehenga hai (dozens of visible symbols), isliye jaisa Binance khud
        # push karta hai (koi bhi symbol ka tick isi ek stream par aata hai),
        # hum poori stream ki freshness ek hi global timestamp se maapte hain.
        # Agar TCP connection technically open hai par koi tick 15/30 sec tak
        # na aaye, us stream ko "stale" maante hain — connected hone se yeh
        # alag baat hai (jaisa spot ke saath pehle fix kiya).
        "mark_last_msg_ts":  0.0,
        "trade_last_msg_ts": 0.0,
    }

_BN_WS_STATE = _get_bn_ws_state()

# ── Watchdog support: live wsapp handles + force-reconnect on stale stream ──
# ping_interval/ping_timeout library ke bharose hain — kuch network/proxy
# setups mein TCP half-open ho sakta hai jahan ping bhi silently drop ho
# jaaye (socket "open" dikhta hai par data flow ruka hota hai). Independent
# watchdog thread yahan _*_last_msg_ts ko baahar se check karke, agar bahut
# zyada stale ho jaaye, socket ko force-close karta hai — jisse wsapp ka
# apna while-loop backoff ke saath reconnect kar leta hai.
@st.cache_resource
def _get_bn_ws_apps() -> dict:
    return {"mark": None, "trade": None, "spot": None}

@st.cache_resource
def _get_bn_ws_apps_lock() -> threading.Lock:
    return threading.Lock()

_BN_WS_APPS = _get_bn_ws_apps()
_BN_WS_APPS_LOCK = _get_bn_ws_apps_lock()
BINANCE_WATCHDOG_CHECK_SEC   = 5     # kitni baar check karein
BINANCE_WATCHDOG_MARK_MULT   = 2.0   # mark: stale-threshold ka itna guna age ho to force-reconnect
BINANCE_WATCHDOG_TRADE_MULT  = 2.0
BINANCE_WATCHDOG_SPOT_SEC    = 20    # spot ke liye seedha seconds (agg trade sparse ho sakta hai)

def _bn_watchdog_loop():
    """Independent watchdog — ping/pong se bhi zyada bharosemand. Har
    BINANCE_WATCHDOG_CHECK_SEC par teeno streams ki last_msg_ts age check
    karta hai; agar koi stream apne stale-threshold se kaafi zyada purani ho
    chuki hai (par "connected" flag abhi bhi True hai — half-open TCP ka
    lakshan), us stream ka socket force-close kar deta hai taaki uska apna
    reconnect-loop turant naya connection try kare."""
    while True:
        try:
            now = time.time()
            with _BN_WS_APPS_LOCK:
                apps = dict(_BN_WS_APPS)

            mark_ts = _BN_WS_STATE.get("mark_last_msg_ts") or 0.0
            if mark_ts and (now - mark_ts) > (BINANCE_MARK_STALE_SEC * BINANCE_WATCHDOG_MARK_MULT):
                ws = apps.get("mark")
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                    _BN_WS_STATE["last_error"] = "watchdog: mark stream stale, force-reconnecting"

            trade_ts = _BN_WS_STATE.get("trade_last_msg_ts") or 0.0
            if trade_ts and (now - trade_ts) > (BINANCE_TRADE_STALE_SEC * BINANCE_WATCHDOG_TRADE_MULT):
                ws = apps.get("trade")
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                    _BN_WS_STATE["last_error"] = "watchdog: trade stream stale, force-reconnecting"

            with _BN_SPOT_LOCK:
                spot_ts = _BN_SPOT_PRICE.get("ts") or 0.0
                spot_source = _BN_SPOT_PRICE.get("source")
            if spot_source == "ws" and spot_ts and (now - spot_ts) > BINANCE_WATCHDOG_SPOT_SEC:
                ws = apps.get("spot")
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                    _BN_WS_STATE["last_error"] = "watchdog: spot stream stale, force-reconnecting"
        except Exception as e:
            _BN_WS_STATE["last_error"] = f"watchdog loop: {e}"
        time.sleep(BINANCE_WATCHDOG_CHECK_SEC)

# ── Step 1: exchangeInfo — strikes/expiries ka structure (rarely changes) ──
def _bn_record_meta_result(ok: bool, msg: str) -> None:
    with _BN_META_LAST_RESULT_LOCK:
        _BN_META_LAST_RESULT["ok"]  = ok
        _BN_META_LAST_RESULT["msg"] = msg
        _BN_META_LAST_RESULT["ts"]  = time.time()

def _bn_refresh_option_meta(force: bool = False) -> tuple[bool, str]:
    now = time.time()
    with _BN_CHAIN_META_LOCK:
        if not force and (now - _BN_CHAIN_META["ts"]) < BINANCE_OC_META_TTL:
            return True, "cached"
    api_key, _ = _get_binance_creds()
    ok, info = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/exchangeInfo", {}, api_key, signed=False)
    if not ok:
        _bn_record_meta_result(False, f"exchangeInfo fail: {info}")
        return False, f"exchangeInfo fail: {info}"
    symbols = info.get("optionSymbols", [])
    btc_syms = [s for s in symbols if s.get("underlying", "") == "BTCUSDT"]
    if not btc_syms:
        _bn_record_meta_result(False, f"Koi BTCUSDT option symbol nahi mila (total symbols in response: {len(symbols)})")
        return False, "Koi BTCUSDT option symbol nahi mila"

    by_expiry: dict = {}
    for s in btc_syms:
        try:
            strike = float(s.get("strikePrice"))
            expiry_epoch = int(s.get("expiryDate"))
        except Exception:
            continue
        sym  = s.get("symbol")
        side = (s.get("side") or "").upper()
        strike_map = by_expiry.setdefault(expiry_epoch, {})
        row = strike_map.setdefault(strike, {"strike": strike, "ce_symbol": None, "pe_symbol": None})
        if side == "CALL":
            row["ce_symbol"] = sym
        elif side == "PUT":
            row["pe_symbol"] = sym

    final_by_expiry = {epoch: sorted(m.values(), key=lambda x: x["strike"]) for epoch, m in by_expiry.items()}
    with _BN_CHAIN_META_LOCK:
        _BN_CHAIN_META["expiries"]  = sorted(final_by_expiry.keys())
        _BN_CHAIN_META["by_expiry"] = final_by_expiry
        _BN_CHAIN_META["ts"] = now
    _bn_record_meta_result(True, f"refreshed — {len(final_by_expiry)} expiries, {len(btc_syms)} BTCUSDT symbols")
    return True, "refreshed"

def _binance_oc_meta_bg_loop():
    while True:
        try:
            _bn_refresh_option_meta()
        except Exception as e:
            _BN_WS_STATE["last_error"] = f"meta loop: {e}"
            _bn_record_meta_result(False, f"meta loop exception: {e}")
        _BN_THREAD_HEARTBEAT["meta"] = time.time()
        time.sleep(30)  # TTL check ke liye baar-baar wake, actual REST call sirf TTL cross hone par

# ── Step 2: 24hr ticker snapshot — OI/Volume/Change% (WS pe available nahi) ─
# POORE market ke liye EK call (symbol param nahi diya), per-symbol nahi.
def _bn_refresh_ticker_snapshot():
    now = time.time()
    if (now - _BN_TICKER_LAST["ts"]) < BINANCE_OC_TICKER_TTL:
        return
    _BN_TICKER_LAST["ts"] = now
    api_key, _ = _get_binance_creds()
    ok, tick = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/ticker", {}, api_key, signed=False)
    if not ok or not isinstance(tick, list):
        return
    with _BN_LIVE_LOCK:
        for t in tick:
            sym = t.get("symbol")
            if not sym:
                continue
            row = _BN_LIVE_QUOTES.setdefault(sym, {})
            row["oi"]     = float(t.get("openInterest", 0) or 0)
            row["chg"]    = float(t.get("priceChange", 0) or 0)
            row["chgp"]   = float(t.get("priceChangePercent", 0) or 0)
            row["volume"] = float(t.get("volume", 0) or 0)
            # WS ne abhi tak kuch na bheja ho to REST se ek baar fallback fill ho jaaye
            row.setdefault("bid",  float(t.get("bidPrice", 0) or 0))
            row.setdefault("ask",  float(t.get("askPrice", 0) or 0))
            row.setdefault("last", float(t.get("lastPrice", 0) or 0))
            row.setdefault("mark", float(t.get("lastPrice", 0) or 0))

def _binance_oc_ticker_bg_loop():
    while True:
        try:
            _bn_refresh_ticker_snapshot()
        except Exception as e:
            _BN_WS_STATE["last_error"] = f"ticker loop: {e}"
        _BN_THREAD_HEARTBEAT["ticker"] = time.time()
        time.sleep(BINANCE_OC_TICKER_TTL)

# ── Reconnect backoff helper — exponential + jitter (TradingView jaisa) ──
# Flat "time.sleep(3)" har baar same gap deta hai chahe server baar-baar
# turant disconnect kare — isse retry storm ban sakta hai. Exponential
# backoff har consecutive failure par gap double karta hai (1s→2s→4s...),
# jitter (chhota random add) isliye taaki agar kabhi multiple threads/users
# same waqt reconnect try karein to sab ek saath thundering-herd na banayein.
# Success (on_open) par caller apna fail-counter reset kar deta hai.
import random as _bn_random

BINANCE_WS_BACKOFF_BASE = 1     # sec — pehla retry
BINANCE_WS_BACKOFF_MAX  = 30    # sec — is se zyada kabhi nahi rukna

def _bn_ws_backoff_sleep(fail_count: int) -> None:
    delay = min(BINANCE_WS_BACKOFF_BASE * (2 ** max(0, fail_count - 1)), BINANCE_WS_BACKOFF_MAX)
    jitter = _bn_random.uniform(0, delay * 0.3)
    time.sleep(delay + jitter)

# ── Reconnect gap-fill — jab mark WS reconnect hoti hai (disconnect ke
# baad), turant REST se fresh data le lo, taaki naye WS ticks ka wait na
# karna pade aur reconnect ke turant baad bhi screen par purana data na
# dikhe.
#
# FIX (rate-limit + coverage): pehle yeh function sirf NEAREST expiry ki
# ATM-window strikes ke liye, HAR symbol ka ALAG REST call karta tha
# (~42 individual calls, 0.05s gap ke saath ~2 sec). Flaky network par
# baar-baar reconnect hone se yeh burst multiple baar overlap ho sakta
# tha aur Binance ka 400 req/min limit tod sakta tha.
#
# Ab /eapi/v1/mark ko bina `symbol` param ke call karte hain — yeh
# poore market ka mark/IV/greeks data EK HI REST call mein deta hai
# (bilkul waisa hi jaisa /eapi/v1/ticker already karta hai). Isse:
#   • Call count 42 → 1 ho jaata hai (rate-limit-safe).
#   • Sirf nearest expiry tak simit rehne ki zaroorat nahi rahi — jo
#     bhi data mila, saari expiries/strikes ke liye apply ho jaata hai.
_BN_GAPFILL_LOCK    = threading.Lock()  # non-blocking — chalu gap-fill ke upar dusra spawn na ho
_BN_GAPFILL_RUNNING = False

def _bn_gapfill_visible_quotes():
    global _BN_GAPFILL_RUNNING
    # ── Dedupe: agar ek gap-fill already chal raha hai to naya spawn na ho
    # (flaky network mein baar-baar reconnect se REST burst na lage). ──
    if not _BN_GAPFILL_LOCK.acquire(blocking=False):
        return
    try:
        _BN_GAPFILL_RUNNING = True
        with _BN_CHAIN_META_LOCK:
            expiries = list(_BN_CHAIN_META["expiries"])
        if not expiries:
            return
        try:
            ok, data = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/mark", {}, "", signed=False)
        except Exception as e:
            _BN_WS_STATE["last_error"] = f"gapfill: {e}"
            return
        if not ok or not isinstance(data, list):
            return
        now = time.time()
        with _BN_LIVE_LOCK:
            for item in data:
                if not isinstance(item, dict):
                    continue
                sym = item.get("symbol")
                if not sym:
                    continue
                row = _BN_LIVE_QUOTES.setdefault(sym, {})
                if item.get("markPrice") not in (None, ""):
                    row["mark"]    = item["markPrice"]
                    row["mark_ts"] = now   # LTP freshness-compare ke liye (leg_from)
                if item.get("bidIV")     is not None:       row["buy_iv"]  = item["bidIV"]
                if item.get("askIV")     is not None:       row["sell_iv"] = item["askIV"]
                if item.get("markIV")    is not None:       row["iv"]      = item["markIV"]
                if item.get("delta")     is not None:       row["delta"]   = item["delta"]
                if item.get("gamma")     is not None:       row["gamma"]   = item["gamma"]
                if item.get("theta")     is not None:       row["theta"]   = item["theta"]
                if item.get("vega")      is not None:       row["vega"]    = item["vega"]
                row["ts"] = now
    except Exception:
        pass
    finally:
        _BN_GAPFILL_RUNNING = False
        _BN_GAPFILL_LOCK.release()

# ── Step 3: WebSocket #1 — Option Mark Price (mark/bid/ask/greeks/IV), LIVE ──
# Single connection, underlying-level ("btcusdt") — sabhi strikes/expiries ka
# data isi ek stream mein aata hai (array push), per-symbol subscribe nahi
# karna padta. Field-map verified: lowercase b/a=buy/sell IV (NOT bid/ask —
# real bid/ask are bo/ao), lowercase v=vega (NOT volume — real volume "V" 24hr ticker se aata hai).
_BN_MARK_FIELD_MAP = {
    "last":  ["c"],
    "bid":   ["bo"],
    "ask":   ["ao"],
    "mark":  ["mp"],
    "delta": ["d"],
    "gamma": ["g"],
    "theta": ["t"],
    "vega":  ["v"],
    "iv":    ["vo"],
    "buy_iv":  ["b"],
    "sell_iv": ["a"],
}

def _bn_apply_mark_msg(msg: dict):
    try:
        sym = msg.get("s")
        if not sym:
            return
        with _BN_LIVE_LOCK:
            row = _BN_LIVE_QUOTES.setdefault(sym, {})
            _touched_mark = False
            for target, keys in _BN_MARK_FIELD_MAP.items():
                for k in keys:
                    if k in msg and msg[k] not in (None, ""):
                        row[target] = msg[k]
                        if target == "mark":
                            _touched_mark = True
                        break
            if _touched_mark:
                row["mark_ts"] = time.time()   # LTP freshness-compare ke liye (leg_from)
            row["ts"] = time.time()
    except Exception:
        pass

def _bn_ws_mark_on_message(ws, message):
    try:
        data = json.loads(message)
        payload = data.get("data") if isinstance(data, dict) and "data" in data else data
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    _bn_apply_mark_msg(item)
        elif isinstance(payload, dict):
            _bn_apply_mark_msg(payload)
        _BN_WS_STATE["last_error"] = None
        _BN_WS_STATE["mark_last_msg_ts"] = time.time()
    except Exception as e:
        _BN_WS_STATE["last_error"] = str(e)

def _bn_ws_mark_loop():
    if websocket is None:
        _BN_WS_STATE["last_error"] = "websocket-client package missing — pip install websocket-client"
        return
    fail_count = 0
    ever_connected = False
    while True:
        try:
            def _on_open(ws):
                nonlocal ever_connected, fail_count
                _BN_WS_STATE.update({"mark_connected": True, "mark_last_msg_ts": time.time()})
                fail_count = 0
                if ever_connected:
                    # Pehli baar connect nahi, yeh ek RECONNECT hai — turant
                    # visible symbols REST se gap-fill karo (disconnect ke
                    # dauraan jo miss hua, naye WS ticks ka wait na karna
                    # pade).
                    threading.Thread(target=_bn_gapfill_visible_quotes, daemon=True).start()
                ever_connected = True
            wsapp = websocket.WebSocketApp(
                BINANCE_WS_MARK_URL,
                on_open=_on_open,
                on_message=_bn_ws_mark_on_message,
                on_error=lambda ws, e: _BN_WS_STATE.update({"mark_connected": False, "last_error": str(e)}),
                on_close=lambda ws, c, m: _BN_WS_STATE.update({"mark_connected": False}),
            )
            with _BN_WS_APPS_LOCK:
                _BN_WS_APPS["mark"] = wsapp
            wsapp.run_forever(ping_interval=20, ping_timeout=10, **_get_ws_proxy())
        except Exception as e:
            _BN_WS_STATE["last_error"] = str(e)
        _BN_WS_STATE["mark_connected"] = False
        fail_count += 1
        _bn_ws_backoff_sleep(fail_count)   # exponential backoff + jitter

# ── Step 4: WebSocket #2 — Option Trade stream (last price + cumulative volume) ──
def _bn_apply_trade_msg(msg: dict):
    try:
        sym = msg.get("s")
        if not sym:
            return
        price, qty = msg.get("p"), msg.get("q")
        with _BN_LIVE_LOCK:
            row = _BN_LIVE_QUOTES.setdefault(sym, {})
            if price not in (None, ""):
                row["last"] = price
                row["last_ts"] = time.time()   # LTP freshness-compare ke liye (leg_from)
            if qty not in (None, ""):
                try:
                    row["vol_cum"] = float(row.get("vol_cum", 0) or 0) + float(qty)
                except Exception:
                    pass
            row["ts"] = time.time()
    except Exception:
        pass

def _bn_ws_trade_on_message(ws, message):
    try:
        data = json.loads(message)
        payload = data.get("data") if isinstance(data, dict) and "data" in data else data
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    _bn_apply_trade_msg(item)
        elif isinstance(payload, dict):
            _bn_apply_trade_msg(payload)
        _BN_WS_STATE["trade_last_msg_ts"] = time.time()
    except Exception:
        pass

def _bn_ws_trade_loop():
    if websocket is None:
        return
    fail_count = 0
    while True:
        try:
            def _on_open(ws):
                nonlocal fail_count
                _BN_WS_STATE.update({"trade_connected": True, "trade_last_msg_ts": time.time()})
                fail_count = 0
            wsapp = websocket.WebSocketApp(
                BINANCE_WS_TRADE_URL,
                on_open=_on_open,
                on_message=_bn_ws_trade_on_message,
                on_error=lambda ws, e: _BN_WS_STATE.update({"trade_connected": False, "last_error": str(e)}),
                on_close=lambda ws, c, m: _BN_WS_STATE.update({"trade_connected": False}),
            )
            with _BN_WS_APPS_LOCK:
                _BN_WS_APPS["trade"] = wsapp
            wsapp.run_forever(ping_interval=20, ping_timeout=10, **_get_ws_proxy())
        except Exception as e:
            _BN_WS_STATE["last_error"] = str(e)
        _BN_WS_STATE["trade_connected"] = False
        fail_count += 1
        _bn_ws_backoff_sleep(fail_count)

# ── Step 5: WebSocket #3 — BTCUSDT spot price (agg trade), REST price-poll ki
# jagah — pehle har second ek REST call lagti thi, ab bilkul nahi. ──────────
def _bn_ws_spot_on_message(ws, message):
    try:
        data = json.loads(message)
        price = data.get("p")
        if price not in (None, ""):
            with _BN_SPOT_LOCK:
                _BN_SPOT_PRICE["price"]  = float(price)
                _BN_SPOT_PRICE["ts"]     = time.time()
                _BN_SPOT_PRICE["source"] = "ws"
    except Exception:
        pass

def _bn_ws_spot_loop():
    if websocket is None:
        return
    fail_count = 0
    while True:
        try:
            def _on_open(ws):
                nonlocal fail_count
                _BN_WS_STATE.update({"spot_connected": True})
                fail_count = 0
            wsapp = websocket.WebSocketApp(
                BINANCE_WS_SPOT_URL,
                on_open=_on_open,
                on_message=_bn_ws_spot_on_message,
                on_error=lambda ws, e: _BN_WS_STATE.update({"spot_connected": False, "last_error": str(e)}),
                on_close=lambda ws, c, m: _BN_WS_STATE.update({"spot_connected": False}),
            )
            with _BN_WS_APPS_LOCK:
                _BN_WS_APPS["spot"] = wsapp
            wsapp.run_forever(ping_interval=20, ping_timeout=10, **_get_ws_proxy())
        except Exception as e:
            _BN_WS_STATE["last_error"] = str(e)
        _BN_WS_STATE["spot_connected"] = False
        fail_count += 1
        _bn_ws_backoff_sleep(fail_count)

def _ensure_binance_ws_threads():
    """Idempotent — Streamlit rerun par dobara call hone par bhi duplicate
    thread nahi banti (thread name check, jaisa _ensure_live_threads karta hai)."""
    names = {t.name for t in threading.enumerate()}
    if "BinanceOptMarkWS" not in names:
        threading.Thread(target=_bn_ws_mark_loop, name="BinanceOptMarkWS", daemon=True).start()
    if "BinanceOptTradeWS" not in names:
        threading.Thread(target=_bn_ws_trade_loop, name="BinanceOptTradeWS", daemon=True).start()
    if "BinanceWSWatchdog" not in names:
        threading.Thread(target=_bn_watchdog_loop, name="BinanceWSWatchdog", daemon=True).start()
    if "BinanceSpotWS" not in names:
        threading.Thread(target=_bn_ws_spot_loop, name="BinanceSpotWS", daemon=True).start()
    if "BinanceOptMetaBG" not in names:
        threading.Thread(target=_binance_oc_meta_bg_loop, name="BinanceOptMetaBG", daemon=True).start()
    if "BinanceOptTickerBG" not in names:
        threading.Thread(target=_binance_oc_ticker_bg_loop, name="BinanceOptTickerBG", daemon=True).start()

# ── Finnhub WebSocket — LIVE ticks for Twelve Data registry symbols
# (Dow/GBP-USD/Apple/Amazon) ────────────────────────────────────────────
# Historical/backfill data Twelve Data se aata hai (td_symbols.py:
# td_update_all, daily background job, Supabase mein store hota hai).
# LIVE price-update ab is Finnhub WebSocket se aata hai — free-plan pe
# bhi real push-based streaming milti hai (Twelve Data free-plan pe
# sirf REST polling milti hai, 8 credit/min cap ke saath, isliye 60s se
# tez nahi ho sakti thi). Same reconnect/backoff pattern jo Binance WS
# threads mein already use ho raha hai (dekho _bn_ws_backoff_sleep).
_FINNHUB_WS_STATE = {"connected": False, "last_msg_ts": None, "last_error": None}
_FINNHUB_WS_BACKOFF_BASE = 2.0
_FINNHUB_WS_BACKOFF_MAX  = 30.0

def _finnhub_ws_backoff_sleep(fail_count: int) -> None:
    import random as _fh_random
    delay = min(_FINNHUB_WS_BACKOFF_BASE * (2 ** max(0, fail_count - 1)), _FINNHUB_WS_BACKOFF_MAX)
    time.sleep(delay + _fh_random.uniform(0, delay * 0.3))

def _finnhub_symbol_to_key_map() -> dict:
    """Finnhub 'finnhub_symbol' → registry 'key' — WS message aane par
    turant pata chal jaaye ye kis symbol ka tick hai."""
    return {e["finnhub_symbol"]: e["key"] for e in _td.TD_SYMBOL_REGISTRY if e.get("finnhub_symbol")}

def _finnhub_ws_on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception:
        return
    if not isinstance(data, dict) or data.get("type") != "trade":
        return
    _sym_to_key = _finnhub_symbol_to_key_map()
    for trade in (data.get("data") or []):
        if not isinstance(trade, dict):
            continue
        sym = trade.get("s")
        key = _sym_to_key.get(sym)
        if not key:
            continue
        price = trade.get("p")
        ts_ms = trade.get("t")   # Finnhub epoch milliseconds
        epoch_ts = (ts_ms / 1000.0) if ts_ms else time.time()
        try:
            _td.td_apply_live_tick(key, price, epoch_ts)
        except Exception:
            pass
    _FINNHUB_WS_STATE["last_msg_ts"] = time.time()
    _FINNHUB_WS_STATE["last_error"] = None

def _finnhub_ws_loop():
    if websocket is None or not FINNHUB_API_KEY:
        _FINNHUB_WS_STATE["last_error"] = (
            "websocket-client missing" if websocket is None else "FINNHUB_API_KEY secret missing"
        )
        return
    fail_count = 0
    while True:
        try:
            def _on_open(ws):
                nonlocal fail_count
                _FINNHUB_WS_STATE.update({"connected": True, "last_msg_ts": time.time(), "last_error": None})
                fail_count = 0
                for entry in _td.TD_SYMBOL_REGISTRY:
                    _fh_sym = entry.get("finnhub_symbol")
                    if _fh_sym:
                        try:
                            ws.send(json.dumps({"type": "subscribe", "symbol": _fh_sym}))
                        except Exception:
                            pass
            wsapp = websocket.WebSocketApp(
                f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}",
                on_open=_on_open,
                on_message=_finnhub_ws_on_message,
                on_error=lambda ws, e: _FINNHUB_WS_STATE.update({"connected": False, "last_error": str(e)}),
                on_close=lambda ws, c, m: _FINNHUB_WS_STATE.update({"connected": False}),
            )
            wsapp.run_forever(ping_interval=20, ping_timeout=10, **_get_ws_proxy())
        except Exception as e:
            _FINNHUB_WS_STATE["last_error"] = str(e)
        _FINNHUB_WS_STATE["connected"] = False
        fail_count += 1
        _finnhub_ws_backoff_sleep(fail_count)

def _ensure_finnhub_ws_thread():
    """Idempotent — dobara call hone par bhi duplicate thread nahi banti."""
    names = {t.name for t in threading.enumerate()}
    if "FinnhubLiveWS" not in names:
        threading.Thread(target=_finnhub_ws_loop, name="FinnhubLiveWS", daemon=True).start()


# ── Step 6: payload builder — SABHI expiries, PURE in-memory (no network) ──
def _bn_ws_status_snapshot(spot_source=None, spot_age=None, expiries_count=None) -> dict:
    """Har jagah se same tarike se WS/thread diagnostics nikalne ke liye —
    isse debug ko pata chal sakta hai ki EXACTLY kahan atka hai, chahe poora
    option-chain payload abhi ban hi na paaya ho (jaisa 'metadata load ho
    raha hai' ya 'spot abhi available nahi' wale early-error cases). Pehle
    ye sirf success path mein banta tha — error paths mein bilkul khaali reh
    jaata tha, isliye debug popup ko root cause pata hi nahi chal pata tha."""
    now = time.time()
    with _BN_META_LAST_RESULT_LOCK:
        _meta_result = dict(_BN_META_LAST_RESULT)
    mark_age  = (now - _BN_WS_STATE["mark_last_msg_ts"])  if _BN_WS_STATE["mark_last_msg_ts"]  else None
    trade_age = (now - _BN_WS_STATE["trade_last_msg_ts"]) if _BN_WS_STATE["trade_last_msg_ts"] else None
    mark_live  = bool(_BN_WS_STATE["mark_connected"])  and mark_age  is not None and mark_age  <= BINANCE_MARK_STALE_SEC
    trade_live = bool(_BN_WS_STATE["trade_connected"]) and trade_age is not None and trade_age <= BINANCE_TRADE_STALE_SEC
    return dict(
        _BN_WS_STATE,
        mark_live=mark_live,
        trade_live=trade_live,
        mark_age_sec=round(mark_age, 1) if mark_age is not None else None,
        trade_age_sec=round(trade_age, 1) if trade_age is not None else None,
        spot_source=spot_source,
        spot_age_sec=round(spot_age, 1) if spot_age is not None else None,
        # Background thread heartbeats — 0 ka matlab hai thread ne abhi tak
        # ek baar bhi iteration complete nahi ki (startup), warna age jitni
        # zyada, thread utni der se "chup" hai.
        meta_thread_age_sec=round(now - _BN_THREAD_HEARTBEAT["meta"], 1) if _BN_THREAD_HEARTBEAT["meta"] else None,
        ticker_thread_age_sec=round(now - _BN_THREAD_HEARTBEAT["ticker"], 1) if _BN_THREAD_HEARTBEAT["ticker"] else None,
        payload_thread_age_sec=round(now - _BN_THREAD_HEARTBEAT["payload"], 1) if _BN_THREAD_HEARTBEAT["payload"] else None,
        # Kitni expiries meta-loop se mil chuki hain — 0 ka matlab exchangeInfo
        # fetch abhi tak kabhi safal nahi hui (ya Binance ne block/reject
        # kar diya). Isse "metadata load ho raha hai" waala error message
        # genuine startup-delay hai ya permanently stuck hai, ye differentiate
        # ho jaata hai (thread age ke saath milaakar dekho).
        meta_expiries_count=expiries_count,
        # EXACT reason — ab guess karne ki zaroorat nahi. ok=None matlab
        # abhi tak ek baar bhi try nahi hua, ok=False+msg matlab yahi
        # exact error hai jo Binance/network se mila.
        meta_last_ok=_meta_result["ok"],
        meta_last_msg=_meta_result["msg"],
        meta_last_age_sec=round(now - _meta_result["ts"], 1) if _meta_result["ts"] else None,
    )

def _bn_rebuild_payload_from_memory() -> dict:
    with _BN_CHAIN_META_LOCK:
        expiries  = list(_BN_CHAIN_META["expiries"])
        by_expiry = dict(_BN_CHAIN_META["by_expiry"])
    if not expiries:
        return {
            "error": "Option chain metadata load ho raha hai… (exchangeInfo abhi fetch nahi hui, thodi der ruko)",
            "ws": _bn_ws_status_snapshot(expiries_count=0),
            "ts": time.time(),
        }

    with _BN_SPOT_LOCK:
        spot        = _BN_SPOT_PRICE["price"]
        spot_ts     = _BN_SPOT_PRICE["ts"]
        spot_source = _BN_SPOT_PRICE["source"]
    spot_age = (time.time() - spot_ts) if spot_ts else None
    if spot is None or spot_age is None or spot_age > BINANCE_SPOT_STALE_SEC:
        # WS spot ya to abhi connect nahi hua (startup) YA silently mar chuki
        # hai (BINANCE_SPOT_STALE_SEC se koi naya tick nahi aaya) — dono
        # cases mein REST se fresh price le lo (gap-fill), taaki frozen
        # number kabhi permanently serve na ho. Source ko "rest" tag karte
        # hain taaki frontend ko pata chale ye push-live tick nahi hai.
        _fresh_spot, _err = binance_get_spot_price("BTCUSDT")
        if _fresh_spot is not None:
            spot        = _fresh_spot
            spot_source = "rest"
            spot_age    = 0.0
            with _BN_SPOT_LOCK:
                _BN_SPOT_PRICE["price"]  = _fresh_spot
                _BN_SPOT_PRICE["ts"]     = time.time()
                _BN_SPOT_PRICE["source"] = "rest"
        elif _err:
            # REST gap-fill bhi fail hua — asli reason (jaise Binance ne
            # is server ki IP block/rate-limit kar di ho) yahan capture
            # karo, taaki debug popup mein exact wajah dikhe, generic
            # "WebSocket connect ho raha hai" ke bajaye.
            return {
                "error": f"BTC spot price abhi available nahi — WS bhi down, REST gap-fill bhi fail: {_err}",
                "ws": _bn_ws_status_snapshot(spot_source=spot_source, spot_age=spot_age, expiries_count=len(expiries)),
                "ts": time.time(),
            }
    if spot is None:
        return {
            "error": "BTC spot price abhi available nahi (WebSocket connect ho raha hai)",
            "ws": _bn_ws_status_snapshot(spot_source=spot_source, spot_age=spot_age, expiries_count=len(expiries)),
            "ts": time.time(),
        }

    with _BN_LIVE_LOCK:
        live_snapshot = {k: dict(v) for k, v in _BN_LIVE_QUOTES.items()}

    def leg_from(sym):
        if not sym:
            return None
        q = live_snapshot.get(sym, {})
        # FIX: pehle "koi data nahi mila" cases mein ltp/mark/bid/ask
        # silently 0 bhej diya jaata tha — jo real "₹0 price" (genuinely
        # illiquid/worthless strike) se bilkul indistinguishable tha,
        # user ko pata hi nahi chal sakta tha ki ye asli quote hai ya
        # sirf missing-data placeholder. Ab jab data hi available nahi
        # hai to None bhejte hain — frontend (_fmtNum) None ko "—" dikhata
        # hai, jo "abhi data nahi mila" ko saaf tarike se signal karta hai.
        _last = q.get("last")
        _mark = q.get("mark")
        _bid  = q.get("bid")
        _ask  = q.get("ask")
        # FIX (LTP freeze bug): pehle LTP hamesha "last traded price" ko
        # blindly prefer karta tha, "mark" ko sirf tab use karta tha jab
        # "last" kabhi mila hi na ho. Ek baar koi trade ho gaya (chahe kitna
        # hi purana ho), LTP hamesha ke liye us purani trade price par FREEZE
        # ho jaata tha — mark price (jo live continuously update ho raha
        # hota hai) kabhi dikhta hi nahi tha, chahe kitna bhi fresh ho.
        # Isi wajah se illiquid strikes ka LTP debug mein "WS: LIVE" dikhne
        # ke bawajood minutes tak same rehta tha. Ab jo bhi zyada RECENT hai
        # (last trade ya mark tick, timestamp se compare karke) wahi LTP
        # banta hai. ──────────────────────────────────────────────────────
        _last_ts = q.get("last_ts") or 0
        _mark_ts = q.get("mark_ts") or 0
        if _last is not None and _mark is not None:
            _ltp = float(_last) if _last_ts >= _mark_ts else float(_mark)
        elif _last is not None:
            _ltp = float(_last)
        elif _mark is not None:
            _ltp = float(_mark)
        else:
            _ltp = None
        return {
            "symbol": sym,
            "ltp":    _ltp,
            "mark":   float(_mark) if _mark is not None else None,
            "chg":    float(q.get("chg") or 0),
            "chgp":   float(q.get("chgp") or 0),
            "oi":     float(q.get("oi") or 0),
            "oich":   0,
            "oichp":  0,
            "volume": float(q.get("volume") or q.get("vol_cum") or 0),
            "bid":    float(_bid) if _bid is not None else None,
            "ask":    float(_ask) if _ask is not None else None,
            "iv":     q.get("iv"),
            "delta":  q.get("delta"),
            "gamma":  q.get("gamma"),
            "theta":  q.get("theta"),
            "vega":   q.get("vega"),
        }

    chains, expiry_meta_list = {}, []
    for epoch in expiries:
        rows_all = by_expiry.get(epoch, [])
        if not rows_all:
            continue
        strikes_sorted = [r["strike"] for r in rows_all]
        atm = min(strikes_sorted, key=lambda x: abs(x - spot))
        atm_idx = strikes_sorted.index(atm)
        lo = max(0, atm_idx - BINANCE_OC_STRIKE_WINDOW)
        hi = min(len(rows_all), atm_idx + BINANCE_OC_STRIKE_WINDOW + 1)
        rows_out = [
            {"strike": r["strike"], "ce": leg_from(r.get("ce_symbol")), "pe": leg_from(r.get("pe_symbol"))}
            for r in rows_all[lo:hi]
        ]
        label = datetime.datetime.utcfromtimestamp(epoch / 1000).strftime("%d %b %Y")
        chains[str(epoch)] = {"atm": atm, "rows": rows_out, "expiry_label": label}
        expiry_meta_list.append({"epoch": epoch, "label": label})

    if not chains:
        return {
            "error": "Koi expiry chain nahi ban paayi",
            "ws": _bn_ws_status_snapshot(spot_source=spot_source, spot_age=spot_age, expiries_count=len(expiries)),
            "ts": time.time(),
        }

    default_epoch = expiries[0]  # nearest expiry
    nearest = chains[str(default_epoch)]

    # ── Honest "live" status — sirf TCP connected hona kaafi nahi, agar
    # stream se koi tick 15/30 sec tak na aaya to us stream ko bhi "stale"
    # maano, chahe socket technically khula ho (jaisa spot ke saath pehle
    # fix kiya, ab mark/trade ke saath bhi wahi pattern). ──
    ws_status = _bn_ws_status_snapshot(spot_source=spot_source, spot_age=spot_age, expiries_count=len(expiries))

    return {
        "spot": spot,
        "expiries": expiry_meta_list,               # SABHI expiries — frontend dropdown ke liye
        "default_expiry_epoch": default_epoch,
        "chains": chains,                            # epoch(str) -> {atm, rows, expiry_label}
        # backward-compat top-level fields = nearest expiry (purane consumers ke liye)
        "atm": nearest["atm"],
        "rows": nearest["rows"],
        "expiry_label": nearest["expiry_label"],
        "expiry_epoch": int(default_epoch / 1000),
        "ws": ws_status,
        "ts": time.time(),
    }

@st.cache_resource
def _get_binance_oc_last_payload() -> dict:
    return {"data": None, "ts": 0.0}

@st.cache_resource
def _get_binance_oc_last_payload_lock() -> threading.Lock:
    return threading.Lock()

_BINANCE_OC_LAST_PAYLOAD = _get_binance_oc_last_payload()
_BINANCE_OC_LAST_PAYLOAD_LOCK = _get_binance_oc_last_payload_lock()

def _binance_oc_bg_loop():
    """Ab koi network call NAHI karta — sirf WS threads ke already-updated
    in-memory data se payload rebuild karta hai, har 300ms (localhost-only
    mode: side-port poll ab primary fast path hai, isliye backend refresh
    bhi tez rakha — koi REST rate-limit risk nahi, purana 1s wala interval
    is naye 300ms fast-poll ka bottleneck ban raha tha).

    FIX (disk I/O): pehle har 300ms iteration par poora payload
    BINANCE_OC_FILE mein disk-write bhi hota tha — din-raat ~3.3
    writes/sec, 24/7. Grep karne par pata chala ye file kahin bhi PADHI
    nahi jaati — /api/binance_optionchain route already seedha in-memory
    _BINANCE_OC_LAST_PAYLOAD se serve karta hai (dekho
    get_cached_binance_option_chain_payload neeche), jaisa
    _register_api_route ke comment mein bhi confirm hai ki ye on-disk
    file wala purana approach already superseded ho chuka tha. In-memory
    update (jo /api/bn_tick jaisi fast-poll routes ke liye zaroori hai)
    ab bhi har 300ms hota hai — sirf disk-write ab har ~2s par throttle
    kiya gaya hai (file ko purane kisi consumer ke liye zinda rakha hai,
    par I/O load ~85% kam)."""
    _last_disk_write_ts = 0.0
    while True:
        try:
            payload = _bn_rebuild_payload_from_memory()
        except Exception as e:
            with _BN_CHAIN_META_LOCK:
                _ec = len(_BN_CHAIN_META["expiries"])
            payload = {
                "error": f"binance bg loop exception: {e}",
                "ws": _bn_ws_status_snapshot(expiries_count=_ec),
                "ts": time.time(),
            }
        _BN_THREAD_HEARTBEAT["payload"] = time.time()
        with _BINANCE_OC_LAST_PAYLOAD_LOCK:
            _BINANCE_OC_LAST_PAYLOAD.update({"data": payload, "ts": time.time()})
        _now_disk = time.time()
        if _now_disk - _last_disk_write_ts >= 2.0:
            try:
                with open(BINANCE_OC_FILE, "w") as f:
                    json.dump(payload, f)
                _last_disk_write_ts = _now_disk
            except Exception:
                pass
        time.sleep(0.3)

def get_cached_binance_option_chain_payload() -> dict:
    with _BINANCE_OC_LAST_PAYLOAD_LOCK:
        data = _BINANCE_OC_LAST_PAYLOAD["data"]
        age  = time.time() - _BINANCE_OC_LAST_PAYLOAD["ts"]
    if data is None:
        # Background thread (BinanceOptionChainBG) ne abhi tak apna PEHLA
        # cycle bhi complete nahi kiya — ws/meta diagnostics yahan bhi jodo,
        # taaki debug popup ko pata chale ye startup delay hai (thread
        # heartbeat abhi 0 hai, expiries abhi load ho rahi hain) ya thread
        # kabhi shuru hi nahi hua / crash ho gaya (heartbeat hamesha 0 rahega).
        with _BN_CHAIN_META_LOCK:
            _ec = len(_BN_CHAIN_META["expiries"])
        return {
            "error": "Binance option chain load ho raha hai… (pehli fetch abhi baaki hai)",
            "ws": _bn_ws_status_snapshot(expiries_count=_ec),
            "ts": time.time(),
        }
    if age > 15:
        d = dict(data)
        d["stale_warning"] = f"Data {int(age)}s purana hai — background refresh check karo"
        return d
    return data


# ─── Market Depth (5-level Bid/Ask order book) — jaisa Fyers app ka
# "Market Depth" bottom-sheet dikhata hai (Qty(Orders) | Bid | Ask | (Orders)Qty,
# total buy/sell %, aur Price Stats: Open/High/Low/PrevClose/AvgPrice/Circuits/
# Volume/LTQ). Ye Option Chain API se ALAG endpoint hai — option chain sirf
# best bid/ask (1 level) deta hai, depth API 5 levels + totalbuyqty/totalsellqty
# deta hai. Har symbol ka apna chhota TTL cache — jab tak koi ek strike ka depth
# modal khula ho, sirf usi symbol ke liye poll hota hai (saare strikes ka depth
# fetch karne ki zaroorat nahi, rate-limit safe). ──────────────────────────────
_DEPTH_CACHE: dict = {}
_DEPTH_LOCK = threading.Lock()
_DEPTH_TTL  = 1.5  # seconds — depth apna alag chhota TTL, sirf active symbol ke liye

_DEPTH_DEBUG = {"last_error": "", "last_status": None, "last_branch": "", "last_symbol": "", "ts": 0.0}

# _market_depth_pusher (Streamlit fragment) ki apni run-count/state — sirf
# debug/diagnostic ke liye, taaki frontend confirm kar sake ki fragment
# zinda hai aur backend ka _active_depth_symbol kya dikh raha hai. Dekho
# _market_depth_pusher ka comment for full context.
_MD_PUSHER_DEBUG = {"runs": 0, "last_active_symbol": None, "last_run_ts": 0.0}

def fyers_get_market_depth(app_id: str, access_token: str, symbol: str) -> "dict | None":
    """Fyers Market Depth API se 5-level bid/ask order book + price stats fetch karta hai.
    Response shape match karta hai Fyers app ke 'Market Depth' screen se:
    bids/asks (5 levels each, price+volume+orders), totalbuyqty/totalsellqty,
    o/h/l/c, ltp/ltq/volume, upper_ckt/lower_ckt, atp (avg price).
    Poori tarah try/except mein wrapped hai — koi bhi unexpected exception yahin
    pakdi jaati hai taaki side-server ka do_GET crash na ho (warna browser ko
    'connection failed' milta hai, kisi asli JSON error ke bajaye)."""
    try:
        headers = {"Authorization": f"{app_id}:{access_token}"}
        params = {"symbol": symbol, "ohlcv_flag": "1"}
        resp = requests.get("https://api-t1.fyers.in/data/depth", headers=headers, params=params, timeout=6)
        last_status = resp.status_code
        try:
            r = resp.json()
        except Exception:
            err = f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": err}

        if not isinstance(r, dict) or r.get("s") != "ok":
            err = (r.get("message", str(r)) if isinstance(r, dict) else str(r))[:300]
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": f"Fyers depth API fail — {err} (HTTP {last_status})"}

        dmap = r.get("d") or {}
        if not isinstance(dmap, dict) or not dmap:
            err = "Fyers ne depth data khaali bheja (dmap empty)"
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": err}

        # Normally dmap ki key exact 'symbol' hoti hai, lekin agar Fyers thoda
        # alag casing/format bhejde to fallback: agar sirf ek hi entry hai to
        # wahi use kar lo (case-insensitive match bhi try karo).
        d = dmap.get(symbol)
        if d is None:
            for k, v in dmap.items():
                if k.upper() == symbol.upper():
                    d = v
                    break
        if d is None and len(dmap) == 1:
            d = next(iter(dmap.values()))
        if not d:
            err = f"Symbol '{symbol}' depth response mein nahi mila. Mile keys: {list(dmap.keys())[:5]}"
            _DEPTH_DEBUG.update({"last_error": err, "last_status": last_status, "ts": time.time()})
            return {"error": err}

        _DEPTH_DEBUG.update({"last_error": "", "last_status": last_status, "ts": time.time()})
        return {
            "symbol": symbol,
            "ltp": d.get("ltp", 0),
            "ch": d.get("ch", 0),
            "chp": d.get("chp", 0),
            "bids": d.get("bids", []),
            "asks": d.get("ask", []),
            "total_buy_qty":  d.get("totalbuyqty", 0),
            "total_sell_qty": d.get("totalsellqty", 0),
            "open": d.get("o", 0),
            "high": d.get("h", 0),
            "low": d.get("l", 0),
            "prev_close": d.get("c", 0),
            "atp": d.get("atp", 0),
            "upper_ckt": d.get("upper_ckt", 0),
            "lower_ckt": d.get("lower_ckt", 0),
            "volume": d.get("v", 0),
            "ltq": d.get("ltq", 0),
            "ts": time.time(),
        }
    except Exception as e:
        _DEPTH_DEBUG.update({"last_error": str(e), "last_status": None, "ts": time.time()})
        return {"error": f"Exception: {e}"}

# ─── Binance Market Depth (BTC options) — same response shape as
# fyers_get_market_depth() so _ocRenderDepth() in chart.html can render
# either asset without any frontend changes. Uses the public (unauthenticated)
# Binance European Options endpoints — /eapi/v1/depth for 5-level bid/ask,
# /eapi/v1/ticker for 24hr price stats (open/high/low/last/volume). Both are
# public data, no api_key/secret needed (unlike Fyers which requires login). ──
def binance_get_market_depth(symbol: str) -> "dict | None":
    """Binance Options Depth + 24hr ticker se Fyers jaisa shape wapas karta hai.
    symbol format: 'BTC-260808-65000-C' (Binance EAPI naming)."""
    try:
        # Binance Options depth API sirf specific limit values accept karta
        # hai (10/20/50/100/500/1000) — 5 invalid hai (confirmed via "Error
        # -4021: Invalid depth limit"). Minimum allowed (10) bhejte hain,
        # neeche _lvl() already sirf pehle 5 rows leta hai, isliye output
        # shape same rehta hai.
        ok_d, depth = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/depth", {"symbol": symbol, "limit": 10}, "")
        if not ok_d:
            _DEPTH_DEBUG.update({"last_error": str(depth), "last_status": None, "ts": time.time()})
            return {"error": f"Binance depth API fail — {depth}"}
        if not isinstance(depth, dict) or ("bids" not in depth and "asks" not in depth):
            err = f"Binance ne depth ke liye anjaan shape bheja: {str(depth)[:200]}"
            _DEPTH_DEBUG.update({"last_error": err, "last_status": None, "ts": time.time()})
            return {"error": err}

        ok_t, ticker = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/ticker", {"symbol": symbol}, "")
        trow = {}
        if ok_t:
            trow = ticker[0] if isinstance(ticker, list) and ticker else (ticker if isinstance(ticker, dict) else {})
        # ok_t fail hone par bhi depth data useful hai — sirf stats khaali dikhenge,
        # poora request fail nahi karte (depth-fetch hi original goal hai).

        def _lvl(rows):
            out = []
            for row in (rows or [])[:5]:
                try:
                    out.append({"price": float(row[0]), "volume": float(row[1]), "ord": 0})
                except Exception:
                    continue
            return out

        bids = _lvl(depth.get("bids"))
        asks = _lvl(depth.get("asks"))
        total_buy_qty  = sum(b["volume"] for b in bids)
        total_sell_qty = sum(a["volume"] for a in asks)

        last_price = float(trow.get("lastPrice", 0) or 0)
        price_change = float(trow.get("priceChange", 0) or 0)
        prev_close = last_price - price_change

        _DEPTH_DEBUG.update({"last_error": "", "last_status": None, "ts": time.time()})
        return {
            "symbol": symbol,
            "ltp": last_price,
            "ch": price_change,
            "chp": float(trow.get("priceChangePercent", 0) or 0),
            "bids": bids,
            "asks": asks,
            "total_buy_qty":  total_buy_qty,
            "total_sell_qty": total_sell_qty,
            "open": float(trow.get("open", 0) or 0),
            "high": float(trow.get("high", 0) or 0),
            "low": float(trow.get("low", 0) or 0),
            "prev_close": prev_close,
            "atp": last_price,  # Binance options 24hr ticker mein alag avg-price field nahi hai
            "upper_ckt": 0,     # crypto options mein price-circuit concept nahi hai (Fyers-specific)
            "lower_ckt": 0,
            "volume": float(trow.get("volume", 0) or 0),
            "ltq": float(trow.get("lastQty", 0) or 0),
            "ts": time.time(),
        }
    except Exception as e:
        _DEPTH_DEBUG.update({"last_error": str(e), "last_status": None, "ts": time.time()})
        return {"error": f"Exception: {e}"}

def refresh_market_depth_cache(symbol: str) -> dict:
    """TTL-cached depth fetch — symbol format se decide karta hai Fyers ya
    Binance API call karni hai, phir ek hi symbol baar-baar poll hone par
    bhi upstream ko sirf har _DEPTH_TTL second mein ek baar hit karta hai.

    DEBUG: har return path apna `_debug.branch` set karta hai taaki frontend
    (chart.html ke Order Book debug panel) ko pata chale ki backend ke andar
    EXACT kaunsi wajah se fail hua — 'cache_hit' / 'fyers_creds_missing' /
    'fyers_api_error' / 'binance_api_error' / 'unrecognized_symbol_format' /
    'exception' / 'ok'.
    """
    branch = "unknown"
    try:
        now = time.time()
        with _DEPTH_LOCK:
            entry = _DEPTH_CACHE.get(symbol)
            if entry and (now - entry["ts"]) < _DEPTH_TTL:
                cached = dict(entry["data"])
                cached["_debug"] = {**cached.get("_debug", {}), "branch": "cache_hit",
                                     "cache_age_s": round(now - entry["ts"], 2)}
                return cached

        # ── Symbol-format detection: BTC/Binance option symbols (jaise
        # 'BTC-260808-65000-C') ko Binance ke public depth API se route
        # karo; NSE/Fyers symbols ('NSE:...') ko Fyers ke depth API se —
        # dono asset classes abhi supported hain. ────────────────────────
        _looks_fyers  = symbol.upper().startswith(("NSE:", "BSE:", "MCX:"))
        _looks_binance = (not _looks_fyers) and symbol.upper().startswith("BTC-")

        if _looks_binance:
            branch = "binance_api_call"
            payload = binance_get_market_depth(symbol) or {"error": "unknown error"}
            branch = "binance_api_error" if payload.get("error") else "ok"
            payload["_debug"] = {"branch": branch, "symbol": symbol, "source": "binance"}
            with _DEPTH_LOCK:
                _DEPTH_CACHE[symbol] = {"data": payload, "ts": now}
            _DEPTH_DEBUG.update({"last_error": payload.get("error", ""), "last_branch": branch,
                                  "last_symbol": symbol, "ts": now})
            return payload

        if not _looks_fyers:
            branch = "unrecognized_symbol_format"
            payload = {
                "error": (
                    f"'{symbol}' — symbol format pehchana nahi gaya. Expected: "
                    f"'NSE:...' (Fyers/index options) ya 'BTC-...' (Binance/BTC options)."
                ),
                "_debug": {"branch": branch, "symbol": symbol},
            }
            with _DEPTH_LOCK:
                _DEPTH_CACHE[symbol] = {"data": payload, "ts": now}
            _DEPTH_DEBUG.update({"last_error": payload["error"], "last_status": None,
                                  "last_branch": branch, "last_symbol": symbol, "ts": now})
            return payload

        creds = load_creds()
        if not creds.get("access_token") or not creds.get("app_id"):
            branch = "fyers_creds_missing"
            payload = {
                "error": "Fyers login nahi mila — pehle login karo.",
                "_debug": {"branch": branch, "symbol": symbol},
            }
        else:
            payload = fyers_get_market_depth(creds["app_id"], creds["access_token"], symbol) or {"error": "unknown error"}
            branch = "fyers_api_error" if payload.get("error") else "ok"
            payload["_debug"] = {"branch": branch, "symbol": symbol,
                                  "http_status": _DEPTH_DEBUG.get("last_status")}
        with _DEPTH_LOCK:
            _DEPTH_CACHE[symbol] = {"data": payload, "ts": now}
        _DEPTH_DEBUG.update({"last_error": payload.get("error", ""), "last_branch": branch,
                              "last_symbol": symbol, "ts": now})
        return payload
    except Exception as e:
        branch = "exception"
        _DEPTH_DEBUG.update({"last_error": str(e), "last_status": None,
                              "last_branch": branch, "last_symbol": symbol, "ts": time.time()})
        return {"error": f"refresh_market_depth_cache exception: {e}",
                "_debug": {"branch": branch, "symbol": symbol}}

# ─── Next-month expiry chain — apna alag TTL cache (SV1's "This Month /
# Next Month" toggle ke liye). Current-month jitni baar refresh karne ki
# zaroorat nahi (next month kam frequently move karta hai) — isliye lamba
# TTL rakha taaki Fyers rate-limit par extra load na pade. ────────────────
_OC_NEXT_CACHE = {"data": None, "ts": 0.0}
_OC_NEXT_LOCK  = threading.Lock()
_OC_NEXT_TTL   = 30  # seconds

def _oc_next_month_chain(app_id: str, access_token: str, expiries: list) -> "dict | None":
    """Current chain ke 'expiries' list se agla (2nd) monthly expiry dhoondh
    kar uska poora CE/PE chain fetch karta hai. Fyers isi endpoint ko
    'timestamp' param (us expiry ka epoch) ke saath dobara call karke deta
    hai — koi alag endpoint nahi hai."""
    now = time.time()
    with _OC_NEXT_LOCK:
        stale = (now - _OC_NEXT_CACHE["ts"]) >= _OC_NEXT_TTL
        cached = _OC_NEXT_CACHE["data"]
    if not stale:
        return cached
    result = None
    if expiries and len(expiries) > 1:
        try:
            next_epoch = int(expiries[1].get("expiry"))
        except (TypeError, ValueError):
            next_epoch = None
        if next_epoch:
            result = fyers_get_option_chain(app_id, access_token, timestamp=str(next_epoch))
    with _OC_NEXT_LOCK:
        _OC_NEXT_CACHE.update({"data": result, "ts": now})
    return result


def refresh_option_chain_cache() -> dict:
    """TTL ke andar cache use karta hai, warna Fyers se dobara fetch karta hai,
    aur fyers_optionchain.json mein likh deta hai (chart iframe poll fallback ke liye).
    Failure hone par bhi ek 'error' field ke saath JSON likhta hai — silently
    hang nahi hota. Saath mein 'next' key mein next-month expiry ka chain bhi
    bundle karta hai taaki frontend This-Month/Next-Month switch client-side
    hi kar sake, koi extra request ki zaroorat nahi."""
    now = time.time()
    with _OC_LOCK:
        stale = (now - _OC_CACHE["ts"]) >= _OC_TTL
    if stale:
        creds = load_creds()
        if not creds.get("access_token") or not creds.get("app_id"):
            payload = {"error": "Fyers login nahi mila — creds file mein access_token/app_id missing hai."}
        else:
            # strikecount=20 — BTC (Binance) window ke barabar, taaki frontend
            # ka "Strikes" dropdown (max 20 each side) BankNifty ke liye bhi
            # bina extra rows-missing ke kaam kare.
            data = fyers_get_option_chain(creds["app_id"], creds["access_token"], strikecount=20)
            if data:
                with _OC_LOCK:
                    _OC_CACHE.update({"data": data, "ts": now})
                payload = dict(data)
                payload["next"] = _oc_next_month_chain(
                    creds["app_id"], creds["access_token"], data.get("expiries") or []
                )
            else:
                payload = {
                    "error": f"Fyers Option Chain API fail ho gayi — {_OC_DEBUG.get('last_error','unknown error')} "
                             f"(HTTP {_OC_DEBUG.get('last_status')}, URL: {_OC_DEBUG.get('last_url')})",
                }
        try:
            with open(OC_FILE, "w") as f:
                json.dump(payload, f)
        except Exception:
            pass
        return payload

    with _OC_LOCK:
        cached = _OC_CACHE["data"]
    if not cached:
        return {"error": "Cache khaali hai"}
    result = dict(cached)
    creds = load_creds()
    if creds.get("access_token") and creds.get("app_id"):
        result["next"] = _oc_next_month_chain(creds["app_id"], creds["access_token"], cached.get("expiries") or [])
    else:
        result["next"] = None
    return result


# ─── Option Chain background refresher ─────────────────────────────────────
# PEHLE: _option_chain_pusher (Streamlit @st.fragment) seedha refresh_option_
# chain_cache() ko call karta tha — jisme Fyers ko REST call jaata tha. Ye call
# session ke execution thread ko block kar deta tha, jiski wajah se _bn_tick_
# pusher (jo sirf in-memory _LIVE dict padhta hai, koi network call nahi) bhi
# rukk jaata tha — spot-price ka tick-by-tick feel isi wajah se lag khata tha.
#
# AB: ek alag background daemon thread khud apni raftaar se (har ~1s check,
# andar TTL-gated hai to asli Fyers call sirf _OC_TTL second mein ek baar hoti
# hai) option chain fetch karta rehta hai aur natije ko _OC_LAST_PAYLOAD mein
# likhta hai. _option_chain_pusher fragment ab sirf ye already-computed cache
# padhta hai — koi network I/O nahi, isliye kabhi block nahi karta. ──────────
_OC_LAST_PAYLOAD: dict = {"data": None, "ts": 0.0}
_OC_LAST_PAYLOAD_LOCK = threading.Lock()

# ── Fyers option-chain BG-loop heartbeat — Binance ke _BN_THREAD_HEARTBEAT
# jaisa hi concept, kyunki Fyers ke liye koi option-chain WebSocket nahi hai
# (ye poori tarah is background poll-loop par depend karta hai). Agar ye
# thread kisi wajah se crash/atak jaaye, isse pehle frontend ko is baat ka
# koi pata hi nahi chalta tha — payload bas purana dikhta reh jaata, aur
# debug panel Binance ke WS fields dikhata (jo Fyers ke liye applicable hi
# nahi hain). Ab har iteration apna timestamp yahan likhta hai, aur wahi
# 'diag' block ke through payload ke saath frontend tak jaata hai. ─────────
@st.cache_resource
def _get_fyers_oc_heartbeat() -> dict:
    return {"bg_loop_ts": 0.0}

_FYERS_OC_HEARTBEAT = _get_fyers_oc_heartbeat()

def _option_chain_bg_loop():
    while True:
        try:
            payload = refresh_option_chain_cache()
        except Exception as e:
            payload = {"error": f"bg loop exception: {e}"}
        now = time.time()
        _FYERS_OC_HEARTBEAT["bg_loop_ts"] = now
        # ── Diagnostics block — payload ke saath hi bundle karke bhejte
        # hain taaki frontend debug panel ko backend se WS ki tarah alag
        # se poll na karna pade. bg_loop hamesha isi tick par abhi-abhi
        # update hua hai (age ~0), isliye ye field khud staleness detect
        # karne ke liye nahi (uske liye frontend apna postMessage-arrival
        # timestamp use karta hai) — ye sirf 'thread zinda hai aur last
        # fetch attempt ka nateeja kya tha' batane ke liye hai. ──────────
        if isinstance(payload, dict):
            payload["diag"] = {
                "bg_loop_last_ts": now,
                "last_fetch_error": _OC_DEBUG.get("last_error"),
                "last_fetch_status": _OC_DEBUG.get("last_status"),
                "last_fetch_url": _OC_DEBUG.get("last_url"),
                "last_fetch_ts": _OC_DEBUG.get("ts"),
            }
        with _OC_LAST_PAYLOAD_LOCK:
            _OC_LAST_PAYLOAD.update({"data": payload, "ts": now})
        time.sleep(1)

def get_cached_option_chain_payload() -> dict:
    """Non-blocking read — background thread ye already update kar raha hai.
    Streamlit fragment/pusher isi ko call kare, kabhi refresh_option_chain_cache()
    seedha na bulaye (warna wapas blocking wapas aa jaayegi)."""
    with _OC_LAST_PAYLOAD_LOCK:
        data = _OC_LAST_PAYLOAD["data"]
        age  = time.time() - _OC_LAST_PAYLOAD["ts"]
    if data is None:
        return {"error": "Option chain load ho raha hai… (pehli fetch abhi baaki hai)"}
    if age > 15:
        # Background thread kisi wajah se ruk gaya ho to purana data dikhane
        # ke bajaye saaf bata do — silently stale data dikhana bhi galat hai.
        d = dict(data)
        d["stale_warning"] = f"Data {int(age)}s purana hai — background refresh check karo"
        return d
    return data

def _on_ws_message(msg):
    try:
        if not isinstance(msg, dict):
            # DataSocket kabhi-kabhi non-dict bhi bhejta hai (e.g. status
            # strings) — pehle ye chup-chaap ignore ho jaata tha, ab kam se
            # kam ek baar type note kar dete hain taaki "WS bilkul kuch
            # nahi bhej raha" vs "WS bhej raha hai but format samajh nahi
            # aa raha" mein farak pata chale.
            with _BN_FEED_DEBUG_LOCK:
                _BN_FEED_DEBUG["ws_last_error"] = f"non-dict message ignored: {type(msg).__name__}"
            return
        # DataSocket sends list of ticks or single tick dict
        ticks = msg if isinstance(msg, list) else [msg]
        got_ltp = False
        for tick in ticks:
            if not isinstance(tick, dict):
                continue
            ltp = tick.get("ltp") or tick.get("LTP")
            if ltp is None:
                continue
            ltp = float(ltp)
            got_ltp = True
            with _LIVE_LOCK:
                _LIVE["ltp"]        = ltp
                _LIVE["prev_close"] = float(tick.get("prev_close_price") or tick.get("prev_close") or _LIVE.get("prev_close") or ltp)
                _LIVE["ts"]         = int(time.time())
                _LIVE["source"]     = "ws"
                # Wake up any /api/bn_tick_stream SSE threads blocked in
                # _LIVE_LOCK.wait() — this is the actual "push" trigger.
                _LIVE_LOCK.notify_all()
            # Build running 1-minute candle from raw LTP ticks
            _update_candle_ltp(ltp)
            # Write bn_live.json so JS chart can poll it
            _write_live_json()
        with _BN_FEED_DEBUG_LOCK:
            if got_ltp:
                _BN_FEED_DEBUG["ws_last_message_ts"] = time.time()
            else:
                # message aaya, but usme koi tick mein 'ltp'/'LTP' key hi
                # nahi mili — ye batayega ki Fyers ne format badal diya
                # ya symbol subscribe hi galat hua.
                _BN_FEED_DEBUG["ws_last_error"] = f"message had no ltp field: {str(msg)[:200]}"
    except Exception as e:
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_last_error"] = f"{type(e).__name__}: {e}"
        _slog_exception("_on_ws_message", e)

def _on_ws_error(msg):
    with _BN_FEED_DEBUG_LOCK:
        _BN_FEED_DEBUG["ws_connected"] = False
        _BN_FEED_DEBUG["ws_last_error"] = str(msg)
    _slog(f"BankNifty WS error: {msg}", level="err")

def _on_ws_close(msg):
    with _BN_FEED_DEBUG_LOCK:
        _BN_FEED_DEBUG["ws_connected"] = False
        _BN_FEED_DEBUG["ws_last_close"] = str(msg)
    _slog(f"BankNifty WS closed: {msg}", level="warn")

def _on_ws_connect():
    try:
        fyers_ws.subscribe(
            symbols=["NSE:NIFTYBANK-INDEX"],
            data_type="SymbolUpdate",
        )
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_connected"]       = True
            _BN_FEED_DEBUG["ws_last_connect_ts"] = time.time()
            _BN_FEED_DEBUG["ws_last_error"]      = None
        _slog("BankNifty WS connected + subscribed to NSE:NIFTYBANK-INDEX", level="ok")
        fyers_ws.keep_running()
    except Exception as e:
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_connected"]  = False
            _BN_FEED_DEBUG["ws_last_error"] = f"{type(e).__name__}: {e}"
        _slog_exception("_on_ws_connect (subscribe/keep_running)", e)

def _get_live_payload():
    """Build the latest live-tick payload straight from in-memory state — no
    disk I/O, so this is as fresh as the WS thread's last update. ts is a
    float (sub-second precision) so multiple ticks arriving within the same
    wall-clock second don't collapse into one (previously ts was int(time.time())
    which made the JS-side dedupe drop intra-second ticks)."""
    with _LIVE_LOCK:
        snap = dict(_LIVE)
    if snap["ltp"] is None:
        return None
    ltp = snap["ltp"]
    now        = time.time()
    minute_epoch = int(now // 60) * 60
    with _CANDLE_LOCK:
        if _CANDLE["minute"] == minute_epoch and _CANDLE["open"] is not None:
            o = _CANDLE["open"]
            h = _CANDLE["high"]
            l = _CANDLE["low"]
        else:
            o = h = l = ltp
    # FIX: pehle koi indication nahi tha ki ye ltp Fyers WebSocket push se
    # aayi hai ya 1s REST-poll fallback se — user ke liye "Live" claim
    # verify karna namumkin tha. Ab source + age explicitly bhejte hain,
    # jaisa Binance/BTC side pehle se karta hai (spot_source/spot_age_sec).
    tick_age = (now - snap["ts"]) if snap["ts"] else None
    with _BN_FEED_DEBUG_LOCK:
        feed_debug = dict(_BN_FEED_DEBUG)
    return {
        "ts":     now,
        "ltp":    ltp,
        "source": snap.get("source"),      # "ws" | "rest" | None
        "age_sec": round(tick_age, 1) if tick_age is not None else None,
        "candle": {
            "time":  minute_epoch,
            "open":  o,
            "high":  h,
            "low":   l,
            "close": ltp,
        },
        # WS/REST health — dekho ab har failure-path _slog() ke through
        # startup-debug-log (🐞 icon) mein bhi likha jaata hai, ye yahan
        # sirf JSON-consumers (chart.html future use) ke liye hai.
        "feed_debug": feed_debug,
    }

def _write_live_json():
    payload = _get_live_payload()
    if payload is None:
        return
    try:
        # bn_live.json = fallback file (Streamlit file server se nahi milti)
        with open(BN_LIVE_FILE, "w") as f:
            json.dump(payload, f)
        # postMessage store — Streamlit injector yahan se padh ke iframe ko bhejta hai
        with _LAST_TICK_LOCK:
            _LAST_TICK_JS["json"] = json.dumps(payload)
    except Exception:
        pass

def _start_ws():
    global _ws_thread_started, fyers_ws
    if _ws_thread_started:
        return
    creds = load_creds()
    if not creds.get("access_token"):
        _slog("BankNifty WS start skip kiya: access_token missing hai creds mein.", level="warn")
        return
    try:
        from fyers_apiv3.FyersWebsocket import data_ws as fw
        access_token = f"{creds['app_id']}:{creds['access_token']}"
        fyers_ws = fw.FyersDataSocket(
            access_token=access_token,
            log_path="",
            litemode=True,
            write_to_file=False,
            reconnect=True,
            on_connect=_on_ws_connect,
            on_close=_on_ws_close,
            on_error=_on_ws_error,
            on_message=_on_ws_message,
        )
        t = threading.Thread(target=fyers_ws.connect, name="FyersWS", daemon=True)
        t.start()
        _ws_thread_started = True
        _slog("BankNifty WS thread (FyersWS) start ho gaya.", level="info")
    except Exception as e:
        with _BN_FEED_DEBUG_LOCK:
            _BN_FEED_DEBUG["ws_last_error"] = f"start failed: {type(e).__name__}: {e}"
        _slog_exception("_start_ws (thread launch)", e)

# ─── Background REST poller (fallback: polls Fyers 1m candles every 3s) ──────
# Har failure-path ab _BN_FEED_DEBUG mein record hoti hai aur err-level
# _slog line ke through startup-debug-log (🐞 icon) mein bhi dikhti hai —
# PEHLE ye saara `except: pass` mein chup jaata tha, isliye jab WS *aur*
# REST dono ek saath fail hote the, tick 82s+ stale ho jaata tha aur
# koi wajah kahin nahi milti thi.
FYERS_REST_BACKOFF_BASE = 1     # sec — WS stale hote hi pehla REST poll
FYERS_REST_BACKOFF_MAX  = 10    # sec — lagaataar failures pe is se zyada kabhi nahi rukna
FYERS_REST_IDLE_SLEEP   = 2     # sec — jab WS fresh hai, sirf timestamp check karna hai,
                                  # isliye har 1s ke bajaye har 2s check (halka, koi
                                  # behavior farak nahi — WS fresh hone ka threshold (8s)
                                  # ke saamne 2s ka poll-check bilkul kaafi hai)

def _fyers_rest_backoff_sleep(fail_count: int) -> None:
    # _bn_ws_backoff_sleep jaisa hi pattern (consistency) — lagaataar REST
    # failures (Fyers rate-limit / network down) par turant-turant retry
    # karne ke bajaye exponentially peeche hatna, taaki outage ke dauraan
    # Fyers API par extra load na pade aur rate-limit aur bhi na bigde.
    delay = min(FYERS_REST_BACKOFF_BASE * (2 ** max(0, fail_count - 1)), FYERS_REST_BACKOFF_MAX)
    jitter = _bn_random.uniform(0, delay * 0.3)
    time.sleep(delay + jitter)

def _rest_live_loop():
    _rest_fail_count = 0
    while True:
        # WebSocket se fresh data aa raha hai to REST call skip karo
        with _LIVE_LOCK:
            ws_fresh = (time.time() - _LIVE["ts"]) < 8
        if ws_fresh:
            _rest_fail_count = 0   # WS theek hai, agli baar stale hone par fresh backoff se shuru
            time.sleep(FYERS_REST_IDLE_SLEEP)
            continue
        if not ws_fresh:
            creds = load_creds()
            if not creds.get("access_token"):
                with _BN_FEED_DEBUG_LOCK:
                    _BN_FEED_DEBUG["rest_last_error"] = "access_token missing in creds"
                _rest_fail_count += 1
            else:
                today = _ist_now().strftime("%Y-%m-%d")
                headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
                params = {
                    "symbol": "NSE:NIFTYBANK-INDEX", "resolution": "1",
                    "date_format": "1", "range_from": today, "range_to": today, "cont_flag": "1",
                }
                with _BN_FEED_DEBUG_LOCK:
                    _BN_FEED_DEBUG["rest_last_attempt_ts"] = time.time()
                try:
                    res = requests.get(
                        "https://api-t1.fyers.in/data/history",
                        headers=headers, params=params, timeout=6,
                    ).json()
                    if res.get("s") == "ok":
                        candles = res.get("candles", [])
                        if candles:
                            last = candles[-1]
                            bar_epoch = int(last[0])
                            o, h, l, c = float(last[1]), float(last[2]), float(last[3]), float(last[4])
                            with _LIVE_LOCK:
                                if time.time() - _LIVE["ts"] > 5:
                                    _LIVE["ltp"]    = c
                                    _LIVE["ts"]     = int(time.time())
                                    _LIVE["source"] = "rest"
                                    # Same push trigger as the WS path above —
                                    # REST-fallback ticks should reach the
                                    # browser just as fast as WS ticks do.
                                    _LIVE_LOCK.notify_all()
                            _set_candle_from_bar(bar_epoch, o, h, l, c)
                            _write_live_json()
                            with _BN_FEED_DEBUG_LOCK:
                                _BN_FEED_DEBUG["rest_last_success_ts"] = time.time()
                                _BN_FEED_DEBUG["rest_last_error"]      = None
                            _rest_fail_count = 0
                        else:
                            with _BN_FEED_DEBUG_LOCK:
                                _BN_FEED_DEBUG["rest_last_error"] = "response ok but candles list empty"
                            _rest_fail_count += 1
                    else:
                        # Fyers ne khud error diya (invalid token, rate
                        # limit, market closed symbol, etc) — poora
                        # response record karo taaki exact reason pata chale.
                        with _BN_FEED_DEBUG_LOCK:
                            _BN_FEED_DEBUG["rest_last_error"] = f"Fyers API error: {str(res)[:300]}"
                        _slog(f"BankNifty REST fallback: Fyers ne error diya: {str(res)[:300]}", level="err")
                        _rest_fail_count += 1
                except Exception as e:
                    with _BN_FEED_DEBUG_LOCK:
                        _BN_FEED_DEBUG["rest_last_error"] = f"{type(e).__name__}: {e}"
                    _slog_exception("_rest_live_loop (REST fallback call)", e)
                    _rest_fail_count += 1
        if _rest_fail_count > 0:
            _fyers_rest_backoff_sleep(_rest_fail_count)
        else:
            # WS abhi-abhi stale hua, ya token missing tha — turant fresh
            # attempt (backoff sirf lagaataar failures par hi lagta hai).
            time.sleep(FYERS_REST_BACKOFF_BASE)

def _ensure_fyers_threads():
    """Sirf Fyers se related background threads (BankNifty REST poll, token
    monitor, Fyers option-chain, Fyers WS). Binance ka koi thread yahan
    start nahi hota — 'Fyers Entry' mode isse hi call karta hai."""
    names = {t.name for t in threading.enumerate()}
    if "FyersRESTPoller" not in names:
        threading.Thread(target=_rest_live_loop, name="FyersRESTPoller", daemon=True).start()
    if "FyersTokenMonitor" not in names:
        threading.Thread(target=_token_monitor_loop, name="FyersTokenMonitor", daemon=True).start()
    if "OptionChainBG" not in names:
        threading.Thread(target=_option_chain_bg_loop, name="OptionChainBG", daemon=True).start()
    _start_ws()
    _register_api_route()   # shared side-server (bn_history/fyers_optionchain), idempotent

def _ensure_binance_threads():
    """Sirf Binance se related background threads (option-chain BG + saare
    Binance WS loops). Fyers ka koi thread yahan start nahi hota —
    'Binance Entry' mode isse hi call karta hai."""
    names = {t.name for t in threading.enumerate()}
    if "BinanceOptionChainBG" not in names:
        threading.Thread(target=_binance_oc_bg_loop, name="BinanceOptionChainBG", daemon=True).start()
    _ensure_binance_ws_threads()
    _register_api_route()   # shared side-server (binance_optionchain/market_depth), idempotent

# ─── Tornado /api/bn_history handler — lazy historical data endpoint ──────────
# Streamlit internally uses Tornado. We inject our own route so chart.html's
# infinite-scroll loader can fetch older BN candles on demand without a page reload.

_HIST_ENDPOINT_REGISTERED = False
_HIST_ENDPOINT_LOCK = threading.Lock()
_API_PORT_RANGE = range(8502, 8511)   # candidate side-ports, tried in order

# ── /api/bn_tick_stream (SSE push) tuning — see route handler in
# _register_api_route() below for how these are used. ──
_SSE_HEARTBEAT_SECONDS = 15   # wake cadence when no tick arrives, to detect dead sockets
_SSE_MAX_CONN_SECONDS  = 6 * 3600   # hard cap per connection; client auto-reconnects well before this
_API_PORT_STATE = {
    "chosen_port": None, "attempts": [], "bound_ts": None, "all_ports_busy": False,
}
_API_PORT_STATE_LOCK = threading.Lock()

# In-memory cache per (resolution, from_date, to_date) — avoids repeat Fyers calls
_HIST_CACHE: dict = {}
_HIST_CACHE_TTL = 300  # 5 min


def _hist_cache_key(resolution: str, from_date: str, to_date: str) -> str:
    return f"{resolution}|{from_date}|{to_date}"


def _bn_history_handler_data(resolution: str, from_date: str, to_date: str) -> dict:
    """Fetch BN history (with in-memory cache). Returns {candles, cached, error}."""
    key = _hist_cache_key(resolution, from_date, to_date)
    now = time.time()
    if key in _HIST_CACHE:
        entry = _HIST_CACHE[key]
        if now - entry["ts"] < _HIST_CACHE_TTL:
            return {"candles": entry["data"], "cached": True}
    candles = _fyers_history(resolution, from_date, to_date)
    if candles is None:
        candles = []
    converted = []
    for c in candles:
        try:
            converted.append({
                "time":   int(c[0]) // 1000,
                "open":   round(float(c[1]), 2),
                "high":   round(float(c[2]), 2),
                "low":    round(float(c[3]), 2),
                "close":  round(float(c[4]), 2),
                "volume": round(float(c[5]), 2) if len(c) > 5 else 0,
            })
        except Exception:
            continue
    _HIST_CACHE[key] = {"ts": now, "data": converted}
    return {"candles": converted, "cached": False}


# ─── Observability: consolidated health snapshot (roadmap item 6) ──────────
# Ye koi naya monitoring system nahi hai — jo heartbeat/state dicts pehle se
# har thread khud maintain kar raha tha (_BN_THREAD_HEARTBEAT, _BN_WS_STATE,
# _FYERS_OC_HEARTBEAT, _BN_FEED_DEBUG, _FINNHUB_WS_STATE, _API_PORT_STATE),
# unhe ek jagah collect karke ek single machine-readable snapshot banata hai
# — taaki "app zinda hai ya nahi" dekhne ke liye alag-alag debug popups mein
# jaana na pade, aur external uptime-monitor (UptimeRobot jaisa) bhi isi ek
# URL ko poll kar sake. PURELY ADDITIVE — koi existing dict/thread/logic
# touch nahi hui, sirf padhta hai.
_HEALTH_STALE_THRESHOLDS = {
    # component -> (heartbeat/ts key ka max acceptable age, seconds mein)
    "bn_meta_loop":    120,   # 30s check-interval, TTL 600s — 2 min tak stale chalega to sahi hai
    "bn_ticker_loop":  30,
    "bn_payload_loop": 10,
    "fyers_oc_loop":   30,
    "fyers_live_tick": 60,    # _LIVE["ts"] — WS ya REST fallback, dono se aata hai
}

def _health_component_status(name: str, last_ts) -> dict:
    now = time.time()
    if not last_ts:
        return {"ok": False, "age_sec": None, "detail": "never ran / no data yet"}
    age = now - float(last_ts)
    threshold = _HEALTH_STALE_THRESHOLDS.get(name, 60)
    return {"ok": age < threshold, "age_sec": round(age, 1), "threshold_sec": threshold}

def _build_health_snapshot() -> dict:
    now = time.time()

    with _LIVE_LOCK:
        fyers_live_ts = _LIVE.get("ts")
        fyers_live_source = _LIVE.get("source")
    with _BN_FEED_DEBUG_LOCK:
        fyers_ws_connected = _BN_FEED_DEBUG.get("ws_connected")
        fyers_rest_last_error = _BN_FEED_DEBUG.get("rest_last_error")

    with _BN_SPOT_LOCK:
        bn_spot_ts = _BN_SPOT_PRICE.get("ts")
        bn_spot_source = _BN_SPOT_PRICE.get("source")

    with _API_PORT_STATE_LOCK:
        api_port_state = dict(_API_PORT_STATE)

    components = {
        "fyers_live_tick": _health_component_status("fyers_live_tick", fyers_live_ts),
        "bn_meta_loop":    _health_component_status("bn_meta_loop", _BN_THREAD_HEARTBEAT.get("meta")),
        "bn_ticker_loop":  _health_component_status("bn_ticker_loop", _BN_THREAD_HEARTBEAT.get("ticker")),
        "bn_payload_loop": _health_component_status("bn_payload_loop", _BN_THREAD_HEARTBEAT.get("payload")),
        "fyers_oc_loop":   _health_component_status("fyers_oc_loop", _FYERS_OC_HEARTBEAT.get("bg_loop_ts")),
    }

    detail = {
        "ts": now,
        "components": components,
        "fyers": {
            "ws_connected": fyers_ws_connected,
            "live_source": fyers_live_source,       # "ws" ya "rest" — kis se aakhri tick aayi
            "rest_last_error": fyers_rest_last_error,
        },
        "binance": {
            "ws_mark_connected":  _BN_WS_STATE.get("mark_connected"),
            "ws_trade_connected": _BN_WS_STATE.get("trade_connected"),
            "ws_spot_connected":  _BN_WS_STATE.get("spot_connected"),
            "ws_last_error":      _BN_WS_STATE.get("last_error"),
            "spot_source":        bn_spot_source,
            "spot_age_sec":       round(now - bn_spot_ts, 1) if bn_spot_ts else None,
        },
        "finnhub": dict(_FINNHUB_WS_STATE),
        "api_side_server": api_port_state,
        "threads": {
            "count": threading.active_count(),
            "names": sorted(t.name for t in threading.enumerate()),
        },
    }
    overall_ok = all(c["ok"] for c in components.values())
    detail["overall"] = "ok" if overall_ok else "degraded"
    return detail


def _register_api_route():
    """Start a lightweight HTTP server on _API_PORT for /api/bn_history.

    Streamlit runs on port 8501 by default but its internal Tornado server
    is hard to hook into reliably across versions.  Instead we spin up our
    own plain HTTP server on a dedicated side-port (8502) inside the same
    Python process.  chart.html auto-detects the port at runtime.
    """
    global _HIST_ENDPOINT_REGISTERED
    with _HIST_ENDPOINT_LOCK:
        if _HIST_ENDPOINT_REGISTERED:
            return
        _HIST_ENDPOINT_REGISTERED = True   # set before thread starts — idempotent

    def _server_loop():
        import http.server, urllib.parse as _up

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass  # suppress stdout noise

            def do_OPTIONS(self):
                # CORS preflight — browser JS local_state_save POST karta hai
                # with Content-Type: application/json, jo "non-simple" request
                # hai, isliye pehle ek OPTIONS preflight bhejta hai. Isko 204
                # + zaroori CORS headers ke saath jawab dena zaroori hai, warna
                # asli POST kabhi jaata hi nahi (browser hi block kar deta hai).
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                parsed = _up.urlparse(self.path)

                # ── Local persistent-storage SAVE — chart drawings/Future
                # Line/zoom/settings/layout, ab Supabase ki jagah HF Space ke
                # /data (bucket-mounted persistent volume) par jaate hain. ──
                if parsed.path == "/api/local_state_save":
                    try:
                        length = int(self.headers.get("Content-Length", "0") or "0")
                        raw = self.rfile.read(length) if length > 0 else b"{}"
                        payload = json.loads(raw.decode("utf-8")) if raw else {}
                        kind = payload.get("kind", "")
                        data = payload.get("data")
                        ok, msg = _local_state_save(kind, data)
                        body = json.dumps({"ok": ok, "message": msg}).encode()
                        self.send_response(200 if ok else 400)
                    except Exception as e:
                        _slog_exception("/api/local_state_save", e)
                        body = json.dumps({"ok": False, "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.end_headers()

            def do_GET(self):
                parsed = _up.urlparse(self.path)

                # ── Health check (roadmap item 6 — observability). Ek jagah
                # se poora system-status: WS connections, thread heartbeats,
                # side-server port state. 200 = sab theek, 503 = kuch stale/
                # dead — external uptime-monitor ya manual check dono ke
                # liye. Purana kisi data ko touch nahi karta, sirf padhta hai. ──
                if parsed.path == "/api/health":
                    try:
                        snap = _build_health_snapshot()
                        body = json.dumps(snap, indent=None, default=str).encode()
                        self.send_response(200 if snap.get("overall") == "ok" else 503)
                    except Exception as e:
                        body = json.dumps({"overall": "error", "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── Local persistent-storage LOAD — state+settings+layout
                # ek hi call mein disk se (see do_POST comment above). ──
                if parsed.path == "/api/local_state_load":
                    try:
                        bundle = _local_state_load_all()
                        body = json.dumps(bundle).encode()
                        self.send_response(200)
                    except Exception as e:
                        _slog_exception("/api/local_state_load", e)
                        body = json.dumps({"error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── Fast tick endpoint — chart.html polls this directly every
                # ~300ms instead of waiting on Streamlit's 1s fragment rerun +
                # postMessage relay (which was the main source of 2-3s lag). ──
                if parsed.path == "/api/bn_tick":
                    payload = _get_live_payload()
                    body = json.dumps(payload if payload is not None else {}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── Real push endpoint (Server-Sent Events) — replaces the
                # browser having to poll /api/bn_tick every 300ms with one
                # long-lived connection per client that the server writes to
                # the instant a new tick actually arrives (via
                # _LIVE_LOCK.notify_all() in the WS/REST handlers above),
                # not on a fixed timer. /api/bn_tick itself is left
                # completely untouched above as the fallback path for any
                # browser/proxy that can't hold a long-lived connection
                # (chart.html falls back to it automatically on SSE error).
                # ThreadingTCPServer already gives every connection its own
                # thread, so one long-lived SSE thread per open chart tab is
                # the same cost model as this server already has for any
                # other concurrent request — nothing new architecturally.
                if parsed.path == "/api/bn_tick_stream":
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "keep-alive")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    last_sent_ts = None
                    # Safety cap so a client that never sends TCP FIN (some
                    # mobile browsers backgrounding a tab) doesn't pin a
                    # thread open forever — chart.html reconnects
                    # automatically (native EventSource behaviour) well
                    # before this, so it's invisible in normal use.
                    conn_deadline = time.time() + _SSE_MAX_CONN_SECONDS
                    try:
                        while time.time() < conn_deadline:
                            with _LIVE_LOCK:
                                # Blocks here — releases the lock while
                                # waiting — until notify_all() fires from a
                                # new WS/REST tick, or the timeout elapses
                                # (timeout is just a heartbeat cadence to
                                # detect dead sockets, not a poll interval).
                                _LIVE_LOCK.wait(timeout=_SSE_HEARTBEAT_SECONDS)
                                current_ts = _LIVE.get("ts")
                            if current_ts != last_sent_ts:
                                last_sent_ts = current_ts
                                payload = _get_live_payload()
                                if payload is not None:
                                    chunk = ("data: " + json.dumps(payload) + "\n\n").encode()
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                    continue
                            # No new tick since last heartbeat window — send
                            # an SSE comment line (ignored by EventSource)
                            # purely to detect a dead socket early via the
                            # write exception, without spamming real data.
                            self.wfile.write(b": hb\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass  # client navigated away / tab closed — normal
                    except Exception as e:
                        _slog_exception("/api/bn_tick_stream", e)
                    return

                # ── Market Depth (5-level order book) — chart.html isi endpoint ko
                # poll karta hai jab koi strike ka depth-icon tap hota hai. Sirf
                # active/open symbol ke liye poll hota hai (background mein nahi),
                # isliye TTL-cache ke bawajood rate-limit par extra load nahi padta. ──
                if parsed.path == "/api/market_depth":
                    dqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    dsymbol = dqs.get("symbol", [""])[0]
                    try:
                        if not dsymbol:
                            body = json.dumps({"error": "symbol missing",
                                                "_debug": {"branch": "no_symbol_in_request"}}).encode()
                            self.send_response(400)
                        else:
                            dpayload = refresh_market_depth_cache(dsymbol)
                            body = json.dumps(dpayload).encode()
                            self.send_response(200)
                    except Exception as e:
                        # Ye wahi jagah hai jo pehle frontend ko generic
                        # "Connection failed" dikhwati thi agar iska response
                        # kabhi malformed/hang ho jaata — ab _debug.branch se
                        # exact pata chalega ki server-side crash hua tha.
                        import traceback
                        body = json.dumps({
                            "error": f"server exception: {e}",
                            "_debug": {"branch": "http_handler_exception",
                                       "symbol": dsymbol,
                                       "trace": traceback.format_exc()[-500:]},
                        }).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── Option chain payloads — served directly from the same
                # in-memory cache the background loops already maintain.
                # Earlier chart.html tried to fetch the on-disk
                # binance_optionchain.json / fyers_optionchain.json files by
                # relative path; that never resolved reliably from inside the
                # Streamlit component iframe (no route backed it), which is
                # why the option chain looked "live" one poll and "polling
                # failed" the next. These routes fix that at the source. ──
                if parsed.path == "/api/binance_optionchain":
                    try:
                        payload = get_cached_binance_option_chain_payload()
                        body = json.dumps(payload).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path == "/api/fyers_optionchain":
                    try:
                        payload = get_cached_option_chain_payload()
                        body = json.dumps(payload).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── SV3 "Long Term Replay" bulk endpoint — saare available
                # stocks ka 3M/9M/27M/81M/243M ek saath (1D nahi, size ke
                # karan) taaki chart.html background mein ek baar fetch karke
                # stock-switch ko instant bana sake, koi Streamlit rerun
                # nahi. Fyers login ki zaroorat nahi (sirf already-saved .gz
                # file padhta hai). ─────────────────────────────────────────
                if parsed.path == "/api/sv3_all_stocks":
                    try:
                        payload = _build_sv3_bulk_all_stocks()
                        body = json.dumps(payload, separators=(",", ":")).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── SV3 symbol-SEARCH list — sirf naam (koi price data
                # nahi), LAZY: chart.html isko tabhi maangta hai jab user
                # khud SV3 symbol-search picker kholta hai (_openSv3SymbolPicker),
                # page-load par nahi. _fetch_nifty500_symbols() khud 1hr
                # cached hai (Supabase '_index.json' se), isliye baar-baar
                # search kholne par bhi dobara network-fetch nahi hoti. ──
                if parsed.path == "/api/sv3_symbol_list":
                    try:
                        body = json.dumps(_sv3_symbol_list(), separators=(",", ":")).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── SV3 last-symbol save (fire-and-forget) — jab instant
                # bulk-switch se symbol badalta hai (koi Streamlit rerun
                # nahi hota us waqt), ye chhota GET call disk par
                # "pichla symbol" turant save kar deta hai — full page
                # reload ke bina bhi. ────────────────────────────────────
                if parsed.path == "/api/sv3_save_last_symbol":
                    sqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    ssym = sqs.get("symbol", [""])[0]
                    try:
                        if ssym:
                            save_sv3_last_symbol(ssym)
                        body = json.dumps({"ok": True}).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"ok": False, "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── v3 — Replay symbols (btcusdt2/3/4/5) ON-DEMAND fetch.
                # chart.html isko call karta hai jab user in 4 mein se
                # koi symbol pehli baar select kare (page-load par ab
                # inka candle-data pehle se load nahi hota — sirf halka
                # metadata, dekho get_replay_symbols_metadata()). Yahan
                # hamesha REPLAY_SUPABASE_URL/REPLAY_SUPABASE_ANON_KEY
                # (2nd, alag Supabase "test" project) use hota hai — ye
                # SUPABASE_PROJECT_URL/SUPABASE_SERVICE_KEY (main project,
                # BTC/BN/layouts ke liye) se BILKUL ALAG hai, dono kabhi
                # mix nahi karne — replay symbols ka master-file aur
                # reveal_state table sirf isi 2nd project mein hain. ──
                if parsed.path == "/api/replay_symbol":
                    rqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    rkey = rqs.get("key", [""])[0]
                    try:
                        _rentry = next((e for e in _replay.REPLAY_SYMBOL_REGISTRY if e["key"] == rkey), None)
                        if not _rentry:
                            body = json.dumps({"error": f"unknown replay key: {rkey}"}).encode()
                            self.send_response(400)
                        elif not (REPLAY_SUPABASE_URL and REPLAY_SUPABASE_ANON_KEY):
                            body = json.dumps({"error": "REPLAY_SUPABASE_URL/REPLAY_SUPABASE_ANON_KEY not configured"}).encode()
                            self.send_response(500)
                        else:
                            _rresult = _replay.replay_get_bucketed(
                                rkey, REPLAY_SUPABASE_URL, REPLAY_SUPABASE_ANON_KEY, log_fn=_slog
                            )
                            body = json.dumps({
                                "key": rkey,
                                "label": _rentry["label"],
                                "bucket_min": 480,
                                "market": None,
                                "candles": _rresult["candles"],
                                "revealed_up_to_t": _rresult["revealed_up_to_t"],  # FIX: countdown-timer ke liye
                            }, separators=(",", ":")).encode()
                            self.send_response(200)
                    except Exception as e:
                        _slog_exception(f"/api/replay_symbol({rkey})", e)
                        body = json.dumps({"error": f"server exception: {e}"}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # ── v3 — Replay symbols: "abhi kaunsa symbol screen par
                # active hai" batata hai (fire-and-forget, jaisa
                # /api/sv3_save_last_symbol). Isse _live_data_pusher ko
                # pata chalta hai ki background 30s-poll mein SIRF isi
                # symbol ka live-push bhejna hai, baaki 3 ka nahi (jo load
                # hi nahi hain unka push bhejne ka koi fayda nahi). ──────
                if parsed.path == "/api/replay_set_active":
                    aqs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    akey = aqs.get("key", [""])[0]
                    try:
                        _replay.replay_set_active_key(akey)
                        body = json.dumps({"ok": True, "active_key": _replay.replay_get_active_key()}).encode()
                        self.send_response(200)
                    except Exception as e:
                        body = json.dumps({"ok": False, "error": str(e)}).encode()
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path != "/api/bn_history":
                    self.send_response(404)
                    self.end_headers()
                    return

                qs = _up.parse_qs(parsed.query, keep_blank_values=False)
                def _q(k, d=""): return qs.get(k, [d])[0]

                resolution = _q("resolution", "1")
                from_date  = _q("from", "")
                to_date    = _q("to", "")
                days_str   = _q("days", "10")

                if not from_date:
                    try:
                        days = int(days_str)
                    except ValueError:
                        days = 10
                    today_ist = _ist_now()
                    to_date   = today_ist.strftime("%Y-%m-%d")
                    from_date = (today_ist - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

                creds = load_creds()
                if not creds.get("access_token"):
                    body = b'{"error":"not_authenticated"}'
                    self.send_response(401)
                else:
                    result = _bn_history_handler_data(resolution, from_date, to_date)
                    body   = json.dumps(result).encode()
                    self.send_response(200)

                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # Try ports 8502..8510 — pick whichever is free. FIX ("port
        # dikkat" — original user complaint): pehle ye purely silent tha —
        # agar saare ports busy milte, loop khaali khatam ho jaata, server
        # kabhi start hi nahi hota, aur is process-lifetime mein koi retry
        # ya error kahin nahi dikhta tha. Ab: har attempt (success/fail)
        # _API_PORT_STATE mein record hota hai (UI/debug se dekha ja sakta
        # hai), chosen port startup_log mein bhi likha jaata hai, aur agar
        # saare 9 ports busy nikle to _slog error se saaf pata chalega
        # (chahe khud port free hone ka koi automatic retry abhi bhi nahi
        # hai — us case mein Space ko restart karna padega, jaisa pehle).
        import socketserver
        bound = False
        for port in _API_PORT_RANGE:
            try:
                srv = socketserver.ThreadingTCPServer(("0.0.0.0", port), _Handler)
                srv.daemon_threads = True
                with _API_PORT_STATE_LOCK:
                    _API_PORT_STATE["chosen_port"] = port
                    _API_PORT_STATE["bound_ts"] = time.time()
                    _API_PORT_STATE["attempts"].append({"port": port, "ok": True})
                # Write chosen port to a file so chart.html JS can read it via Streamlit component
                try:
                    with open(".api_port", "w") as _f:
                        _f.write(str(port))
                except Exception:
                    pass
                _slog(f"BNHistoryAPI side-server bound on port {port}", level="info")
                bound = True
                srv.serve_forever()
                break
            except OSError as e:
                with _API_PORT_STATE_LOCK:
                    _API_PORT_STATE["attempts"].append({"port": port, "ok": False, "error": str(e)})
                continue   # port in use, try next
        if not bound:
            with _API_PORT_STATE_LOCK:
                _API_PORT_STATE["all_ports_busy"] = True
            _slog(
                f"BNHistoryAPI: saare candidate ports "
                f"({_API_PORT_RANGE.start}-{_API_PORT_RANGE.stop - 1}) busy mile — "
                f"side-server start NAHI ho paaya. Space restart se theek hoga.",
                level="err",
            )

    threading.Thread(target=_server_loop, name="BNHistoryAPI", daemon=True).start()
