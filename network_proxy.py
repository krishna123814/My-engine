"""
network_proxy.py — Optional outbound proxy configuration (host/port/user/pwd),
shared across all Fyers/Binance REST + WebSocket calls.
"""
import os
import json
import time
import threading
import requests
import streamlit as st

from config import BINANCE_BASE_URL, BINANCE_EAPI_URL

@st.cache_resource
def _get_proxy_cache() -> dict:
    return {
        "host": "",
        "port": "",
        "user": "",
        "password": "",
        "on": True,         # Toggle state — default ON (user OFF kare to band hoga)
        "enabled": False,   # True = on AND host+port dono set hain
    }

@st.cache_resource
def _get_proxy_lock() -> threading.Lock:
    return threading.Lock()

_PROXY_CACHE: dict = _get_proxy_cache()
_PROXY_LOCK = _get_proxy_lock()

def _proxy_apply(host: str, port: str, user: str, pwd: str, on: bool) -> None:
    """Diye gaye values RAM (_PROXY_CACHE) mein daalo. Koi disk write nahi —
    sirf is chalte hue process ke liye. Restart hone par _load_proxy_from_env()
    dobara HF Space secrets se fill karega."""
    host = host.strip(); port = port.strip()
    user = user.strip(); pwd  = pwd.strip()
    with _PROXY_LOCK:
        _PROXY_CACHE["host"]     = host
        _PROXY_CACHE["port"]     = port
        _PROXY_CACHE["user"]     = user
        _PROXY_CACHE["password"] = pwd
        _PROXY_CACHE["on"]       = on
        _PROXY_CACHE["enabled"]  = bool(on and host and port)

def _load_proxy_from_env() -> None:
    """Startup par HF Space secrets (environment variables) se proxy settings
    RAM (_PROXY_CACHE) mein fill karo — PROXY_HOST/PROXY_PORT zaroori hain,
    PROXY_USER/PROXY_PASS/PROXY_ON optional. Secrets set na hon to RAM apni
    default (khaali) state mein rehti hai — UI se manually Apply kar sakte ho."""
    _env_host = os.environ.get("PROXY_HOST", "").strip()
    _env_port = os.environ.get("PROXY_PORT", "").strip()
    if not (_env_host and _env_port):
        return  # secrets set nahi — RAM khaali hi rahegi
    _env_user = os.environ.get("PROXY_USER", "").strip()
    _env_pass = os.environ.get("PROXY_PASS", "").strip()
    _env_on_raw = os.environ.get("PROXY_ON", "true").strip().lower()
    _env_on = _env_on_raw not in ("false", "0", "off", "no", "")
    _proxy_apply(_env_host, _env_port, _env_user, _env_pass, _env_on)

def _proxy_url() -> str:
    """http://user:pass@host:port ya http://host:port string banao."""
    with _PROXY_LOCK:
        host = _PROXY_CACHE["host"]
        port = _PROXY_CACHE["port"]
        user = _PROXY_CACHE["user"]
        pwd  = _PROXY_CACHE["password"]
    if user and pwd:
        return f"http://{user}:{pwd}@{host}:{port}"
    return f"http://{host}:{port}"

def _get_proxy_dict() -> "dict | None":
    """requests library ke liye proxies dict — None agar proxy disabled ho."""
    with _PROXY_LOCK:
        enabled = _PROXY_CACHE["enabled"]
    if not enabled:
        return None
    url = _proxy_url()
    return {"http": url, "https": url}

