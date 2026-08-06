
import time
import hmac
import hashlib
import requests
import streamlit as st
from urllib.parse import urlencode
import threading
import json
from collections import defaultdict
try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None

BASE_URL = "https://api.binance.com"
EAPI_URL = "https://eapi.binance.com"

# -------------------------
# Common helpers
# -------------------------
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

    if signed:
        qs = sign_request(params.copy(), secret_key)
    else:
        qs = urlencode(params, doseq=True)

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
            else:
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

# -------------------------
# Spot account
# -------------------------
def get_spot_balance(api_key, secret_key):
    ok, data = call_api(BASE_URL, "/api/v3/account", {}, api_key, signed=True, secret_key=secret_key)
    if not ok:
        return False, data

    balances = [
        b for b in data.get("balances", [])
        if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0
    ]
    return True, balances

def get_spot_price(symbol="BTCUSDT"):
    ok, data = call_api(BASE_URL, "/api/v3/ticker/price", {"symbol": symbol}, "", signed=False)
    if not ok:
        return None, data
    return float(data["price"]), None

# -------------------------
# Full option chain (live) - metadata + websocket
# -------------------------
WS_BASE = "wss://nbstream.binance.com/eoptions/ws"

# live cache
CHAIN_META = {
    "underlying": "BTCUSDT",
    "expiries": [],
    "by_expiry": {},   # expiry -> sorted list of rows
    "symbols": [],     # all symbols for selected underlying
}

LIVE_QUOTES = {}       # symbol -> latest live fields
WS_STATE = {
    "running": False,
    "connected": False,
    "last_error": None,
    "last_message_ts": 0,
    "subscribed_symbols": [],
    "thread": None,
    "wsapp": None,
}


def parse_option_symbol(symbol):
    """
    Example: BTC-260806-64500-C
    Returns:
    {
        "symbol": "...",
        "underlying": "BTCUSDT",
        "expiry": "260806",
        "strike": 64500.0,
        "side": "CALL"
    }
    """
    try:
        parts = symbol.split("-")
        if len(parts) != 4:
            return None

        underlying_coin, expiry, strike, cp = parts
        side = "CALL" if cp.upper() == "C" else "PUT" if cp.upper() == "P" else None
        if not side:
            return None

        return {
            "symbol": symbol,
            "underlying": f"{underlying_coin}USDT",
            "expiry": expiry,
            "strike": float(strike),
            "side": side,
        }
    except Exception:
        return None


def get_option_universe(api_key, underlying="BTCUSDT"):
    ok, data = call_api(EAPI_URL, "/eapi/v1/exchangeInfo", {}, api_key, signed=False)
    if not ok:
        return False, data

    option_symbols = data.get("optionSymbols", [])
    filtered = []
    expiries = set()

    for item in option_symbols:
        sym = item.get("symbol")
        parsed = parse_option_symbol(sym)
        if not parsed:
            continue
        if parsed["underlying"] != underlying:
            continue

        filtered.append(parsed)
        expiries.add(parsed["expiry"])

    filtered.sort(key=lambda x: (x["expiry"], x["strike"], x["side"]))
    expiries = sorted(expiries)

    return True, {
        "underlying": underlying,
        "symbols": filtered,
        "expiries": expiries,
    }


def build_chain_map(parsed_symbols):
    """
    expiry -> list of rows
    each row:
    {
        "strike": 64500.0,
        "call_symbol": "BTC-260806-64500-C",
        "put_symbol": "BTC-260806-64500-P"
    }
    """
    expiry_map = defaultdict(dict)

    for item in parsed_symbols:
        expiry = item["expiry"]
        strike = item["strike"]
        side = item["side"]
        symbol = item["symbol"]

        if strike not in expiry_map[expiry]:
            expiry_map[expiry][strike] = {
                "strike": strike,
                "call_symbol": None,
                "put_symbol": None,
            }

        if side == "CALL":
            expiry_map[expiry][strike]["call_symbol"] = symbol
        elif side == "PUT":
            expiry_map[expiry][strike]["put_symbol"] = symbol

    final_map = {}
    for expiry, strike_map in expiry_map.items():
        rows = list(strike_map.values())
        rows.sort(key=lambda x: x["strike"])
        final_map[expiry] = rows

    return final_map


