import io
import json
import os
import time
import threading
import hashlib
import hmac
import zipfile
import requests
import pyotp
import streamlit as st
import streamlit.components.v1 as components
import datetime
from urllib.parse import urlencode

# ─── Fast2SMS API key ──────────────────────────────────────────────────────
# HF Spaces: Settings → Variables and secrets (env vars).
# Streamlit Cloud: Settings → Secrets (st.secrets) — kept as fallback.
def _get_secret(name: str, default: str = "") -> str:
    val = os.environ.get(name, "")
    if val:
        return val
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

FAST2SMS_KEY = _get_secret("FAST2SMS_KEY")

st.set_page_config(
    page_title="BankNifty Live Chart",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown("""<style>
/* ── Streamlit ke saare ads/badges/watermarks permanently hide ── */
#MainMenu                        {display:none!important}
footer                           {display:none!important}
header                           {display:none!important}
[data-testid="stToolbar"]        {display:none!important}
[data-testid="stDecoration"]     {display:none!important}
[data-testid="stStatusWidget"]   {display:none!important}
[data-testid="manage-app-button"]{display:none!important}
.reportview-container .main footer{display:none!important}
.viewerBadge_container__1QSob   {display:none!important}
.styles_viewerBadge__1yB5_      {display:none!important}
#stDecoration                    {display:none!important}
/* ── Layout ── */
.main .block-container{padding:0!important;max-width:100%!important;margin:0!important}
.stApp{background:#131722;overflow:hidden}
iframe{border:none!important}
</style>""", unsafe_allow_html=True)

# ─── Constants ────────────────────────────────────────────────────────────────
CREDS_FILE        = ".fyers_creds.json"
BN_LIVE_FILE      = "bn_live.json"
DAILY_CACHE_FILE  = "btc_daily_cache.json"
BN_DAILY_CACHE    = "bn_daily_cache.json"
DAILY_CACHE_TTL   = 300        # 5 min — aaj ki candle bhi update rahe
HIST_CACHE_TTL    = 300       # seconds for intraday cache (5 min — reduces API load)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Default credentials (user can override in sidebar).
# HF Spaces env vars / Streamlit Cloud secrets se aate hain (see _get_secret
# above); agar kahin set nahi hain (jaise local dev mein), khaali string
# fallback hoti hai — app tab manual-login form dikha dega.
DEFAULT_APP_ID    = _get_secret("FYERS_APP_ID")
DEFAULT_SECRET    = _get_secret("FYERS_SECRET")
DEFAULT_CLIENT_ID = _get_secret("FYERS_CLIENT_ID")
DEFAULT_PASSWORD  = _get_secret("FYERS_PASSWORD")
REDIRECT_URI = "https://www.google.com"   # generic, not sensitive — never changes

# ─── App-startup / login debug log ─────────────────────────────────────────
# Login ke turant baad screen chart par redirect ho jaati hai, isliye startup
# ke exact steps (creds load, session check, thread launch, koi bhi exception)
# yahan capture karte hain — taaki header ke chhote debug icon se poora
# startup trace copy karke dekha ja sake, chahe crash/redirect kitna bhi
# jaldi ho jaaye.
_STARTUP_LOG: list = []
_STARTUP_LOG_LOCK = threading.Lock()
_STARTUP_LOG_MAX  = 300

def _slog(msg: str, level: str = "info") -> None:
    """Thread-safe startup/diagnostic log line add karo. level: info|ok|warn|err"""
    try:
        t = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%H:%M:%S")
    except Exception:
        t = time.strftime("%H:%M:%S")
    line = {"t": t, "level": level, "msg": str(msg)}
    with _STARTUP_LOG_LOCK:
        _STARTUP_LOG.append(line)
        if len(_STARTUP_LOG) > _STARTUP_LOG_MAX:
            del _STARTUP_LOG[: len(_STARTUP_LOG) - _STARTUP_LOG_MAX]

def _slog_exception(where: str, exc: Exception) -> None:
    """Exception ko poori traceback ke saath log karo — copy-paste karne layak."""
    import traceback
    tb = traceback.format_exc()
    _slog(f"EXCEPTION in {where}: {exc}\n{tb}", level="err")

def _startup_log_snapshot() -> list:
    with _STARTUP_LOG_LOCK:
        return list(_STARTUP_LOG)

# ─── Global live-tick store (updated by WebSocket thread) ─────────────────────
_LIVE: dict = {
    "ltp":       None,
    "prev_close": None,
    "ts":        0,
    "source":    None,   # "ws" (Fyers WebSocket push) | "rest" (1s REST-poll fallback)
}
_LIVE_LOCK = threading.Lock()

# Latest tick JSON string — postMessage injector ise padh ke iframe ko bhejta hai
_LAST_TICK_JS: dict = {"json": ""}
_LAST_TICK_LOCK = threading.Lock()

# ─── Per-minute candle tracker — resets at each new minute boundary ────────────
_CANDLE: dict = {"minute": None, "open": None, "high": None, "low": None}
_CANDLE_LOCK = threading.Lock()

def _update_candle_ltp(ltp: float) -> None:
    """Feed one LTP tick into the running 1-minute candle."""
    now_sec      = int(time.time())
    minute_epoch = (now_sec // 60) * 60
    with _CANDLE_LOCK:
        if _CANDLE["minute"] != minute_epoch:
            _CANDLE["minute"] = minute_epoch
            _CANDLE["open"]   = ltp
            _CANDLE["high"]   = ltp
            _CANDLE["low"]    = ltp
        else:
            if ltp > (_CANDLE["high"] or ltp): _CANDLE["high"] = ltp
            if ltp < (_CANDLE["low"]  or ltp): _CANDLE["low"]  = ltp

def _set_candle_from_bar(minute_epoch: int, o: float, h: float, l: float, c: float) -> None:
    """Populate candle directly from a complete 1-min OHLC bar (REST path)."""
    with _CANDLE_LOCK:
        _CANDLE["minute"] = minute_epoch
        _CANDLE["open"]   = o
        _CANDLE["high"]   = h
        _CANDLE["low"]    = l

# ─── IST helper ───────────────────────────────────────────────────────────────
def _ist_now():
    return datetime.datetime.now(IST)

# ─── Credential helpers ───────────────────────────────────────────────────────
def load_creds() -> dict:
    if os.path.exists(CREDS_FILE):
        try:
            with open(CREDS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_creds(d: dict):
    with open(CREDS_FILE, "w") as f:
        json.dump(d, f)

# ─── OTP-based automated Fyers login ──────────────────────────────────────────
def fyers_send_otp(client_id: str, app_id: str) -> tuple[bool, str]:
    """Step 1: send OTP to user's registered mobile. Returns (ok, request_key_or_error)."""
    try:
        r = requests.post(
            "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
            json={"fy_id": client_id, "app_id": app_id.split("-")[0]},
            timeout=10,
        ).json()
        if r.get("s") == "ok" or "request_key" in r:
            return True, r["request_key"]
        return False, r.get("message", str(r))
    except Exception as e:
        return False, str(e)

def fyers_verify_otp(request_key: str, otp: str) -> tuple[bool, str]:
    """Step 2: verify OTP. Returns (ok, new_request_key_or_error)."""
    try:
        r = requests.post(
            "https://api-t2.fyers.in/vagator/v2/verify_otp",
            json={"request_key": request_key, "otp": otp},
            timeout=10,
        ).json()
        if r.get("s") == "ok" or "request_key" in r:
            return True, r["request_key"]
        return False, r.get("message", str(r))
    except Exception as e:
        return False, str(e)

def fyers_verify_pin(request_key: str, password: str) -> tuple[bool, str]:
    """Step 3: verify PIN/password. Returns (ok, token_or_error)."""
    pin_hash = hashlib.sha256(password.encode()).hexdigest()
    try:
        r = requests.post(
            "https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
            json={"request_key": request_key, "identity_type": "pin", "identifier": pin_hash},
            timeout=10,
        ).json()
        if r.get("s") == "ok" or ("data" in r and "token" in r.get("data", {})):
            return True, r["data"]["token"]
        return False, r.get("message", str(r))
    except Exception as e:
        return False, str(e)

def fyers_get_auth_code(token: str, client_id: str, app_id: str) -> tuple[bool, str]:
    """Step 4: exchange session token for auth_code."""
    try:
        payload = {
            "fyers_id":     client_id,
            "app_id":       app_id.split("-")[0],
            "redirect_uri": REDIRECT_URI,
            "appType":      "100",
            "code_challenge": "",
            "state":        "None",
            "scope":        "",
            "nonce":        "",
            "response_type": "code",
            "create_cookie": True,
        }
        r = requests.post(
            "https://api-t1.fyers.in/api/v3/token",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ).json()
        url = r.get("Url", "")
        if "auth_code=" in url:
            code = url.split("auth_code=")[1].split("&")[0]
            return True, code
        return False, r.get("message", str(r))
    except Exception as e:
        return False, str(e)

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
        creds      = load_creds()
        balance    = _BINANCE_META_CACHE["usdt_balance"]
        api_key    = creds.get("binance_api_key", "")
        secret_key = creds.get("binance_secret_key", "")
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

BN_OC_SYMBOL = "NSE:NIFTYBANK-INDEX"

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
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_EAPI_URL = "https://eapi.binance.com"

def _binance_server_time() -> int:
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/time", timeout=10)
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
        r = requests.get(url, headers=headers, timeout=20)
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

# ─── Binance FULL option chain (multi-strike, CE/PE grid) — WEBSOCKET LIVE ──
# Stack View 1 bottom-bar "⛓ Chain" panel ke liye — jab top-left symbol BTC ho
# to isi shape ka data Fyers wale option-chain jaisa hi (rows: strike/ce/pe)
# frontend ko milta hai, taaki wahi ek UI dono asset render kar sake.
#
# PEHLE: har 1 second REST se exchangeInfo + ticker + price teeno fetch hote
# the — Binance API limit cross hone ka risk. AB: sirf WebSocket se live data
# aata hai (mark price/bid/ask/greeks/IV + last trade + spot price), aur REST
# sirf 2 jagah, bahut kam frequency par:
#   • exchangeInfo (strikes/expiries list) — har 10 minute mein 1 baar
#     (BINANCE_OC_META_TTL) — ye rarely badalta hai.
#   • 24hr ticker (OI/Volume/Change% — WS pe koi public option-OI stream
#     nahi hai) — har 5 second mein 1 baar, POORE market ke liye EK hi call
#     (per-symbol nahi) — (BINANCE_OC_TICKER_TTL).
# Payload build karne wala background loop (_binance_oc_bg_loop) ab bilkul
# koi network call NAHI karta — sirf in-memory WS data se render karta hai.
try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None

BINANCE_OC_FILE          = "binance_optionchain.json"
BINANCE_OC_STRIKE_WINDOW = 10     # ATM ke dono taraf, HAR expiry ke liye itni strikes
BINANCE_OC_META_TTL      = 600    # exchangeInfo (strikes/expiries) refresh — 10 min
BINANCE_OC_TICKER_TTL    = 5      # 24hr ticker snapshot (OI/vol/chg%) refresh — 5 sec
BINANCE_SPOT_STALE_SEC   = 10     # spot WS tick se purana ho to REST se fresh price lo
BINANCE_MARK_STALE_SEC   = 15     # mark-price stream se 15 sec tak koi bhi tick na aaye to stale
BINANCE_TRADE_STALE_SEC  = 30     # trade stream sparse hoti hai, isliye thoda zyada margin

BINANCE_WS_MARK_URL  = "wss://fstream.binance.com/market/stream?streams=btcusdt@optionMarkPrice"
BINANCE_WS_TRADE_URL = "wss://fstream.binance.com/public/stream?streams=btcusdt@optionTrade"
BINANCE_WS_SPOT_URL  = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# symbol -> {mark,bid,ask,last,delta,gamma,theta,vega,iv,oi,chg,chgp,volume,vol_cum,ts}
_BN_LIVE_QUOTES = {}
_BN_LIVE_LOCK   = threading.Lock()

_BN_SPOT_PRICE = {"price": None, "ts": 0.0, "source": None}   # source: "ws" | "rest"
_BN_SPOT_LOCK  = threading.Lock()

# exchangeInfo se banaya gaya static-ish structure: expiry_epoch(ms) -> [{strike, ce_symbol, pe_symbol}]
_BN_CHAIN_META = {"expiries": [], "by_expiry": {}, "ts": 0.0}
_BN_CHAIN_META_LOCK = threading.Lock()

_BN_TICKER_LAST = {"ts": 0.0}

# ── Background-thread heartbeats — agar in mein se koi thread kisi
# unhandled exception se silently mar jaaye (Python thread crash ho jaaye
# to koi global alert nahi milta), to bhi frontend debug turant flag kar
# sake ki fulaan loop ruk gaya hai. Har loop apni iteration ke saath yahan
# apna timestamp likhta rehta hai — agar wo bahut purana ho jaaye, thread
# dead maano. ────────────────────────────────────────────────────────────
_BN_THREAD_HEARTBEAT = {"meta": 0.0, "ticker": 0.0, "payload": 0.0}

_BN_WS_STATE = {
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

# ── Watchdog support: live wsapp handles + force-reconnect on stale stream ──
# ping_interval/ping_timeout library ke bharose hain — kuch network/proxy
# setups mein TCP half-open ho sakta hai jahan ping bhi silently drop ho
# jaaye (socket "open" dikhta hai par data flow ruka hota hai). Independent
# watchdog thread yahan _*_last_msg_ts ko baahar se check karke, agar bahut
# zyada stale ho jaaye, socket ko force-close karta hai — jisse wsapp ka
# apna while-loop backoff ke saath reconnect kar leta hai.
_BN_WS_APPS = {"mark": None, "trade": None, "spot": None}
_BN_WS_APPS_LOCK = threading.Lock()
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
def _bn_refresh_option_meta(force: bool = False) -> tuple[bool, str]:
    now = time.time()
    with _BN_CHAIN_META_LOCK:
        if not force and (now - _BN_CHAIN_META["ts"]) < BINANCE_OC_META_TTL:
            return True, "cached"
    creds = load_creds()
    api_key = creds.get("binance_api_key", "")
    ok, info = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/exchangeInfo", {}, api_key, signed=False)
    if not ok:
        return False, f"exchangeInfo fail: {info}"
    symbols = info.get("optionSymbols", [])
    btc_syms = [s for s in symbols if s.get("underlying", "") == "BTCUSDT"]
    if not btc_syms:
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
    return True, "refreshed"

def _binance_oc_meta_bg_loop():
    while True:
        try:
            _bn_refresh_option_meta()
        except Exception as e:
            _BN_WS_STATE["last_error"] = f"meta loop: {e}"
        _BN_THREAD_HEARTBEAT["meta"] = time.time()
        time.sleep(30)  # TTL check ke liye baar-baar wake, actual REST call sirf TTL cross hone par

# ── Step 2: 24hr ticker snapshot — OI/Volume/Change% (WS pe available nahi) ─
# POORE market ke liye EK call (symbol param nahi diya), per-symbol nahi.
def _bn_refresh_ticker_snapshot():
    now = time.time()
    if (now - _BN_TICKER_LAST["ts"]) < BINANCE_OC_TICKER_TTL:
        return
    _BN_TICKER_LAST["ts"] = now
    creds = load_creds()
    api_key = creds.get("binance_api_key", "")
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
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
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
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
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
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
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

# ── Step 6: payload builder — SABHI expiries, PURE in-memory (no network) ──
def _bn_ws_status_snapshot(spot_source=None, spot_age=None, expiries_count=None) -> dict:
    """Har jagah se same tarike se WS/thread diagnostics nikalne ke liye —
    isse debug ko pata chal sakta hai ki EXACTLY kahan atka hai, chahe poora
    option-chain payload abhi ban hi na paaya ho (jaisa 'metadata load ho
    raha hai' ya 'spot abhi available nahi' wale early-error cases). Pehle
    ye sirf success path mein banta tha — error paths mein bilkul khaali reh
    jaata tha, isliye debug popup ko root cause pata hi nahi chal pata tha."""
    now = time.time()
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

_BINANCE_OC_LAST_PAYLOAD = {"data": None, "ts": 0.0}
_BINANCE_OC_LAST_PAYLOAD_LOCK = threading.Lock()

def _binance_oc_bg_loop():
    """Ab koi network call NAHI karta — sirf WS threads ke already-updated
    in-memory data se payload rebuild karta hai, har 1 second. Isi wajah se
    1s interval bilkul safe hai (koi REST rate-limit risk nahi)."""
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
        try:
            with open(BINANCE_OC_FILE, "w") as f:
                json.dump(payload, f)
        except Exception:
            pass
        time.sleep(1)

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
        ok_d, depth = _binance_call(BINANCE_EAPI_URL, "/eapi/v1/depth", {"symbol": symbol, "limit": 5}, "")
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
            data = fyers_get_option_chain(creds["app_id"], creds["access_token"])
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

def _option_chain_bg_loop():
    while True:
        try:
            payload = refresh_option_chain_cache()
        except Exception as e:
            payload = {"error": f"bg loop exception: {e}"}
        with _OC_LAST_PAYLOAD_LOCK:
            _OC_LAST_PAYLOAD.update({"data": payload, "ts": time.time()})
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


def _write_login_log(payload: dict, status_code: int, response: dict):
    """Write login attempt details to login_debug.json for inspection."""
    try:
        safe_payload = {k: ("***" if k == "code" else v) for k, v in payload.items()}
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S IST", time.localtime()),
            "request": safe_payload,
            "http_status": status_code,
            "response": response,
        }
        with open("login_debug.json", "w") as f:
            json.dump(entry, f, indent=2)
    except Exception:
        pass

# ─── Fully automated Fyers login (TOTP-based, zero user input) ────────────────
def auto_fyers_login() -> tuple[bool, str, dict]:
    """Auto-login using stored client_id + password + TOTP secret.
    Returns (ok, msg, step_log) where step_log has each API step result."""
    creds = load_creds()
    client_id   = creds.get("client_id",    DEFAULT_CLIENT_ID)
    password    = creds.get("password",     DEFAULT_PASSWORD)
    totp_secret = creds.get("totp_secret",  "")
    app_id      = creds.get("app_id",       DEFAULT_APP_ID)
    secret_key  = creds.get("secret_key",   DEFAULT_SECRET)

    log = {}

    if not totp_secret:
        return False, "TOTP secret not configured", log

    # Step 1: Send OTP
    ok1, rkey = fyers_send_otp(client_id, app_id)
    log["step1_send_otp"] = {"ok": ok1, "result": rkey}
    if not ok1:
        _write_totp_log(log)
        return False, f"Step1 Send OTP failed: {rkey}", log

    # Step 2: Verify TOTP
    totp_code = pyotp.TOTP(totp_secret).now()
    log["step2_totp_code_used"] = totp_code
    ok2, rkey2 = fyers_verify_otp(rkey, totp_code)
    log["step2_verify_otp"] = {"ok": ok2, "result": rkey2}
    if not ok2:
        _write_totp_log(log)
        return False, f"Step2 TOTP verify failed: {rkey2}", log

    # Step 3: Verify PIN/password
    ok3, token = fyers_verify_pin(rkey2, password)
    log["step3_verify_pin"] = {"ok": ok3, "result": token if not ok3 else "***token***"}
    if not ok3:
        _write_totp_log(log)
        return False, f"Step3 PIN verify failed: {token}", log

    # Step 4: Get auth_code
    ok4, auth_code = fyers_get_auth_code(token, client_id, app_id)
    log["step4_get_authcode"] = {"ok": ok4, "result": auth_code[:20] + "..." if ok4 and len(auth_code) > 20 else auth_code}
    if not ok4:
        _write_totp_log(log)
        return False, f"Step4 Auth code failed: {auth_code}", log

    # Step 5: Get access_token
    ok5, access_token, raw5 = fyers_get_access_token(app_id, secret_key, auth_code)
    log["step5_validate_authcode"] = {"ok": ok5, "response": raw5}
    _write_totp_log(log)
    if not ok5:
        return False, f"Step5 Access token failed: {access_token}", log

    # Save new token
    creds["access_token"] = access_token
    save_creds(creds)
    _sess_cache.update({"active": True, "ts": time.time()})
    # session_state yahan set nahi kar sakte (background thread) — caller karega
    return True, access_token, log


def _write_totp_log(log: dict):
    """Save TOTP auto-login step log to totp_debug.json."""
    try:
        log["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open("totp_debug.json", "w") as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass


# ─── Token expiry monitor (background) ─────────────────────────────────────────
# NOTE: Fyers vagator login API is IP-restricted (blocks cloud/VPS IPs).
# So we only MONITOR expiry and set a flag — user does a quick 15-sec re-auth.
_TOKEN_STATUS: dict = {"expired": False, "checked_at": 0.0, "running": False}
_TOKEN_STATUS_LOCK = threading.Lock()

_SMS_SENT_FLAG: dict = {"last_sent": 0.0}  # avoid duplicate SMS within 1 hour

def _send_sms_alert(message: str) -> bool:
    """Send SMS via Fast2SMS (Indian SMS API)."""
    api_key = FAST2SMS_KEY
    if not api_key:
        return False
    # Rate-limit: only send once per hour
    now = time.time()
    if now - _SMS_SENT_FLAG["last_sent"] < 3600:
        return False
    creds = load_creds()
    phone = creds.get("alert_phone", "7018093451")
    try:
        r = requests.get(
            "https://www.fast2sms.com/dev/bulkV2",
            headers={"authorization": api_key},
            params={
                "route":   "q",
                "numbers": str(phone),
                "message": message,
                "flash":   0,
            },
            timeout=10,
        )
        data = r.json()
        ok = data.get("return", False)
        if ok:
            _SMS_SENT_FLAG["last_sent"] = now
        return ok
    except Exception:
        return False


def _token_monitor_loop():
    """Checks Fyers token validity every 5 min. Sets expired flag and sends SMS alert."""
    with _TOKEN_STATUS_LOCK:
        if _TOKEN_STATUS["running"]:
            return
        _TOKEN_STATUS["running"] = True

    while True:
        try:
            creds = load_creds()
            if not creds.get("access_token"):
                time.sleep(60)
                continue

            headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
            today = _ist_now().strftime("%Y-%m-%d")
            try:
                res = requests.get(
                    "https://api-t1.fyers.in/data/history",
                    headers=headers,
                    params={"symbol": "NSE:NIFTYBANK-INDEX", "resolution": "D",
                            "date_format": "1", "range_from": today, "range_to": today, "cont_flag": "1"},
                    timeout=8,
                ).json()
                still_active = res.get("s") == "ok"
            except Exception:
                still_active = True  # network glitch, assume ok

            with _TOKEN_STATUS_LOCK:
                _TOKEN_STATUS["expired"] = not still_active
                _TOKEN_STATUS["checked_at"] = time.time()

            # Reset session cache so sidebar reflects truth
            if not still_active:
                _sess_cache.update({"active": False, "ts": 0.0})
                # Write sentinel so next rerun clears _force_active
                try:
                    with open(".token_expired_flag", "w") as _f:
                        _f.write("1")
                except Exception:
                    pass
                # Send SMS alert (once per hour max)
                _send_sms_alert(
                    "BankNifty Dashboard Alert: Fyers token expired! "
                    "Please re-login at your dashboard to restore live data."
                )

            time.sleep(300)  # check every 5 minutes
        except Exception:
            time.sleep(60)


def _extract_auth_code(url_or_code: str) -> str:
    """Extract auth_code from a full Google redirect URL or return as-is."""
    import urllib.parse
    s = url_or_code.strip()
    if s.startswith("http"):
        parsed = urllib.parse.urlparse(s)
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get("auth_code", [s])[0]
    return s

# ─── Session check ─────────────────────────────────────────────────────────────
_sess_cache = {"active": False, "ts": 0.0}

def is_session_active() -> bool:
    """Token exist karna + profile API ok = session active.
    Market band hone par bhi False nahi karega."""
    now = time.time()

    # 1. token_monitor ne expire flag set kiya? clear _force_active
    if os.path.exists(".token_expired_flag"):
        try:
            os.remove(".token_expired_flag")
        except Exception:
            pass
        st.session_state["_force_active"] = False
        _sess_cache.update({"active": False, "ts": 0.0})

    # 2. login ke turant baad force-active flag
    if st.session_state.get("_force_active"):
        _sess_cache.update({"active": True, "ts": now})
        return True

    # 2. fresh cache
    if now - _sess_cache["ts"] < 120:
        return _sess_cache["active"]

    creds = load_creds()
    if not creds.get("access_token"):
        _sess_cache.update({"active": False, "ts": now})
        return False

    # 3. Profile endpoint use karo — market hours se independent
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    try:
        res = requests.get(
            "https://api-t1.fyers.in/api/v3/profile",
            headers=headers, timeout=4,
        ).json()
        active = res.get("s") == "ok" or res.get("code") == 200
        if not active:
            # fallback: history endpoint — "no_data" = market closed but token valid
            today = _ist_now().strftime("%Y-%m-%d")
            res2 = requests.get(
                "https://api-t1.fyers.in/data/history",
                headers=headers,
                params={"symbol": "NSE:NIFTYBANK-INDEX", "resolution": "D",
                        "date_format": "1", "range_from": today,
                        "range_to": today, "cont_flag": "1"},
                timeout=4,
            ).json()
            active = res2.get("s") in ("ok", "no_data")
    except Exception:
        active = True  # network glitch -> assume ok

    _sess_cache.update({"active": active, "ts": now})
    return active

# ─── Stack View 2: GitHub se .gz data load + resample ────────────────────────
import gzip as _gzip
import io as _io

_SV2_CACHE: dict = {}   # in-memory cache taaki har rerun pe re-read na ho

# GitHub raw URLs — repo: krishna123814/My-engine, branch: main
_GH_BASE    = "https://raw.githubusercontent.com/krishna123814/My-engine/main"
_GH_BN_URL  = f"{_GH_BASE}/banknifty_5m_csv_json.gz"
_GH_BTC_URL = f"{_GH_BASE}/Bitcoin_BTCUSDT_IST_5m_json.gz"

def _sv2_fetch_gz_from_url(url: str) -> list:
    """GitHub raw URL se .gz fetch karo, decompress karo, JSON parse karo."""
    try:
        resp = requests.get(url, timeout=90)
        resp.raise_for_status()
        with _gzip.open(_io.BytesIO(resp.content), "rb") as f:
            data = json.load(f)
        # Both formats supported: {"meta":..,"data":[..]} or plain list
        return data["data"] if isinstance(data, dict) else data
    except Exception:
        return []

def _sv2_load_bn_gz() -> list:
    """BankNifty 1m candles — GitHub se fetch karo (cached)."""
    if "bn_raw" in _SV2_CACHE:
        return _SV2_CACHE["bn_raw"]
    rows = _sv2_fetch_gz_from_url(_GH_BN_URL)
    if not rows:
        _SV2_CACHE["bn_err"] = "GITHUB_FETCH_FAILED"
        return []
    _SV2_CACHE["bn_raw"] = rows
    return rows

def _sv2_load_btc_gz() -> list:
    """BTC 5m candles — GitHub se fetch karo (cached)."""
    if "btc_raw" in _SV2_CACHE:
        return _SV2_CACHE["btc_raw"]
    rows = _sv2_fetch_gz_from_url(_GH_BTC_URL)
    if not rows:
        _SV2_CACHE["btc_err"] = "GITHUB_FETCH_FAILED"
        return []
    _SV2_CACHE["btc_raw"] = rows
    return rows

## ── IST-naive timestamp constants ───────────────────────────────────────────
## .gz data mein timestamps IST-naive hain: 9:15 IST ko 09:15 UTC ki tarah
## store kiya gaya hai. LightweightCharts real UTC chahta hai (IST timezone ke
## sath display karta hai: UTC + 5:30). Fix: har output time se 19800 subtract karo.
_IST_NAIVE_OFFSET = 19800   # 5.5 * 3600 — IST-naive → real UTC conversion
_SESSION_START    = 33300   # 9:15 IST = 9*3600 + 15*60 seconds from midnight
_SESSION_END      = 55800   # 15:30 IST = 15*3600 + 30*60 seconds from midnight

def _sv2_fill_bn_gaps(rows: list) -> list:
    """BN 5m raw data mein missing 5-min slots (no-trade gaps) forward-fill karo.

    Vendor ka 5m data kai jagah beech mein slots miss karta hai (illiquid /
    no-trade moments). Agar poore 125m/etc bucket ke saare 5m-slots missing
    hon, to us bucket ka candle hi resample output se gayab ho jaata hai —
    chart mein genuine "candles ke beech gap" dikhta hai. Fix: har trading
    session (9:15–15:29:XX IST, 375 minutes/day = 75 slots of 5m) ke liye
    poori 5-min sequence banao — jo slot missing ho use pichle available
    close se flat candle (o=h=l=c=prev_close) se bhar do. Isse koi bhi
    downstream resample bucket kabhi khaali nahi rahega.

    NOTE: raw .gz ab 5m granularity hai (pehle 1m thi) — isliye step 300
    seconds (5 min) hai, 60 seconds (1 min) nahi.
    """
    if not rows:
        return rows
    by_day: dict = {}
    for r in rows:
        t   = r["t"]
        mod = t % 86400
        if mod < _SESSION_START or mod >= _SESSION_END:
            continue
        day_start = t - mod
        by_day.setdefault(day_start, {})[t] = r

    out = []
    prev_close = None
    for day_start in sorted(by_day.keys()):
        day_rows = by_day[day_start]
        for sec_off in range(_SESSION_START, _SESSION_END, 300):
            t = day_start + sec_off
            if t in day_rows:
                r = day_rows[t]
                out.append(r)
                prev_close = r["c"]
            elif prev_close is not None:
                out.append({"t": t, "o": prev_close, "h": prev_close,
                            "l": prev_close, "c": prev_close})
            # agar dataset ke bilkul shuru mein hi pehla slot missing ho
            # (prev_close abhi None hai), to use silently skip karo — us
            # point tak koi reference close available hi nahi hai.
    return out

def _sv2_resample_bn_intraday(rows: list, tf_min: int) -> list:
    """BN 5m data ko intraday TF mein resample karo.

    .gz timestamps IST-naive hain (9:15 IST stored as 09:15 UTC epoch).
    Per-day anchor: har din 9:15 IST se bucket 0 start hota hai.
    Output timestamps real UTC mein (LightweightCharts + IST timezone ke liye).
    Session filter: sirf 9:15–15:30 IST ke candles.

    NOTE: raw .gz ab 5m granularity hai (pehle 1m thi). Isliye passthrough
    (bina bucketing) sirf tf_min<=5 par hota hai — 125m ab yahin se actual
    bucket-resample hoke banta hai (375-min session / 125m = 3 buckets/din).
    """
    sec = tf_min * 60
    if tf_min <= 5:
        out = []
        for r in rows:
            mod = r["t"] % 86400
            if mod < _SESSION_START or mod >= _SESSION_END:
                continue
            out.append({"time": r["t"] - _IST_NAIVE_OFFSET,
                        "open": r["o"], "high": r["h"],
                        "low":  r["l"], "close": r["c"]})
        return out

    buckets: dict = {}
    for r in rows:
        t       = r["t"]
        mod     = t % 86400                        # seconds since IST midnight
        if mod < _SESSION_START or mod >= _SESSION_END:
            continue
        day_start   = t - mod                      # IST-naive midnight of this day
        since_open  = mod - _SESSION_START         # seconds elapsed since 9:15 IST
        bucket_idx  = since_open // sec            # which bucket (0-based per day)
        bucket_sec  = _SESSION_START + bucket_idx * sec  # seconds from midnight
        key_utc     = (day_start + bucket_sec) - _IST_NAIVE_OFFSET  # real UTC

        if key_utc not in buckets:
            buckets[key_utc] = {"time": key_utc,
                                "open": r["o"], "high": r["h"],
                                "low":  r["l"], "close": r["c"]}
        else:
            b = buckets[key_utc]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]
    return sorted(buckets.values(), key=lambda x: x["time"])

def _sv2_resample_bn_daily(rows: list, n_days: int = 1) -> list:
    """BN 1m data ko daily / multi-day candles mein resample karo.

    Har trading day ka open = 9:15 IST (real UTC: 3:45 AM = 13500s from UTC midnight).
    .gz timestamps IST-naive hain — 19800 subtract karo real UTC ke liye.
    """
    day_buckets: dict = {}
    for r in rows:
        t   = r["t"]
        mod = t % 86400
        if mod < _SESSION_START or mod >= _SESSION_END:
            continue
        day_start = t - mod                              # IST-naive midnight
        key_utc   = (day_start + _SESSION_START) - _IST_NAIVE_OFFSET  # 3:45 UTC

        if key_utc not in day_buckets:
            day_buckets[key_utc] = {"time": key_utc,
                                    "open": r["o"], "high": r["h"],
                                    "low":  r["l"], "close": r["c"]}
        else:
            b = day_buckets[key_utc]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]

    days = sorted(day_buckets.values(), key=lambda x: x["time"])
    if n_days <= 1:
        return days

    out = []
    for i in range(0, len(days), n_days):
        chunk = days[i:i + n_days]
        if not chunk:
            break
        out.append({
            "time":  chunk[0]["time"],
            "open":  chunk[0]["open"],
            "high":  max(c["high"] for c in chunk),
            "low":   min(c["low"]  for c in chunk),
            "close": chunk[-1]["close"],
        })
    return out

def _sv2_resample_btc(rows: list, tf_min: int) -> list:
    """BTC 5m data ko UTC-anchored TF mein resample karo (24/7 crypto).

    NOTE: sirf 8H (intraday, tf_min < 1440) ke liye use karo (160m band kar
    diya gaya hai). Daily+
    (1D/3D/9D/27D) ke liye _sv2_resample_btc_daily() use karo — wo epoch
    (1970) anchor ki jagah data ke apne Day-1 se index-based chunking
    karta hai, jisse 3D/9D/27D hamesha same date se sync start hote hain.

    NOTE: raw .gz 5m granularity hai (BankNifty 1m par hai, BTC 5m par
    wapas revert kar diya gaya hai). Isliye passthrough (bina bucketing)
    tf_min<=5 par hota hai.
    """
    if tf_min <= 5:
        return [{"time": r["t"], "open": r["o"], "high": r["h"],
                 "low": r["l"], "close": r["c"]} for r in rows]
    sec = tf_min * 60
    buckets: dict = {}
    for r in rows:
        key = (r["t"] // sec) * sec
        if key not in buckets:
            buckets[key] = {"time": key, "open": r["o"], "high": r["h"],
                            "low": r["l"], "close": r["c"]}
        else:
            b = buckets[key]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]
    return sorted(buckets.values(), key=lambda x: x["time"])

def _sv2_resample_btc_daily(rows: list, n_days: int = 1) -> list:
    """BTC 5m data ko daily / multi-day candles mein resample karo.

    Crypto 24/7 hai (koi session/weekday filter nahi) — sirf UTC
    calendar-day buckets banao, phir un dailies ko INDEX se (BN ke
    _sv2_resample_bn_daily jaisa: array index-0 = data ka pehla din)
    groups of n_days mein chunk karo.

    Ye zaroori hai kyunki purana _sv2_resample_btc() epoch (1 Jan 1970)
    se seedha `(t // (n_days*86400)) * (n_days*86400)` karta tha — us
    approach mein 3D/9D/27D ke cycle-boundaries data-start (2017) se
    alag-alag remainder dete hain, isliye teeno TF alag-alag calendar
    dates se start hote the. Index-based chunking (yahan) sabko data ke
    Day-1 se hi sync rakhta hai — BN aur SV2 replay (_liveAggregateDailyPlus,
    jo already index-based hai) dono ke saath consistent.
    """
    day_buckets: dict = {}
    for r in rows:
        t = r["t"]
        day_start = (t // 86400) * 86400          # UTC calendar-day start
        if day_start not in day_buckets:
            day_buckets[day_start] = {"time": day_start,
                                       "open": r["o"], "high": r["h"],
                                       "low": r["l"], "close": r["c"]}
        else:
            b = day_buckets[day_start]
            b["high"]  = max(b["high"],  r["h"])
            b["low"]   = min(b["low"],   r["l"])
            b["close"] = r["c"]

    days = sorted(day_buckets.values(), key=lambda x: x["time"])
    if n_days <= 1:
        return days

    out = []
    for i in range(0, len(days), n_days):
        chunk = days[i:i + n_days]
        if not chunk:
            break
        out.append({
            "time":  chunk[0]["time"],
            "open":  chunk[0]["open"],
            "high":  max(c["high"] for c in chunk),
            "low":   min(c["low"]  for c in chunk),
            "close": chunk[-1]["close"],
        })
    return out

def _sv2_date_to_anchor_epoch(d) -> int:
    """Calendar date (datetime.date) ko wahi 'IST-naive-as-UTC' epoch scheme
    mein convert karo jisme SV2 .gz data ke timestamps stored hain (jaise
    9:15 IST ko 09:15 UTC ki tarah store kiya gaya hai — is file ke top
    comments dekho: _IST_NAIVE_OFFSET)."""
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp())

# ─── Mobile ke liye max candles per TF (chunked inject) ───────────────────────
# Ab BankNifty aur BTC ke liye ALAG-ALAG default hain (pehle "1D/3D/9D/27D"
# jaisi daily labels dono asset ke beech shared hoti thi — ab har asset ki
# apni settings hain). Ye "factory defaults" hain; asli effective values
# _sv2_get_max() se aati hain, jo isme user ke saved overrides (bottom-bar
# ke 📦 Chunk icon se set kiye gaye) merge karta hai.
_SV2_MAX_BN_DEFAULT = {
    "5m_raw": 12000,
}
_SV2_MAX_BTC_DEFAULT = {
    # 160m removed (no longer used); 8H/1D/3D/9D/27D are now FULL-HISTORY
    # (no chunking/trim) for BTC — see _build_sv2_data(). Only 5m_raw (used
    # for forming-candle interpolation smoothness) is still size-limited.
    "5m_raw": 64000,
}
# Safe min/max bounds per label — user chahe jitna bhi likhe, isi range mein
# clamp ho jaayega (mobile hang / bahut kam data dono se bachne ke liye).
_SV2_MAX_BOUNDS = {
    "5m_raw": (500, 120000),
}

SV2_CHUNK_SETTINGS_FILE = "sv2_chunk_settings.json"
SV2_LAST_DATE_FILE      = "sv2_last_dates.json"

def load_sv2_last_dates() -> dict:
    """Pichli baar starting-page pe select ki gayi chunk dates (asset-wise) —
    ye disk pe save hoti hain taaki naya browser session khulne par bhi
    date-input pehle se usi date pe fill mile (load abhi bhi automatic
    NAHI hota, button dabana zaroori hai — sirf field pre-filled rehti hai)."""
    if os.path.exists(SV2_LAST_DATE_FILE):
        try:
            with open(SV2_LAST_DATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sv2_last_dates(d: dict):
    with open(SV2_LAST_DATE_FILE, "w") as f:
        json.dump(d, f)

def _sv2_default_date(asset: str):
    """Date-input widget ka default value: is session mein already select
    kiya ho to wahi, warna last-saved (disk se), warna aaj ki date."""
    sess_key = f"_sv2_anchor_date_{asset}"
    if st.session_state.get(sess_key):
        return st.session_state[sess_key]
    _saved = load_sv2_last_dates().get(asset)
    if _saved:
        try:
            return datetime.date.fromisoformat(_saved)
        except Exception:
            pass
    return datetime.date.today()

def _sv2_remember_dates(bn_date, btc_date):
    """Har rerun pe current widget selection ko disk pe likh do — isse agli
    baar (naya session/browser refresh) pe bhi wahi date pehle se fill milti
    hai, chahe 'Chart Kholo' button dabaya ho ya nahi."""
    save_sv2_last_dates({"bn": str(bn_date), "btc": str(btc_date)})

def load_sv2_chunk_settings() -> dict:
    """Saved overrides load karo: {"bn": {...}, "btc": {...}}."""
    if os.path.exists(SV2_CHUNK_SETTINGS_FILE):
        try:
            with open(SV2_CHUNK_SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sv2_chunk_settings(d: dict):
    with open(SV2_CHUNK_SETTINGS_FILE, "w") as f:
        json.dump(d, f)

def _sv2_get_max(asset: str) -> dict:
    """Asset ('bn'/'btc') ki effective per-TF candle-count limits: factory
    default + user ke saved overrides (clamped to _SV2_MAX_BOUNDS). Session
    ke andar cache hota hai taaki har rerun pe file dobara na padhni pade."""
    cache_key = f"_sv2_max_eff_{asset}"
    if cache_key not in st.session_state:
        default = _SV2_MAX_BN_DEFAULT if asset == "bn" else _SV2_MAX_BTC_DEFAULT
        saved   = load_sv2_chunk_settings().get(asset, {})
        eff = dict(default)
        for k, v in saved.items():
            if k in eff:
                try:
                    lo, hi = _SV2_MAX_BOUNDS.get(k, (10, 200000))
                    eff[k] = max(lo, min(hi, int(v)))
                except Exception:
                    pass
        st.session_state[cache_key] = eff
    return st.session_state[cache_key]

def _sv2_trim(data: list, label: str, anchor_epoch: int = None, asset: str = "bn") -> list:
    """Chunk select karo (mobile hang prevention, per-asset per-TF limits).

    - anchor_epoch None  → purana default: sirf LAST N candles (recent data).
    - anchor_epoch diya  → us date ke aas-paas se window nikaalo: thoda
      history anchor se PEHLE ka (context ke liye) aur baaki (zyada hissa)
      anchor ke BAAD ka (kyunki replay ko aage badhne ke liye future candles
      chahiye) — total count wahi N jo _sv2_get_max(asset) se aata hai.
    """
    n = _sv2_get_max(asset).get(label, 2000)
    if len(data) <= n:
        return data
    if anchor_epoch is None:
        return data[-n:]
    # Binary search: pehla index jahan bar['time'] >= anchor_epoch
    lo, hi = 0, len(data)
    while lo < hi:
        mid = (lo + hi) // 2
        if data[mid]["time"] < anchor_epoch:
            lo = mid + 1
        else:
            hi = mid
    idx    = lo
    before = n // 4                       # ~25% budget anchor se pehle
    start  = max(0, idx - before)
    end    = min(len(data), start + n)
    start  = max(0, end - n)              # series ke end tak clip ho to peeche khiskao
    return data[start:end]

def _sv2_to_js(data: list) -> str:
    """List of dicts → compact JSON string for inline JS."""
    return json.dumps(data, separators=(",", ":"))

def _build_sv2_data(bn_anchor: int = None, btc_anchor: int = None) -> dict:
    """Dono .gz files se sab TFs ka data return karo, ek diye gaye date
    ("chunk") ke aas-paas trim karke.

    IMPORTANT: fetch + resample (GitHub se .gz download + TF resampling)
    sirf EK BAAR hota hai process/session mein pehli dafa (result
    _SV2_CACHE["bn_tfs_full"] / ["btc_tfs_full"] mein cache hota hai — full,
    untrimmed). Uske baad, chahe user koi bhi naya "chunk date" chune, sirf
    halka-sa trim step (_sv2_trim) dobara chalta hai — GitHub fetch ya
    resample dobara NAHI hota.
    """
    if "bn_tfs_full" not in _SV2_CACHE or "btc_tfs_full" not in _SV2_CACHE:
        bn_raw  = _sv2_fill_bn_gaps(_sv2_load_bn_gz())
        btc_raw = _sv2_load_btc_gz()

        _SV2_CACHE["bn_tfs_full"] = {
            "5m_raw": _sv2_resample_bn_intraday(bn_raw, 5),
            "125m": _sv2_resample_bn_intraday(bn_raw,  125),
            "1D":   _sv2_resample_bn_daily   (bn_raw,  1),
            "3D":   _sv2_resample_bn_daily   (bn_raw,  3),
            "9D":   _sv2_resample_bn_daily   (bn_raw,  9),
            "27D":  _sv2_resample_bn_daily   (bn_raw,  27),
        }
        _SV2_CACHE["btc_tfs_full"] = {
            "5m_raw": _sv2_resample_btc(btc_raw, 5),
            "8H":   _sv2_resample_btc(btc_raw, 480),
            "1D":   _sv2_resample_btc_daily(btc_raw, 1),
            "3D":   _sv2_resample_btc_daily(btc_raw, 3),
            "9D":   _sv2_resample_btc_daily(btc_raw, 9),
            "27D":  _sv2_resample_btc_daily(btc_raw, 27),
        }

    bn_tfs  = _SV2_CACHE["bn_tfs_full"]
    btc_tfs = _SV2_CACHE["btc_tfs_full"]
    # Ab BN aur BTC dono ke liye SAME pattern: sirf "5m_raw" (forming-candle
    # interpolation ke liye) trimmed/chunked rehta hai; baaki saare TFs
    # (125m/1D/3D/9D/27D for BN, 8H/1D/3D/9D/27D for BTC) ab FULL-HISTORY
    # untrimmed jaate hain — koi bar-replay chunk-limit nahi (bade TFs hain,
    # candle-count kam hoti hai, phone hang nahi karta).
    agg = {
        "bn":  {k: (_sv2_trim(v, k, bn_anchor, "bn") if k == "5m_raw" else v)
                for k, v in bn_tfs.items()},
        "btc": {k: (_sv2_trim(v, k, btc_anchor, "btc") if k == "5m_raw" else v)
                for k, v in btc_tfs.items()},
    }
    return agg

# ─── Fyers historical data ─────────────────────────────────────────────────────
def _fyers_history(resolution: str, from_date: str, to_date: str) -> list:
    creds = load_creds()
    if not creds.get("access_token"):
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol":     "NSE:NIFTYBANK-INDEX",
        "resolution": resolution,
        "date_format": "1",
        "range_from": from_date,
        "range_to":   to_date,
        "cont_flag":  "1",
    }
    try:
        res = requests.get(
            "https://api-t1.fyers.in/data/history",
            headers=headers, params=params, timeout=15,
        ).json()
        if res.get("s") == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]]
                    for c in res.get("candles", [])]
    except Exception:
        pass
    return []

