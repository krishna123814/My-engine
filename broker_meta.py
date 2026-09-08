"""
broker_meta.py — Fyers aur Binance account balance/meta cache helpers
(login token exchange, available-balance, nearest BankNifty strike,
aur full BankNifty option-chain fetch se Fyers).
"""
import time
import json
import hashlib
import threading
import requests
import streamlit as st

from credentials import load_creds, _get_binance_creds
from config import BINANCE_BASE_URL, BINANCE_EAPI_URL, BN_OC_SYMBOL
from live_state import _LIVE
from login_session import _write_login_log
from binance_rest import binance_get_spot_balance

def fyers_get_access_token(app_id: str, secret_key: str, auth_code: str) -> tuple[bool, str, dict]:
    """Step 5: exchange auth_code for access_token. Returns (ok, token_or_msg, full_response)."""
    app_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
    payload = {"grant_type": "authorization_code", "appIdHash": app_hash, "code": auth_code}
    try:
        resp = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            json=payload,
            timeout=10,
        )
        raw = {}
        try:
            raw = resp.json()
        except Exception:
            raw = {"raw_text": resp.text, "status_code": resp.status_code}
        # Log to file for debugging
        _write_login_log(payload, resp.status_code, raw)
        if raw.get("s") == "ok" and "access_token" in raw:
            return True, raw["access_token"], raw
        return False, raw.get("message", str(raw)), raw
    except Exception as e:
        err = {"exception": str(e)}
        _write_login_log(payload, 0, err)
        return False, str(e), err


# ─── Funds (Available Balance) + Nearest Strike helpers ───────────────────────
FYERS_META_FILE = "fyers_meta.json"
_FYERS_META_CACHE = {"balance": None, "strike": None, "ts": 0.0}
_FYERS_META_LOCK = threading.Lock()
_FYERS_META_TTL = 20  # seconds — funds API ko itni jaldi baar-baar hit nahi karna

BN_STRIKE_STEP = 100  # BankNifty option strikes 100 ke multiples mein hote hain

def fyers_get_available_balance(app_id: str, access_token: str) -> tuple[bool, "float | str"]:
    """Fyers /api/v3/funds se 'Available Balance' nikalta hai."""
    try:
        headers = {"Authorization": f"{app_id}:{access_token}"}
        r = requests.get(
            "https://api-t1.fyers.in/api/v3/funds",
            headers=headers, timeout=6,
        ).json()
        if r.get("s") != "ok":
            return False, r.get("message", str(r))
        for item in r.get("fund_limit", []):
            if item.get("title") == "Available Balance":
                return True, float(item.get("equityAmount", 0))
        return False, "Available Balance field not found"
    except Exception as e:
        return False, str(e)

def get_nearest_bn_strike() -> "int | None":
    """Current live BankNifty LTP se nazdiktareen strike (round to 100) nikalta hai."""
    ltp = _LIVE.get("ltp")
    if not ltp:
        return None
    return int(round(ltp / BN_STRIKE_STEP) * BN_STRIKE_STEP)