def load_chain_metadata(api_key, underlying="BTCUSDT"):
    ok, data = get_option_universe(api_key, underlying=underlying)
    if not ok:
        return False, data

    chain_map = build_chain_map(data["symbols"])

    CHAIN_META["underlying"] = underlying
    CHAIN_META["expiries"] = data["expiries"]
    CHAIN_META["by_expiry"] = chain_map
    CHAIN_META["symbols"] = [x["symbol"] for x in data["symbols"]]

    return True, CHAIN_META


def get_symbols_for_expiry(expiry):
    rows = CHAIN_META["by_expiry"].get(expiry, [])
    symbols = []
    for row in rows:
        if row.get("call_symbol"):
            symbols.append(row["call_symbol"])
        if row.get("put_symbol"):
            symbols.append(row["put_symbol"])
    return symbols


def update_live_quote_from_msg(msg):
    """
    Defensive parser for option live ticker-style updates.
    Expected symbol key usually in one of: s, symbol
    """
    try:
        symbol = msg.get("s") or msg.get("symbol")
        if not symbol:
            return

        LIVE_QUOTES.setdefault(symbol, {})

        # Field map verified against official Binance options 24hr ticker
        # stream docs (developers.binance.com -> Option -> WebSocket Market
        # Streams -> "24hr Ticker by Underlying Asset and Expiration Data").
        # IMPORTANT: in the options payload, lowercase "b" = Buy Implied
        # Volatility and lowercase "a" = Sell Implied Volatility - they are
        # NOT bid/ask price. Real bid/ask price are "bo"/"ao". Also
        # lowercase "v" = vega (NOT volume) - real trading volume is "V"
        # (capital). Mixing these up silently corrupts the live table, so
        # they are kept as separate, non-overlapping candidate lists below.
        field_map = {
            "last": ["c", "last", "lastPrice"],
            "bid": ["bo", "bidPrice"],
            "ask": ["ao", "askPrice"],
            "mark": ["mp", "mark", "markPrice"],
            "vol": ["V", "volume"],           # V = trading volume (contracts)
            "amount": ["A", "amount"],        # A = trade amount (quote asset)
            "delta": ["d", "delta"],
            "gamma": ["g", "gamma"],
            "theta": ["t", "theta"],
            "vega": ["v", "vega"],            # v = vega
            "iv": ["vo", "iv", "markIV"],      # vo = mark implied volatility
            "buy_iv": ["b", "buy_iv"],         # b = buy implied volatility
            "sell_iv": ["a", "sell_iv"],       # a = sell implied volatility
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


def ws_on_open(ws):
    WS_STATE["connected"] = True
    WS_STATE["last_error"] = None

def ws_on_message(ws, message):
    try:
        data = json.loads(message)

        # some streams may wrap payload
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], dict):
                update_live_quote_from_msg(data["data"])
            else:
                update_live_quote_from_msg(data)
    except Exception as e:
        WS_STATE["last_error"] = str(e)

def ws_on_error(ws, error):
    WS_STATE["connected"] = False
    WS_STATE["last_error"] = str(error)

def ws_on_close(ws, close_status_code, close_msg):
    WS_STATE["connected"] = False


def build_ws_url_for_symbols(symbols):
    """
    Example stream names may need adjustment depending on exact Binance options stream format.
    Adapt stream suffix if your environment expects a different one.
    """
    streams = []
    for sym in symbols:
        stream_sym = sym.lower()
        streams.append(f"{stream_sym}@ticker")

    stream_part = "/".join(streams)
    return f"wss://nbstream.binance.com/eoptions/stream?streams={stream_part}"


