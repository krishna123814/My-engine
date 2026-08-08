"""
Binance Option Chain — WebSocket-first architecture
=====================================================
Spot price      : WebSocket (btcusdt@trade) — real-time
Option chain    : WebSocket (@optionTicker per expiry) — real-time bid/ask/mark/iv/delta
Chain metadata  : REST once + every 10 min (symbols/strikes/expiries)
Order book REST : REST on-demand (initial snapshot on strike click)
Order book live : Browser-side JS WebSocket (no Streamlit rerun needed → no blink)
Chain live      : Browser-side JS polling a local snapshot HTTP server (no Streamlit
                  rerun needed → no iframe reload → no blink)

Architecture note
-----------------
The iframe (chart.html) is embedded ONCE — either on first load or when the
user changes the selected expiry. It is NOT re-embedded on a timer anymore.
Instead, a lightweight local HTTP server (started in a background thread)
serves the current snapshot as JSON at /snapshot?expiry=XXXXXX. The browser
JS inside the iframe polls this endpoint on its own interval and patches the
DOM in place — the iframe document itself never reloads, so there's no blink.

This mirrors the order-book pattern: initial data comes in once, then the
browser talks directly to a live source on its own, independent of Streamlit
reruns.

Caveat: this local HTTP server is only reachable if the extra port is
network-reachable from the browser (fine for local/dev; on some hosted
platforms that only expose a single port, e.g. Streamlit Community Cloud,
this port won't be reachable — the JS falls back to showing the last known
snapshot and reports "live polling unavailable" in the debug panel).
"""

import time
import hmac
import hashlib
import json
import threading
import traceback
import http.server
import socketserver
import urllib.parse
from pathlib import Path
from datetime import datetime

import requests
import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import urlencode

try:
    import websocket  # pip install websocket-client
    WEBSOCKET_LIB_OK = True
except ImportError:
    websocket = None
    WEBSOCKET_LIB_OK = False

BASE_URL   = "https://api.binance.com"
EAPI_URL   = "https://eapi.binance.com"
UNDERLYING = "BTCUSDT"
DEFAULT_REFRESH_SEC = 3
SNAPSHOT_HTTP_PORT  = 8765   # local JSON polling server for live chain updates

# ─────────────────────────────────────────────────────────────────────
# Persistent stores  (survive Streamlit reruns — process-wide singletons)
# ─────────────────────────────────────────────────────────────────────
@st.cache_resource
def _get_live_quotes_store():   return {}

@st.cache_resource
def _get_ws_states():
    return {
        "spot":   {"running": False, "connected": False, "last_error": None,
                   "last_message_ts": 0, "connect_count": 0, "error_count": 0},
        "mark":   {"running": False, "connected": False, "last_error": None,
                   "last_message_ts": 0, "connect_count": 0, "error_count": 0},
        "trade":  {"running": False, "connected": False, "last_error": None,
                   "last_message_ts": 0, "connect_count": 0, "error_count": 0},
        "ticker": {"running": False, "connected": False, "last_error": None,
                   "last_message_ts": 0, "connect_count": 0, "error_count": 0,
                   "subscribed_expiry": None, "active_url": None, "tried_urls": []},
    }

@st.cache_resource
def _get_chain_store():
    return {"underlying": UNDERLYING, "expiries": [], "by_expiry": {}, "symbols": [],
            "last_ok": False, "last_error": None, "last_loaded_ts": 0}

@st.cache_resource
def _get_spot_store():
    return {"price": None, "ts": 0, "last_error": None, "msg_count": 0, "err_count": 0}

@st.cache_resource
def _get_creds_store():
    return {"api_key": "", "secret_key": ""}

@st.cache_resource
def _get_backend_flags():
    return {"backend_started": False, "snapshot_http_started": False}

@st.cache_resource
def _get_debug_log_store():
    return {"lock": threading.Lock(), "lines": []}

@st.cache_resource
def _get_order_book_store():
    return {"symbol": None, "data": None, "last_error": None, "fetching": False, "ts": 0}

@st.cache_resource
def _get_render_stats_store():
    return {"lock": threading.Lock(), "script_runs": 0, "fragment_runs": 0}

@st.cache_resource
def _get_ticker_ws_ref():
    # holds the live wsapp so we can close/resubscribe when expiry changes
    return {"wsapp": None, "lock": threading.Lock()}

LIVE_QUOTES   = _get_live_quotes_store()
WS_STATES     = _get_ws_states()
CHAIN_META    = _get_chain_store()
SPOT_STORE    = _get_spot_store()
CREDS         = _get_creds_store()
FLAGS         = _get_backend_flags()
DEBUG_LOG     = _get_debug_log_store()
ORDER_BOOK    = _get_order_book_store()
TICKER_WS_REF = _get_ticker_ws_ref()
RENDER_STATS  = _get_render_stats_store()

