"""
Binance Option Chain — Login Gate + Live Backend + Advanced Debug Panel
========================================================================
IMPORTANT ARCHITECTURE NOTE (why this version is different):
Earlier version ran a side HTTP server on 127.0.0.1:8502 and had
chart.html poll it via fetch(). That only works when the browser and
the server are the SAME machine (local dev). On a hosted deployment
(Render, etc.) the browser is on the user's phone/PC and 127.0.0.1
there means "the phone itself" — so the connection can never succeed.
That's exactly the "Backend se connect nahi ho pa raha" error seen.

FIX: no network calls from the browser at all. Every few seconds,
Streamlit reruns this script, we build one JSON "snapshot" of
everything (spot price, nearest strike, option chain rows, AND a
full debug log of what every background thread is doing / any
errors), and embed that JSON directly inside the HTML we send to
components.html(). The debug panel in chart.html just reads that
embedded JSON — no fetch, no ports, works anywhere Streamlit works.
"""

import time
import hmac
import hashlib
import json
import threading
import traceback
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

BASE_URL = "https://api.binance.com"
EAPI_URL = "https://eapi.binance.com"
UNDERLYING = "BTCUSDT"
DEFAULT_REFRESH_SEC = 3

# =====================================================================
# Persistent stores (survive Streamlit reruns — process-wide singletons)
# =====================================================================
@st.cache_resource
def _get_live_quotes_store():
    return {}

@st.cache_resource
def _get_ws_state_store():
    return {"running": False, "connected": False, "last_error": None,
             "last_message_ts": 0, "connect_count": 0, "error_count": 0}

@st.cache_resource
def _get_trade_ws_state_store():
    return {"running": False, "connected": False, "last_error": None,
             "last_message_ts": 0, "connect_count": 0, "error_count": 0}

@st.cache_resource
def _get_chain_store():
    return {"underlying": UNDERLYING, "expiries": [], "by_expiry": {}, "symbols": [],
            "last_ok": False, "last_error": None, "last_loaded_ts": 0}

@st.cache_resource
def _get_spot_store():
    return {"price": None, "ts": 0, "last_error": None, "ok_count": 0, "err_count": 0}

@st.cache_resource
def _get_creds_store():
    return {"api_key": "", "secret_key": ""}

@st.cache_resource
def _get_backend_flags():
    return {"backend_started": False}

@st.cache_resource
def _get_debug_log_store():
    return {"lock": threading.Lock(), "lines": []}

LIVE_QUOTES = _get_live_quotes_store()
WS_STATE = _get_ws_state_store()
TRADE_WS_STATE = _get_trade_ws_state_store()
CHAIN_META = _get_chain_store()
SPOT_STORE = _get_spot_store()
CREDS = _get_creds_store()
FLAGS = _get_backend_flags()
DEBUG_LOG = _get_debug_log_store()

DEBUG_LOG_MAX = 250