def start_options_ws(symbols):
    if websocket is None:
        return False, "websocket-client package not installed"

    stop_options_ws()

    if not symbols:
        return False, "No symbols to subscribe"

    ws_url = build_ws_url_for_symbols(symbols)

    WS_STATE["running"] = True
    WS_STATE["subscribed_symbols"] = symbols[:]
    WS_STATE["last_error"] = None

    def _run():
        while WS_STATE["running"]:
            try:
                wsapp = websocket.WebSocketApp(
                    ws_url,
                    on_open=ws_on_open,
                    on_message=ws_on_message,
                    on_error=ws_on_error,
                    on_close=ws_on_close,
                )
                WS_STATE["wsapp"] = wsapp
                wsapp.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                WS_STATE["last_error"] = str(e)
                WS_STATE["connected"] = False

            if WS_STATE["running"]:
                time.sleep(3)

    t = threading.Thread(target=_run, daemon=True)
    WS_STATE["thread"] = t
    t.start()

    return True, "WebSocket started"


def stop_options_ws():
    WS_STATE["running"] = False
    WS_STATE["connected"] = False

    wsapp = WS_STATE.get("wsapp")
    if wsapp:
        try:
            wsapp.close()
        except Exception:
            pass

    WS_STATE["wsapp"] = None
    WS_STATE["thread"] = None
    WS_STATE["subscribed_symbols"] = []


def get_live_chain_for_expiry(expiry):
    rows = CHAIN_META["by_expiry"].get(expiry, [])
    out = []

    for row in rows:
        call_q = LIVE_QUOTES.get(row.get("call_symbol"), {})
        put_q = LIVE_QUOTES.get(row.get("put_symbol"), {})

        out.append({
            "strike": row["strike"],

            "call_symbol": row.get("call_symbol"),
            "call_bid": call_q.get("bid"),
            "call_ask": call_q.get("ask"),
            "call_last": call_q.get("last"),
            "call_mark": call_q.get("mark"),
            "call_iv": call_q.get("iv"),
            "call_delta": call_q.get("delta"),

            "put_symbol": row.get("put_symbol"),
            "put_bid": put_q.get("bid"),
            "put_ask": put_q.get("ask"),
            "put_last": put_q.get("last"),
            "put_mark": put_q.get("mark"),
            "put_iv": put_q.get("iv"),
            "put_delta": put_q.get("delta"),
        })

    return out


def filter_chain_around_spot(chain_rows, spot_price, window=10):
    if not chain_rows or spot_price is None:
        return chain_rows

    sorted_rows = sorted(chain_rows, key=lambda x: abs(x["strike"] - spot_price))
    nearest = sorted_rows[0]["strike"]

    all_strikes = [r["strike"] for r in chain_rows]
    all_strikes_sorted = sorted(all_strikes)

    try:
        idx = all_strikes_sorted.index(nearest)
    except ValueError:
        return chain_rows

    lo = max(0, idx - window)
    hi = min(len(all_strikes_sorted), idx + window + 1)
    selected = set(all_strikes_sorted[lo:hi])

    return [r for r in chain_rows if r["strike"] in selected]


# -------------------------
# Options market (existing single-strike fallback logic, kept unchanged)
# -------------------------
def get_nearest_option(api_key, btc_price, side="CALL"):
    ok, data = call_api(EAPI_URL, "/eapi/v1/exchangeInfo", {}, api_key, signed=False)
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
            "distance": abs(strike - btc_price)
        })

    if not candidates:
        return None, f"No BTCUSDT {side} option found"

    candidates.sort(key=lambda x: x["distance"])
    return candidates[0], None

def get_option_premium(api_key, option_symbol):
    # mark endpoint usually gives premium-like mark price data
    ok, data = call_api(EAPI_URL, "/eapi/v1/mark", {"symbol": option_symbol}, api_key, signed=False)
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

