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

st.set_page_config(
    page_title="BankNifty Live Chart",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown("""<style>
#MainMenu,footer,header{visibility:hidden}
.main .block-container{padding:0!important;max-width:100%!important;margin:0!important}
.stApp{background:#131722;overflow:hidden}
iframe{border:none!important}
</style>""", unsafe_allow_html=True)

# ─── Constants ────────────────────────────────────────────────────────────────
CREDS_FILE        = ".fyers_creds.json"
BN_LIVE_FILE      = "bn_live.json"
DAILY_CACHE_FILE  = "btc_daily_cache.json"
BN_DAILY_CACHE    = "bn_daily_cache.json"
DAILY_CACHE_TTL   = 86400
HIST_CACHE_TTL    = 55        # seconds for intraday cache

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
    "open":      None,
    "high":      None,
    "low":       None,
    "prev_close": None,
    "ts":        0,
}
_LIVE_LOCK = threading.Lock()

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

def fyers_get_access_token(app_id: str, secret_key: str, auth_code: str) -> tuple[bool, str]:
    """Step 5: exchange auth_code for access_token."""
    app_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
    try:
        r = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            json={"grant_type": "authorization_code", "appIdHash": app_hash, "code": auth_code},
            timeout=10,
        ).json()
        if r.get("s") == "ok" and "access_token" in r:
            return True, r["access_token"]
        return False, r.get("message", str(r))
    except Exception as e:
        return False, str(e)

# ─── Fully automated Fyers login (TOTP-based, zero user input) ────────────────
def auto_fyers_login() -> tuple[bool, str]:
    """Auto-login using stored client_id + password + TOTP secret. Returns (ok, msg)."""
    creds = load_creds()
    client_id  = creds.get("client_id",   DEFAULT_CLIENT_ID)
    password   = creds.get("password",    DEFAULT_PASSWORD)
    totp_secret = creds.get("totp_secret", "")
    app_id     = creds.get("app_id",      DEFAULT_APP_ID)
    secret_key = creds.get("secret_key",  DEFAULT_SECRET)

    if not totp_secret:
        return False, "TOTP secret not configured"

    # Step 1: Send OTP (Fyers will accept TOTP in verify step)
    ok1, rkey = fyers_send_otp(client_id, app_id)
    if not ok1:
        return False, f"Send OTP failed: {rkey}"

    # Step 2: Verify TOTP (auto-generated)
    totp_code = pyotp.TOTP(totp_secret).now()
    ok2, rkey2 = fyers_verify_otp(rkey, totp_code)
    if not ok2:
        return False, f"TOTP verify failed: {rkey2}"

    # Step 3: Verify PIN/password
    ok3, token = fyers_verify_pin(rkey2, password)
    if not ok3:
        return False, f"PIN verify failed: {token}"

    # Step 4: Get auth_code
    ok4, auth_code = fyers_get_auth_code(token, client_id, app_id)
    if not ok4:
        return False, f"Auth code failed: {auth_code}"

    # Step 5: Get access_token
    ok5, access_token = fyers_get_access_token(app_id, secret_key, auth_code)
    if not ok5:
        return False, f"Access token failed: {access_token}"

    # Save new token
    creds["access_token"] = access_token
    save_creds(creds)
    _sess_cache.update({"active": True, "ts": time.time()})
    return True, access_token


# ─── Token expiry monitor (background) ─────────────────────────────────────────
# NOTE: Fyers vagator login API is IP-restricted (blocks cloud/VPS IPs).
# So we only MONITOR expiry and set a flag — user does a quick 15-sec re-auth.
_TOKEN_STATUS: dict = {"expired": False, "checked_at": 0.0, "running": False}
_TOKEN_STATUS_LOCK = threading.Lock()

def _token_monitor_loop():
    """Checks Fyers token validity every 5 min. Sets expired flag for sidebar alert."""
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

            # Also reset session cache so sidebar reflects truth
            if not still_active:
                _sess_cache.update({"active": False, "ts": 0.0})

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
    now = time.time()
    if now - _sess_cache["ts"] < 60:
        return _sess_cache["active"]
    creds = load_creds()
    if not creds.get("access_token"):
        _sess_cache.update({"active": False, "ts": now})
        return False
    today = _ist_now().strftime("%Y-%m-%d")
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol": "NSE:NIFTYBANK-INDEX", "resolution": "D",
        "date_format": "1", "range_from": today, "range_to": today, "cont_flag": "1",
    }
    try:
        res = requests.get(
            "https://api-t1.fyers.in/data/history",
            headers=headers, params=params, timeout=6,
        ).json()
        active = res.get("s") == "ok"
    except Exception:
        active = False
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
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]]
                    for c in res.get("candles", [])]
    except Exception:
        pass
    return []

def fetch_bn_intraday(interval_mins: int) -> list:
    today = _ist_now().strftime("%Y-%m-%d")
    from_d = (_ist_now() - datetime.timedelta(days=95)).strftime("%Y-%m-%d")
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
    if os.path.exists(DAILY_CACHE_FILE):
        try:
            with open(DAILY_CACHE_FILE) as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < DAILY_CACHE_TTL:
                return c.get("data", [])
        except Exception:
            pass
    data = fetch_btc("1d", 1000)
    if data:
        try:
            with open(DAILY_CACHE_FILE, "w") as f:
                json.dump({"ts": time.time(), "data": data}, f)
        except Exception:
            pass
    return data

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
            with _LIVE_LOCK:
                _LIVE["ltp"]       = float(ltp)
                _LIVE["open"]      = float(tick.get("open_price") or tick.get("open") or _LIVE.get("open") or ltp)
                _LIVE["high"]      = float(tick.get("high_price") or tick.get("high") or _LIVE.get("high") or ltp)
                _LIVE["low"]       = float(tick.get("low_price")  or tick.get("low")  or _LIVE.get("low")  or ltp)
                _LIVE["prev_close"]= float(tick.get("prev_close_price") or tick.get("prev_close") or _LIVE.get("prev_close") or ltp)
                _LIVE["ts"]        = int(time.time())
            # Also update bn_live.json so JS chart can poll it
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

