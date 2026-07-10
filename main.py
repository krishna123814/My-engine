import io
import json
import os
import time
import threading
import hashlib
import zipfile
import requests
import pyotp
import streamlit as st
import streamlit.components.v1 as components
import datetime

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
DAILY_CACHE_FILE  = "btc_daily_cache.json"
BN_DAILY_CACHE    = "bn_daily_cache.json"
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
        # Log to file for debugging
        _write_login_log(payload, resp.status_code, raw)
        if raw.get("s") == "ok" and "access_token" in raw:
            return True, raw["access_token"], raw
        return False, raw.get("message", str(raw)), raw
    except Exception as e:
        err = {"exception": str(e)}
        _write_login_log(payload, 0, err)
        return False, str(e), err


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

def _sv2_resample_bn_intraday(rows: list, tf_min: int) -> list:
    """BN 1m data ko intraday TF mein resample karo.

    .gz timestamps IST-naive hain (9:15 IST stored as 09:15 UTC epoch).
    Per-day anchor: har din 9:15 IST se bucket 0 start hota hai.
    Output timestamps real UTC mein (LightweightCharts + IST timezone ke liye).
    Session filter: sirf 9:15–15:30 IST ke candles.

    NOTE: raw .gz ab 1m granularity hai (pehle 5m thi). Isliye passthrough
    (bina bucketing) sirf tf_min==1 par hota hai — 5m/15m/45m/135m sabko
    ab yahin se actual bucket-resample hona zaroori hai.
    """
    sec = tf_min * 60
    if tf_min <= 1:
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

    NOTE: sirf 160m/8H (intraday, tf_min < 1440) ke liye use karo. Daily+
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

# Mobile ke liye max candles per TF (chunked inject)
_SV2_MAX = {
    "5m": 6000, "15m": 3000, "45m": 2000, "135m": 2000,
    "160m": 2000, "8H": 2000,
    "1D": 2000, "3D": 1500, "9D": 800, "27D": 400,
}

def _sv2_trim(data: list, label: str) -> list:
    """Last N candles rakh, baaki drop karo (mobile hang prevention)."""
    n = _SV2_MAX.get(label, 2000)
    return data[-n:] if len(data) > n else data

def _sv2_to_js(data: list) -> str:
    """List of dicts → compact JSON string for inline JS."""
    return json.dumps(data, separators=(",", ":"))