def get_option_order_book(api_key, option_symbol, limit=10):
    ok, data = call_api(
        EAPI_URL,
        "/eapi/v1/depth",
        {"symbol": option_symbol, "limit": limit},
        api_key,
        signed=False
    )
    if not ok:
        return None, data
    return data, None

# -------------------------
# Options balance
# -------------------------
def get_options_balance(api_key, secret_key):
    """
    Fetch options wallet/account balance first.
    Position endpoint is only fallback/debug, not actual wallet balance.
    """

    # Priority order: wallet/account first, position later
    candidate_paths = [
        "/eapi/v1/account",
        "/eapi/v1/balance",
        "/eapi/v1/position",
    ]

    full_debug = []

    for path in candidate_paths:
        ok, data = call_api(EAPI_URL, path, {}, api_key, signed=True, secret_key=secret_key)
        full_debug.append({
            "path": path,
            "ok": ok,
            "response": data
        })

        if ok:
            # 1) If endpoint gives account object, return directly
            if isinstance(data, dict):
                return True, {
                    "endpoint": path,
                    "type": "account",
                    "data": data,
                    "debug": full_debug
                }

            # 2) If endpoint gives list with rows, return directly
            if isinstance(data, list) and len(data) > 0:
                return True, {
                    "endpoint": path,
                    "type": "list",
                    "data": data,
                    "debug": full_debug
                }

            # 3) Empty list usually means no positions, not no balance
            # keep trying other candidate paths

    return False, full_debug

# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="Binance Spot + Options Checker", layout="centered")
st.title("Binance Login + Spot Balance + BTC Option Strike/Premium")

api_key = st.text_input("Binance API Key", type="password")
secret_key = st.text_input("Binance Secret Key", type="password")
side = st.selectbox("Option Side", ["CALL", "PUT"])

# -------------------------
# Full live option chain (all expiries) - session state init
# -------------------------
if "chain_loaded" not in st.session_state:
    st.session_state.chain_loaded = False

if "selected_expiry" not in st.session_state:
    st.session_state.selected_expiry = None

if "current_underlying" not in st.session_state:
    st.session_state.current_underlying = "BTCUSDT"

if "ws_started_for_expiry" not in st.session_state:
    st.session_state.ws_started_for_expiry = None

st.subheader("Full Live Option Chain (All Expiries)")

if st.button("Load Full Option Chain"):
    if not api_key:
        st.error("Please enter API key")
    else:
        ok, result = load_chain_metadata(api_key, underlying="BTCUSDT")
        if ok:
            st.session_state.chain_loaded = True
            if result["expiries"]:
                st.session_state.selected_expiry = result["expiries"][0]
            st.success(f"Loaded {len(result['symbols'])} option symbols across {len(result['expiries'])} expiries")
        else:
            st.error(result)

if st.session_state.chain_loaded and CHAIN_META["expiries"]:
    selected_expiry = st.selectbox(
        "Select Expiry",
        CHAIN_META["expiries"],
        index=CHAIN_META["expiries"].index(st.session_state.selected_expiry) if st.session_state.selected_expiry in CHAIN_META["expiries"] else 0
    )
    st.session_state.selected_expiry = selected_expiry

if st.session_state.chain_loaded and st.session_state.selected_expiry:
    expiry_symbols = get_symbols_for_expiry(st.session_state.selected_expiry)

    if st.session_state.ws_started_for_expiry != st.session_state.selected_expiry:
        ok, msg = start_options_ws(expiry_symbols)
        if ok:
            st.session_state.ws_started_for_expiry = st.session_state.selected_expiry
        else:
            st.error(msg)

chain_spot_price, chain_spot_err = get_spot_price("BTCUSDT")
if chain_spot_price:
    st.write(f"BTC Spot: {chain_spot_price}")
elif chain_spot_err:
    st.warning(f"Spot price issue: {chain_spot_err}")

