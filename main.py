
import time
import hmac
import hashlib
import requests
import streamlit as st
from urllib.parse import urlencode

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
# Options market
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

# -------------------------
# Options balance
# -------------------------
def get_options_balance(api_key, secret_key):
    """
    Try likely options-account endpoint(s).
    If your account/environment doesn't support them,
    keep the raw error visible.
    """

    candidate_paths = [
        "/eapi/v1/account",   # most likely
        "/eapi/v1/position",  # sometimes useful to inspect options positions
    ]

    errors = []

    for path in candidate_paths:
        ok, data = call_api(EAPI_URL, path, {}, api_key, signed=True, secret_key=secret_key)
        if ok:
            return True, {
                "endpoint": path,
                "data": data
            }
        else:
            errors.append(f"{path} -> {data}")

    return False, " | ".join(errors)

# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="Binance Spot + Options Checker", layout="centered")
st.title("Binance Login + Spot Balance + BTC Option Strike/Premium")

api_key = st.text_input("Binance API Key", type="password")
secret_key = st.text_input("Binance Secret Key", type="password")
side = st.selectbox("Option Side", ["CALL", "PUT"])

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

        # 5) options balance
        st.subheader("Options Balance")
        ok_opt, opt_result = get_options_balance(api_key, secret_key)
        if ok_opt:
            st.success(f"Options account endpoint worked: {opt_result['endpoint']}")
            st.json(opt_result["data"])
        else:
            st.error(f"Options balance error: {opt_result}")