def dlog(msg, level="info"):
    """Thread-safe debug log — shows up live in the debug panel."""
    line = {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": str(msg)}
    with DEBUG_LOG["lock"]:
        DEBUG_LOG["lines"].append(line)
        if len(DEBUG_LOG["lines"]) > DEBUG_LOG_MAX:
            del DEBUG_LOG["lines"][: len(DEBUG_LOG["lines"]) - DEBUG_LOG_MAX]

def dlog_exception(where, exc):
    dlog(f"EXCEPTION in {where}: {exc}\n{traceback.format_exc()[-800:]}", level="err")

def debug_log_snapshot():
    with DEBUG_LOG["lock"]:
        return list(DEBUG_LOG["lines"])

if not WEBSOCKET_LIB_OK:
    dlog("websocket-client package NOT installed — run: pip install websocket-client", level="err")

# =====================================================================
# Binance REST helpers
# =====================================================================
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
        r = requests.get(url, headers=headers, timeout=20)
        ct = r.headers.get("Content-Type", "")
        if "application/json" in ct:
            data = r.json()
            if r.status_code == 200:
                return True, data
            return False, f"Error {data.get('code', r.status_code)}: {data.get('msg', 'Unknown error')}"
        text = r.text.strip()
        if "<html" in text.lower():
            return False, f"HTTP {r.status_code}: Binance returned HTML instead of JSON"
        return False, f"HTTP {r.status_code}: {text[:300]}"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except requests.exceptions.ConnectionError:
        return False, "Connection error (network egress blocked / no internet?)"
    except Exception as e:
        return False, str(e)

def get_spot_balance(api_key, secret_key):
    ok, data = call_api(BASE_URL, "/api/v3/account", {}, api_key, signed=True, secret_key=secret_key)
    if not ok:
        return False, data
    balances = [b for b in data.get("balances", []) if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0]
    return True, balances

def get_spot_price(symbol=UNDERLYING):
    ok, data = call_api(BASE_URL, "/api/v3/ticker/price", {"symbol": symbol}, "", signed=False)
    if not ok:
        return None, data
    return float(data["price"]), None

def parse_option_symbol(symbol):
    try:
        parts = symbol.split("-")
        if len(parts) != 4:
            return None
        underlying_coin, expiry, strike, cp = parts
        side = "CALL" if cp.upper() == "C" else "PUT" if cp.upper() == "P" else None
        if not side:
            return None
        return {"symbol": symbol, "underlying": f"{underlying_coin}USDT",
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

# =====================================================================
# Option chain map (expiry -> strikes)
# =====================================================================
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
    return {expiry: sorted(rows.values(), key=lambda x: x["strike"]) for expiry, rows in expiry_map.items()}

def load_chain_metadata(api_key, underlying=UNDERLYING):
    ok, parsed_symbols = get_option_universe(api_key, underlying=underlying)
    if not ok:
        CHAIN_META["last_ok"] = False
        CHAIN_META["last_error"] = parsed_symbols
        dlog(f"Chain metadata load FAILED: {parsed_symbols}", level="err")
        return False, parsed_symbols
    chain_map = build_chain_map(parsed_symbols)
    CHAIN_META["underlying"] = underlying
    CHAIN_META["expiries"] = sorted(chain_map.keys())
    CHAIN_META["by_expiry"] = chain_map
    CHAIN_META["symbols"] = [x["symbol"] for x in parsed_symbols]
    CHAIN_META["last_ok"] = True
    CHAIN_META["last_error"] = None
    CHAIN_META["last_loaded_ts"] = int(time.time() * 1000)
    dlog(f"Chain metadata loaded OK — {len(CHAIN_META['expiries'])} expiries, {len(parsed_symbols)} symbols", level="ok")
    return True, CHAIN_META

def get_symbols_for_expiry(expiry):
    rows = CHAIN_META["by_expiry"].get(expiry, [])
    out = []
    for row in rows:
        if row.get("call_symbol"):
            out.append(row["call_symbol"])
        if row.get("put_symbol"):
            out.append(row["put_symbol"])
    return out

def get_live_chain_for_expiry(expiry):
    rows = CHAIN_META["by_expiry"].get(expiry, [])
    out = []
    for row in rows:
        call_q = LIVE_QUOTES.get(row.get("call_symbol"), {})
        put_q = LIVE_QUOTES.get(row.get("put_symbol"), {})
        out.append({
            "strike": row["strike"],
            "call_symbol": row.get("call_symbol"), "call_bid": call_q.get("bid"),
            "call_ask": call_q.get("ask"), "call_mark": call_q.get("mark"),
            "call_last": call_q.get("last"), "call_iv": call_q.get("iv"),
            "call_delta": call_q.get("delta"),
            "put_symbol": row.get("put_symbol"), "put_bid": put_q.get("bid"),
            "put_ask": put_q.get("ask"), "put_mark": put_q.get("mark"),
            "put_last": put_q.get("last"), "put_iv": put_q.get("iv"),
            "put_delta": put_q.get("delta"),
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
                        "call_symbol": row.get("call_symbol"), "put_symbol": row.get("put_symbol")}
    return best

# =====================================================================
# Live WebSocket — options mark price + trade streams
# =====================================================================
def update_live_quote_from_msg(msg):
    try:
        symbol = msg.get("s") or msg.get("symbol")
        if not symbol:
            return
        LIVE_QUOTES.setdefault(symbol, {})
        field_map = {
            "last": ["c", "last", "lastPrice"], "bid": ["bo", "bidPrice"], "ask": ["ao", "askPrice"],
            "mark": ["mp", "mark", "markPrice"], "vol": ["V", "volume"],
            "delta": ["d", "delta"], "gamma": ["g", "gamma"], "theta": ["t", "theta"], "vega": ["v", "vega"],
            "iv": ["vo", "iv", "markIV"], "buy_iv": ["b", "buy_iv"], "sell_iv": ["a", "sell_iv"],
        }
        for target, candidates in field_map.items():
            for key in candidates:
                if key in msg and msg[key] not in (None, ""):
                    LIVE_QUOTES[symbol][target] = msg[key]
                    break
        LIVE_QUOTES[symbol]["symbol"] = symbol
        LIVE_QUOTES[symbol]["ts"] = int(time.time() * 1000)
        WS_STATE["last_message_ts"] = LIVE_QUOTES[symbol]["ts"]
    except Exception as e:
        WS_STATE["last_error"] = str(e)
        dlog_exception("update_live_quote_from_msg", e)

def update_live_trade_from_msg(msg):
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
        TRADE_WS_STATE["last_message_ts"] = LIVE_QUOTES[symbol]["last_trade_ts"]
    except Exception as e:
        TRADE_WS_STATE["last_error"] = str(e)
        dlog_exception("update_live_trade_from_msg", e)

def _ws_loop(name, state, url, on_message_fn):
    def _on_open(ws):
        state["connected"] = True
        state["last_error"] = None
        state["connect_count"] += 1
        dlog(f"{name} WebSocket connected", level="ok")

    def _on_message(ws, message):
        try:
            data = json.loads(message)
            payload = data.get("data") if isinstance(data, dict) and "data" in data else data
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        on_message_fn(item)
            elif isinstance(payload, dict):
                on_message_fn(payload)
        except Exception as e:
            state["last_error"] = str(e)
            state["error_count"] += 1

    def _on_error(ws, error):
        state["connected"] = False
        state["last_error"] = str(error)
        state["error_count"] += 1
        dlog(f"{name} WebSocket error: {error}", level="err")

    def _on_close(ws, code, msg):
        state["connected"] = False
        dlog(f"{name} WebSocket closed (code={code})", level="warn")

    while state["running"]:
        try:
            wsapp = websocket.WebSocketApp(url, on_open=_on_open, on_message=_on_message,
                                            on_error=_on_error, on_close=_on_close)
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            state["last_error"] = str(e)
            state["connected"] = False
            dlog_exception(f"{name} WebSocket loop", e)
        if state["running"]:
            time.sleep(3)

def start_mark_price_ws(underlying=UNDERLYING):
    if not WEBSOCKET_LIB_OK or WS_STATE["running"]:
        return
    url = f"wss://fstream.binance.com/market/stream?streams={underlying.lower()}@optionMarkPrice"
    WS_STATE["running"] = True
    threading.Thread(target=_ws_loop, args=("Mark-Price", WS_STATE, url, update_live_quote_from_msg), daemon=True).start()
    dlog("Mark-Price WebSocket thread started")

def start_trade_ws(underlying=UNDERLYING):
    if not WEBSOCKET_LIB_OK or TRADE_WS_STATE["running"]:
        return
    url = f"wss://fstream.binance.com/public/stream?streams={underlying.lower()}@optionTrade"
    TRADE_WS_STATE["running"] = True
    threading.Thread(target=_ws_loop, args=("Trade", TRADE_WS_STATE, url, update_live_trade_from_msg), daemon=True).start()
    dlog("Trade WebSocket thread started")

# =====================================================================
# Background loops
# =====================================================================
def _spot_refresh_loop():
    while True:
        price, err = get_spot_price(UNDERLYING)
        if err:
            SPOT_STORE["last_error"] = err
            SPOT_STORE["err_count"] += 1
        else:
            SPOT_STORE["price"] = price
            SPOT_STORE["ts"] = int(time.time() * 1000)
            SPOT_STORE["last_error"] = None
            SPOT_STORE["ok_count"] += 1
        time.sleep(3)

def _chain_meta_refresh_loop():
    first_run = True
    while True:
        api_key = CREDS.get("api_key", "")
        if api_key:
            try:
                load_chain_metadata(api_key, UNDERLYING)
            except Exception as e:
                dlog_exception("_chain_meta_refresh_loop", e)
        time.sleep(5 if first_run else 600)  # fast first attempt, then every 10 min
        first_run = False

def ensure_backend_started():
    if FLAGS["backend_started"]:
        return
    FLAGS["backend_started"] = True
    dlog("Starting background threads (spot refresh, chain metadata, WebSockets)...")
    threading.Thread(target=_spot_refresh_loop, daemon=True).start()
    threading.Thread(target=_chain_meta_refresh_loop, daemon=True).start()
    start_mark_price_ws(UNDERLYING)
    start_trade_ws(UNDERLYING)

# =====================================================================
# Build the data+debug snapshot embedded into chart.html on every rerun
# =====================================================================
def build_snapshot(selected_expiry):
    now_ms = int(time.time() * 1000)

    def age_sec(ts):
        return round((now_ms - ts) / 1000, 1) if ts else None

    nearest = find_global_nearest_strike(SPOT_STORE["price"])
    nearest_out = None
    if nearest:
        call_q = dict(LIVE_QUOTES.get(nearest["call_symbol"], {}))
        put_q = dict(LIVE_QUOTES.get(nearest["put_symbol"], {}))
        nearest_out = {
            "expiry": nearest["expiry"], "strike": nearest["strike"],
            "call": {"symbol": nearest["call_symbol"], **call_q},
            "put": {"symbol": nearest["put_symbol"], **put_q},
        }

    chain_rows = get_live_chain_for_expiry(selected_expiry) if selected_expiry else []

    snapshot = {
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "spot": {
            "price": SPOT_STORE["price"],
            "age_sec": age_sec(SPOT_STORE["ts"]),
            "ok_count": SPOT_STORE["ok_count"],
            "err_count": SPOT_STORE["err_count"],
            "last_error": SPOT_STORE["last_error"],
        },
        "nearest": nearest_out,
        "chain": {
            "selected_expiry": selected_expiry,
            "expiries": CHAIN_META["expiries"],
            "rows": chain_rows,
            "last_ok": CHAIN_META["last_ok"],
            "last_error": CHAIN_META["last_error"],
            "age_sec": age_sec(CHAIN_META["last_loaded_ts"]),
            "symbols_count": len(CHAIN_META["symbols"]),
        },
        "debug": {
            "websocket_lib_installed": WEBSOCKET_LIB_OK,
            "api_key_present": bool(CREDS.get("api_key")),
            "ws_mark": {
                "running": WS_STATE["running"], "connected": WS_STATE["connected"],
                "last_error": WS_STATE["last_error"],
                "last_msg_age_sec": age_sec(WS_STATE["last_message_ts"]),
                "connect_count": WS_STATE["connect_count"], "error_count": WS_STATE["error_count"],
            },
            "ws_trade": {
                "running": TRADE_WS_STATE["running"], "connected": TRADE_WS_STATE["connected"],
                "last_error": TRADE_WS_STATE["last_error"],
                "last_msg_age_sec": age_sec(TRADE_WS_STATE["last_message_ts"]),
                "connect_count": TRADE_WS_STATE["connect_count"], "error_count": TRADE_WS_STATE["error_count"],
            },
            "log": debug_log_snapshot(),
        },
    }
    return snapshot

# =====================================================================
# Streamlit UI
# =====================================================================
st.set_page_config(page_title="Binance Live Option Chain", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
#MainMenu, footer, header {visibility:hidden;}
.block-container{padding-top:1.2rem;}
</style>""", unsafe_allow_html=True)

if "binance_logged_in" not in st.session_state:
    st.session_state.binance_logged_in = False

# ---------------------------------------------------------------
# LOGIN GATE
# ---------------------------------------------------------------
if not st.session_state.binance_logged_in:
    st.title("🟡 Binance Login")
    st.caption("API Key aur Secret Key daalo — verify hone ke baad live option chain dashboard khulega.")

    api_key_input = st.text_input("Binance API Key", type="password")
    secret_key_input = st.text_input("Binance Secret Key", type="password")

    if st.button("Login", type="primary", use_container_width=True):
        if not api_key_input or not secret_key_input:
            dlog("Login blocked — API key/secret empty", level="warn")
            st.error("Pehle API Key aur Secret Key daalo")
        else:
            dlog("Login attempt started")
            with st.spinner("Binance se verify ho raha hai..."):
                ok, result = get_spot_balance(api_key_input, secret_key_input)
            if ok:
                CREDS["api_key"] = api_key_input
                CREDS["secret_key"] = secret_key_input
                st.session_state.binance_logged_in = True
                dlog("Login SUCCESS — keys saved", level="ok")
                ensure_backend_started()
                st.success("Login successful!")
                st.rerun()
            else:
                dlog(f"Login FAILED: {result}", level="err")
                st.error(f"Login failed: {result}")

    with st.expander("🐞 Debug (pre-login)"):
        st.write("websocket-client installed:", WEBSOCKET_LIB_OK)
        for line in debug_log_snapshot()[-30:]:
            st.text(f"[{line['t']}] {line['level'].upper()}: {line['msg']}")
    st.stop()

# ---------------------------------------------------------------
# LOGGED IN — dashboard
# ---------------------------------------------------------------
ensure_backend_started()

top_col1, top_col2, top_col3 = st.columns([5, 2, 1])
with top_col1:
    st.title("📊 Live BTC Option Chain — Binance")
with top_col2:
    refresh_sec = st.slider("Refresh every (sec)", 2, 10, DEFAULT_REFRESH_SEC)
    auto_refresh = st.checkbox("Auto refresh", value=True)
with top_col3:
    if st.button("Logout"):
        st.session_state.binance_logged_in = False
        st.rerun()

expiries = CHAIN_META["expiries"]
if not expiries:
    st.info("Option chain metadata load ho rahi hai... thoda ruko (5-10 sec). Neeche debug icon se live status dekh sakte ho.")
    selected_expiry = None
else:
    if "selected_expiry" not in st.session_state or st.session_state.selected_expiry not in expiries:
        st.session_state.selected_expiry = expiries[0]
    selected_expiry = st.selectbox("Expiry", expiries, key="selected_expiry")

snapshot = build_snapshot(selected_expiry)

chart_path = Path(__file__).parent / "chart.html"
if chart_path.exists():
    html = chart_path.read_text(encoding="utf-8")
    html = html.replace("__SNAPSHOT_JSON__", json.dumps(snapshot))
    components.html(html, height=950, scrolling=True)
else:
    st.error("chart.html not found next to main.py — same folder me rakho.")

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()