# shortcuts
SPOT_WS   = WS_STATES["spot"]
MARK_WS   = WS_STATES["mark"]
TRADE_WS  = WS_STATES["trade"]
TICKER_WS = WS_STATES["ticker"]

DEBUG_LOG_MAX = 250

# ─────────────────────────────────────────────────────────────────────
# Debug logger
# ─────────────────────────────────────────────────────────────────────
def dlog(msg, level="info"):
    line = {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": str(msg)}
    with DEBUG_LOG["lock"]:
        DEBUG_LOG["lines"].append(line)
        if len(DEBUG_LOG["lines"]) > DEBUG_LOG_MAX:
            del DEBUG_LOG["lines"][: len(DEBUG_LOG["lines"]) - DEBUG_LOG_MAX]

def dlog_exception(where, exc):
    dlog(f"EXCEPTION in {where}: {exc}\n{traceback.format_exc()[-600:]}", level="err")

def debug_log_snapshot():
    with DEBUG_LOG["lock"]:
        return list(DEBUG_LOG["lines"])

if not WEBSOCKET_LIB_OK:
    dlog("websocket-client NOT installed — pip install websocket-client", level="err")

# ─────────────────────────────────────────────────────────────────────
# REST helpers
# ─────────────────────────────────────────────────────────────────────
def get_server_time():
    try:
        r = requests.get(f"{BASE_URL}/api/v3/time", timeout=10)
        if r.status_code == 200:
            return r.json().get("serverTime", int(time.time() * 1000))
    except Exception:
        pass
    return int(time.time() * 1000)

def sign_request(params, secret_key):
    params["timestamp"] = get_server_time()
    params["recvWindow"] = 10000
    qs = urlencode(params, doseq=True)
    sig = hmac.new(secret_key.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"

def call_api(base, path, params, api_key, signed=False, secret_key=None):
    headers = {"X-MBX-APIKEY": api_key} if api_key else {}
    qs = sign_request(params.copy(), secret_key) if signed else urlencode(params, doseq=True)
    url = f"{base}{path}"
    if qs:
        url = f"{url}?{qs}"
    try:
        r  = requests.get(url, headers=headers, timeout=20)
        ct = r.headers.get("Content-Type", "")
        if "application/json" in ct:
            data = r.json()
            if r.status_code == 200:
                return True, data
            return False, f"Error {data.get('code', r.status_code)}: {data.get('msg', 'Unknown')}"
        text = r.text.strip()
        if "<html" in text.lower():
            return False, f"HTTP {r.status_code}: HTML response (not JSON)"
        return False, f"HTTP {r.status_code}: {text[:300]}"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except requests.exceptions.ConnectionError:
        return False, "Connection error (network blocked?)"
    except Exception as e:
        return False, str(e)

def get_spot_balance(api_key, secret_key):
    ok, data = call_api(BASE_URL, "/api/v3/account", {}, api_key, signed=True, secret_key=secret_key)
    if not ok:
        return False, data
    balances = [b for b in data.get("balances", [])
                if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0]
    return True, balances

def get_order_book(symbol, limit=10):
    """REST snapshot of order book — used on first click."""
    ok, data = call_api(EAPI_URL, "/eapi/v1/depth", {"symbol": symbol, "limit": limit}, "", signed=False)
    if not ok:
        return False, data
    return True, {
        "symbol": symbol,
        "bids": data.get("bids", []),
        "asks": data.get("asks", []),
        "ts": int(time.time() * 1000),
    }

# ─────────────────────────────────────────────────────────────────────
# Option chain metadata (REST — once + every 10 min)
# ─────────────────────────────────────────────────────────────────────
def parse_option_symbol(symbol):
    try:
        parts = symbol.split("-")
        if len(parts) != 4:
            return None
        coin, expiry, strike, cp = parts
        side = "CALL" if cp.upper() == "C" else "PUT" if cp.upper() == "P" else None
        if not side:
            return None
        return {"symbol": symbol, "underlying": f"{coin}USDT",
                "expiry": expiry, "strike": float(strike), "side": side}
    except Exception:
        return None

def get_option_universe(api_key, underlying=UNDERLYING):
    ok, data = call_api(EAPI_URL, "/eapi/v1/exchangeInfo", {}, api_key, signed=False)
    if not ok:
        return False, data
    filtered = []
    for item in data.get("optionSymbols", []):
        parsed = parse_option_symbol(item.get("symbol"))
        if parsed and parsed["underlying"] == underlying:
            filtered.append(parsed)
    return True, filtered

def build_chain_map(parsed_symbols):
    expiry_map = {}
    for item in parsed_symbols:
        expiry, strike, side, symbol = item["expiry"], item["strike"], item["side"], item["symbol"]
        expiry_map.setdefault(expiry, {})
        expiry_map[expiry].setdefault(strike, {"strike": strike, "call_symbol": None, "put_symbol": None})
        if side == "CALL":
            expiry_map[expiry][strike]["call_symbol"] = symbol
        else:
            expiry_map[expiry][strike]["put_symbol"] = symbol
    return {exp: sorted(rows.values(), key=lambda x: x["strike"])
            for exp, rows in expiry_map.items()}

def load_chain_metadata(api_key, underlying=UNDERLYING):
    ok, parsed_symbols = get_option_universe(api_key, underlying=underlying)
    if not ok:
        CHAIN_META["last_ok"]    = False
        CHAIN_META["last_error"] = parsed_symbols
        dlog(f"Chain metadata FAILED: {parsed_symbols}", level="err")
        return False, parsed_symbols
    chain_map = build_chain_map(parsed_symbols)
    CHAIN_META.update({
        "underlying":     underlying,
        "expiries":       sorted(chain_map.keys()),
        "by_expiry":      chain_map,
        "symbols":        [x["symbol"] for x in parsed_symbols],
        "last_ok":        True,
        "last_error":     None,
        "last_loaded_ts": int(time.time() * 1000),
    })
    dlog(f"Chain metadata OK — {len(CHAIN_META['expiries'])} expiries, "
         f"{len(parsed_symbols)} symbols", level="ok")
    return True, CHAIN_META

# ─────────────────────────────────────────────────────────────────────
# Live data builders
# ─────────────────────────────────────────────────────────────────────
def get_live_chain_for_expiry(expiry):
    rows = CHAIN_META["by_expiry"].get(expiry, [])
    out  = []
    for row in rows:
        call_q = LIVE_QUOTES.get(row.get("call_symbol"), {})
        put_q  = LIVE_QUOTES.get(row.get("put_symbol"),  {})
        out.append({
            "strike":     row["strike"],
            "call_symbol": row.get("call_symbol"),
            "call_bid":   call_q.get("bid"),  "call_ask":   call_q.get("ask"),
            "call_mark":  call_q.get("mark"), "call_last":  call_q.get("last"),
            "call_iv":    call_q.get("iv"),   "call_delta": call_q.get("delta"),
            "put_symbol": row.get("put_symbol"),
            "put_bid":    put_q.get("bid"),   "put_ask":    put_q.get("ask"),
            "put_mark":   put_q.get("mark"),  "put_last":   put_q.get("last"),
            "put_iv":     put_q.get("iv"),    "put_delta":  put_q.get("delta"),
        })
    return out

def find_global_nearest_strike(spot_price):
    if spot_price is None:
        return None
    best, best_dist = None, None
    for expiry, rows in CHAIN_META["by_expiry"].items():
        for row in rows:
            dist = abs(row["strike"] - spot_price)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = {"expiry": expiry, "strike": row["strike"],
                        "call_symbol": row.get("call_symbol"),
                        "put_symbol":  row.get("put_symbol")}
    return best

# ─────────────────────────────────────────────────────────────────────
# Generic WebSocket loop
# ─────────────────────────────────────────────────────────────────────
def _ws_loop(name, state, url_or_urls, on_message_fn, store_wsapp_ref=None):
    """
    Runs a reconnecting WebSocket loop.
    url_or_urls can be a single URL string, or a list of candidate URLs —
    when a list is given, each connection attempt tries the next candidate
    in rotation and logs which one it's trying, so you can see in the debug
    log which endpoint actually works.
    """
    urls = url_or_urls if isinstance(url_or_urls, (list, tuple)) else [url_or_urls]
    idx = 0
    attempt_connected = {"v": False}

    def _on_open(ws):
        state["connected"]     = True
        state["last_error"]    = None
        state["connect_count"] += 1
        attempt_connected["v"] = True
        if store_wsapp_ref is not None:
            with TICKER_WS_REF["lock"]:
                TICKER_WS_REF["wsapp"] = ws
        dlog(f"{name} WS connected -> {state.get('active_url')}", level="ok")

    def _on_message(ws, message):
        try:
            data    = json.loads(message)
            payload = data.get("data") if isinstance(data, dict) and "data" in data else data
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        on_message_fn(item)
            elif isinstance(payload, dict):
                on_message_fn(payload)
        except Exception as e:
            state["last_error"]   = str(e)
            state["error_count"] += 1

    def _on_error(ws, error):
        state["connected"]    = False
        state["last_error"]   = str(error)
        state["error_count"] += 1
        dlog(f"{name} WS error on {state.get('active_url')}: {error}", level="err")

    def _on_close(ws, code, msg):
        state["connected"] = False
        if store_wsapp_ref is not None:
            with TICKER_WS_REF["lock"]:
                TICKER_WS_REF["wsapp"] = None
        dlog(f"{name} WS closed (code={code})", level="warn")

    while state["running"]:
        url = urls[idx % len(urls)]
        state["active_url"] = url
        attempt_connected["v"] = False
        if "tried_urls" in state and url not in state["tried_urls"]:
            state["tried_urls"].append(url)
        if len(urls) > 1:
            dlog(f"{name} WS trying candidate {idx % len(urls) + 1}/{len(urls)}: {url}")
        try:
            wsapp = websocket.WebSocketApp(
                url,
                on_open=_on_open, on_message=_on_message,
                on_error=_on_error, on_close=_on_close,
            )
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            state["last_error"]   = str(e)
            state["connected"]    = False
            dlog_exception(f"{name} WS loop ({url})", e)
        if state["running"]:
            # If this URL never connected during this attempt, move on to the
            # next candidate. If it did connect and later dropped, retry the
            # same URL first (it's the one that's known to work).
            if not attempt_connected["v"]:
                idx += 1
            time.sleep(3)

# ─────────────────────────────────────────────────────────────────────
# Message handlers
# ─────────────────────────────────────────────────────────────────────
def _update_spot_from_trade(msg):
    """Handler for btcusdt@trade — extracts last price."""
    try:
        price = msg.get("p") or msg.get("price")
        if price not in (None, ""):
            SPOT_STORE["price"]     = float(price)
            SPOT_STORE["ts"]        = int(time.time() * 1000)
            SPOT_STORE["last_error"] = None
            SPOT_STORE["msg_count"] += 1
            SPOT_WS["last_message_ts"] = SPOT_STORE["ts"]
    except Exception as e:
        SPOT_STORE["err_count"] += 1
        SPOT_STORE["last_error"]  = str(e)

def _update_from_mark_price(msg):
    """Handler for optionMarkPrice stream."""
    try:
        symbol = msg.get("s") or msg.get("symbol")
        if not symbol:
            return
        LIVE_QUOTES.setdefault(symbol, {})
        field_map = {
            "last":    ["c", "last", "lastPrice"],
            "bid":     ["bo", "bidPrice"],
            "ask":     ["ao", "askPrice"],
            "mark":    ["mp", "mark", "markPrice"],
            "vol":     ["V",  "volume"],
            "delta":   ["d",  "delta"],
            "gamma":   ["g",  "gamma"],
            "theta":   ["t",  "theta"],
            "vega":    ["v",  "vega"],
            "iv":      ["vo", "iv", "markIV"],
            "buy_iv":  ["b",  "buy_iv"],
            "sell_iv": ["a",  "sell_iv"],
        }
        for target, candidates in field_map.items():
            for key in candidates:
                if key in msg and msg[key] not in (None, ""):
                    LIVE_QUOTES[symbol][target] = msg[key]
                    break
        LIVE_QUOTES[symbol]["symbol"] = symbol
        LIVE_QUOTES[symbol]["ts"]     = int(time.time() * 1000)
        MARK_WS["last_message_ts"]    = LIVE_QUOTES[symbol]["ts"]
    except Exception as e:
        MARK_WS["last_error"] = str(e)
        dlog_exception("_update_from_mark_price", e)

def _update_from_trade(msg):
    """Handler for optionTrade stream — updates last traded price."""
    try:
        symbol = msg.get("s")
        if not symbol:
            return
        LIVE_QUOTES.setdefault(symbol, {})
        price, qty = msg.get("p"), msg.get("q")
        if price not in (None, ""):
            LIVE_QUOTES[symbol]["last"] = price
        if qty not in (None, ""):
            try:
                prev = float(LIVE_QUOTES[symbol].get("vol_cum", 0) or 0)
                LIVE_QUOTES[symbol]["vol_cum"] = prev + float(qty)
            except Exception:
                pass
        LIVE_QUOTES[symbol]["last_trade_ts"] = int(time.time() * 1000)
        TRADE_WS["last_message_ts"]          = LIVE_QUOTES[symbol]["last_trade_ts"]
    except Exception as e:
        TRADE_WS["last_error"] = str(e)
        dlog_exception("_update_from_trade", e)

def _update_from_ticker(msg):
    """
    Handler for @ticker stream — full bid/ask/mark/iv/delta per symbol.
    Binance eoptions ticker fields:
      s=symbol, o=open, h=high, l=low, c=close(last), V=volume,
      A=amount, P=priceChangePct, p=priceChange,
      bo=bestBidPrice, ba=bestBidQty, ao=bestAskPrice, aa=bestAskQty,
      mp=markPrice, vo=impliedVolatility, d=delta, t=theta, g=gamma, v=vega,
      T=expiry, e=event
    """
    try:
        symbol = msg.get("s") or msg.get("symbol")
        if not symbol:
            return
        LIVE_QUOTES.setdefault(symbol, {})
        q = LIVE_QUOTES[symbol]

        def _set(target, keys):
            for k in keys:
                val = msg.get(k)
                if val not in (None, ""):
                    q[target] = val
                    return

        _set("last",  ["c", "close", "lastPrice"])
        _set("bid",   ["bo", "bestBidPrice"])
        _set("ask",   ["ao", "bestAskPrice"])
        _set("mark",  ["mp", "markPrice"])
        _set("iv",    ["vo", "impliedVolatility", "markIV"])
        _set("delta", ["d",  "delta"])
        _set("gamma", ["g",  "gamma"])
        _set("theta", ["t",  "theta"])
        _set("vega",  ["v",  "vega"])
        _set("vol",   ["V",  "volume"])

        q["symbol"] = symbol
        q["ts"]     = int(time.time() * 1000)
        TICKER_WS["last_message_ts"] = q["ts"]
    except Exception as e:
        TICKER_WS["last_error"] = str(e)
        dlog_exception("_update_from_ticker", e)

# ─────────────────────────────────────────────────────────────────────
# WebSocket starters
# ─────────────────────────────────────────────────────────────────────
def start_spot_ws():
    """Real-time BTC spot price via trade stream."""
    if not WEBSOCKET_LIB_OK or SPOT_WS["running"]:
        return
    url = f"wss://stream.binance.com/ws/{UNDERLYING.lower()}@trade"
    SPOT_WS["running"] = True
    threading.Thread(
        target=_ws_loop,
        args=("Spot-Trade", SPOT_WS, url, _update_spot_from_trade),
        daemon=True,
    ).start()
    dlog("Spot-Trade WebSocket thread started")

def start_mark_price_ws():
    if not WEBSOCKET_LIB_OK or MARK_WS["running"]:
        return
    url = (f"wss://fstream.binance.com/market/stream?streams="
           f"{UNDERLYING.lower()}@optionMarkPrice")
    MARK_WS["running"] = True
    threading.Thread(
        target=_ws_loop,
        args=("Mark-Price", MARK_WS, url, _update_from_mark_price),
        daemon=True,
    ).start()
    dlog("Mark-Price WebSocket thread started")

def start_trade_ws():
    if not WEBSOCKET_LIB_OK or TRADE_WS["running"]:
        return
    url = (f"wss://fstream.binance.com/public/stream?streams="
           f"{UNDERLYING.lower()}@optionTrade")
    TRADE_WS["running"] = True
    threading.Thread(
        target=_ws_loop,
        args=("Trade", TRADE_WS, url, _update_from_trade),
        daemon=True,
    ).start()
    dlog("Trade WebSocket thread started")

def start_ticker_ws_for_expiry(expiry):
    """
    Subscribe to @ticker for every symbol in the given expiry.
    Binance eoptions combined stream URL:
      wss://nbstream.binance.com/eoptions/stream?streams=SYM1@ticker/SYM2@ticker/...
    If already running for same expiry → skip.
    If running for different expiry → close old, open new.
    """
    if not WEBSOCKET_LIB_OK or not expiry:
        return
    if TICKER_WS["subscribed_expiry"] == expiry and TICKER_WS["running"]:
        return  # already live for this expiry

    rows = CHAIN_META["by_expiry"].get(expiry, [])
    if not rows:
        dlog(f"Ticker WS: no rows for expiry {expiry}, skipping", level="warn")
        return

    symbols = []
    for row in rows:
        if row.get("call_symbol"):
            symbols.append(row["call_symbol"])
        if row.get("put_symbol"):
            symbols.append(row["put_symbol"])

    if not symbols:
        return

    # Close existing WS if any
    if TICKER_WS["running"]:
        TICKER_WS["running"] = False
        with TICKER_WS_REF["lock"]:
            ws = TICKER_WS_REF["wsapp"]
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        time.sleep(0.5)

    # Per official Binance Options docs, the correct stream name is @optionTicker
    # (not @ticker) and the correct base URL is fstream.binance.com/public/ or
    # /market/ (not nbstream.binance.com/eoptions/, which returns 404).
    # We try the documented candidates first and keep the old URL as a last
    # resort fallback, in case Binance routes it differently in practice.
    streams_new = "/".join(f"{s.lower()}@optionTicker" for s in symbols)
    streams_old = "/".join(f"{s.lower()}@ticker" for s in symbols)
    url_candidates = [
        f"wss://fstream.binance.com/public/stream?streams={streams_new}",
        f"wss://fstream.binance.com/market/stream?streams={streams_new}",
        f"wss://nbstream.binance.com/eoptions/stream?streams={streams_old}",
    ]
    TICKER_WS["running"]            = True
    TICKER_WS["subscribed_expiry"]  = expiry
    TICKER_WS["tried_urls"]         = []
    threading.Thread(
        target=_ws_loop,
        args=("Ticker", TICKER_WS, url_candidates, _update_from_ticker),
        kwargs={"store_wsapp_ref": True},
        daemon=True,
    ).start()
    dlog(f"Ticker WS started for expiry {expiry} ({len(symbols)} symbols), "
         f"{len(url_candidates)} endpoint candidates queued", level="ok")

# ─────────────────────────────────────────────────────────────────────
# Background loops
# ─────────────────────────────────────────────────────────────────────
def _chain_meta_refresh_loop():
    first_run = True
    while True:
        api_key = CREDS.get("api_key", "")
        if api_key:
            try:
                load_chain_metadata(api_key, UNDERLYING)
            except Exception as e:
                dlog_exception("_chain_meta_refresh_loop", e)
        time.sleep(5 if first_run else 600)
        first_run = False

def ensure_backend_started():
    if FLAGS["backend_started"]:
        return
    FLAGS["backend_started"] = True
    dlog("Starting background threads & WebSockets...")
    threading.Thread(target=_chain_meta_refresh_loop, daemon=True).start()
    start_spot_ws()
    start_mark_price_ws()
    start_trade_ws()
    start_snapshot_http_server()
    # Ticker WS for option chain is started later when expiry is known

# ─────────────────────────────────────────────────────────────────────
# Local snapshot HTTP server — lets the browser poll for fresh chain data
# on its own, without Streamlit needing to rerun/re-embed the iframe.
# This is what actually removes the blink (see architecture note at top).
# ─────────────────────────────────────────────────────────────────────
class _SnapshotHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default request logging to stdout

    def _write_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client (browser) navigated away / poll aborted — harmless

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs     = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/snapshot":
                expiry = (qs.get("expiry") or [None])[0]
                payload = build_snapshot(expiry)
                self._write_json(200, payload)
                return
            if parsed.path == "/order_book":
                symbol = (qs.get("symbol") or [None])[0]
                if not symbol:
                    self._write_json(400, {"error": "missing symbol"})
                    return
                ORDER_BOOK["symbol"]     = symbol
                ORDER_BOOK["fetching"]   = True
                ORDER_BOOK["last_error"] = None
                ok, result = get_order_book(symbol, limit=10)
                if ok:
                    ORDER_BOOK["data"]       = result
                    ORDER_BOOK["ts"]         = int(time.time() * 1000)
                    ORDER_BOOK["last_error"] = None
                    dlog(f"Order book REST OK: {symbol}", level="ok")
                    ORDER_BOOK["fetching"] = False
                    self._write_json(200, {
                        "symbol": symbol, "bids": result.get("bids", []),
                        "asks": result.get("asks", []), "last_error": None,
                    })
                else:
                    ORDER_BOOK["data"]       = None
                    ORDER_BOOK["last_error"] = result
                    ORDER_BOOK["fetching"]   = False
                    dlog(f"Order book REST FAILED: {symbol}: {result}", level="err")
                    self._write_json(200, {
                        "symbol": symbol, "bids": [], "asks": [], "last_error": result,
                    })
                return
            self._write_json(404, {"error": "not found"})
        except Exception as e:
            dlog_exception("_SnapshotHTTPHandler.do_GET", e)
            try:
                self._write_json(500, {"error": str(e)})
            except Exception:
                pass

    def do_OPTIONS(self):
        # Not strictly needed for simple GET fetches, but harmless to have
        # in case a browser sends a CORS preflight for any reason.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

def start_snapshot_http_server():
    if FLAGS.get("snapshot_http_started"):
        return
    FLAGS["snapshot_http_started"] = True
    try:
        server = socketserver.ThreadingTCPServer(
            ("0.0.0.0", SNAPSHOT_HTTP_PORT), _SnapshotHTTPHandler
        )
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        dlog(f"Snapshot HTTP server started on port {SNAPSHOT_HTTP_PORT} "
             f"(browser polls this for live chain updates)", level="ok")
    except OSError as e:
        # Most likely "address already in use" — a previous Streamlit rerun's
        # server is probably still alive and fine to keep using.
        dlog(f"Snapshot HTTP server bind skipped/failed on port "
             f"{SNAPSHOT_HTTP_PORT}: {e}", level="warn")
    except Exception as e:
        dlog_exception("start_snapshot_http_server", e)

# ─────────────────────────────────────────────────────────────────────
# Snapshot builder
# ─────────────────────────────────────────────────────────────────────
def build_snapshot(selected_expiry):
    now_ms = int(time.time() * 1000)

    def age_sec(ts):
        return round((now_ms - ts) / 1000, 1) if ts else None

    nearest = find_global_nearest_strike(SPOT_STORE["price"])
    nearest_out = None
    if nearest:
        call_q = dict(LIVE_QUOTES.get(nearest["call_symbol"] or "", {}))
        put_q  = dict(LIVE_QUOTES.get(nearest["put_symbol"]  or "", {}))
        nearest_out = {
            "expiry": nearest["expiry"], "strike": nearest["strike"],
            "call": {"symbol": nearest["call_symbol"], **call_q},
            "put":  {"symbol": nearest["put_symbol"],  **put_q},
        }

    chain_rows = get_live_chain_for_expiry(selected_expiry) if selected_expiry else []

    order_book_out = {
        "symbol":     ORDER_BOOK["symbol"],
        "bids":       (ORDER_BOOK["data"] or {}).get("bids", []),
        "asks":       (ORDER_BOOK["data"] or {}).get("asks", []),
        "last_error": ORDER_BOOK["last_error"],
        "age_sec":    age_sec(ORDER_BOOK["ts"]) if ORDER_BOOK["ts"] else None,
        "fetching":   ORDER_BOOK["fetching"],
    }

    return {
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "spot": {
            "price":      SPOT_STORE["price"],
            "age_sec":    age_sec(SPOT_STORE["ts"]),
            "msg_count":  SPOT_STORE["msg_count"],
            "err_count":  SPOT_STORE["err_count"],
            "last_error": SPOT_STORE["last_error"],
        },
        "nearest": nearest_out,
        "chain": {
            "selected_expiry": selected_expiry,
            "expiries":        CHAIN_META["expiries"],
            "rows":            chain_rows,
            "last_ok":         CHAIN_META["last_ok"],
            "last_error":      CHAIN_META["last_error"],
            "age_sec":         age_sec(CHAIN_META["last_loaded_ts"]),
            "symbols_count":   len(CHAIN_META["symbols"]),
        },
        "order_book": order_book_out,
        "debug": {
            "websocket_lib_installed": WEBSOCKET_LIB_OK,
            "api_key_present":         bool(CREDS.get("api_key")),
            "render_stats": {
                "script_runs":    RENDER_STATS["script_runs"],
                "fragment_runs":  RENDER_STATS["fragment_runs"],
            },
            "ws_spot":   {
                "running":           SPOT_WS["running"],
                "connected":         SPOT_WS["connected"],
                "last_error":        SPOT_WS["last_error"],
                "last_msg_age_sec":  age_sec(SPOT_WS["last_message_ts"]),
                "connect_count":     SPOT_WS["connect_count"],
                "error_count":       SPOT_WS["error_count"],
            },
            "ws_mark":   {
                "running":           MARK_WS["running"],
                "connected":         MARK_WS["connected"],
                "last_error":        MARK_WS["last_error"],
                "last_msg_age_sec":  age_sec(MARK_WS["last_message_ts"]),
                "connect_count":     MARK_WS["connect_count"],
                "error_count":       MARK_WS["error_count"],
            },
            "ws_trade":  {
                "running":           TRADE_WS["running"],
                "connected":         TRADE_WS["connected"],
                "last_error":        TRADE_WS["last_error"],
                "last_msg_age_sec":  age_sec(TRADE_WS["last_message_ts"]),
                "connect_count":     TRADE_WS["connect_count"],
                "error_count":       TRADE_WS["error_count"],
            },
            "ws_ticker": {
                "running":              TICKER_WS["running"],
                "connected":            TICKER_WS["connected"],
                "last_error":           TICKER_WS["last_error"],
                "last_msg_age_sec":     age_sec(TICKER_WS["last_message_ts"]),
                "subscribed_expiry":    TICKER_WS["subscribed_expiry"],
                "connect_count":        TICKER_WS["connect_count"],
                "error_count":          TICKER_WS["error_count"],
                "active_url":           TICKER_WS.get("active_url"),
                "tried_urls":           TICKER_WS.get("tried_urls", []),
            },
            "log": debug_log_snapshot(),
        },
    }

# ─────────────────────────────────────────────────────────────────────
# Streamlit UI
# ─────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Binance Live Option Chain",
                   layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
#MainMenu, footer, header {visibility:hidden;}
.block-container{padding-top:1.2rem;}
</style>""", unsafe_allow_html=True)

if "binance_logged_in" not in st.session_state:
    st.session_state.binance_logged_in = False

# ── Login gate ──────────────────────────────────────────────────────
if not st.session_state.binance_logged_in:
    st.title("🟡 Binance Login")
    st.caption("API Key aur Secret Key daalo — live option chain dashboard khulega.")

    api_key_input    = st.text_input("Binance API Key",    type="password")
    secret_key_input = st.text_input("Binance Secret Key", type="password")

    if st.button("Login", type="primary", use_container_width=True):
        if not api_key_input or not secret_key_input:
            st.error("Pehle API Key aur Secret Key daalo")
        else:
            with st.spinner("Binance se verify ho raha hai..."):
                ok, result = get_spot_balance(api_key_input, secret_key_input)
            if ok:
                CREDS["api_key"]    = api_key_input
                CREDS["secret_key"] = secret_key_input
                st.session_state.binance_logged_in = True
                ensure_backend_started()
                st.success("Login successful!")
                st.rerun()
            else:
                st.error(f"Login failed: {result}")

    with st.expander("🐞 Debug (pre-login)"):
        st.write("websocket-client installed:", WEBSOCKET_LIB_OK)
        for line in debug_log_snapshot()[-30:]:
            st.text(f"[{line['t']}] {line['level'].upper()}: {line['msg']}")
    st.stop()

# ── Dashboard ───────────────────────────────────────────────────────
ensure_backend_started()
with RENDER_STATS["lock"]:
    RENDER_STATS["script_runs"] += 1

top_col1, top_col2, top_col3 = st.columns([5, 2, 1])
with top_col1:
    st.title("📊 Live BTC Option Chain — Binance")
with top_col2:
    refresh_sec  = st.slider("Live-poll interval (sec)", 1, 10, DEFAULT_REFRESH_SEC,
                              help="Kitni jaldi browser JS local snapshot server se fresh "
                                   "data khinche. Yeh ab Streamlit rerun trigger NAHI karta — "
                                   "iframe sirf ek baar load hota hai, isliye blink nahi hoti.")
    auto_refresh = st.checkbox("Live updates on", value=True)
with top_col3:
    if st.button("Logout"):
        st.session_state.binance_logged_in = False
        st.rerun()

expiries = CHAIN_META["expiries"]
if not expiries:
    st.info("Option chain metadata load ho rahi hai... (5-10 sec)")
    selected_expiry = None
else:
    if ("selected_expiry" not in st.session_state
            or st.session_state.selected_expiry not in expiries):
        st.session_state.selected_expiry = expiries[0]
    selected_expiry = st.selectbox("Expiry", expiries, key="selected_expiry")
    # Start/switch ticker WS for the selected expiry
    start_ticker_ws_for_expiry(selected_expiry)

# NOTE: order book REST snapshot used to go through a Streamlit query-param
# + rerun dance here. That's no longer needed — chart.html now fetches
# /order_book directly from the local snapshot HTTP server (see
# _SnapshotHTTPHandler above), which avoids triggering any Streamlit rerun.

chart_path = Path(__file__).parent / "chart.html"

# ── Chart iframe: embedded ONCE per real script rerun ──────────────────
# (login, logout, expiry change, manual browser refresh) — NOT on a timer.
# Live number updates now happen entirely client-side via the browser
# polling the local snapshot HTTP server (see start_snapshot_http_server
# above), so the iframe document itself never reloads → no blink.
with RENDER_STATS["lock"]:
    RENDER_STATS["fragment_runs"] += 1   # kept for the debug-panel comparison

snapshot = build_snapshot(selected_expiry)
if chart_path.exists():
    html = chart_path.read_text(encoding="utf-8")
    html = html.replace("__SNAPSHOT_JSON__", json.dumps(snapshot))
    html = html.replace("__POLL_MS__", str(int(refresh_sec * 1000) if auto_refresh else "0"))
    html = html.replace("__SNAPSHOT_PORT__", str(SNAPSHOT_HTTP_PORT))
    html = html.replace("__SELECTED_EXPIRY__", json.dumps(selected_expiry))
    components.html(html, height=950, scrolling=True)
else:
    st.error("chart.html not found — same folder me rakho.")