def _build_sv2_data() -> dict:
    """Dono .gz files se sab TFs ka aggregated data return karo.

    IMPORTANT: yeh ab sirf EK BAAR resample karta hai (jab process/session mein
    pehli dafa call hota hai) aur result ko _SV2_CACHE["agg"] mein store kar
    deta hai. Uske baad har stackview-2 open / Streamlit rerun pe seedha
    cached aggregated data return hota hai — resample functions dobara nahi
    chalte. Raw 1m .gz data ka source same hai, bas resampling ab repeat
    nahi hoti.
    """
    if "agg" in _SV2_CACHE:
        return _SV2_CACHE["agg"]

    bn_raw  = _sv2_load_bn_gz()
    btc_raw = _sv2_load_btc_gz()

    bn_tfs = {
        "5m":   _sv2_resample_bn_intraday(bn_raw,  5),
        "15m":  _sv2_resample_bn_intraday(bn_raw,  15),
        "45m":  _sv2_resample_bn_intraday(bn_raw,  45),
        "135m": _sv2_resample_bn_intraday(bn_raw,  135),
        "1D":   _sv2_resample_bn_daily   (bn_raw,  1),
        "3D":   _sv2_resample_bn_daily   (bn_raw,  3),
        "9D":   _sv2_resample_bn_daily   (bn_raw,  9),
        "27D":  _sv2_resample_bn_daily   (bn_raw,  27),
    }
    btc_tfs = {
        "160m": _sv2_resample_btc(btc_raw, 160),
        "8H":   _sv2_resample_btc(btc_raw, 480),
        "1D":   _sv2_resample_btc_daily(btc_raw, 1),
        "3D":   _sv2_resample_btc_daily(btc_raw, 3),
        "9D":   _sv2_resample_btc_daily(btc_raw, 9),
        "27D":  _sv2_resample_btc_daily(btc_raw, 27),
    }
    agg = {
        "bn":  {k: _sv2_trim(v, k) for k, v in bn_tfs.items()},
        "btc": {k: _sv2_trim(v, k) for k, v in btc_tfs.items()},
    }
    _SV2_CACHE["agg"] = agg
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
    """Fetch BankNifty daily candles 2020→now in yearly chunks (~1200 bars)."""
    if os.path.exists(BN_DAILY_CACHE):
        try:
            with open(BN_DAILY_CACHE) as f:
                cache = json.load(f)
            if time.time() - cache.get("ts", 0) < DAILY_CACHE_TTL:
                return cache.get("data", [])
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

    if all_candles:
        try:
            with open(BN_DAILY_CACHE, "w") as f:
                json.dump({"ts": time.time(), "data": all_candles}, f)
        except Exception:
            pass
    return all_candles

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
    _sv2_err_msg = ""
    try:
        _sv2 = _build_sv2_data()
        _bn  = _sv2["bn"]
        _btc = _sv2["btc"]
        # Debug info: paths + counts inject karo
        _sv2_debug_info = {
            "bn_path":  _SV2_CACHE.get("bn_path", "?"),
            "btc_path": _SV2_CACHE.get("btc_path", "?"),
            "bn_err":   _SV2_CACHE.get("bn_err", ""),
            "btc_err":  _SV2_CACHE.get("btc_err", ""),
            "bn_counts": {k: len(v) for k, v in _bn.items()},
            "btc_counts": {k: len(v) for k, v in _btc.items()},
        }
        _sv2_err_msg = json.dumps(_sv2_debug_info)
        html = html.replace("__SV2_BN_5M__",   _sv2_to_js(_bn["5m"]))
        html = html.replace("__SV2_BN_15M__",  _sv2_to_js(_bn["15m"]))
        html = html.replace("__SV2_BN_45M__",  _sv2_to_js(_bn["45m"]))
        html = html.replace("__SV2_BN_135M__", _sv2_to_js(_bn["135m"]))
        html = html.replace("__SV2_BN_1D__",   _sv2_to_js(_bn["1D"]))
        html = html.replace("__SV2_BN_3D__",   _sv2_to_js(_bn["3D"]))
        html = html.replace("__SV2_BN_9D__",   _sv2_to_js(_bn["9D"]))
        html = html.replace("__SV2_BN_27D__",  _sv2_to_js(_bn["27D"]))
        html = html.replace("__SV2_BTC_160M__", _sv2_to_js(_btc["160m"]))
        html = html.replace("__SV2_BTC_8H__",   _sv2_to_js(_btc["8H"]))
        html = html.replace("__SV2_BTC_1D__",   _sv2_to_js(_btc["1D"]))
        html = html.replace("__SV2_BTC_3D__",   _sv2_to_js(_btc["3D"]))
        html = html.replace("__SV2_BTC_9D__",   _sv2_to_js(_btc["9D"]))
        html = html.replace("__SV2_BTC_27D__",  _sv2_to_js(_btc["27D"]))
    except Exception as _sv2_ex:
        _sv2_err_msg = f"EXCEPTION: {_sv2_ex} | cache={_SV2_CACHE}"
        # Fallback: empty arrays agar gz file missing/corrupt ho
        for _ph in ["__SV2_BN_5M__","__SV2_BN_15M__","__SV2_BN_45M__","__SV2_BN_135M__",
                    "__SV2_BN_1D__","__SV2_BN_3D__","__SV2_BN_9D__","__SV2_BN_27D__",
                    "__SV2_BTC_160M__","__SV2_BTC_8H__","__SV2_BTC_1D__",
                    "__SV2_BTC_3D__","__SV2_BTC_9D__","__SV2_BTC_27D__"]:
            html = html.replace(_ph, "[]")
    # Inject debug info as JS variable — visible via window.__SV2_DEBUG in browser console
    _sv2_safe = _sv2_err_msg.replace("</", "<\\/")
    html = html.replace("</body>",
        f"<script>window.__SV2_DEBUG={json.dumps(_sv2_safe)};</script>\n</body>", 1)
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
_btc_only = st.session_state.get("_btc_only_mode", False)

if sess_active or _btc_only:
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

