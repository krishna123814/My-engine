import json
import os
import time
import threading
import hashlib
import requests
import pyotp
import streamlit as st
import streamlit.components.v1 as components
import datetime

# ─── BN Historical Data Manager ──────────────────────────────────────────────
from bn_data_manager import (
    load_bin, update_from_fyers, get_stats, csv_to_bin,
    download_from_github, ensure_bin_file, GITHUB_URL,
    start_auto_update, get_auto_update_status,
    check_github_update, force_download_from_github,
)
import replay_data as _replay_data

# ─── Fast2SMS API key (hardcoded) ────────────────────────────────────────────
FAST2SMS_KEY = "TnrcsN4L3xpA8RVeG5dq1KhtWOiSEo7YyPFmlCIQHfjgavMwbU9iH7wDM2yjE5hkrROt06eBboJVa8u1"

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
DAILY_CACHE_TTL   = 300        # 5 min — aaj ki candle bhi update rahe
HIST_CACHE_TTL    = 300       # seconds for intraday cache (5 min — reduces API load)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Default credentials (user can override in sidebar)
DEFAULT_APP_ID    = "PPGUYSDHX7-100"
DEFAULT_SECRET    = "RWKTJYZ2YI"
DEFAULT_CLIENT_ID = "FAJ86844"
DEFAULT_PASSWORD  = "2552"
REDIRECT_URI      = "https://www.google.com"

# ─── Global live-tick store (updated by WebSocket thread) ─────────────────────
_LIVE: dict = {
    "ltp":       None,
    "prev_close": None,
    "ts":        0,
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
        if raw.get("s") == "ok" and "access_token" in raw:
            return True, raw["access_token"], raw
        return False, raw.get("message", str(raw)), raw
    except Exception as e:
        err = {"exception": str(e)}
        return False, str(e), err



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
        return False, f"Step1 Send OTP failed: {rkey}", log

    # Step 2: Verify TOTP
    totp_code = pyotp.TOTP(totp_secret).now()
    log["step2_totp_code_used"] = totp_code
    ok2, rkey2 = fyers_verify_otp(rkey, totp_code)
    log["step2_verify_otp"] = {"ok": ok2, "result": rkey2}
    if not ok2:
        return False, f"Step2 TOTP verify failed: {rkey2}", log

    # Step 3: Verify PIN/password
    ok3, token = fyers_verify_pin(rkey2, password)
    log["step3_verify_pin"] = {"ok": ok3, "result": token if not ok3 else "***token***"}
    if not ok3:
        return False, f"Step3 PIN verify failed: {token}", log

    # Step 4: Get auth_code
    ok4, auth_code = fyers_get_auth_code(token, client_id, app_id)
    log["step4_get_authcode"] = {"ok": ok4, "result": auth_code[:20] + "..." if ok4 and len(auth_code) > 20 else auth_code}
    if not ok4:
        return False, f"Step4 Auth code failed: {auth_code}", log

    # Step 5: Get access_token
    ok5, access_token, raw5 = fyers_get_access_token(app_id, secret_key, auth_code)
    log["step5_validate_authcode"] = {"ok": ok5, "response": raw5}
    if not ok5:
        return False, f"Step5 Access token failed: {access_token}", log

    # Save new token
    creds["access_token"] = access_token
    save_creds(creds)
    _sess_cache.update({"active": True, "ts": time.time()})
    # session_state yahan set nahi kar sakte (background thread) — caller karega
    return True, access_token, log



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

                # ── Try automatic re-login first (TOTP-based, no human needed) ──
                relogin_ok = False
                if creds.get("totp_secret"):
                    try:
                        relogin_ok, _msg, _log = auto_fyers_login()
                    except Exception:
                        relogin_ok = False

                if relogin_ok:
                    # Fresh token saved inside auto_fyers_login(); clear expired state
                    with _TOKEN_STATUS_LOCK:
                        _TOKEN_STATUS["expired"] = False
                    _sess_cache.update({"active": True, "ts": time.time()})
                    try:
                        if os.path.exists(".token_expired_flag"):
                            os.remove(".token_expired_flag")
                    except Exception:
                        pass
                    time.sleep(300)
                    continue

                # Write sentinel so next rerun clears _force_active
                try:
                    with open(".token_expired_flag", "w") as _f:
                        _f.write("1")
                except Exception:
                    pass
                # Auto re-login failed (or TOTP not configured) — alert human (once per hour max)
                _send_sms_alert(
                    "BankNifty Dashboard Alert: Fyers token expired and auto re-login failed! "
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
            candles = [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]]
                    for c in res.get("candles", [])]
            return candles
    except Exception:
        pass
    return []


def fetch_bn_intraday(interval_mins: int) -> list:
    """Seedha Fyers se fetch karo — no Supabase dependency."""
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
    """Seedha Fyers se daily candles fetch karo — no Supabase dependency."""
    today_str = _ist_now().strftime("%Y-%m-%d")
    all_candles = []
    seen = set()
    for yr in range(max(2020, _ist_now().year - 2), _ist_now().year + 1):  # Sirf last 3 saal
        for (from_d, to_d) in [(f"{yr}-01-01", f"{yr}-06-30"), (f"{yr}-07-01", f"{yr}-12-31")]:
            if from_d > today_str:
                break
            actual_to = min(to_d, today_str)
            chunk = _fyers_history_chunk("D", from_d, actual_to)
            for c in chunk:
                if c[0] not in seen:
                    seen.add(c[0])
                    all_candles.append(c)
            time.sleep(0.1)
    all_candles.sort(key=lambda x: x[0])
    return all_candles