def fetch_bn_intraday(interval_mins: int) -> list:
    # Fyers TF ke hisaab se max safe range:
    # 1m  → 10 days, 5m → 30 days, 15m → 60 days, 45m → 90 days
    _days = {1: 10, 5: 30, 15: 60, 45: 90}.get(interval_mins, 30)
    today  = _ist_now().strftime("%Y-%m-%d")
    from_d = (_ist_now() - datetime.timedelta(days=_days)).strftime("%Y-%m-%d")
    return _fyers_history(str(interval_mins), from_d, today)

def _fyers_history_chunk(resolution: str, from_date: str, to_date: str) -> list:
    """Same as _fyers_history but doesn't read creds again (for chunked calls)."""
    creds = load_creds()
    if not creds.get("access_token"):
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol": "NSE:NIFTYBANK-INDEX", "resolution": resolution,
        "date_format": "1", "range_from": from_date, "range_to": to_date, "cont_flag": "1",
    }
    try:
        res = requests.get("https://api-t1.fyers.in/data/history",
                           headers=headers, params=params, timeout=15).json()
        if res.get("s") == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]] for c in res.get("candles", [])]
    except Exception:
        pass
    return []

def load_bn_daily() -> list:
    """Fetch BankNifty daily candles 2020→now in yearly chunks (~1200 bars).

    FIX: agar disk pe purana (stale bhi) cache maujood hai, use hamesha
    fallback ke roop mein yaad rakho. Fresh Fyers fetch fail ho ya partial
    aaye (rate-limit / token expiry / network hiccup — jo intermittently
    hota rehta hai), to empty/half list return karke chart blank karne ke
    bajaye purana valid cache hi return karo. Isse 1D chart kabhi blank
    nahi dikhega — worst case thoda stale rahega, jab tak fresh fetch phir
    se successful na ho jaye.
    """
    stale_fallback: list = []
    if os.path.exists(BN_DAILY_CACHE):
        try:
            with open(BN_DAILY_CACHE) as f:
                cache = json.load(f)
            stale_fallback = cache.get("data", [])
            if time.time() - cache.get("ts", 0) < DAILY_CACHE_TTL:
                return stale_fallback
        except Exception:
            pass

    today = _ist_now()
    today_str = today.strftime("%Y-%m-%d")
    start_year = 2020
    cur_year   = today.year

    # Build half-year chunks (Fyers allows max ~1yr range)
    chunks: list[tuple[str, str]] = []
    for yr in range(start_year, cur_year + 1):
        chunks.append((f"{yr}-01-01", f"{yr}-06-30"))
        chunks.append((f"{yr}-07-01", f"{yr}-12-31"))

    all_candles: list = []
    seen_times: set  = set()
    for i, (from_d, to_d) in enumerate(chunks):
        if from_d > today_str:
            break
        actual_to = min(to_d, today_str)
        if i > 0:
            time.sleep(0.25)  # avoid Fyers rate-limit on rapid chunk calls
        chunk = _fyers_history_chunk("D", from_d, actual_to)
        for c in chunk:
            if c[0] not in seen_times:
                seen_times.add(c[0])
                all_candles.append(c)

    all_candles.sort(key=lambda x: x[0])

    # Normalize timestamps to 9:15 AM IST of each IST calendar day.
    # Fyers may return midnight UTC or any session-start epoch — normalize to
    # 3:45 AM UTC (= 9:15 AM IST) so chart.html resample() stays consistent.
    _IST_OFF   = 19800          # 5.5 * 3600
    _NSE_OPEN  = 33300          # 9:15 AM = 9*3600+15*60 seconds from IST midnight
    normalized = []
    for c in all_candles:
        t_ms   = int(c[0])
        t_sec  = t_ms // 1000
        ist_sec        = t_sec + _IST_OFF
        ist_midnight   = ist_sec - (ist_sec % 86400)   # IST midnight of that day
        t_fixed        = (ist_midnight - _IST_OFF) + _NSE_OPEN  # 9:15 AM IST in UTC epoch
        normalized.append([t_fixed * 1000] + list(c[1:]))
    all_candles = normalized

    # Fresh fetch ka sanity check: agar ye purane cache se kaafi chhota hai
    # (e.g. Fyers ne sirf kuch chunks return kiye, baaki rate-limit/timeout
    # se fail hue), to ise "partial failure" maano — purane pura cache ko
    # hi trust karo, chart ko incomplete/blank mat karo.
    if stale_fallback and len(all_candles) < len(stale_fallback) * 0.9:
        return stale_fallback

    if all_candles:
        try:
            with open(BN_DAILY_CACHE, "w") as f:
                json.dump({"ts": time.time(), "data": all_candles}, f)
        except Exception:
            pass
        return all_candles

    # Fresh fetch poori tarah fail ho gaya (empty) — purana cache hi wapas do
    # taaki 1D chart kabhi blank na dikhe.
    return stale_fallback