def _write_live_json():
    with _LIVE_LOCK:
        snap = dict(_LIVE)
    if snap["ltp"] is None:
        return
    try:
        now_sec = int(time.time())
        minute_epoch = (now_sec // 60) * 60
        payload = {
            "ts": now_sec,
            "ltp": snap["ltp"],
            "candle": {
                "time":  minute_epoch,
                "open":  snap["open"]  or snap["ltp"],
                "high":  snap["high"]  or snap["ltp"],
                "low":   snap["low"]   or snap["ltp"],
                "close": snap["ltp"],
            },
        }
        with open(BN_LIVE_FILE, "w") as f:
            json.dump(payload, f)
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
                        with _LIVE_LOCK:
                            # Only update if WebSocket hasn't given us fresher data
                            if time.time() - _LIVE["ts"] > 5:
                                _LIVE["ltp"]  = float(last[4])
                                _LIVE["open"] = float(last[1])
                                _LIVE["high"] = float(last[2])
                                _LIVE["low"]  = float(last[3])
                                _LIVE["ts"]   = int(time.time())
                        _write_live_json()
            except Exception:
                pass
        time.sleep(3)

def _ensure_live_threads():
    names = {t.name for t in threading.enumerate()}
    if "FyersRESTPoller" not in names:
        threading.Thread(target=_rest_live_loop, name="FyersRESTPoller", daemon=True).start()
    if "FyersTokenMonitor" not in names:
        threading.Thread(target=_token_monitor_loop, name="FyersTokenMonitor", daemon=True).start()
    _start_ws()

# ─── ZIP export ───────────────────────────────────────────────────────────────
def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ("dashboard.py", "chart.html"):
            if os.path.exists(fname):
                zf.write(fname)
    return buf.getvalue()

# ─── Sidebar: Auth UI ──────────────────────────────────────────────────────────
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

        auth_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={app_id}"
            f"&redirect_uri=https%3A%2F%2Fwww.google.com"
            f"&response_type=code&state=None"
        )

        if has_old_creds:
            st.error("🔴 Token expire ho gaya! Re-login karo (15 sec)")
        else:
            st.warning("⚠️ Login karo")

        st.markdown(f"**Step 1 →** [👉 Fyers Login Link]({auth_url})")
        st.caption("Fyers login hoga → Google page aayega → poora URL copy karo")

        url_input = st.text_input(
            "**Step 2 →** Poora URL ya auth_code paste karo",
            placeholder="https://www.google.com/?s=ok&auth_code=eyJ... ya sirf code",
        )

        if st.button("⚡ Connect", use_container_width=True, type="primary"):
            raw = url_input.strip()
            if raw:
                code = _extract_auth_code(raw)
                ok, access_token = fyers_get_access_token(app_id, secret, code)
                if ok:
                    save_creds({
                        **creds,
                        "app_id":       app_id,
                        "secret_key":   secret,
                        "client_id":    DEFAULT_CLIENT_ID,
                        "password":     DEFAULT_PASSWORD,
                        "access_token": access_token,
                    })
                    _sess_cache.update({"active": True, "ts": time.time()})
                    st.success("🎉 Connected!")
                    st.rerun()
                else:
                    st.error(f"Failed: {access_token}")
            else:
                st.error("URL ya code paste karo pehle")

    st.markdown("---")
    st.download_button(
        "⬇️ Download Project ZIP",
        data=_make_zip(),
        file_name="banknifty_chart.zip",
        mime="application/zip",
        use_container_width=True,
    )

# ─── Fetch all chart data ─────────────────────────────────────────────────────
@st.cache_data(ttl=HIST_CACHE_TTL, show_spinner=False)
def _get_chart_data(sess: bool):
    btc_1m   = fetch_btc("1m",  1000)
    btc_15m  = fetch_btc("15m", 1000)
    btc_day  = load_btc_daily()
    bn_1m    = fetch_bn_intraday(1)  if sess else []
    bn_15m   = fetch_bn_intraday(15) if sess else []
    bn_day   = load_bn_daily()       if sess else []
    return btc_1m, btc_15m, btc_day, bn_1m, bn_15m, bn_day

btc_1m, btc_15m, btc_day, bn_1m, bn_15m, bn_day = _get_chart_data(sess_active)

# ─── Render chart ─────────────────────────────────────────────────────────────
if os.path.exists("chart.html"):
    with open("chart.html") as f:
        raw = f.read()

    html = (
        raw
        .replace("__BTC_CANDLES__", json.dumps(to_ohlc(btc_1m)))
        .replace("__BTC_15M__",     json.dumps(to_ohlc(btc_15m)))
        .replace("__BTC_DAILY__",   json.dumps(to_ohlc(btc_day)))
        .replace("__BN_CANDLES__",  json.dumps(to_ohlc(bn_1m)))
        .replace("__BN_15M__",      json.dumps(to_ohlc(bn_15m)))
        .replace("__BN_DAILY__",    json.dumps(to_ohlc(bn_day)))
    )
    components.html(html, height=1200, scrolling=False)

    if not sess_active:
        st.info("📊 BTC chart loaded. **Sidebar खोलो → Fyers से login karo** BankNifty live data ke liye.", icon="ℹ️")
else:
    st.error("❌ chart.html not found!")
