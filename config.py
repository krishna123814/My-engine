"""
config.py — App-wide secrets, constants, and small helpers.

Isme koi live/threading logic nahi hai — sirf: secrets kaise padhein
(_get_secret), file-path constants, cache TTLs, aur IST timezone helper.
Baaki sab modules yahan se import karte hain taaki secret-reading logic
sirf ek jagah rahe.
"""
import os
import datetime
import streamlit as st

# ─── Secret reader ──────────────────────────────────────────────────────────
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

# ─── HF Space identity (used by hf_admin.py) ───────────────────────────────
HF_TOKEN = _get_secret("HF_TOKEN")
HF_SPACE_ID = os.environ.get("SPACE_ID", "")

# ─── File-path constants ────────────────────────────────────────────────────
CREDS_FILE        = ".fyers_creds.json"
BN_LIVE_FILE       = "bn_live.json"
DAILY_CACHE_FILE   = "btc_daily_cache.json"
BN_DAILY_CACHE     = "bn_daily_cache.json"
DAILY_CACHE_TTL    = 300        # 5 min — aaj ki candle bhi update rahe
HIST_CACHE_TTL     = 300        # seconds for intraday cache (5 min — reduces API load)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def _ist_now():
    return datetime.datetime.now(IST)

# ─── Default Fyers credentials (sidebar override kar sakta hai) ────────────
DEFAULT_APP_ID    = _get_secret("FYERS_APP_ID")
DEFAULT_SECRET    = _get_secret("FYERS_SECRET")
DEFAULT_CLIENT_ID = _get_secret("FYERS_CLIENT_ID")
DEFAULT_PASSWORD  = _get_secret("FYERS_PASSWORD")
REDIRECT_URI = "https://www.google.com"   # generic, not sensitive — never changes

# ─── Cross-module shared constants (broker URLs, symbols, time offsets) ───
# In sabko yahan (config.py — ek "leaf" module, kisi aur module par depend
# nahi karta) rakha gaya hai taaki broker_meta / binance_rest / market_data
# jaise modules ek-doosre ko circularly import na karein.
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_EAPI_URL = "https://eapi.binance.com"
BN_OC_SYMBOL = "NSE:NIFTYBANK-INDEX"

BTC_1D_SYMBOL         = "BTCUSDT"
BTC_1D_FILENAME       = f"{BTC_1D_SYMBOL}.json"
_BTC_LISTING_START_MS = 1502928000000   # 2017-08-17 00:00:00 UTC — Binance par BTCUSDT listing ki date

_IST_NAIVE_OFFSET = 19800   # 5.5 * 3600 — IST-naive → real UTC conversion
_GZ_APPEND_OFFSET = 19800   # BankNifty ke liye real-UTC → IST-naive (same as _IST_NAIVE_OFFSET)
_SESSION_START    = 33300   # 9:15 IST = 9*3600 + 15*60 seconds from midnight
_SESSION_END      = 55800   # 15:30 IST = 15*3600 + 30*60 seconds from midnight