# ─── BTC (Binance) ────────────────────────────────────────────────────────────
def fetch_btc(interval: str = "1m", limit: int = 1000) -> list:
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval={interval}&limit={limit}",
            timeout=10,
        ).json()
        return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                 float(x[4]), float(x[5])] for x in r]
    except Exception:
        return []

def load_btc_daily() -> list:
    """Fetch BTC daily candles 2017->now in yearly chunks (same as BankNifty pattern)."""
    today_str = _ist_now().strftime("%Y-%m-%d")
    if os.path.exists(DAILY_CACHE_FILE):
        try:
            with open(DAILY_CACHE_FILE) as f:
                c = json.load(f)
            data = c.get("data", [])
            cache_ok = time.time() - c.get("ts", 0) < DAILY_CACHE_TTL
            if cache_ok and data:
                last_ts = data[-1][0] // 1000
                last_date = datetime.datetime.utcfromtimestamp(last_ts).strftime("%Y-%m-%d")
                if last_date < today_str:
                    cache_ok = False
            if cache_ok:
                return data
        except Exception:
            pass

    start_year = 2017
    cur_year   = _ist_now().year

    chunks: list[tuple[str, str]] = []
    for yr in range(start_year, cur_year + 1):
        chunks.append((f"{yr}-01-01", f"{yr}-06-30"))
        chunks.append((f"{yr}-07-01", f"{yr}-12-31"))

    all_candles: list = []
    seen_times: set   = set()

    for i, (from_d, to_d) in enumerate(chunks):
        if from_d > today_str:
            break
        actual_to = min(to_d, today_str)
        from_ms = int(datetime.datetime.strptime(from_d, "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc).timestamp() * 1000)
        to_ms   = int(datetime.datetime.strptime(actual_to, "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc).timestamp() * 1000) + 86400000
        try:
            r = requests.get(
                f"https://api.binance.com/api/v3/klines"
                f"?symbol=BTCUSDT&interval=1d&startTime={from_ms}&endTime={to_ms}&limit=1000",
                timeout=15,
            ).json()
            if isinstance(r, list):
                for x in r:
                    ts = int(x[0])
                    if ts not in seen_times:
                        seen_times.add(ts)
                        all_candles.append([ts, float(x[1]), float(x[2]),
                                            float(x[3]), float(x[4]), float(x[5])])
        except Exception:
            pass
        if i > 0:
            time.sleep(0.1)

    all_candles.sort(key=lambda x: x[0])

    if all_candles:
        try:
            with open(DAILY_CACHE_FILE, "w") as f:
                json.dump({"ts": time.time(), "data": all_candles}, f)
        except Exception:
            pass
    return all_candles

# ─── OHLC converter ───────────────────────────────────────────────────────────
def to_ohlc(bars: list) -> list:
    out = []
    for b in bars:
        try:
            out.append({
                "time":   int(b[0]) // 1000,
                "open":   round(float(b[1]), 2),
                "high":   round(float(b[2]), 2),
                "low":    round(float(b[3]), 2),
                "close":  round(float(b[4]), 2),
                "volume": round(float(b[5]), 2) if len(b) > 5 else 0,
            })
        except Exception:
            continue
    return out

# ─── Fyers WebSocket (DataSocket) — live BankNifty ticks ─────────────────────
_ws_thread_started = False

def _on_ws_message(msg):
    try:
        if not isinstance(msg, dict):
            return
        # DataSocket sends list of ticks or single tick dict
        ticks = msg if isinstance(msg, list) else [msg]
        for tick in ticks:
            if not isinstance(tick, dict):
                continue
            ltp = tick.get("ltp") or tick.get("LTP")
            if ltp is None:
                continue
            ltp = float(ltp)
            with _LIVE_LOCK:
                _LIVE["ltp"]        = ltp
                _LIVE["prev_close"] = float(tick.get("prev_close_price") or tick.get("prev_close") or _LIVE.get("prev_close") or ltp)
                _LIVE["ts"]         = int(time.time())
                _LIVE["source"]     = "ws"
            # Build running 1-minute candle from raw LTP ticks
            _update_candle_ltp(ltp)
            # Write bn_live.json so JS chart can poll it
            _write_live_json()
    except Exception:
        pass

def _on_ws_error(msg):
    pass

def _on_ws_close(msg):
    pass

def _on_ws_connect():
    try:
        fyers_ws.subscribe(
            symbols=["NSE:NIFTYBANK-INDEX"],
            data_type="SymbolUpdate",
        )
        fyers_ws.keep_running()
    except Exception:
        pass

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
    except Exception:
        pass

# ─── Background REST poller (fallback: polls Fyers 1m candles every 3s) ──────
def _rest_live_loop():
    while True:
        # WebSocket se fresh data aa raha hai to REST call skip karo
        with _LIVE_LOCK:
            ws_fresh = (time.time() - _LIVE["ts"]) < 8
        if not ws_fresh:
            creds = load_creds()
            if creds.get("access_token"):
                today = _ist_now().strftime("%Y-%m-%d")
                headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
                params = {
                    "symbol": "NSE:NIFTYBANK-INDEX", "resolution": "1",
                    "date_format": "1", "range_from": today, "range_to": today, "cont_flag": "1",
                }
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
                            _set_candle_from_bar(bar_epoch, o, h, l, c)
                            _write_live_json()
                except Exception:
                    pass
        time.sleep(1)

def _ensure_live_threads():
    names = {t.name for t in threading.enumerate()}
    if "FyersRESTPoller" not in names:
        threading.Thread(target=_rest_live_loop, name="FyersRESTPoller", daemon=True).start()
    if "FyersTokenMonitor" not in names:
        threading.Thread(target=_token_monitor_loop, name="FyersTokenMonitor", daemon=True).start()
    if "OptionChainBG" not in names:
        threading.Thread(target=_option_chain_bg_loop, name="OptionChainBG", daemon=True).start()
    if "BinanceOptionChainBG" not in names:
        threading.Thread(target=_binance_oc_bg_loop, name="BinanceOptionChainBG", daemon=True).start()
    _ensure_binance_ws_threads()
    _start_ws()
    _register_api_route()

# ─── Tornado /api/bn_history handler — lazy historical data endpoint ──────────
# Streamlit internally uses Tornado. We inject our own route so chart.html's
# infinite-scroll loader can fetch older BN candles on demand without a page reload.

_HIST_ENDPOINT_REGISTERED = False
_HIST_ENDPOINT_LOCK = threading.Lock()

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

            def do_GET(self):
                parsed = _up.urlparse(self.path)

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

        # Try ports 8502..8510 — pick whichever is free
        import socketserver
        for port in range(8502, 8511):
            try:
                srv = socketserver.ThreadingTCPServer(("0.0.0.0", port), _Handler)
                srv.daemon_threads = True
                # Write chosen port to a file so chart.html JS can read it via Streamlit component
                try:
                    with open(".api_port", "w") as _f:
                        _f.write(str(port))
                except Exception:
                    pass
                srv.serve_forever()
                break
            except OSError:
                continue   # port in use, try next

    threading.Thread(target=_server_loop, name="BNHistoryAPI", daemon=True).start()

# ─── ZIP export ───────────────────────────────────────────────────────────────
def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ("dashboard.py", "chart.html"):
            if os.path.exists(fname):
                zf.write(fname)
    return buf.getvalue()

# ─── Auto-startup TOTP login (runs once per session on any machine) ───────────
if not st.session_state.get("_startup_login_done"):
    st.session_state["_startup_login_done"] = True
    _boot_creds = load_creds()
    if not is_session_active() and _boot_creds.get("totp_secret"):
        _ok_boot, _msg_boot, _ = auto_fyers_login()
        if _ok_boot:
            _sess_cache.update({"active": True, "ts": time.time()}); st.session_state["_force_active"] = True
        st.rerun()

# ─── In-chart broker panel: handle query params from iframe form submits ───────
_qp = st.query_params

# Handler 1: Manual Google URL auth_code
if "fyers_code" in _qp:
    _code   = _qp.get("fyers_code",   "").strip()
    _app_id = _qp.get("fyers_app_id", DEFAULT_APP_ID).strip()
    _secret = _qp.get("fyers_secret", DEFAULT_SECRET).strip()
    st.query_params.clear()
    if _code:
        _ok, _tok, _resp = fyers_get_access_token(_app_id, _secret, _code)
        if _ok:
            save_creds({
                **load_creds(),
                "app_id": _app_id, "secret_key": _secret,
                "client_id": DEFAULT_CLIENT_ID, "password": DEFAULT_PASSWORD,
                "access_token": _tok,
            })
            _sess_cache.update({"active": True, "ts": time.time()}); st.session_state["_force_active"] = True
    st.rerun()

# Handler 2: TOTP auto-login triggered from chart panel
if _qp.get("totp_trigger") == "1":
    _totp_sec = _qp.get("totp_secret",  "").strip()
    _app_id   = _qp.get("fyers_app_id", DEFAULT_APP_ID).strip()
    _secret   = _qp.get("fyers_secret", DEFAULT_SECRET).strip()
    st.query_params.clear()
    if _totp_sec:
        # Save TOTP secret + credentials so auto_fyers_login picks them up
        _cur = load_creds()
        save_creds({**_cur, "totp_secret": _totp_sec,
                    "app_id": _app_id, "secret_key": _secret,
                    "client_id": _cur.get("client_id", DEFAULT_CLIENT_ID),
                    "password":  _cur.get("password",  DEFAULT_PASSWORD)})
        _ok2, _msg2, _log2 = auto_fyers_login()
        if _ok2:
            _sess_cache.update({"active": True, "ts": time.time()}); st.session_state["_force_active"] = True
        else:
            # Store error so it shows briefly above chart on reload
            st.session_state["totp_err"] = _msg2
            st.session_state["totp_log"] = _log2
    st.rerun()

# Handler 3: SV2 chunk / candle-count settings — Apply (bottom-bar 📦 icon)
if _qp.get("sv2_chunk_trigger") == "1":
    _new_bn, _new_btc = {}, {}
    for _k in _SV2_MAX_BN_DEFAULT.keys():
        _pk = f"sv2bn_{_k}"
        if _pk in _qp:
            try:
                _new_bn[_k] = int(_qp.get(_pk))
            except Exception:
                pass
    for _k in _SV2_MAX_BTC_DEFAULT.keys():
        _pk = f"sv2btc_{_k}"
        if _pk in _qp:
            try:
                _new_btc[_k] = int(_qp.get(_pk))
            except Exception:
                pass
    st.query_params.clear()
    if _new_bn or _new_btc:
        _cs_settings = load_sv2_chunk_settings()
        _cs_settings.setdefault("bn",  {}).update(_new_bn)
        _cs_settings.setdefault("btc", {}).update(_new_btc)
        save_sv2_chunk_settings(_cs_settings)
        # session cache invalidate karo taaki naya value turant lagu ho
        st.session_state.pop("_sv2_max_eff_bn",  None)
        st.session_state.pop("_sv2_max_eff_btc", None)
    st.rerun()

# Handler 3b: SV2 in-chart calendar's lazy-load fallback (safety net —
# normally not needed anymore since SV2 data is now always pre-requested
# before the chart renders, see below near `_btc_only`). Agar phir bhi
# JS side se ye trigger ho (purana cached chart.html, ya koi aur edge
# case), to isse properly handle karo taaki app sach me "restart" na ho
# jaaye — bas data request kar ke turant chart wapas dikha do.
if _qp.get("sv2_load") == "1":
    st.query_params.clear()
    st.session_state.setdefault("_sv2_anchor_date_bn", _sv2_default_date("bn"))
    st.session_state["_sv2_anchor_date_btc"] = None
    st.session_state["_sv2_data_requested"]  = True
    st.session_state["_btc_only_mode"] = True
    st.rerun()

# Handler 4: SV2 chunk / candle-count settings — Reset to default
if _qp.get("sv2_chunk_reset") == "1":
    st.query_params.clear()
    if os.path.exists(SV2_CHUNK_SETTINGS_FILE):
        try:
            os.remove(SV2_CHUNK_SETTINGS_FILE)
        except Exception:
            pass
    st.session_state.pop("_sv2_max_eff_bn",  None)
    st.session_state.pop("_sv2_max_eff_btc", None)
    st.rerun()

# Handler 5: (removed) — pehle yahan BTC Market Depth open/close bridge tha
# jo `depth_symbol` query-param ke zariye har baar poore app ka st.rerun()
# trigger karta tha (isi wajah se BTC depth khulte hi pura app reload hota
# tha). BTC depth ab seedha browser se Binance public REST se fetch hoti
# hai (chart.html), backend ko is bridge ki zaroorat hi nahi rahi.

# ── Startup debug: is script-run se pehle process kitni purani hai — agar
# _STARTUP_LOG khaali hai to matlab ye is container/process ka BILKUL PEHLA
# run hai (fresh boot / cold start / restart). Agar pehle se lines hain to
# same process continue ho raha hai (sirf Streamlit rerun hua hai). ──
_is_fresh_boot = (len(_STARTUP_LOG) == 0)
_slog(f"▶ Script run start" + (" — FRESH PROCESS BOOT (cold start/restart)" if _is_fresh_boot else " (rerun, same process)"))

creds      = load_creds()
_slog(
    "Creds loaded — fyers: app_id=%s secret=%s access_token=%s | binance: key=%s secret=%s" % (
        "yes" if creds.get("app_id") else "NO",
        "yes" if creds.get("secret_key") else "NO",
        "yes" if creds.get("access_token") else "NO",
        "yes" if creds.get("binance_api_key") else "NO",
        "yes" if creds.get("binance_secret_key") else "NO",
    )
)

try:
    sess_active = is_session_active()
except Exception as _e_sess:
    _slog_exception("is_session_active()", _e_sess)
    sess_active = False
_slog(f"sess_active (Fyers session valid) = {sess_active}")

# "Replay Mode" (starting page ka naya button) BTC-only chart hi kholta hai,
# bas SV2 replay data eagerly preload karta hai — isliye chart-render ke
# liye ye bhi _btc_only jaisa treat hota hai.
_btc_only_early = st.session_state.get("_btc_only_mode", False) or st.session_state.get("_replay_mode", False)
_slog(
    "_btc_only_mode=%s  _replay_mode=%s  binance_logged_in(session)=%s" % (
        st.session_state.get("_btc_only_mode", False),
        st.session_state.get("_replay_mode", False),
        st.session_state.get("binance_logged_in", False),
    )
)

# BinanceOptionChainBG thread BTC-only users ke liye bhi chahiye (Binance
# option chain Fyers session se independent hai) — isliye sirf sess_active
# nahi, _btc_only bhi is check me shaamil.
if sess_active or _btc_only_early:
    _slog(f"Trigger condition MET (sess_active={sess_active} or btc_only={_btc_only_early}) → calling _ensure_live_threads()")
    try:
        _ensure_live_threads()
        _running = sorted(t.name for t in threading.enumerate())
        _slog(f"_ensure_live_threads() done. Threads alive now: {_running}", level="ok")
    except Exception as _e_threads:
        _slog_exception("_ensure_live_threads()", _e_threads)
else:
    _slog(
        "Trigger condition NOT met (sess_active=False, btc_only=False) → "
        "_ensure_live_threads() ko is run mein call hi NAHI kiya gaya. "
        "Agar debug popup mein 'never started' dikh raha hai, yahi wajah hai.",
        level="warn",
    )

with st.sidebar:
    st.title("🔑 Fyers Login")

    if sess_active:
        st.success("✅ Live data active!")
        with _LIVE_LOCK:
            ltp_now = _LIVE["ltp"]
        if ltp_now:
            st.metric("BANKNIFTY LTP", f"₹{ltp_now:,.2f}")

        st.caption("Token auto-monitored every 5 min")

        if st.button("🔌 Disconnect", use_container_width=True):
            if os.path.exists(CREDS_FILE):
                os.remove(CREDS_FILE)
            _sess_cache.update({"active": False, "ts": 0.0})
            st.rerun()

    else:
        # Check if it's an expiry (creds exist but token dead) or fresh login
        has_old_creds = bool(creds.get("access_token"))
        app_id = creds.get("app_id", DEFAULT_APP_ID)
        secret = creds.get("secret_key", DEFAULT_SECRET)

        # Unique nonce per page-load → forces Fyers to generate a FRESH auth_code each time
        import random
        _nonce = str(int(time.time())) + str(random.randint(1000, 9999))
        auth_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={app_id}"
            f"&redirect_uri=https%3A%2F%2Fwww.google.com"
            f"&response_type=code"
            f"&state={_nonce}"
            f"&nonce={_nonce}"
        )

        if has_old_creds:
            st.error("🔴 Token expire ho gaya! Re-login karo")
        else:
            st.warning("⚠️ Login karo")

        # ── Method A: TOTP Auto Login ────────────────────────────────────────────
        creds_now = load_creds()
        has_totp  = bool(creds_now.get("totp_secret", ""))

        with st.expander("🤖 Method A — TOTP Auto Login (Recommended)", expanded=has_totp):
            st.caption("Ek baar TOTP secret save karo → phir sirf ek button click")

            totp_inp = st.text_input(
                "TOTP Secret (32-char base32 key)",
                value=creds_now.get("totp_secret", ""),
                type="password",
                placeholder="JBSWY3DPEHPK3PXP...",
                key="totp_secret_inp",
            )
            if st.button("💾 Save TOTP Secret", use_container_width=True, key="save_totp"):
                save_creds({**creds_now, "totp_secret": totp_inp.strip()})
                st.success("✅ Saved!")
                st.rerun()

            if has_totp:
                if st.button("🚀 Auto Login (TOTP)", use_container_width=True,
                             type="primary", key="totp_login_btn"):
                    with st.spinner("Logging in… (Steps 1→5)"):
                        ok, msg, step_log = auto_fyers_login()
                    if ok:
                        st.session_state["_force_active"] = True
                        _sess_cache.update({"active": True, "ts": time.time()})
                        st.success("🎉 TOTP Login ho gaya!")
                        st.rerun()
                    else:
                        st.error(f"❌ Failed: {msg}")
                        st.markdown("**Step-by-step debug:**")
                        st.code(json.dumps(step_log, indent=2), language="json")
            else:
                st.info("Pehle TOTP secret save karo phir button aayega")

        st.markdown("---")

        # ── Method B: Manual Google URL ─────────────────────────────────────────
        with st.expander("🔗 Method B — Manual Google URL", expanded=not has_totp):
            st.markdown(f"**Step 1 →** [👉 Fyers Fresh Login Link]({auth_url})")
            st.warning("⚠️ Upar wala FRESH link click karo — purana cached URL mat use karo!")
            st.caption("Link click → Google page aayega → us page ka poora URL copy karo")

            url_input = st.text_input(
                "**Step 2 →** Poora URL ya auth_code paste karo",
                placeholder="https://www.google.com/?s=ok&auth_code=eyJ...",
            )

            if st.button("⚡ Connect", use_container_width=True, type="primary"):
                raw = url_input.strip()
                if raw:
                    code = _extract_auth_code(raw)
                    st.caption(f"🔍 Extracted code: `{code[:20]}...`")
                    ok, access_token, full_resp = fyers_get_access_token(app_id, secret, code)
                    if ok:
                        save_creds({
                            **creds,
                            "app_id":       app_id,
                            "secret_key":   secret,
                            "client_id":    DEFAULT_CLIENT_ID,
                            "password":     DEFAULT_PASSWORD,
                            "access_token": access_token,
                        })
                        _sess_cache.update({"active": True, "ts": time.time()}); st.session_state["_force_active"] = True
                        st.success("🎉 Connected!")
                        st.rerun()
                    else:
                        st.error(f"❌ Login Failed: {access_token}")
                        st.markdown("**Full Fyers Response:**")
                        st.code(json.dumps(full_resp, indent=2), language="json")
                else:
                    st.error("URL ya code paste karo pehle")

    st.markdown("---")

    # ── SMS Alert Setup ─────────────────────────────────────────────────────
    with st.expander("📱 SMS Alert Setup", expanded=False):
        st.caption("Token expire hone par SMS aayega 7018093451 par")
        st.success("✅ Fast2SMS connected")

        # Allow changing phone number
        creds_now = load_creds()
        phone_val = creds_now.get("alert_phone", "7018093451")
        new_phone = st.text_input("Alert Phone", value=phone_val, max_chars=12)
        if st.button("💾 Save Phone", use_container_width=True):
            save_creds({**creds_now, "alert_phone": new_phone.strip()})
            st.success(f"Saved: {new_phone}")

    st.markdown("---")
    st.download_button(
        "⬇️ Download Project ZIP",
        data=_make_zip(),
        file_name="banknifty_chart.zip",
        mime="application/zip",
        use_container_width=True,
    )

# ─── Fetch all chart data ─────────────────────────────────────────────────────
# Cache key includes first 8 chars of token so new token → fresh fetch
@st.cache_data(ttl=HIST_CACHE_TTL, show_spinner=False)
def _get_chart_data(sess: bool, _tok: str = ""):
    btc_1m   = fetch_btc("1m",  1000)
    btc_15m  = fetch_btc("15m", 1000)
    btc_day  = load_btc_daily()
    bn_1m    = fetch_bn_intraday(1)  if sess else []
    bn_5m    = fetch_bn_intraday(5)  if sess else []
    bn_15m   = fetch_bn_intraday(15) if sess else []
    bn_45m   = fetch_bn_intraday(45) if sess else []
    bn_day   = load_bn_daily()       if sess else []
    return btc_1m, btc_15m, btc_day, bn_1m, bn_5m, bn_15m, bn_45m, bn_day

_tok_hint = creds.get("access_token", "")[:8] if sess_active else ""
btc_1m, btc_15m, btc_day, bn_1m, bn_5m, bn_15m, bn_45m, bn_day = _get_chart_data(sess_active, _tok_hint)

# ─── TOTP error notification (from iframe-triggered auto-login failure) ────────
if "totp_err" in st.session_state:
    st.error(f"❌ TOTP Login failed: {st.session_state.pop('totp_err')}")
    _log_data = st.session_state.pop("totp_log", None)
    if _log_data:
        with st.expander("Debug log dekhein"):
            st.code(json.dumps(_log_data, indent=2), language="json")


# ─── Chart HTML builder — injects live data directly into chart.html ──────────
def _build_chart_html(
    btc_1m, btc_15m, btc_day,
    bn_1m,  bn_5m,  bn_15m,  bn_45m,  bn_day,
    sess_active: bool
) -> str:
    """Read chart.html and replace all __PLACEHOLDERS__ with real data."""
    import os, json as _json

    # Load chart.html from same directory as app.py
    _html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.html")
    if not os.path.exists(_html_path):
        return "<p style='color:red'>chart.html not found</p>"

    with open(_html_path, "r", encoding="utf-8") as _f:
        html = _f.read()

    def _to_lwc(candles: list) -> str:
        """Convert [[epoch_ms, o, h, l, c, v], ...] or [{time,open,...}] to LWC format."""
        out = []
        for b in candles:
            try:
                if isinstance(b, (list, tuple)):
                    t = int(b[0]) // 1000  # ms→sec
                    o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
                    v = float(b[5]) if len(b) > 5 else 0
                else:
                    t = int(b.get("time", 0))
                    o = float(b.get("open",  0))
                    h = float(b.get("high",  0))
                    l = float(b.get("low",   0))
                    c = float(b.get("close", 0))
                    v = float(b.get("volume", 0))
                out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
            except Exception:
                continue
        # Deduplicate by time, keep last
        seen = {}
        for b in out:
            seen[b["time"]] = b
        return _json.dumps(sorted(seen.values(), key=lambda x: x["time"]))

    _creds = load_creds()
    status = "connected" if sess_active else "disconnected"
    app_id  = _creds.get("app_id",    DEFAULT_APP_ID)
    secret  = _creds.get("secret_key", DEFAULT_SECRET)

    html = html.replace("__BTC_CANDLES__", _to_lwc(btc_1m))
    html = html.replace("__BTC_15M__",     _to_lwc(btc_15m))
    html = html.replace("__BTC_DAILY__",   _to_lwc(btc_day))
    html = html.replace("__BN_CANDLES__",  _to_lwc(bn_1m))
    html = html.replace("__BN_5M__",       _to_lwc(bn_5m))
    html = html.replace("__BN_15M__",      _to_lwc(bn_15m))
    html = html.replace("__BN_45M__",      _to_lwc(bn_45m))
    html = html.replace("__BN_DAILY__",    _to_lwc(bn_day))

    # ── Stack View 2: .gz se pre-resampled data inject karo ─────────────────
    # LAZY LOAD: jab tak starting screen par "📊 Chart Kholo (Chunk Date Se)"
    # button se date select karke request nahi ki jaati (_sv2_data_requested),
    # GitHub se koi fetch hi nahi hoga — sirf empty placeholders inject
    # honge. Ye both BankNifty aur BTC dono par lagu hai. Jab request hoti
    # hai, chuni gayi date ("chunk") ke aas-paas hi data trim hokar aata hai
    # (existing _SV2_MAX limits ke hisaab se) — poori history nahi.
    _sv2_data_requested = bool(st.session_state.get("_sv2_data_requested"))
    _sv2_all_placeholders = [
        "__SV2_BN_5M_RAW__","__SV2_BN_125M__",
        "__SV2_BN_1D__","__SV2_BN_3D__","__SV2_BN_9D__","__SV2_BN_27D__",
        "__SV2_BTC_5M_RAW__","__SV2_BTC_8H__","__SV2_BTC_1D__",
        "__SV2_BTC_3D__","__SV2_BTC_9D__","__SV2_BTC_27D__",
    ]
    _sv2_err_msg = ""
    _sv2_data_loaded_ok = False
    if not _sv2_data_requested:
        # Abhi tak koi chunk date select nahi hui — GitHub ko chhuna hi nahi hai
        for _ph in _sv2_all_placeholders:
            html = html.replace(_ph, "[]")
        _sv2_err_msg = "NOT_REQUESTED_YET"
    else:
        try:
            _bn_anchor_d  = st.session_state.get("_sv2_anchor_date_bn")
            _btc_anchor_d = st.session_state.get("_sv2_anchor_date_btc")
            _bn_anchor  = _sv2_date_to_anchor_epoch(_bn_anchor_d)  if _bn_anchor_d  else None
            _btc_anchor = _sv2_date_to_anchor_epoch(_btc_anchor_d) if _btc_anchor_d else None
            _sv2 = _build_sv2_data(bn_anchor=_bn_anchor, btc_anchor=_btc_anchor)
            _bn  = _sv2["bn"]
            _btc = _sv2["btc"]
            # Debug info: paths + counts inject karo
            _sv2_debug_info = {
                "bn_path":  _SV2_CACHE.get("bn_path", "?"),
                "btc_path": _SV2_CACHE.get("btc_path", "?"),
                "bn_err":   _SV2_CACHE.get("bn_err", ""),
                "btc_err":  _SV2_CACHE.get("btc_err", ""),
                "bn_chunk_date":  str(_bn_anchor_d)  or "(latest)",
                "btc_chunk_date": str(_btc_anchor_d) or "(latest)",
                "bn_counts": {k: len(v) for k, v in _bn.items()},
                "btc_counts": {k: len(v) for k, v in _btc.items()},
            }
            _sv2_err_msg = json.dumps(_sv2_debug_info)
            html = html.replace("__SV2_BN_5M_RAW__", _sv2_to_js(_bn["5m_raw"]))
            html = html.replace("__SV2_BN_125M__", _sv2_to_js(_bn["125m"]))
            html = html.replace("__SV2_BN_1D__",   _sv2_to_js(_bn["1D"]))
            html = html.replace("__SV2_BN_3D__",   _sv2_to_js(_bn["3D"]))
            html = html.replace("__SV2_BN_9D__",   _sv2_to_js(_bn["9D"]))
            html = html.replace("__SV2_BN_27D__",  _sv2_to_js(_bn["27D"]))
            html = html.replace("__SV2_BTC_5M_RAW__", _sv2_to_js(_btc["5m_raw"]))
            html = html.replace("__SV2_BTC_8H__",   _sv2_to_js(_btc["8H"]))
            html = html.replace("__SV2_BTC_1D__",   _sv2_to_js(_btc["1D"]))
            html = html.replace("__SV2_BTC_3D__",   _sv2_to_js(_btc["3D"]))
            html = html.replace("__SV2_BTC_9D__",   _sv2_to_js(_btc["9D"]))
            html = html.replace("__SV2_BTC_27D__",  _sv2_to_js(_btc["27D"]))
            _sv2_data_loaded_ok = True
        except Exception as _sv2_ex:
            _sv2_err_msg = f"EXCEPTION: {_sv2_ex} | cache={_SV2_CACHE}"
            # Fallback: empty arrays agar gz file missing/corrupt ho
            for _ph in _sv2_all_placeholders:
                html = html.replace(_ph, "[]")
    # Inject debug info + loaded-flag as JS variables — chart.html isi flag
    # se decide karta hai ki data already load hai ya reload trigger karna hai
    _sv2_safe = _sv2_err_msg.replace("</", "<\\/")
    # Bottom-bar "📦 Chunk" icon panel ke liye: current effective candle-count
    # limits (BN + BTC), unki safe bounds, aur current chunk dates.
    _sv2_chunk_ui_info = {
        "bn":            _sv2_get_max("bn"),
        "btc":           _sv2_get_max("btc"),
        "bounds":        _SV2_MAX_BOUNDS,
        "bn_chunk_date":  str(st.session_state.get("_sv2_anchor_date_bn")  or ""),
        "btc_chunk_date": str(st.session_state.get("_sv2_anchor_date_btc") or ""),
    }
    # ── App-startup / login debug log — snapshot le lo taaki chart render
    # hone se pehle jitne bhi steps (creds, session check, thread launch,
    # koi exception) hue hain, header ke chhote debug icon se copy kiye
    # ja saken. Render ke turant baad ka bhi ek final marker line daal
    # rahe hain taaki pata chale ye poora startup trace hai.
    try:
        _slog(f"Chart HTML render ho raha hai — sess_active={sess_active} btc_only={_btc_only_early}", level="ok")
    except Exception:
        pass
    _startup_log_safe = json.dumps(_startup_log_snapshot()).replace("</", "<\\/")
    html = html.replace("</body>",
        f"<script>window.__STARTUP_LOG__={_startup_log_safe};"
        "try{ if (typeof _bootDebugRenderLog === 'function') _bootDebugRenderLog(); }catch(_){}"
        "</script>\n</body>", 1)

    html = html.replace("</body>",
        f"<script>window.__SV2_DEBUG={json.dumps(_sv2_safe)};"
        f"window.__SV2_DATA_LOADED={json.dumps(_sv2_data_loaded_ok)};"
        f"window.__SV2_CHUNK_SETTINGS={json.dumps(_sv2_chunk_ui_info)};</script>\n</body>", 1)
    html = html.replace("__FYERS_APP_ID__", app_id)
    html = html.replace("__FYERS_SECRET__",  secret)

    # ── Supabase sync (saved layouts / settings / app state) ────────────────
    # URL is not sensitive, but pulling both from Streamlit Cloud secrets
    # (Settings → Secrets) keeps things in one place. Publishable key is
    # safe for the browser (RLS + anonymous auth restrict access per device).
    _sb_url = _get_secret("SUPABASE_URL")
    _sb_key = _get_secret("SUPABASE_ANON_KEY")
    html = html.replace("__SUPABASE_URL__",      _sb_url)
    html = html.replace("__SUPABASE_ANON_KEY__", _sb_key)

    # Optional auto-login credentials (personal app — same trust model as
    # the existing Fyers app_id/secret injection above). If left blank in
    # secrets, chart.html falls back to showing the manual login form.
    _sb_email = _get_secret("SUPABASE_LOGIN_EMAIL")
    _sb_pass  = _get_secret("SUPABASE_LOGIN_PASSWORD")
    html = html.replace("__SUPABASE_LOGIN_EMAIL__",    _sb_email)
    html = html.replace("__SUPABASE_LOGIN_PASSWORD__", _sb_pass)

    # ── Inject side-API port so chart.html knows which port to call ──────────
    _api_port = 0
    try:
        if os.path.exists(".api_port"):
            with open(".api_port") as _pf:
                _api_port = int(_pf.read().strip())
    except Exception:
        _api_port = 0
    html = html.replace("__API_PORT__", str(_api_port))

    # ── Startup mein last known BN tick inject karo (polling se pehle) ──────
    tick = None
    try:
        if os.path.exists("bn_live.json"):
            with open("bn_live.json") as _tf:
                tick = json.load(_tf)
    except Exception:
        tick = None
    if tick:
        tick_js = json.dumps(tick)
        inject = (
            "\n<script>"
            "(function(){"
            "  setTimeout(function(){"
            "    try{if(typeof _applyBNLiveTick==='function'){_applyBNLiveTick(" + tick_js + ");}}"
            "    catch(_){}"
            "  }, 1200);"
            "})();"
            "</script>"
        )
        html = html.replace("</body>", inject + "\n</body>")

    return html


# ─── Main area: embed chart directly (no separate API server needed) ─────────
st.markdown("## 📊 BankNifty Live Chart")

# ── BTC-only mode (no Fyers needed) ──────────────────────────────────────────
_replay_mode = st.session_state.get("_replay_mode", False)
_btc_only = st.session_state.get("_btc_only_mode", False) or _replay_mode

# Chart render hone se pehle SV2 (replay) data "requested" hona chahiye —
# Fyers-login (sess_active) aur "Replay Mode" button (_replay_mode) dono
# ke liye eagerly preload hota hai, taaki pehli date-selection par koi
# full-page ?sv2_load=1 reload na ho. Plain BTC-only flow (_btc_only bina
# _replay_mode ke — "BTC Chart Kholo (Fyers ke bina)" button) ke liye
# jaan-boojh kar NAHI: replay data tabhi load hona chahiye jab SV2 me user
# khud date select kare ya "Resume Last Replay" dabaye. Agar in-chart
# calendar se date select/resume hota hai aur data abhi tak load nahi
# hua, JS ka _sv2RequestDataLoad() ek ?sv2_load=1 reload trigger karta hai
# jise "Handler 3b" (_qp.get("sv2_load")) safely handle karta hai.
if (sess_active or _replay_mode) and not st.session_state.get("_sv2_data_requested"):
    st.session_state.setdefault("_sv2_anchor_date_bn",  _sv2_default_date("bn"))
    st.session_state["_sv2_anchor_date_btc"] = None
    st.session_state["_sv2_data_requested"]  = True

if sess_active or _btc_only:
    if sess_active:
        st.success("✅ Fyers connected — live data active")
    elif _replay_mode:
        st.success("📼 Replay Mode active — replay data preloaded")
    else:
        st.info("📊 BTC Chart mode — BankNifty data available nahi (Fyers login nahi hai)")

    # ── Stack View 2 chunk-date re-selector (Fyers login wale users ke
    # liye bhi — starting screen wala picker sirf login-less flow mein
    # dikhta hai, isliye yahan bhi wahi option chhota expander mein) ───────
    with st.expander("📅 Stack View 2 — Chunk Date badlo (BankNifty)"):
        _sv2_e_bn = st.date_input(
            "BankNifty chunk date",
            value=_sv2_default_date("bn"),
            key="sv2_bn_date_inp_2",
        )
        _sv2_remember_dates(_sv2_e_bn, None)
        if st.button("🔄 Is Date Ka Chunk Load Karo", key="sv2_chunk_reload_btn"):
            st.session_state["_sv2_anchor_date_bn"]  = _sv2_e_bn
            st.session_state["_sv2_anchor_date_btc"] = None
            st.session_state["_sv2_data_requested"]  = True
            st.rerun()

    _chart_html = _build_chart_html(
        btc_1m, btc_15m, btc_day,
        bn_1m,  bn_5m,  bn_15m,  bn_45m,  bn_day,
        sess_active,
    )
    components.html(_chart_html, height=950, scrolling=False)

    # ── BN Live Tick → postMessage pusher ────────────────────────────────────
    # @st.fragment se sirf yeh component rerun hoga — chart flicker nahi karega.
    # Har 1s pe latest tick iframe ko postMessage se milega.
    # chart.html ka _applyBNLiveTick() + resampleForTF() automatically
    # 1m/15m/45m/135m/1d/3d/9d — sab TFs pe live candle update karta hai.
    if sess_active:
        @st.fragment(run_every=1)
        def _bn_tick_pusher():
            with _LAST_TICK_LOCK:
                _tick_json = _LAST_TICK_JS["json"]

            # Agar WS/REST se abhi tak tick nahi aaya to bn_live.json fallback
            if not _tick_json and os.path.exists(BN_LIVE_FILE):
                try:
                    with open(BN_LIVE_FILE) as _tf:
                        _tick_json = json.dumps(json.load(_tf))
                except Exception:
                    _tick_json = ""

            if not _tick_json:
                return  # koi tick nahi — kuch mat karo

            # Yeh JS snippet:
            # 1. Pehli baar: setInterval setup karta hai jo har 800ms iframe ko push karta hai
            # 2. Baad mein: window._bnLastTick update karta hai — interval khud push kar deta hai
            _script = f"""
<script>
(function() {{
  var tick = {_tick_json};

  // Global tick store update karo
  window._bnLastTick = tick;

  // Pehli baar interval setup karo (duplicate safe)
  if (!window._bnPusherReady) {{
    window._bnPusherReady = true;
    window._bnLastSentTs  = 0;

    setInterval(function() {{
      var t = window._bnLastTick;
      if (!t || t.ts === window._bnLastSentTs) return;
      var frames = window.parent.document.querySelectorAll('iframe');
      for (var i = 0; i < frames.length; i++) {{
        try {{
          frames[i].contentWindow.postMessage(
            JSON.stringify({{ type: 'bn_live', data: t }}), '*'
          );
        }} catch(e) {{}}
      }}
      window._bnLastSentTs = t.ts;
    }}, 800);
  }}

  // Turant push karo (naya tick aaya hai)
  if (tick.ts !== window._bnLastSentTs) {{
    var frames = window.parent.document.querySelectorAll('iframe');
    for (var i = 0; i < frames.length; i++) {{
      try {{
        frames[i].contentWindow.postMessage(
          JSON.stringify({{ type: 'bn_live', data: tick }}), '*'
        );
      }} catch(e) {{}}
    }}
    window._bnLastSentTs = tick.ts;
  }}
}})();
</script>
"""
            components.html(_script, height=0, scrolling=False)

        _bn_tick_pusher()

    # ── Fyers Balance + Nearest Strike → postMessage pusher ─────────────────
    # Har ~5s pe refresh (funds cache khud har 20s pe hi actually API hit karta
    # hai), phir 💰 Balance panel (Stack View 1 bottom-bar) ko postMessage se
    # bhej deta hai. Iframe poll (fyers_meta.json) fallback ke roop mein bhi
    # kaam karta hai agar postMessage miss ho jaye.
    if sess_active:
        @st.fragment(run_every=5)
        def _fyers_meta_pusher():
            meta = refresh_fyers_meta_cache()
            _meta_json = json.dumps(meta)
            _script2 = f"""
<script>
(function() {{
  var meta = {_meta_json};
  var frames = window.parent.document.querySelectorAll('iframe');
  for (var i = 0; i < frames.length; i++) {{
    try {{
      frames[i].contentWindow.postMessage(
        JSON.stringify({{ type: 'fyers_meta', data: meta }}), '*'
      );
    }} catch(e) {{}}
  }}
}})();
</script>
"""
            components.html(_script2, height=0, scrolling=False)

        _fyers_meta_pusher()

    # ── Option Chain → postMessage pusher ───────────────────────────────────
    # Ab ye kabhi Fyers ko seedha network call NAHI karta — sirf background
    # thread (OptionChainBG) ka already-fetched cache padhta hai. Isliye run_every
    # ko 1s rakhna bhi safe hai: koi blocking I/O nahi, spot-tick pusher
    # (_bn_tick_pusher) ab kabhi iske peeche wait nahi karega.
    if sess_active:
        @st.fragment(run_every=1)
        def _option_chain_pusher():
            oc = get_cached_option_chain_payload()
            if not oc:
                oc = {"error": "Kuch data nahi mila (unknown reason)"}
            _oc_json = json.dumps(oc)
            _script3 = f"""
<script>
(function() {{
  var oc = {_oc_json};
  var frames = window.parent.document.querySelectorAll('iframe');
  for (var i = 0; i < frames.length; i++) {{
    try {{
      frames[i].contentWindow.postMessage(
        JSON.stringify({{ type: 'option_chain', data: oc }}), '*'
      );
    }} catch(e) {{}}
  }}
}})();
</script>
"""
            components.html(_script3, height=0, scrolling=False)

        _option_chain_pusher()

    # ── Binance BTC Option Chain → postMessage pusher ───────────────────────
    # Same pattern as _option_chain_pusher, alag message type ('binance_option_
    # chain') se — Chain panel (Stack View 1) frontend par asset (BTC/BankNifty)
    # ke hisaab se in dono mein se sahi wala pick karke render karta hai.
    # BTC-only (Fyers-less) users ke liye bhi chalna chahiye — Binance data
    # Fyers session se independent hai, isliye _btc_only bhi allowed.
    if sess_active or _btc_only:
        @st.fragment(run_every=1)
        def _binance_option_chain_pusher():
            boc = get_cached_binance_option_chain_payload()
            if not boc:
                boc = {"error": "Kuch data nahi mila (unknown reason)"}
            _boc_json = json.dumps(boc)
            _script5 = f"""
<script>
(function() {{
  var oc = {_boc_json};
  var frames = window.parent.document.querySelectorAll('iframe');
  for (var i = 0; i < frames.length; i++) {{
    try {{
      frames[i].contentWindow.postMessage(
        JSON.stringify({{ type: 'binance_option_chain', data: oc }}), '*'
      );
    }} catch(e) {{}}
  }}
}})();
</script>
"""
            components.html(_script5, height=0, scrolling=False)

        _binance_option_chain_pusher()

    # ── Binance USDT Balance → postMessage pusher ───────────────────────────
    # Same pattern as _fyers_meta_pusher, alag message type ('binance_meta')
    # se — Balance tab (Stack View 1 Option Chain) BTCUSDT active hone par
    # isi USDT balance ko dikhata hai, Fyers ₹ balance ki jagah.
    if sess_active or _btc_only:
        @st.fragment(run_every=5)
        def _binance_meta_pusher():
            bmeta = refresh_binance_meta_cache()
            _bmeta_json = json.dumps(bmeta)
            _script6 = f"""
<script>
(function() {{
  var meta = {_bmeta_json};
  var frames = window.parent.document.querySelectorAll('iframe');
  for (var i = 0; i < frames.length; i++) {{
    try {{
      frames[i].contentWindow.postMessage(
        JSON.stringify({{ type: 'binance_meta', data: meta }}), '*'
      );
    }} catch(e) {{}}
  }}
}})();
</script>
"""
            components.html(_script6, height=0, scrolling=False)

        _binance_meta_pusher()

    # ── BTC Market Depth: pehle yahan ek postMessage pusher fragment tha
    # (Option A). Ab BTC options depth seedha browser se Binance public
    # REST (eapi.binance.com) se fetch hoti hai (chart.html me
    # _ocOpenDepth) — isliye ye backend pusher delete kar diya. Koi
    # st.fragment/backend involvement nahi, koi page reload nahi.

else:
    # ─── Main area inline Login Panel ─────────────────────────────────────────
    _creds_main = load_creds()
    _has_old    = bool(_creds_main.get("access_token"))
    _app_id_m   = _creds_main.get("app_id",    DEFAULT_APP_ID)
    _secret_m   = _creds_main.get("secret_key", DEFAULT_SECRET)
    _has_totp_m = bool(_creds_main.get("totp_secret", ""))

    import random as _rand
    _nonce_m = str(int(time.time())) + str(_rand.randint(1000, 9999))
    _auth_url_m = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={_app_id_m}"
        f"&redirect_uri=https%3A%2F%2Fwww.google.com"
        f"&response_type=code"
        f"&state={_nonce_m}"
        f"&nonce={_nonce_m}"
    )

    st.markdown("""
    <style>
    .login-card{
        background:#1e222d;border:1px solid #2a2e3e;border-radius:14px;
        padding:32px 28px;max-width:620px;margin:30px auto;
    }
    .login-title{color:#e0e3eb;font-size:1.5rem;font-weight:700;margin-bottom:4px;}
    .login-sub{color:#848da0;font-size:0.9rem;margin-bottom:24px;}
    .method-label{
        color:#a3aabf;font-size:0.78rem;font-weight:600;letter-spacing:.08em;
        text-transform:uppercase;margin-bottom:8px;
    }
    .step-badge{
        background:#1a73e8;color:#fff;border-radius:50%;
        width:22px;height:22px;display:inline-flex;align-items:center;
        justify-content:center;font-size:.75rem;font-weight:700;margin-right:8px;
    }
    </style>
    """, unsafe_allow_html=True)

    if _has_old:
        st.error("🔴 Fyers token expire ho gaya — dobara login karo")

    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🔑 Fyers Login</div>''', unsafe_allow_html=True)
    st.markdown('''<div class="login-sub">Login karo — phir live BankNifty chart khulega</div>''', unsafe_allow_html=True)

    # ── METHOD A: TOTP Auto Login ──────────────────────────────────────────────
    st.markdown('''<div class="method-label">⚡ Method A — TOTP Auto Login (Recommended)</div>''', unsafe_allow_html=True)

    _totp_val_m = st.text_input(
        "TOTP Secret (32-char base32 key)",
        value=_creds_main.get("totp_secret", ""),
        type="password",
        placeholder="JBSWY3DPEHPK3PXP...",
        key="main_totp_secret",
    )

    col_save, col_login = st.columns([1, 1])
    with col_save:
        if st.button("💾 Save TOTP Secret", use_container_width=True, key="main_save_totp"):
            save_creds({**_creds_main, "totp_secret": _totp_val_m.strip()})
            st.success("✅ Saved!")
            st.rerun()

    with col_login:
        _btn_disabled_m = not bool(_totp_val_m.strip() or _has_totp_m)
        if st.button(
            "🚀 Auto Login",
            use_container_width=True,
            type="primary",
            key="main_totp_login",
            disabled=_btn_disabled_m,
        ):
            with st.spinner("Logging in… (Steps 1→5)"):
                if _totp_val_m.strip():
                    save_creds({**_creds_main, "totp_secret": _totp_val_m.strip()})
                _ok_m2, _msg_m2, _log_m2 = auto_fyers_login()
            if _ok_m2:
                st.session_state["_force_active"] = True
                _sess_cache.update({"active": True, "ts": time.time()})
                st.success("🎉 Login ho gaya!")
                st.rerun()
            else:
                st.error(f"❌ Failed: {_msg_m2}")
                with st.expander("Debug log"):
                    st.code(json.dumps(_log_m2, indent=2), language="json")

    if _btn_disabled_m:
        st.caption("👆 Pehle TOTP secret daalo phir Auto Login button active hoga")

    st.markdown("<hr style='border:none;border-top:1px solid #2a2e3e;margin:22px 0;'>", unsafe_allow_html=True)

    # ── METHOD B: Google URL ───────────────────────────────────────────────────
    st.markdown('''<div class="method-label">🔗 Method B — Manual Google URL</div>''', unsafe_allow_html=True)

    st.markdown(
        f'''<p style="margin:6px 0 10px;">'''
        f'''<span class="step-badge">1</span>'''
        f'''<a href="{_auth_url_m}" target="_blank" style="color:#1a73e8;font-weight:600;">'''
        f'''👉 Yahan click karo — Fyers Fresh Login Link</a></p>''',
        unsafe_allow_html=True,
    )
    st.caption("⚠️ Link click karo → Google page khulega → us page ka poora URL copy karo")

    _url_inp_m = st.text_input(
        "Step 2 → Poora Google URL ya sirf auth_code paste karo",
        placeholder="https://www.google.com/?s=ok&auth_code=eyJ...",
        key="main_url_inp",
    )

    if st.button("⚡ Connect", use_container_width=True, type="primary", key="main_url_connect"):
        _raw_m = _url_inp_m.strip()
        if _raw_m:
            _code_m = _extract_auth_code(_raw_m)
            _ok_u, _tok_u, _resp_u = fyers_get_access_token(_app_id_m, _secret_m, _code_m)
            if _ok_u:
                save_creds({
                    **_creds_main,
                    "app_id":       _app_id_m,
                    "secret_key":   _secret_m,
                    "client_id":    DEFAULT_CLIENT_ID,
                    "password":     DEFAULT_PASSWORD,
                    "access_token": _tok_u,
                })
                st.session_state["_force_active"] = True
                _sess_cache.update({"active": True, "ts": time.time()})
                st.success("🎉 Connected!")
                st.rerun()
            else:
                st.error(f"❌ Login Failed: {_tok_u}")
                with st.expander("Full Fyers Response"):
                    st.code(json.dumps(_resp_u, indent=2), language="json")
        else:
            st.warning("URL ya auth_code paste karo pehle")

    st.markdown('''</div>''', unsafe_allow_html=True)

    # ── Binance Login Card (sirf login — live option chain background thread ────
    #    isi saved key/secret ko use karta hai; yahan koi extra data fetch/
    #    display nahi hota) ─────────────────────────────────────────────────────
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🟡 Binance Login</div>''', unsafe_allow_html=True)
    st.markdown('''<div class="login-sub">API Key/Secret daalo aur login karo — live option chain isi se chalti hai</div>''', unsafe_allow_html=True)

    _bn_api_key    = st.text_input("Binance API Key", type="password", key="binance_api_key")
    _bn_secret_key = st.text_input("Binance Secret Key", type="password", key="binance_secret_key")

    if st.button("🔌 Binance Login", use_container_width=True, type="primary", key="binance_login_btn"):
        _slog("👉 'Binance Login' button clicked")
        if not _bn_api_key or not _bn_secret_key:
            _slog("Binance Login blocked — API Key/Secret Key khaali the", level="warn")
            st.error("Pehle API Key aur Secret Key daalo")
        else:
            with st.spinner("Binance se login ho raha hai…"):
                # Sirf credentials valid hain ya nahi check karne ke liye ek
                # lightweight signed call — result yahan display nahi hota,
                # sirf pass/fail dekha jaata hai.
                try:
                    _ok_login, _login_result = binance_get_spot_balance(_bn_api_key, _bn_secret_key)
                except Exception as _e_bn_login:
                    _slog_exception("Binance Login → binance_get_spot_balance()", _e_bn_login)
                    _ok_login, _login_result = False, str(_e_bn_login)

            if _ok_login:
                # Live option-chain background thread (_ensure_binance_ws_threads
                # / _binance_oc_bg_loop) ke liye keys save karo — wahi in
                # credentials ko use karke chart chalata hai.
                save_creds({**load_creds(), "binance_api_key": _bn_api_key, "binance_secret_key": _bn_secret_key})
                st.session_state["binance_logged_in"] = True
                _slog("Binance Login SUCCESS — keys saved", level="ok")
                st.success("✅ Binance login successful")
            else:
                st.session_state["binance_logged_in"] = False
                _slog(f"Binance Login FAILED — {_login_result}", level="err")
                st.error(f"❌ Login failed: {_login_result}")

    st.markdown('''</div>''', unsafe_allow_html=True)

    # ── BTC Chart without Fyers ────────────────────────────────────────────────
    st.markdown("""
    <style>
    .btc-divider{
        display:flex;align-items:center;gap:12px;margin:28px 0 18px;max-width:620px;margin-left:auto;margin-right:auto;
    }
    .btc-divider-line{flex:1;height:1px;background:#2a2e3e}
    .btc-divider-txt{color:#555;font-size:0.8rem;white-space:nowrap}
    .btc-access-card{
        background:#131722;border:1px solid #2a2e3e;border-radius:10px;
        padding:18px 22px;max-width:620px;margin:0 auto;
        display:flex;align-items:center;gap:16px;
    }
    .btc-access-icon{font-size:2rem;flex-shrink:0}
    .btc-access-info{flex:1;min-width:0}
    .btc-access-title{color:#d1d4dc;font-size:1rem;font-weight:700;margin-bottom:3px}
    .btc-access-sub{color:#555;font-size:0.8rem}
    </style>
    <div class="btc-divider">
        <div class="btc-divider-line"></div>
        <div class="btc-divider-txt">YA PHIR</div>
        <div class="btc-divider-line"></div>
    </div>
    <div class="btc-access-card">
        <div class="btc-access-icon">₿</div>
        <div class="btc-access-info">
            <div class="btc-access-title">Sirf BTCUSDT Chart dekhna hai?</div>
            <div class="btc-access-sub">Fyers login ki zaroorat nahi — Binance se seedha data aata hai (replay data load NAHI hota)</div>
        </div>
    </div>
    <div class="btc-access-card" style="margin-top:12px;">
        <div class="btc-access-icon">📼</div>
        <div class="btc-access-info">
            <div class="btc-access-title">Replay Mode chahiye?</div>
            <div class="btc-access-sub">Purana .gz replay data turant preload hoga — date select karte hi turant chalega, koi reload nahi</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<div style='max-width:620px;margin:10px auto 0;'>", unsafe_allow_html=True)
    if st.button("₿ BTC Chart Kholo (Fyers ke bina)", use_container_width=True, key="btc_only_btn"):
        # Sirf BTC-only mode ON karo — SV2 replay data yahan turant load
        # NAHI karna. Chart empty/lazy state me khulega; replay data tabhi
        # load hoga jab user khud SV2 me date select kare ya "Resume Last
        # Replay" dabaye (us waqt JS ka _sv2RequestDataLoad() ek
        # ?sv2_load=1 reload trigger karega jise Handler 3b handle karta
        # hai).
        _slog("👉 'BTC Chart Kholo (Fyers ke bina)' clicked → _btc_only_mode=True set kiya, st.rerun() call ho raha hai. "
              "(Threads AGLE run mein start hongi, isi click ke run mein nahi.)")
        st.session_state["_btc_only_mode"] = True
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='max-width:620px;margin:10px auto 0;'>", unsafe_allow_html=True)
    if st.button("📼 Replay Mode Kholo", use_container_width=True, key="replay_mode_btn"):
        # Replay Mode: SV2 (.gz) replay data ABHI eagerly preload karo
        # (BTC-only ke ulat) — taaki chart khulte hi date select turant
        # kaam kare, koi ?sv2_load=1 full-page reload na ho.
        _slog("👉 'Replay Mode Kholo' clicked → _replay_mode=True set kiya, st.rerun() call ho raha hai.")
        st.session_state["_replay_mode"] = True
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

