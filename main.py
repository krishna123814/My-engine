"""
Binance Option Chain — Login Gate + Live Backend
=================================================
- Pehle sirf Binance login card dikhta hai (API key + secret).
- Login verify hone ke baad hi background threads start hoti hain
  (REST option-chain metadata, live WebSocket mark-price/trade streams)
  aur ek chhota local HTTP server (side-port 8502) chalta hai jo
  chart.html ko live JSON data serve karta hai.
- chart.html isi Streamlit app ke andar iframe ki tarah embed hota hai
  aur plain JS fetch() se local server ko poll karke UI update karta hai.
"""

import time
import hmac
import hashlib
import json
import threading
import http.server
import urllib.parse as urlparse
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import urlencode

try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None

BASE_URL = "https://api.binance.com"
EAPI_URL = "https://eapi.binance.com"
API_PORT = 8502
UNDERLYING = "BTCUSDT"

# =====================================================================
# Persistent stores (survive Streamlit reruns)
# =====================================================================
@st.cache_resource
def _get_live_quotes_store():
    return {}   # symbol -> latest live fields

@st.cache_resource
def _get_ws_state_store():
    return {"running": False, "connected": False, "last_error": None,
             "last_message_ts": 0, "thread": None, "wsapp": None}

@st.cache_resource
def _get_trade_ws_state_store():
    return {"running": False, "connected": False, "last_error": None,
             "thread": None, "wsapp": None}

@st.cache_resource
def _get_chain_store():
    return {"underlying": UNDERLYING, "expiries": [], "by_expiry": {}, "symbols": []}

@st.cache_resource
def _get_spot_store():
    return {"price": None, "ts": 0}

@st.cache_resource
def _get_creds_store():
    return {"api_key": "", "secret_key": ""}

@st.cache_resource
def _get_backend_flags():
    # Idempotency flags so threads/server start exactly once per process.
    return {"backend_started": False, "http_server_started": False}

LIVE_QUOTES = _get_live_quotes_store()
WS_STATE = _get_ws_state_store()
TRADE_WS_STATE = _get_trade_ws_state_store()
CHAIN_META = _get_chain_store()
SPOT_STORE = _get_spot_store()
CREDS = _get_creds_store()
FLAGS = _get_backend_flags()

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
        return False, "Connection error"
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

def get_option_premium(api_key, option_symbol):
    ok, data = call_api(EAPI_URL, "/eapi/v1/mark", {"symbol": option_symbol}, api_key, signed=False)
    if not ok:
        return None, data
    row = data[0] if isinstance(data, list) and data else data
    return {
        "symbol": row.get("symbol", option_symbol), "markPrice": row.get("markPrice"),
        "bidIV": row.get("bidIV"), "askIV": row.get("askIV"), "markIV": row.get("markIV"),
        "delta": row.get("delta"), "gamma": row.get("gamma"),
        "theta": row.get("theta"), "vega": row.get("vega"),
    }, None

def get_option_order_book(api_key, option_symbol, limit=10):
    ok, data = call_api(EAPI_URL, "/eapi/v1/depth", {"symbol": option_symbol, "limit": limit}, api_key, signed=False)
    if not ok:
        return None, data
    return data, None

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
        return False, parsed_symbols
    chain_map = build_chain_map(parsed_symbols)
    CHAIN_META["underlying"] = underlying
    CHAIN_META["expiries"] = sorted(chain_map.keys())
    CHAIN_META["by_expiry"] = chain_map
    CHAIN_META["symbols"] = [x["symbol"] for x in parsed_symbols]
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

def nearest_expiry_to_now():
    if not CHAIN_META["expiries"]:
        return None
    return CHAIN_META["expiries"][0]

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
    except Exception as e:
        TRADE_WS_STATE["last_error"] = str(e)

def _ws_loop(state, url, on_message_fn):
    def _on_open(ws):
        state["connected"] = True
        state["last_error"] = None
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
    def _on_error(ws, error):
        state["connected"] = False
        state["last_error"] = str(error)
    def _on_close(ws, code, msg):
        state["connected"] = False

    while state["running"]:
        try:
            wsapp = websocket.WebSocketApp(url, on_open=_on_open, on_message=_on_message,
                                            on_error=_on_error, on_close=_on_close)
            state["wsapp"] = wsapp
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            state["last_error"] = str(e)
            state["connected"] = False
        if state["running"]:
            time.sleep(3)

def start_mark_price_ws(underlying=UNDERLYING):
    if websocket is None or WS_STATE["running"]:
        return
    url = f"wss://fstream.binance.com/market/stream?streams={underlying.lower()}@optionMarkPrice"
    WS_STATE["running"] = True
    t = threading.Thread(target=_ws_loop, args=(WS_STATE, url, update_live_quote_from_msg), daemon=True)
    WS_STATE["thread"] = t
    t.start()

def start_trade_ws(underlying=UNDERLYING):
    if websocket is None or TRADE_WS_STATE["running"]:
        return
    url = f"wss://fstream.binance.com/public/stream?streams={underlying.lower()}@optionTrade"
    TRADE_WS_STATE["running"] = True
    t = threading.Thread(target=_ws_loop, args=(TRADE_WS_STATE, url, update_live_trade_from_msg), daemon=True)
    TRADE_WS_STATE["thread"] = t
    t.start()

# =====================================================================
# Background loops — chain metadata refresh + spot price refresh
# =====================================================================
def _spot_refresh_loop():
    while True:
        price, err = get_spot_price(UNDERLYING)
        if not err:
            SPOT_STORE["price"] = price
            SPOT_STORE["ts"] = int(time.time() * 1000)
        time.sleep(3)

