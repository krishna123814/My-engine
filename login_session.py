"""
login_session.py — Fyers login attempt logging, SMS alert on login,
background access-token expiry monitor, aur session-active check
(is_session_active — poore app mein sabse zyada use hone wala auth gate).
"""
import os
import json
import time
import threading
import requests
import streamlit as st

from config import _get_secret, FAST2SMS_KEY, IST, _ist_now
from startup_log import _slog
from credentials import load_creds

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

# ─── Session-check debug instrumentation ───────────────────────────────────
# Har is_session_active() call, kis reason se True/False decide hua, aur
# kahan se call hua — ye sab yahan log hota hai (_STARTUP_LOG mein, jo
# already thread-safe + global hai). "🔍 Session Debug" panel (login page
# ke top par) isi log ko live dikhata hai — koi restart/rebuild ki zaroorat
# nahi, agla rerun hote hi naye lines dikh jaate hain.
def _sess_debug_caller() -> str:
    """Kis function ne is_session_active() call kiya — stack se nikaalo."""
    import inspect
    try:
        stack = inspect.stack()
        # frame[0]=yahi helper, frame[1]=is_session_active, frame[2]=asli caller
        return stack[2].function if len(stack) > 2 else "?"
    except Exception:
        return "?"

def _sess_debug(reason: str, active: bool, update_cache: bool, caller: str) -> None:
    try:
        age = time.time() - _sess_cache["ts"]
    except Exception:
        age = -1
    _slog(
        f"🔍 SESSDBG caller={caller} update_cache={update_cache} → "
        f"reason={reason} result={active} | "
        f"cache_now(active={_sess_cache.get('active')}, age={age:.1f}s) "
        f"force_active={st.session_state.get('_force_active')}",
        level="info",
    )

def is_session_active(update_cache: bool = True) -> bool:
    """Token exist karna + profile API ok = session active.
    Market band hone par bhi False nahi karega.

    update_cache=True (default): sirf top-level PAGE-ROUTING check ke liye
    use karo (jo decide karta hai login-page dikhana hai ya chart). Ye
    global `_sess_cache` (poore server-process ke liye shared, session-
    specific nahi) mein result likhta hai.

    update_cache=False: kisi bhi doosre jagah se — jaise data-update
    buttons (BankNifty/BTC Update Karo) — call karo, jinka kaam sirf
    "abhi fetch ke liye token valid hai ya nahi" jaanna hai. Ye result
    ko global cache mein WRITE nahi karta, taaki aisa button galti se
    poore app ko "logged in" mode mein switch na kar de (asal login-flow
    complete kiye bina) — ye hi pehle wala bug tha.

    Har return path pe _sess_debug() se ek log-line jaati hai — taaki
    exactly pata chale kis WAJAH se True/False mila (debug panel isi
    log ko dikhata hai, login page ke top par, live + copyable).
    """
    now = time.time()
    caller = _sess_debug_caller()

    # 1. token_monitor ne expire flag set kiya? clear _force_active
    if os.path.exists(".token_expired_flag"):
        try:
            os.remove(".token_expired_flag")
        except Exception:
            pass
        if update_cache:
            st.session_state["_force_active"] = False
            _sess_cache.update({"active": False, "ts": 0.0})

    # 2. login ke turant baad force-active flag
    if st.session_state.get("_force_active"):
        if update_cache:
            _sess_cache.update({"active": True, "ts": now})
        _sess_debug("force_active_flag", True, update_cache, caller)
        return True

    # 2. fresh cache — read-only calls (update_cache=False) bhi cache PADH
    # sakte hain (taaki wo bhi rate-limited rahein), bas WRITE nahi karte.
    if now - _sess_cache["ts"] < 120:
        _sess_debug(f"cache_fresh(age={now - _sess_cache['ts']:.1f}s)", _sess_cache["active"], update_cache, caller)
        return _sess_cache["active"]

    creds = load_creds()
    if not creds.get("access_token"):
        if update_cache:
            _sess_cache.update({"active": False, "ts": now})
        _sess_debug("no_access_token", False, update_cache, caller)
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
        # FIX: pehle yahan "active = True" tha (fail-open) — soch ye thi ki
        # transient network glitch par user ko galti se logged-out na dikhaya
        # jaaye. Lekin isi wajah se ek confusing bug ban gaya tha: agar token
        # sach mein expire ho chuka ho aur Fyers API isi wajah se fail/timeout
        # ho (jo bhi ho sakta hai jab auth hi invalid ho), ye code galat se
        # "session valid hai" maan leta tha aur chart mode khol deta tha —
        # jabki asli data-fetch (jo alag se sahi tarah fail hoti hai) kabhi
        # kaam nahi karta. Ab fail-safe: real check fail ho to session ko
        # INVALID maano, taaki UI aur asli data-fetch dono ek hi (sahi) nateeje
        # par sehmat rahein.
        active = False

    if update_cache:
        _sess_cache.update({"active": active, "ts": now})
    _sess_debug("live_profile_check", active, update_cache, caller)
    return active

# ─── Stack View 2: .gz data load (local file pehle, GitHub fallback) + resample ─