def _get_ws_proxy() -> dict:
    """websocket-client run_forever() ke liye proxy kwargs dict.
    Returns empty dict agar proxy disabled ho (** se unpack hoga bina kuch kiye)."""
    with _PROXY_LOCK:
        enabled = _PROXY_CACHE["enabled"]
        host    = _PROXY_CACHE["host"]
        port    = _PROXY_CACHE["port"]
        user    = _PROXY_CACHE["user"]
        pwd     = _PROXY_CACHE["password"]
    if not enabled:
        return {}
    kwargs = {
        "http_proxy_host": host,
        "http_proxy_port": int(port) if port.isdigit() else 8080,
        # websocket-client ye key expect karta hai aur "http","socks4","socks5"
        # ke alawa kuch bhi (missing/None included) reject kar deta hai —
        # isse pehle ye set hi nahi hoti thi, isliye WS hamesha
        # "Only http, socks4, socks5 proxy protocols are supported" error
        # ke saath fail ho raha tha, chahe proxy khud bilkul theek ho.
        "proxy_type": "http",
    }
    if user and pwd:
        kwargs["http_proxy_auth"] = (user, pwd)
    return kwargs

def _test_proxy() -> tuple[bool, str]:
    """Proxy se Binance ke TEEN alag domains hit karke test karo — sirf spot
    (api.binance.com) test karna kaafi nahi hai, kyunki option chain data
    eapi.binance.com (REST) aur fstream.binance.com / stream.binance.com
    (WebSocket) se aata hai. Ek domain proxy se allowed ho aur doosra block/
    unreachable ho — aisa aam hai (geo-block ya proxy provider ke ACL rules
    domain-specific hote hain). (ok, msg) return karo — msg mein har domain
    ka alag-alag result hota hai taaki pata chale EXACTLY kahan atka hai.
    Note: ye teeno hi REST (HTTP GET) checks hain — WebSocket (wss://) ke
    liye proxy ka CONNECT-tunnel support alag cheez hai aur isse yahan test
    nahi hota (websocket-client apna alag proxy path use karta hai, dekho
    _get_ws_proxy()). Isliye ye teeno pass hone ke baad bhi WS disconnect
    reh sakta hai — lekin agar in teeno mein se koi bhi fail hota hai, to
    wahi sabse pehla, sabse confirm-able root cause hai."""
    proxy_dict = _get_proxy_dict()
    if not proxy_dict:
        return False, "Proxy settings set nahi hain"

    # NOTE: fstream.binance.com jaanbujh kar yahan test NAHI hota — wo pure
    # WebSocket-only domain hai (koi REST endpoint serve nahi karta), isliye
    # usko REST GET se test karna hamesha 404 dega chahe proxy bilkul theek
    # ho. WS domains (fstream / stream.binance.com:9443) ka asli test sirf
    # actual WS handshake se ho sakta hai, jo _get_ws_proxy() path use karta
    # hai — dekho debug panel ke "Mark WS / Trade WS / Spot WS" status.
    checks = [
        ("Spot (api.binance.com)",      f"{BINANCE_BASE_URL}/api/v3/time"),
        ("Options (eapi.binance.com)",  f"{BINANCE_EAPI_URL}/eapi/v1/time"),
    ]

    results = []
    all_ok = True
    for label, url in checks:
        try:
            r = requests.get(url, proxies=proxy_dict, timeout=10)
            if r.status_code == 200:
                results.append(f"✅ {label}: OK (server time {r.json().get('serverTime')})")
            else:
                all_ok = False
                results.append(f"❌ {label}: HTTP {r.status_code} — {r.text[:120]}")
        except Exception as e:
            all_ok = False
            results.append(f"❌ {label}: {e}")

    msg = "\n".join(results)
    if all_ok:
        msg = "✅ Proxy Binance REST se (Spot + Options) kaam kar raha hai:\n" + msg + \
              "\n\nMatlab exchangeInfo/expiries/strikes ab load ho jaane chahiye. " \
              "Agar WebSocket (Mark/Trade/Spot) status abhi bhi DISCONNECTED dikhe, " \
              "to proxy ka wss:// CONNECT-tunnel support na hona sabse likely wajah " \
              "hai — wo REST test se cover nahi hota, sirf actual WS connect attempt se pata chalta hai."
    else:
        msg = "⚠️ Proxy sabhi domains se kaam nahi kar raha — jahan ❌ hai wahi block/unreachable hai:\n" + msg
    return all_ok, msg