def _chain_meta_refresh_loop():
    while True:
        api_key = CREDS.get("api_key", "")
        if api_key:
            ok, _ = load_chain_metadata(api_key, UNDERLYING)
            if ok:
                expiry = nearest_expiry_to_now()
                if expiry:
                    symbols = get_symbols_for_expiry(expiry)
                    if symbols and set(symbols) - set(WS_STATE.get("subscribed", [])):
                        WS_STATE["subscribed"] = symbols
        time.sleep(600)  # metadata rarely changes — 10 min refresh

def ensure_backend_started():
    """Start all background threads exactly once per process."""
    if FLAGS["backend_started"]:
        return
    FLAGS["backend_started"] = True
    threading.Thread(target=_spot_refresh_loop, daemon=True).start()
    threading.Thread(target=_chain_meta_refresh_loop, daemon=True).start()
    start_mark_price_ws(UNDERLYING)
    start_trade_ws(UNDERLYING)

# =====================================================================
# Local side-port HTTP API server — chart.html polls this via fetch()
# =====================================================================
def _json_response(handler, status, payload):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

def start_api_server():
    if FLAGS["http_server_started"]:
        return
    FLAGS["http_server_started"] = True

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parsed = urlparse.urlparse(self.path)
            qs = urlparse.parse_qs(parsed.query)

            if parsed.path == "/api/spot":
                _json_response(self, 200, {"price": SPOT_STORE["price"], "ts": SPOT_STORE["ts"]})
                return

            if parsed.path == "/api/expiries":
                _json_response(self, 200, {"expiries": CHAIN_META["expiries"]})
                return

            if parsed.path == "/api/nearest":
                nearest = find_global_nearest_strike(SPOT_STORE["price"])
                if not nearest:
                    _json_response(self, 200, {"error": "chain not loaded yet"})
                    return
                api_key = CREDS.get("api_key", "")
                call_q = dict(LIVE_QUOTES.get(nearest["call_symbol"], {}))
                put_q = dict(LIVE_QUOTES.get(nearest["put_symbol"], {}))
                _json_response(self, 200, {
                    "spot": SPOT_STORE["price"], "expiry": nearest["expiry"], "strike": nearest["strike"],
                    "call": {"symbol": nearest["call_symbol"], **call_q},
                    "put": {"symbol": nearest["put_symbol"], **put_q},
                })
                return

            if parsed.path == "/api/optionchain":
                expiry = qs.get("expiry", [None])[0] or nearest_expiry_to_now()
                if not expiry:
                    _json_response(self, 200, {"expiry": None, "rows": []})
                    return
                symbols = get_symbols_for_expiry(expiry)
                # Lazily subscribe if this expiry's symbols aren't live yet.
                if symbols:
                    start_mark_price_ws(UNDERLYING)
                rows = get_live_chain_for_expiry(expiry)
                _json_response(self, 200, {"expiry": expiry, "spot": SPOT_STORE["price"], "rows": rows})
                return

            if parsed.path == "/api/depth":
                symbol = qs.get("symbol", [""])[0]
                api_key = CREDS.get("api_key", "")
                if not symbol:
                    _json_response(self, 400, {"error": "symbol missing"})
                    return
                data, err = get_option_order_book(api_key, symbol, limit=10)
                if err:
                    _json_response(self, 200, {"error": err})
                    return
                _json_response(self, 200, data)
                return

            self.send_response(404)
            self.end_headers()

    def _run():
        server = http.server.ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler)
        server.serve_forever()

    threading.Thread(target=_run, daemon=True).start()

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
# LOGIN GATE — sirf ye dikhta hai jab tak login successful na ho
# ---------------------------------------------------------------
if not st.session_state.binance_logged_in:
    st.title("🟡 Binance Login")
    st.caption("API Key aur Secret Key daalo — verify hone ke baad live option chain dashboard khulega.")

    api_key_input = st.text_input("Binance API Key", type="password")
    secret_key_input = st.text_input("Binance Secret Key", type="password")

    if st.button("Login", type="primary", use_container_width=True):
        if not api_key_input or not secret_key_input:
            st.error("Pehle API Key aur Secret Key daalo")
        else:
            with st.spinner("Binance se verify ho raha hai..."):
                ok, result = get_spot_balance(api_key_input, secret_key_input)
            if ok:
                CREDS["api_key"] = api_key_input
                CREDS["secret_key"] = secret_key_input
                st.session_state.binance_logged_in = True
                ensure_backend_started()
                start_api_server()
                st.success("Login successful!")
                st.rerun()
            else:
                st.error(f"Login failed: {result}")

    st.stop()  # login ho jaane tak neeche kuch bhi render nahi hoga

# ---------------------------------------------------------------
# LOGIN HO CHUKA HAI — ab dashboard dikhao
# ---------------------------------------------------------------
ensure_backend_started()
start_api_server()

top_col1, top_col2 = st.columns([6, 1])
with top_col1:
    st.title("📊 Live BTC Option Chain — Binance")
with top_col2:
    if st.button("Logout"):
        st.session_state.binance_logged_in = False
        st.rerun()

chart_path = Path(__file__).parent / "chart.html"
if chart_path.exists():
    html = chart_path.read_text(encoding="utf-8").replace("__API_PORT__", str(API_PORT))
    components.html(html, height=900, scrolling=True)
else:
    st.error("chart.html not found next to main.py — same folder me rakho.")
