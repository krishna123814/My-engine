import time
import hmac
import hashlib
import requests
import streamlit as st
from urllib.parse import urlencode

BASE_URL = "https://api.binance.com"
EAPI_URL = "https://eapi.binance.com"


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
    headers = {"X-MBX-APIKEY": api_key}
    if signed:
        qs = sign_request(params.copy(), secret_key)
    else:
        qs = urlencode(params, doseq=True)
    url = f"{base}{path}?{qs}"
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
        else:
            text = r.text.strip()
            if "<html" in text.lower():
                return False, f"HTTP {r.status_code}: Binance returned HTML instead of JSON (endpoint may be unavailable)"
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
            strike = float(s["strikePrice"])
        except Exception:
            continue
        candidates.append({
            "symbol": s.get("symbol"),
            "strikePrice": strike,
            "expiryDate": s.get("expiryDate"),
            "distance": abs(strike - btc_price),
        })

    if not candidates:
        return None, f"No BTCUSDT {side} options found in exchangeInfo"

    candidates.sort(key=lambda x: x["distance"])
    nearest = candidates[0]
    return nearest, None


def get_option_premium(api_key, symbol):
    ok, data = call_api(EAPI_URL, "/eapi/v1/mark", {"symbol": symbol}, api_key, signed=False)
    if not ok:
        return None, data
    # Response can be a list or a dict
    if isinstance(data, list):
        for item in data:
            if item.get("symbol") == symbol:
                return item, None
        if data:
            return data[0], None
    elif isinstance(data, dict):
        return data, None
    return None, "Unexpected response format"


# ── UI ──────────────────────────────────────────────────────────────────────

st.title("Binance Viewer")

with st.form("keys_form"):
    api_key = st.text_input("API Key", type="password")
    secret_key = st.text_input("Secret Key", type="password")
    side = st.selectbox("Option side", ["CALL", "PUT"])
    submitted = st.form_submit_button("Fetch")

if submitted:
    if not api_key or not secret_key:
        st.error("Enter both API Key and Secret Key.")
    else:
        # ── Spot Balance ──────────────────────────────────────────────────
        st.subheader("Spot Balance")
        with st.spinner("Fetching spot balance..."):
            ok, spot = get_spot_balance(api_key, secret_key)
        if ok:
            if spot:
                st.dataframe(spot, use_container_width=True)
            else:
                st.info("No non-zero balances.")
        else:
            st.error(f"Spot fetch failed: {spot}")

        # ── Nearest Option Strike + Premium ───────────────────────────────
        st.subheader(f"Nearest BTC {side} Option (Strike + Premium)")
        with st.spinner("Fetching BTC spot price..."):
            btc_price, err = get_spot_price("BTCUSDT")
        if err:
            st.error(f"BTC price fetch failed: {err}")
        else:
            st.write(f"BTC Spot Price: **{btc_price:,.2f} USDT**")
            with st.spinner("Finding nearest strike..."):
                nearest, err = get_nearest_option(api_key, btc_price, side)
            if err:
                st.error(f"Options exchangeInfo failed: {err}")
            else:
                st.write(f"Symbol: **{nearest['symbol']}**")
                st.write(f"Strike Price: **{nearest['strikePrice']:,.2f} USDT**")
                st.write(f"Expiry: **{nearest['expiryDate']}**")
                st.write(f"Distance from spot: **{nearest['distance']:,.2f} USDT**")
                with st.spinner("Fetching premium..."):
                    mark, err = get_option_premium(api_key, nearest["symbol"])
                if err:
                    st.error(f"Premium fetch failed: {err}")
                else:
                    st.write("**Mark / Premium data:**")
                    st.json(mark)