# ─── BTC (Binance) ────────────────────────────────────────────────────────────
def fetch_btc(interval: str = "1m", limit: int = 1000) -> list:
    """Seedha Binance se fetch karo — no Supabase dependency."""
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
    """Seedha Binance se daily candles fetch karo — no Supabase dependency."""
    today_str = _ist_now().strftime("%Y-%m-%d")
    all_candles = []
    seen = set()
    for yr in range(max(2017, _ist_now().year - 3), _ist_now().year + 1):  # Sirf last 4 saal
        for (from_d, to_d) in [(f"{yr}-01-01", f"{yr}-06-30"), (f"{yr}-07-01", f"{yr}-12-31")]:
            if from_d > today_str:
                break
            actual_to = min(to_d, today_str)
            from_ms = int(datetime.datetime.strptime(from_d, "%Y-%m-%d").replace(
                tzinfo=datetime.timezone.utc).timestamp() * 1000)
            to_ms = int(datetime.datetime.strptime(actual_to, "%Y-%m-%d").replace(
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
                        if ts not in seen:
                            seen.add(ts)
                            all_candles.append([ts, float(x[1]), float(x[2]),
                                                float(x[3]), float(x[4]), float(x[5])])
            except Exception:
                pass
            time.sleep(0.05)
    all_candles.sort(key=lambda x: x[0])
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
            # Build running 1-minute candle from raw LTP ticks
            _update_candle_ltp(ltp)
            # Write bn_live.json so JS chart can poll it
            _write_live_json()
    except Exception:
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
    which made the JS-side dedupe drop intra-second ticks).

    Market band hone ke baad None return karta hai — JS chart stale data se
    bar-bar update na kare (jo SR lines ko galat draw karta tha)."""
    if not _is_nse_market_open():
        return None
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
    return {
        "ts":  now,
        "ltp": ltp,
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
        # bn_live.json = fallback file
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
            on_close=lambda msg: None,
            on_error=lambda msg: None,
            on_message=_on_ws_message,
        )
        t = threading.Thread(target=fyers_ws.connect, name="FyersWS", daemon=True)
        t.start()
        _ws_thread_started = True
    except Exception:
        pass

# ─── Background REST poller (fallback: polls Fyers 1m candles every 3s) ──────
def _is_nse_market_open() -> bool:
    """NSE market 9:15–15:30 IST, Mon–Fri, non-holiday."""
    now_ist = _ist_now()
    if now_ist.weekday() >= 5:   # Sat=5, Sun=6
        return False
    t = now_ist.hour * 60 + now_ist.minute
    return (9 * 60 + 15) <= t <= (15 * 60 + 30)

def _rest_live_loop():
    while True:
        # Market band ho to REST calls skip karo — stale data bhejne se
        # JS chart mein SR lines dobara draw hoti thi aur price flicker hoti thi.
        if not _is_nse_market_open():
            time.sleep(5)
            continue
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
                                    _LIVE["ltp"] = c
                                    _LIVE["ts"]  = int(time.time())
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
    _start_ws()
    _register_api_route()

    # ── BN 1m data auto-update: app open hone par Fyers se latest data fetch ──
    if "BNAutoUpdater" not in names:
        creds = load_creds()
        if creds.get("access_token") and creds.get("app_id"):
            start_auto_update(creds["app_id"], creds["access_token"])

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


# ─── Replay data endpoint — in-memory cache (asset|from|to → data) ────────────
_REPLAY_CACHE: dict = {}
_REPLAY_CACHE_LOCK = threading.Lock()
_REPLAY_CACHE_TTL = 1800  # 30 min — BTC fetch especially is expensive, reuse it


def _replay_cache_key(asset: str, from_date: str, to_date: str) -> str:
    return f"{asset}|{from_date}|{to_date}"


def _get_replay_data_cached(asset: str, from_date: str, to_date: str) -> dict:
    key = _replay_cache_key(asset, from_date, to_date)
    now = time.time()
    with _REPLAY_CACHE_LOCK:
        entry = _REPLAY_CACHE.get(key)
        if entry and (now - entry["ts"] < _REPLAY_CACHE_TTL):
            return entry["data"]
    data = _replay_data.get_replay_data(asset, from_date, to_date)
    with _REPLAY_CACHE_LOCK:
        _REPLAY_CACHE[key] = {"ts": now, "data": data}
    return data


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

                if parsed.path == "/api/replay_data":
                    qs = _up.parse_qs(parsed.query, keep_blank_values=False)
                    def _qr(k, d=""): return qs.get(k, [d])[0]
                    asset = _qr("asset", "BANKNIFTY").upper()
                    from_date = _qr("from", "")
                    to_date   = _qr("to", "")
                    if not from_date or not to_date:
                        body = json.dumps({"error": "from/to date required"}).encode()
                        self.send_response(400)
                    else:
                        try:
                            data = _get_replay_data_cached(asset, from_date, to_date)
                            body = json.dumps(data).encode()
                            self.send_response(200)
                        except Exception as e:
                            body = json.dumps({"error": str(e)}).encode()
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

# Handler 2b: REMOVED — rp_asset top-level handler hata diya gaya.
# form.submit() se query params aate the → st.rerun() → full app restart.
# Ab chart.html postMessage bhejta hai, _replay_bridge() fragment pakadta hai.
# Fragment apne aap 1s mein rerun hota hai — koi manual st.rerun() nahi.

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


creds      = load_creds()
sess_active = is_session_active()

if sess_active:
    _ensure_live_threads()

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

    # ── Historical Data Status + Manual Update ──────────────────────────────
    with st.expander("📦 Historical Data", expanded=False):
        _stats = get_stats()
        if _stats.get("exists"):
            st.success(f"✅ {_stats['count']:,} candles")
            st.caption(f"From: {_stats.get('first','?')}")
            st.caption(f"To:   {_stats.get('last','?')}")
            st.caption(f"Size: {_stats.get('size_mb','?')} MB")
        else:
            st.error("❌ bn_1m.bin.gz not found")
            _github_url = st.text_input(
                "GitHub Raw URL",
                value=GITHUB_URL,
                placeholder="https://raw.githubusercontent.com/...",
                key="github_url_inp",
            )
            if st.button("⬇️ GitHub se Download", use_container_width=True, key="github_dl_btn"):
                with st.spinner("Downloading from GitHub..."):
                    _ok_dl = download_from_github(url=_github_url.strip())
                if _ok_dl:
                    st.success("✅ Download complete!")
                    _get_chart_data.clear()
                    st.rerun()
                else:
                    st.error("❌ Download failed — GitHub URL sahi hai? File publicly accessible hai?")

        # ── Auto-update status ──────────────────────────────────────────────
        _au = get_auto_update_status()
        if _au["running"]:
            st.info("⏳ Auto-update chal raha hai...")
        elif _au["last_result"]:
            _r = _au["last_result"]
            if _r.get("error"):
                st.warning(f"⚠️ Auto-update error: {_r['error']}")
            elif _r.get("added", 0) > 0:
                st.success(f"🔄 Auto-updated: +{_r['added']} candles (till {_r.get('last_date','')})")
            elif _r.get("skipped"):
                st.caption(f"✅ Data already current till {_r.get('last_date','')}")

        if sess_active and _stats.get("exists"):
            if st.button("🔄 Force Update (Fyers)", use_container_width=True, key="hist_force_upd"):
                with st.spinner("Fyers se update ho raha hai..."):
                    _r = _maybe_update_historical(creds, force=True)
                if _r.get("added", 0) > 0:
                    st.success(f"✅ +{_r['added']} nayi candles")
                    st.rerun()
                elif _r.get("skipped"):
                    st.info("Already up to date")
                else:
                    st.warning("Koi naya data nahi mila")

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


# ─── Historical data auto-update logic ───────────────────────────────────────
def _maybe_update_historical(creds: dict, force: bool = False) -> dict:
    """
    3:35 PM IST ke baad ya force=True par Fyers se historical update karo.
    Session mein ek baar hi chalta hai.
    """
    update_key = "bn_hist_updated_today"
    now_ist    = _ist_now()
    today_str  = now_ist.strftime("%Y-%m-%d")

    if not force:
        if st.session_state.get(update_key) == today_str:
            return {"skipped": True, "reason": "already updated today"}
        close_time = now_ist.replace(hour=15, minute=35, second=0, microsecond=0)
        if now_ist < close_time:
            return {"skipped": True, "reason": "market not closed"}

    app_id       = creds.get("app_id", "")
    access_token = creds.get("access_token", "")
    if not app_id or not access_token:
        return {"skipped": True, "reason": "no fyers creds"}

    result = update_from_fyers(app_id, access_token, force=force)
    if not result.get("skipped"):
        st.session_state[update_key] = today_str
        _get_chart_data.clear()   # Cache invalidate → fresh data load hoga
    return result


# ─── Fetch all chart data ─────────────────────────────────────────────────────
# _hist_ts changes jab file update hoti hai → cache auto-invalidate
@st.cache_data(ttl=HIST_CACHE_TTL, show_spinner=False)
def _get_chart_data(sess: bool, _tok: str = "", _hist_ts: int = 0):
    """
    Historical + live data merge karke deta hai.
    _hist_ts: bn_1m.bin.gz ki mtime — nayi file par cache reset hoti hai.
    """
    btc_1m   = fetch_btc("1m",  1000)
    btc_15m  = fetch_btc("15m", 1000)
    btc_day  = load_btc_daily()

    # ── Google Drive se auto-download agar local file nahi hai ───────────────
    ensure_bin_file()          # File nahi → Drive se download
    hist_all = load_bin(auto_download=False)   # Already ensured above

    if sess:
        live_1m  = fetch_bn_intraday(1)
        live_5m  = fetch_bn_intraday(5)
        live_15m = fetch_bn_intraday(15)
        live_45m = fetch_bn_intraday(45)
        bn_day   = load_bn_daily()

        # Merge: historical + live (dedup by timestamp)
        if hist_all and live_1m:
            live_ts   = {r[0] for r in live_1m}
            hist_trim = [r for r in hist_all if r[0] not in live_ts]
            bn_1m     = hist_trim + live_1m
        elif hist_all:
            bn_1m = hist_all
        else:
            bn_1m = live_1m

        bn_5m  = live_5m
        bn_15m = live_15m
        bn_45m = live_45m
    else:
        # No Fyers session — sirf historical file use karo (replay mode)
        bn_1m  = hist_all
        bn_5m  = []
        bn_15m = []
        bn_45m = []
        bn_day = []

    return btc_1m, btc_15m, btc_day, bn_1m, bn_5m, bn_15m, bn_45m, bn_day


# Auto-update check + chart data load
_hist_update_result = _maybe_update_historical(creds) if sess_active else {"skipped": True}

# Auto-update status dikhao
if not _hist_update_result.get("skipped"):
    _added = _hist_update_result.get("added", 0)
    if _added > 0:
        st.info(f"🔁 Auto-updated: +{_added} candles")

_hist_file_ts = int(os.path.getmtime("bn_1m.bin.gz")) if os.path.exists("bn_1m.bin.gz") else 0

_tok_hint = creds.get("access_token", "")[:8] if sess_active else ""

# Cache miss par Fyers + Binance calls slow hoti hain — spinner dikhao
with st.spinner("📡 Chart data load ho raha hai... (pehli baar 20-30 sec lag sakte hain)"):
    btc_1m, btc_15m, btc_day, bn_1m, bn_5m, bn_15m, bn_45m, bn_day = _get_chart_data(
        sess_active, _tok_hint, _hist_file_ts
    )

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

    def _to_lwc(candles: list, max_candles: int = 0) -> str:
        """Convert [[epoch_ms, o, h, l, c, v], ...] or [{time,open,...}] to LWC format.
        max_candles: 0 = no limit, otherwise keep only last N candles (mobile RAM fix).
        """
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
        sorted_out = sorted(seen.values(), key=lambda x: x["time"])
        # Trim to last N candles to prevent mobile browser OOM crash
        if max_candles > 0 and len(sorted_out) > max_candles:
            sorted_out = sorted_out[-max_candles:]
        return _json.dumps(sorted_out)

    _creds = load_creds()
    status = "connected" if sess_active else "disconnected"
    app_id  = _creds.get("app_id",    DEFAULT_APP_ID)
    secret  = _creds.get("secret_key", DEFAULT_SECRET)

    html = html.replace("__BTC_CANDLES__", _to_lwc(btc_1m, max_candles=2160))   # ~1.5 days 1m
    html = html.replace("__BTC_15M__",     _to_lwc(btc_15m, max_candles=500))
    html = html.replace("__BTC_DAILY__",   _to_lwc(btc_day))
    html = html.replace("__BN_CANDLES__",  _to_lwc(bn_1m,  max_candles=1170))   # ~3 days 1m (375 bars/day)
    html = html.replace("__BN_5M__",       _to_lwc(bn_5m,  max_candles=300))    # ~25 days 5m
    html = html.replace("__BN_15M__",      _to_lwc(bn_15m, max_candles=200))
    html = html.replace("__BN_45M__",      _to_lwc(bn_45m, max_candles=150))
    html = html.replace("__BN_DAILY__",    _to_lwc(bn_day))
    html = html.replace("__FYERS_STATUS__", status)
    html = html.replace("__FYERS_APP_ID__", app_id)
    html = html.replace("__FYERS_SECRET__",  secret)

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
    # Market band ho to stale bn_live.json inject mat karo — warna chart pe
    # old price dikhega aur SR lines galat draw hongi (bug fix)
    tick = None
    if _is_nse_market_open():
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
_btc_only = st.session_state.get("_btc_only_mode", False)

if sess_active or _btc_only:

    # ── Data Update Check (chart se pehle, ek baar) ──────────────────────────
    _upd_done    = st.session_state.get("_data_update_done", False)
    _upd_skipped = st.session_state.get("_data_update_skipped", False)

    if not _upd_done and not _upd_skipped:
        # Auto-check GitHub (cache session mein)
        if "gh_check_result" not in st.session_state:
            with st.spinner("📡 Data update check ho raha hai..."):
                st.session_state["gh_check_result"] = check_github_update()

        _gh = st.session_state["gh_check_result"]
        _gh_st = _gh.get("status", "error")

        # Up-to-date bhi ho, phir bhi user se explicit click lo — auto-redirect nahi
        if _gh_st == "up_to_date":
            st.markdown("## 📊 BankNifty Live Chart")

            _stats_now = get_stats()
            _last_candle = _stats_now.get("last", "?") if _stats_now.get("exists") else "file missing"

            st.markdown(f"""
            <div style='background:#0d1f17;border:1px solid #1a4731;border-radius:10px;
                        padding:16px 20px;margin-bottom:12px;'>
                <div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;'>
                    <span style='font-size:1.3rem'>✅</span>
                    <span style='color:#26a69a;font-weight:700;font-size:1rem;'>
                        GitHub se match karti hai (ETag same)
                    </span>
                </div>
                <div style='color:#787b86;font-size:0.78rem;'>
                    GitHub: {_gh.get('github_modified','?')}<br>
                    Local:&nbsp; {_gh.get('local_modified','?')}
                </div>
                <div style='color:#f0b429;font-size:0.85rem;margin-top:8px;font-weight:700;'>
                    📅 Data ki aakhri candle: {_last_candle}
                </div>
                <div style='color:#555;font-size:0.72rem;margin-top:2px;'>
                    (Yeh asli freshness hai — upar wala ETag sirf GitHub=Local match batata hai)
                </div>
            </div>
            """, unsafe_allow_html=True)

            # ── Update/Download buttons sirf PEHLI screen par hain — yahan
            # sirf info dikhao, taaki confusion na ho ───────────────────────

            if st.button("⏭️ Skip — Chart Kholo", use_container_width=True,
                         key="upd_sess_skip_uptodate"):
                st.session_state["_data_update_done"] = True
                st.rerun()
            st.stop()  # jab tak button na dabe, chart nahi khulega

        else:
            # Show update card — chart abhi nahi dikhega
            st.markdown("## 📊 BankNifty Live Chart")

            if _gh_st == "outdated":
                st.markdown(f"""
                <div style='background:#1a1500;border:1px solid #3d2e00;border-radius:10px;
                            padding:16px 20px;margin-bottom:12px;'>
                    <div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;'>
                        <span style='font-size:1.3rem'>⚠️</span>
                        <span style='color:#f0b429;font-weight:700;font-size:1rem;'>
                            GitHub par nayi data file hai!
                        </span>
                    </div>
                    <div style='color:#787b86;font-size:0.78rem;'>
                        GitHub: {_gh.get('github_modified','?')}<br>
                        Local:&nbsp; {_gh.get('local_modified','?')}
                    </div>
                </div>
                """, unsafe_allow_html=True)
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("⬇️ Abhi Update Karo", use_container_width=True,
                                 type="primary", key="upd_sess_now"):
                        with st.spinner("⬇️ Download ho raha hai..."):
                            _dl = force_download_from_github()
                        if _dl["ok"]:
                            st.success(f"✅ Done! {_dl['size_mb']} MB")
                            st.session_state["_data_update_done"] = True
                            st.session_state["gh_check_result"]   = None
                            _get_chart_data.clear()
                            st.rerun()
                        else:
                            st.error(f"❌ {_dl['error']}")
                            st.session_state["_data_update_skipped"] = True
                            st.rerun()
                with _c2:
                    if st.button("⏭️ Skip — Chart kholo", use_container_width=True,
                                 key="upd_sess_skip"):
                        st.session_state["_data_update_skipped"] = True
                        st.rerun()
            
            elif _gh_st == "no_local":
                st.error("❌ bn_1m.bin.gz local mein nahi hai — download karo")
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("⬇️ Download Karo", use_container_width=True,
                                 type="primary", key="upd_sess_dl"):
                        with st.spinner("⬇️ Download ho raha hai..."):
                            _dl = force_download_from_github()
                        if _dl["ok"]:
                            st.success(f"✅ Done! {_dl['size_mb']} MB")
                            st.session_state["_data_update_done"] = True
                            st.session_state["gh_check_result"]   = None
                            _get_chart_data.clear()
                            st.rerun()
                        else:
                            st.error(f"❌ {_dl['error']}")
                with _c2:
                    if st.button("⏭️ Skip", use_container_width=True, key="upd_sess_skip2"):
                        st.session_state["_data_update_skipped"] = True
                        st.rerun()

            else:  # error
                _err = _gh.get("error", "Unknown error")
                _is_retry = any(k in _err.lower() for k in ["timeout","thodi der","retry","503","502","429"])
                st.markdown(f"""
                <div style='background:#1a0c0c;border:1px solid #3e1a1a;border-radius:10px;
                            padding:16px 20px;margin-bottom:12px;'>
                    <div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;'>
                        <span style='font-size:1.3rem'>{'⏳' if _is_retry else '❌'}</span>
                        <span style='color:#ef5350;font-weight:700;'>
                            {'GitHub check timeout' if _is_retry else 'GitHub check failed'}
                        </span>
                    </div>
                    <div style='color:#ef5350;font-size:0.75rem;background:#0d0505;
                                border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-word;'>
{_err}
                    </div>
                </div>
                """, unsafe_allow_html=True)
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("🔄 Retry", use_container_width=True,
                                 type="primary", key="upd_sess_retry"):
                        with st.spinner("Check ho raha hai..."):
                            st.session_state["gh_check_result"] = check_github_update()
                        st.rerun()
                with _c2:
                    if st.button("⏭️ Skip — Chart kholo", use_container_width=True,
                                 key="upd_sess_skip3"):
                        st.session_state["_data_update_skipped"] = True
                        st.rerun()

            st.stop()  # chart render mat karo jab tak update/skip na ho

    # ── Chart render ─────────────────────────────────────────────────────────
    if sess_active:
        st.success("✅ Fyers connected — live data active")
    else:
        st.info("📊 BTC Chart mode — BankNifty data available nahi (Fyers login nahi hai)")
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
            # Market band ho to postMessage push skip karo — stale tick se
            # JS chart SR lines dobara draw karta tha (bug fix)
            if not _is_nse_market_open():
                return

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

    # ── Replay postMessage bridge ─────────────────────────────────────────────
    # ARCHITECTURE (restart-free):
    #
    # 1. chart.html JS → window.parent.postMessage({type:'rp_request',...})
    #    (form.submit() hata diya — woh full page reload karta tha)
    #
    # 2. Streamlit parent page pe ek injected JS listener hai jo yeh message
    #    pakad ke ek temp file ".rp_pending.json" mein likh deta hai —
    #    koi query_params change nahi, koi navigation nahi.
    #
    # 3. _replay_bridge fragment har 1s pe woh file check karta hai.
    #    File milne par → data fetch → rp_data postMessage → file delete.
    #    Fragment kabhi st.rerun() nahi karta — sirf components.html inject karta hai.
    #
    # Yeh poora flow bina kisi app restart ke kaam karta hai.

    # Step 2: Parent page pe listener inject karo (sirf ek baar, idempotent guard hai)
    # FIX: sessionStorage Python nahi padh sakta. Ab JS seedha window.parent ka
    # URL update karta hai query_params ke zariye — Streamlit fragment woh padh leta hai.
    _listener_js = """
<script>
(function(){
  if (window._rpListenerReady) return;
  window._rpListenerReady = true;
  window.addEventListener('message', function(evt){
    try {
      var msg = (typeof evt.data === 'string') ? JSON.parse(evt.data) : evt.data;
      if (!msg || msg.type !== 'rp_request') return;
      // Seedha parent window ka URL update karo query_params se
      // Streamlit fragment yeh params padh leta hai bina page reload ke
      var params = new URLSearchParams(window.parent.location.search);
      params.set('rp_asset', msg.asset || 'BANKNIFTY');
      params.set('rp_from',  msg.from  || '');
      params.set('rp_to',    msg.to    || '');
      params.set('rp_req',   msg.req_id || String(Date.now()));
      // replaceState — page reload NAHI hoga, sirf URL update hoga
      window.parent.history.replaceState(null, '', '?' + params.toString());
    } catch(e){}
  });
})();
</script>
"""
    components.html(_listener_js, height=0, scrolling=False)

    @st.fragment(run_every=1)
    def _replay_bridge():
        qp = st.query_params
        if "rp_asset" not in qp:
            return
        rp_asset  = qp.get("rp_asset", "BANKNIFTY").upper()
        rp_from   = qp.get("rp_from", "")
        rp_to     = qp.get("rp_to", "")
        # Query params clear karo — lekin st.rerun() BILKUL MAT KARO
        # Fragment apne aap 1s mein rerun hota hai — manual rerun = full app restart
        try:
            st.query_params.clear()
        except Exception:
            pass
        if not rp_from or not rp_to:
            return
        try:
            data = _get_replay_data_cached(rp_asset, rp_from, rp_to)
            import json as _json
            data_js = _json.dumps(data)
            _script = f"""
<script>
(function(){{
  var data = {data_js};
  var msg  = JSON.stringify({{ type: 'rp_data', asset: '{rp_asset}', data: data }});
  // Apne parent ke saare iframes ko postMessage bhejo
  var frames = window.parent ? window.parent.document.querySelectorAll('iframe')
                             : document.querySelectorAll('iframe');
  for(var i=0;i<frames.length;i++){{
    try{{ frames[i].contentWindow.postMessage(msg, '*'); }}catch(e){{}}
  }}
  // Apne aap ko bhi bhejo (agar seedha same window mein hai)
  try{{ window.postMessage(msg, '*'); }}catch(e){{}}
}})();
</script>"""
            components.html(_script, height=0, scrolling=False)
        except Exception as e:
            _err_script = f"""
<script>
(function(){{
  var msg = JSON.stringify({{type:'rp_error',error:{repr(str(e))}}});
  var frames = window.parent ? window.parent.document.querySelectorAll('iframe')
                             : document.querySelectorAll('iframe');
  for(var i=0;i<frames.length;i++){{
    try{{ frames[i].contentWindow.postMessage(msg,'*'); }}catch(_){{}}
  }}
}})();
</script>"""
            components.html(_err_script, height=0, scrolling=False)

    _replay_bridge()

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
            <div class="btc-access-sub">Fyers login ki zaroorat nahi — Binance se seedha data aata hai</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<div style='max-width:620px;margin:10px auto 0;'>", unsafe_allow_html=True)
    if st.button("₿ BTC Chart Kholo (Fyers ke bina)", use_container_width=True, key="btc_only_btn"):
        st.session_state["_btc_only_mode"] = True
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Update Data Section ────────────────────────────────────────────────────
    st.markdown("""
    <div style='display:flex;align-items:center;gap:12px;margin:28px auto 18px;max-width:620px;'>
        <div style='flex:1;height:1px;background:#2a2e3e'></div>
        <div style='color:#555;font-size:0.8rem;white-space:nowrap'>DATA UPDATE</div>
        <div style='flex:1;height:1px;background:#2a2e3e'></div>
    </div>
    """, unsafe_allow_html=True)

    # ── ASLI update: Fyers se naya data khinch ke local bin.gz mein bharo ────
    # (yeh GitHub-check se ALAG hai — yeh sach mein naya data laata hai)
    from bn_data_manager import BIN_FILE
    _creds_now = load_creds()

    _stats_pre = get_stats()

    if _creds_now.get("access_token"):
        st.markdown("<div style='max-width:620px;margin:0 auto 10px;'>", unsafe_allow_html=True)
        if st.button("🔄 Fyers se Naya Data Lao (Asli Update)", use_container_width=True,
                     type="primary", key="real_fyers_update_btn"):
            # Update se PEHLE ki file ki details save kar lo, taaki baad mein
            # purani vs nayi side-by-side dikha sakein
            st.session_state["_old_stats_snapshot"] = _stats_pre
            try:
                with st.spinner("📡 Fyers se naya data aa raha hai..."):
                    _res = update_from_fyers(
                        _creds_now["app_id"], _creds_now["access_token"], force=True
                    )
            except Exception:
                import traceback
                st.session_state["_update_error"] = traceback.format_exc()
                _res = None

            if _res is not None:
                if _res.get("skipped"):
                    st.session_state["_update_error"]  = None
                    st.session_state["_update_skipped"] = _res.get("reason", "?")
                elif _res.get("error"):
                    st.session_state["_update_error"]  = _res["error"]
                    st.session_state["_update_skipped"] = None
                else:
                    st.session_state["_update_error"]   = None
                    st.session_state["_update_skipped"] = None
                    st.session_state["gh_check_result"]  = None
                    _get_chart_data.clear()
            st.session_state["_new_stats_snapshot"] = get_stats()
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.caption("⚠️ Fyers se login nahi hai — pehle login karo, fir 'Asli Update' chalega")

    # ── Result dikhao — agar kabhi update try hua ho is session mein ────────
    _old_s = st.session_state.get("_old_stats_snapshot")
    _new_s = st.session_state.get("_new_stats_snapshot")
    _upd_err = st.session_state.get("_update_error")
    _upd_skip_reason = st.session_state.get("_update_skipped")

    if _upd_err:
        st.error("❌ Update karte waqt error aaya:")
        st.code(_upd_err)
    elif _upd_skip_reason:
        st.warning(f"⏭️ Skip ho gaya — reason: {_upd_skip_reason}")

    if _old_s is not None and _new_s is not None:
        # Update kabhi try hua tha is session mein — purani vs nayi dikhao
        _o_last = _old_s.get("last", "—") if _old_s.get("exists") else "file thi nahi"
        _n_last = _new_s.get("last", "—") if _new_s.get("exists") else "file nahi hai"
        _o_cnt  = _old_s.get("count", "—")
        _n_cnt  = _new_s.get("count", "—")
        _changed = (_o_last != _n_last)
        st.markdown(f"""
        <div style='max-width:620px;margin:0 auto 10px;background:#131722;
                    border:1px solid {'#1a4731' if _changed else '#2a2e3e'};border-radius:8px;
                    padding:12px 16px;'>
            <div style='display:flex;justify-content:space-between;gap:12px;'>
                <div style='flex:1;'>
                    <div style='color:#555;font-size:0.72rem;'>PURANI FILE</div>
                    <div style='color:#787b86;font-size:0.85rem;font-weight:600;'>{_o_last}</div>
                    <div style='color:#555;font-size:0.7rem;'>{_o_cnt} candles</div>
                </div>
                <div style='color:#555;font-size:1.2rem;align-self:center;'>→</div>
                <div style='flex:1;'>
                    <div style='color:#555;font-size:0.72rem;'>NAYI FILE (ABHI)</div>
                    <div style='color:{"#26a69a" if _changed else "#787b86"};font-size:0.85rem;font-weight:700;'>{_n_last}</div>
                    <div style='color:#555;font-size:0.7rem;'>{_n_cnt} candles</div>
                </div>
            </div>
            {"<div style='color:#26a69a;font-size:0.78rem;margin-top:8px;'>✅ Naya data add hua</div>" if _changed else "<div style='color:#f0b429;font-size:0.78rem;margin-top:8px;'>⚠️ Koi badlaav nahi hua — date same hai</div>"}
        </div>
        """, unsafe_allow_html=True)
    else:
        # Update kabhi try nahi hua is session mein — sirf current file dikhao
        if _stats_pre.get("exists"):
            st.markdown(f"""
            <div style='max-width:620px;margin:0 auto 10px;background:#131722;
                        border:1px solid #2a2e3e;border-radius:8px;padding:10px 16px;'>
                <span style='color:#787b86;font-size:0.85rem;'>
                    📅 Abhi file ki aakhri candle: <b style='color:#f0b429'>{_stats_pre.get('last','?')}</b>
                    &nbsp;|&nbsp; Size: {_stats_pre.get('size_mb','?')} MB
                </span>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.warning("⚠️ bn_1m.bin.gz abhi maujood nahi hai")

    # ── Mobile-friendly: bn_1m.bin.gz seedha phone me download karo ──────────
    # (taaki GitHub par manually upload kiya ja sake, bina laptop ke)
    if os.path.exists(BIN_FILE):
        _bin_size_mb = os.path.getsize(BIN_FILE) / 1024 / 1024
        st.markdown("<div style='max-width:620px;margin:0 auto 14px;'>", unsafe_allow_html=True)
        with open(BIN_FILE, "rb") as _f:
            st.download_button(
                label=f"📥 bn_1m.bin.gz Download Karo ({_bin_size_mb:.1f} MB)",
                data=_f.read(),
                file_name="bn_1m.bin.gz",
                mime="application/gzip",
                use_container_width=True,
                key="dl_bin_btn",
            )
        st.caption("Download karne ke baad GitHub par 'Add file → Upload files' se daal dein")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.caption("⚠️ bn_1m.bin.gz file abhi server par maujood nahi hai")

    # Check karo — already skipped hai session mein?
    _upd_skipped = st.session_state.get("_data_update_skipped", False)
    _upd_done    = st.session_state.get("_data_update_done", False)

    if _upd_done:
        st.markdown("""
        <div style='background:#0d1f17;border:1px solid #1a4731;border-radius:10px;
                    padding:14px 18px;max-width:620px;margin:0 auto;
                    display:flex;align-items:center;gap:12px;'>
            <span style='font-size:1.5rem'>✅</span>
            <div>
                <div style='color:#26a69a;font-weight:700;font-size:0.95rem'>Data Updated!</div>
                <div style='color:#555;font-size:0.8rem'>bn_1m.bin.gz latest version hai</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    elif _upd_skipped:
        st.markdown("""
        <div style='background:#131722;border:1px solid #2a2e3e;border-radius:10px;
                    padding:14px 18px;max-width:620px;margin:0 auto;
                    display:flex;align-items:center;gap:12px;'>
            <span style='font-size:1.5rem'>⏭️</span>
            <div>
                <div style='color:#787b86;font-weight:700;font-size:0.95rem'>Update Skip kiya</div>
                <div style='color:#555;font-size:0.8rem'>Historical data update nahi hua</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("<div style='max-width:620px;margin:8px auto 0;'>", unsafe_allow_html=True)
        if st.button("🔄 Phir se try karo", use_container_width=True, key="upd_retry_btn"):
            st.session_state["_data_update_skipped"] = False
            st.session_state["_data_update_done"]    = False
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    else:
        # Check result session mein cache karo
        if "gh_check_result" not in st.session_state:
            st.session_state["gh_check_result"] = None

        _gh_res = st.session_state.get("gh_check_result")

        # Auto-check on first load (agar result nahi hai)
        if _gh_res is None:
            with st.spinner("📡 GitHub check ho raha hai..."):
                _gh_res = check_github_update()
            st.session_state["gh_check_result"] = _gh_res

        _gh_status = _gh_res.get("status", "error")

        # ── Status card ────────────────────────────────────────────────────
        if _gh_status == "up_to_date":
            st.markdown(f"""
            <div style='background:#0d1f17;border:1px solid #1a4731;border-radius:10px;
                        padding:14px 18px;max-width:620px;margin:0 auto;'>
                <div style='display:flex;align-items:center;gap:10px;margin-bottom:6px;'>
                    <span style='font-size:1.2rem'>✅</span>
                    <span style='color:#26a69a;font-weight:700;'>Data already latest hai</span>
                </div>
                <div style='color:#555;font-size:0.78rem;padding-left:2px;'>
                    GitHub: {_gh_res.get('github_modified','?')}<br>
                    Local:&nbsp; {_gh_res.get('local_modified','?')}
                </div>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("<div style='max-width:620px;margin:8px auto 0;'>", unsafe_allow_html=True)
            if st.button("⏭️ Skip — Chart par jao", use_container_width=True, key="upd_skip_ok_btn"):
                st.session_state["_data_update_skipped"] = True
                st.session_state["_data_update_done"]    = True
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

        elif _gh_status == "outdated":
            st.markdown(f"""
            <div style='background:#1a1500;border:1px solid #3d2e00;border-radius:10px;
                        padding:14px 18px;max-width:620px;margin:0 auto;'>
                <div style='display:flex;align-items:center;gap:10px;margin-bottom:6px;'>
                    <span style='font-size:1.2rem'>⚠️</span>
                    <span style='color:#f0b429;font-weight:700;'>GitHub par nayi file available hai!</span>
                </div>
                <div style='color:#787b86;font-size:0.78rem;padding-left:2px;'>
                    GitHub: {_gh_res.get('github_modified','?')}<br>
                    Local:&nbsp; {_gh_res.get('local_modified','?')}
                </div>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("<div style='max-width:620px;margin:8px auto 0;'>", unsafe_allow_html=True)
            _c1, _c2 = st.columns(2)
            with _c1:
                if st.button("⬇️ Abhi Update Karo", use_container_width=True,
                             type="primary", key="upd_now_btn"):
                    with st.spinner("⬇️ GitHub se download ho raha hai..."):
                        _dl = force_download_from_github()
                    if _dl["ok"]:
                        st.success(f"✅ Done! {_dl['size_mb']} MB downloaded")
                        st.session_state["_data_update_done"]    = True
                        st.session_state["_data_update_skipped"] = False
                        st.session_state["gh_check_result"]      = None
                        _get_chart_data.clear()
                        st.rerun()
                    else:
                        st.error(f"❌ Failed: {_dl['error']}")
                        st.session_state["gh_check_result"] = {
                            "status": "error", "error": _dl["error"],
                            "github_modified": None, "local_modified": None,
                        }
                        st.rerun()
            with _c2:
                if st.button("⏭️ Skip", use_container_width=True, key="upd_skip_outdated_btn"):
                    st.session_state["_data_update_skipped"] = True
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

        elif _gh_status == "no_local":
            st.markdown("""
            <div style='background:#1a0c0c;border:1px solid #3e1a1a;border-radius:10px;
                        padding:14px 18px;max-width:620px;margin:0 auto;'>
                <div style='display:flex;align-items:center;gap:10px;margin-bottom:4px;'>
                    <span style='font-size:1.2rem'>❌</span>
                    <span style='color:#ef5350;font-weight:700;'>Local data file nahi hai!</span>
                </div>
                <div style='color:#787b86;font-size:0.78rem;'>
                    bn_1m.bin.gz missing — GitHub se download karo
                </div>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("<div style='max-width:620px;margin:8px auto 0;'>", unsafe_allow_html=True)
            _c1, _c2 = st.columns(2)
            with _c1:
                if st.button("⬇️ Download Karo", use_container_width=True,
                             type="primary", key="upd_dl_nolocal_btn"):
                    with st.spinner("⬇️ Downloading..."):
                        _dl = force_download_from_github()
                    if _dl["ok"]:
                        st.success(f"✅ Done! {_dl['size_mb']} MB")
                        st.session_state["_data_update_done"]    = True
                        st.session_state["_data_update_skipped"] = False
                        st.session_state["gh_check_result"]      = None
                        _get_chart_data.clear()
                        st.rerun()
                    else:
                        st.error(f"❌ Failed: {_dl['error']}")
            with _c2:
                if st.button("⏭️ Skip", use_container_width=True, key="upd_skip_nolocal_btn"):
                    st.session_state["_data_update_skipped"] = True
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

        elif _gh_status == "error":
            _err_msg = _gh_res.get("error", "Unknown error")
            _is_retry = any(k in _err_msg.lower() for k in ["timeout","thodi der","retry","503","502","429"])
            st.markdown(f"""
            <div style='background:#1a0c0c;border:1px solid #3e1a1a;border-radius:10px;
                        padding:14px 18px;max-width:620px;margin:0 auto;'>
                <div style='display:flex;align-items:center;gap:10px;margin-bottom:6px;'>
                    <span style='font-size:1.2rem'>{'⏳' if _is_retry else '❌'}</span>
                    <span style='color:#ef5350;font-weight:700;'>
                        {'Thoda time lagega' if _is_retry else 'GitHub check failed'}
                    </span>
                </div>
                <div style='color:#ef5350;font-size:0.75rem;background:#0d0505;
                            border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-word;'>
{_err_msg}
                </div>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("<div style='max-width:620px;margin:8px auto 0;'>", unsafe_allow_html=True)
            _c1, _c2 = st.columns(2)
            with _c1:
                _retry_label = "🔄 Retry" if _is_retry else "⬇️ Force Download"
                if st.button(_retry_label, use_container_width=True,
                             type="primary", key="upd_err_retry_btn"):
                    if _is_retry:
                        with st.spinner("Retry ho raha hai..."):
                            _new_res = check_github_update()
                        st.session_state["gh_check_result"] = _new_res
                        st.rerun()
                    else:
                        with st.spinner("⬇️ Force download ho raha hai..."):
                            _dl = force_download_from_github()
                        if _dl["ok"]:
                            st.success(f"✅ Done! {_dl['size_mb']} MB")
                            st.session_state["_data_update_done"]    = True
                            st.session_state["_data_update_skipped"] = False
                            st.session_state["gh_check_result"]      = None
                            _get_chart_data.clear()
                            st.rerun()
                        else:
                            st.error(f"❌ {_dl['error']}")
            with _c2:
                if st.button("⏭️ Skip", use_container_width=True, key="upd_skip_err_btn"):
                    st.session_state["_data_update_skipped"] = True
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