def refresh_fyers_meta_cache() -> dict:
    """Balance + nearest strike ko cache karta hai (TTL ke andar dobara fetch nahi karta),
    aur fyers_meta.json mein likh deta hai taaki chart iframe use poll kar sake."""
    now = time.time()
    with _FYERS_META_LOCK:
        stale = (now - _FYERS_META_CACHE["ts"]) >= _FYERS_META_TTL
    if stale:
        creds = load_creds()
        balance = _FYERS_META_CACHE["balance"]
        if creds.get("access_token") and creds.get("app_id"):
            ok, val = fyers_get_available_balance(creds["app_id"], creds["access_token"])
            if ok:
                balance = val
        strike = get_nearest_bn_strike()
        with _FYERS_META_LOCK:
            _FYERS_META_CACHE.update({"balance": balance, "strike": strike, "ts": now})
    with _FYERS_META_LOCK:
        payload = dict(_FYERS_META_CACHE)
    try:
        with open(FYERS_META_FILE, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass
    return payload


# ─── Binance USDT Balance (Real) — Option Chain "Balance" tab ke liye,
# BTCUSDT chain active hone par Fyers ₹ ki jagah USDT balance dikhana hai ──
BINANCE_META_FILE = "binance_meta.json"
_BINANCE_META_CACHE = {"usdt_balance": None, "ts": 0.0}
_BINANCE_META_LOCK = threading.Lock()
_BINANCE_META_TTL = 20  # seconds — Fyers meta jaisa hi rate-limit-safe TTL

def binance_get_usdt_balance(api_key: str, secret_key: str) -> tuple[bool, "float | str"]:
    """Binance spot account se USDT ka free+locked balance nikalta hai."""
    ok, balances = binance_get_spot_balance(api_key, secret_key)
    if not ok:
        return False, balances
    for b in balances:
        if b.get("asset") == "USDT":
            return True, float(b.get("free", 0)) + float(b.get("locked", 0))
    # USDT balance 0 ho to non-zero filter ki wajah se list mein hi nahi
    # aata — is case mein bhi ye "success, 0" maana jaaye, error nahi.
    return True, 0.0

def refresh_binance_meta_cache() -> dict:
    """USDT balance ko cache karta hai (TTL ke andar dobara fetch nahi karta),
    aur binance_meta.json mein likh deta hai taaki chart iframe use poll kar sake."""
    now = time.time()
    with _BINANCE_META_LOCK:
        stale = (now - _BINANCE_META_CACHE["ts"]) >= _BINANCE_META_TTL
    if stale:
        balance    = _BINANCE_META_CACHE["usdt_balance"]
        api_key, secret_key = _get_binance_creds()
        if api_key and secret_key:
            ok, val = binance_get_usdt_balance(api_key, secret_key)
            if ok:
                balance = val
        with _BINANCE_META_LOCK:
            _BINANCE_META_CACHE.update({"usdt_balance": balance, "ts": now})
    with _BINANCE_META_LOCK:
        payload = dict(_BINANCE_META_CACHE)
    try:
        with open(BINANCE_META_FILE, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass
    return payload


# ─── Real Option Chain (CE/PE, LTP, OI, Chg%) — jaisa broker app mein hota hai ──
OC_FILE   = "fyers_optionchain.json"
_OC_CACHE = {"data": None, "ts": 0.0}
_OC_LOCK  = threading.Lock()
_OC_TTL   = 2  # seconds — real-broker jaisa near-live feel, phir bhi rate-limit safe
_OC_DEBUG = {"last_error": "", "last_status": None, "last_url": "", "ts": 0.0}

# BN_OC_SYMBOL ab config.py se import hota hai (upar dekho)

def fyers_get_option_chain(app_id: str, access_token: str, symbol: str = BN_OC_SYMBOL,
                            strikecount: int = 10, timestamp: str = "") -> "dict | None":
    """Fyers Option Chain API se CE/PE strikes fetch karta hai.
    Primary domain fail ho to fallback domain try karta hai (Fyers docs mein
    dono variants dikhte hain). Har attempt ki debug info _OC_DEBUG mein
    save hoti hai taaki failure ka exact reason pata chal sake."""
    headers = {"Authorization": f"{app_id}:{access_token}"}
    params = {"symbol": symbol, "strikecount": strikecount, "timestamp": timestamp}
    urls = [
        "https://api-t1.fyers.in/data/options-chain",
        "https://api.fyers.in/v3/data/options-chain",
        "https://api-t1.fyers.in/data/options-chain-v3",
    ]
    raw = None
    last_err = ""
    last_status = None
    last_url = ""
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=8)
            last_status = resp.status_code
            last_url = url
            try:
                r = resp.json()
            except Exception:
                last_err = f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
                continue
            if r.get("s") == "ok":
                raw = r
                break
            else:
                last_err = r.get("message", str(r))[:300]
        except Exception as e:
            last_err = str(e)
            last_url = url

    _OC_DEBUG.update({"last_error": last_err, "last_status": last_status,
                       "last_url": last_url, "ts": time.time()})

    if not raw:
        return None

    d = raw.get("data", {})
    chain = d.get("optionsChain", [])

    # Underlying/spot entry pehchano (isme option_type nahi hota, 'fp' field hoti hai)
    spot = None
    for item in chain:
        if not item.get("option_type"):
            spot = item.get("ltp") or item.get("fp")
            break
    if spot is None:
        spot = _LIVE.get("ltp")

    # Strike-wise CE/PE group karo
    rows_map: dict = {}
    for item in chain:
        ot = item.get("option_type")
        if ot not in ("CE", "PE"):
            continue
        strike = item.get("strike_price")
        if strike is None:
            continue
        row = rows_map.setdefault(strike, {"strike": strike, "ce": None, "pe": None})
        leg = {
            "ltp":   item.get("ltp", 0),
            "chg":   item.get("ltpch", 0),
            "chgp":  item.get("ltpchp", 0),
            "oi":    item.get("oi", 0),
            "oich":  item.get("oich", 0),
            "oichp": item.get("oichp", 0),
            "volume":item.get("volume", 0),
            "bid":   item.get("bid", 0),
            "ask":   item.get("ask", 0),
            "symbol":item.get("symbol", ""),
        }
        if ot == "CE":
            row["ce"] = leg
        else:
            row["pe"] = leg

    rows = sorted(rows_map.values(), key=lambda x: x["strike"])
    atm = get_nearest_bn_strike() if spot is None else int(round(spot / BN_STRIKE_STEP) * BN_STRIKE_STEP)

    expiries = d.get("expiryData", [])
    selected_expiry_label = expiries[0].get("date") if expiries else ""
    # Fyers "expiry" field on the expiryData item is epoch seconds (string) —
    # frontend Rollover/Greeks features need this to compute time-to-expiry.
    try:
        selected_expiry_epoch = int(expiries[0].get("expiry")) if expiries and expiries[0].get("expiry") else None
    except (TypeError, ValueError):
        selected_expiry_epoch = None

    return {
        "spot": spot,
        "atm": atm,
        "rows": rows,
        "call_oi": d.get("callOi", 0),
        "put_oi": d.get("putOi", 0),
        "expiries": expiries,
        "expiry_label": selected_expiry_label,
        "expiry_epoch": selected_expiry_epoch,
        "ts": time.time(),
    }

# ─── Binance (BTC Options) — manual API key/secret login ───────────────────
# Reference file (user-provided) ke pattern se ported. Module-level rakha hai
# taaki background thread (BinanceOptionChainBG) bhi inhe use kar sake — login
# page ke andar local def karne se background thread inhe access nahi kar pata.
# BINANCE_BASE_URL / BINANCE_EAPI_URL ab config.py se import hote hain (upar dekho)

# ─── Proxy Configuration — RAM only, loaded from HF Space secrets ──────────
# Proxy settings sirf RAM (_PROXY_CACHE) mein rehte hain — koi disk/.json
# file involved nahi hai (pehle wala disk-persistence approach confusing tha
# aur hosting ka disk ephemeral hone par bhi kaam nahi karta tha). Startup par
# _load_proxy_from_env() HF Space secrets (env vars PROXY_HOST/PROXY_PORT/
# PROXY_USER/PROXY_PASS/PROXY_ON) se RAM fill kar deta hai. UI se Apply karna
# ho to bhi sirf is session ke RAM ko update karta hai — restart hone par
# wapas env-secrets se hi load hoga.
#
# FIX: pehle _PROXY_CACHE plain module-level dict tha, jo har Streamlit rerun
# par (dekho _STARTUP_LOG note) khaali reset ho jaata tha — jabki
# _load_proxy_from_env() sirf _is_fresh_boot par chalta hai. Isliye pehle
# rerun ke baad proxy hamesha khaali reh jaata tha, aur Verify + option-chain
# dono restricted-location error dete the. @st.cache_resource dict ko
# process-wide singleton banata hai (refer.py ke _get_chain_store() jaisa
# pattern) — function sirf pehli baar chalta hai, baaki sab reruns wahi ek
# dict object wapas paate hain, jab tak process khud restart na ho.