if st.session_state.chain_loaded and st.session_state.selected_expiry:
    chain_rows = get_live_chain_for_expiry(st.session_state.selected_expiry)

    # optional: ATM-centered subset
    show_only_near_atm = st.checkbox("Show ATM nearby strikes only", value=True)
    strike_window = st.slider("ATM strike window", min_value=5, max_value=30, value=10)

    if show_only_near_atm and chain_spot_price:
        chain_rows = filter_chain_around_spot(chain_rows, chain_spot_price, window=strike_window)

    st.caption(
        f"WS connected: {WS_STATE['connected']} | "
        f"Subscribed symbols: {len(WS_STATE['subscribed_symbols'])} | "
        f"Last error: {WS_STATE['last_error']}"
    )

    st.dataframe(chain_rows, use_container_width=True)

    auto_refresh = st.checkbox("Auto refresh view", value=True)
    if auto_refresh:
        time.sleep(2)
        st.rerun()

if st.button("Stop WebSocket"):
    stop_options_ws()
    st.session_state.ws_started_for_expiry = None
    st.warning("WebSocket stopped")

st.divider()
st.subheader("Single Strike Fetch (legacy / fallback)")

if st.button("Fetch Data"):
    if not api_key or not secret_key:
        st.error("Please enter API key and secret key")
    else:
        st.info("Checking Binance account...")

        # 1) Spot balance
        ok_spot, spot_result = get_spot_balance(api_key, secret_key)
        if ok_spot:
            st.success("Spot login successful")
            st.subheader("Spot Balances")
            if spot_result:
                st.json(spot_result)
            else:
                st.write("No non-zero spot balances found")
        else:
            st.error(f"Spot balance error: {spot_result}")

        # 2) BTC price
        btc_price, err = get_spot_price("BTCUSDT")
        if err:
            st.error(f"BTC price fetch error: {err}")
        else:
            st.subheader("BTC Spot Price")
            st.write(btc_price)

            # 3) nearest option
            nearest, err = get_nearest_option(api_key, btc_price, side=side)
            if err:
                st.error(f"Nearest option error: {err}")
            else:
                st.subheader("Nearest BTC Option")
                st.json(nearest)

                # 4) premium
                premium, err = get_option_premium(api_key, nearest["symbol"])
                if err:
                    st.error(f"Option premium error: {err}")
                else:
                    st.subheader("Option Premium")
                    st.json(premium)

                # 5) order book
                order_book, err = get_option_order_book(api_key, nearest["symbol"], limit=10)
                if err:
                    st.error(f"Option order book error: {err}")
                else:
                    st.subheader("Option Order Book")
                    st.json(order_book)

        # 6) options balance
        st.subheader("Options Balance")
        ok_opt, opt_result = get_options_balance(api_key, secret_key)

        if ok_opt:
            st.success(f"Options endpoint worked: {opt_result['endpoint']}")

            data = opt_result["data"]

            if isinstance(data, dict):
                st.write("Raw options account data:")
                st.json(data)

                # Try common balance-like fields
                possible_fields = [
                    "asset",
                    "currency",
                    "balance",
                    "available",
                    "availableBalance",
                    "equity",
                    "marginBalance",
                    "unrealizedPNL",
                    "initialMargin",
                    "maintenanceMargin",
                ]

                extracted = {k: data.get(k) for k in possible_fields if k in data}
                if extracted:
                    st.write("Detected balance-related fields:")
                    st.json(extracted)

            elif isinstance(data, list):
                if len(data) == 0:
                    st.warning("Endpoint returned empty list. This usually means no open option positions, not necessarily zero option wallet balance.")
                else:
                    st.write("Raw options rows:")
                    st.json(data)
            else:
                st.write(data)

            with st.expander("Debug: all candidate endpoints tried"):
                st.json(opt_result.get("debug", []))

        else:
            st.error(f"Options balance error: could not fetch balance from any candidate endpoint")
            with st.expander("Debug: all candidate endpoints tried"):
                st.json(opt_result)
