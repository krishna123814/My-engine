import io
import json
import os
import time
import threading
import zipfile
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="BTC/USDT Live", page_icon="₿",
                   layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
#MainMenu,footer,header{visibility:hidden}
.main .block-container{padding:0!important;max-width:100%!important;margin:0!important}
.stApp{background:#131722;overflow:hidden}
iframe{border:none!important}
</style>""", unsafe_allow_html=True)

DAILY_CACHE_FILE = "daily_cache.json"
DAILY_CACHE_TTL  = 86400          # refresh once per day (seconds)
BTC_LAUNCH_MS    = 1502928000000  # 2017-08-17 UTC


# ─── Download ZIP (sidebar) ───────────────────────────────────────────────
def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write("main.py",    "main.py")
        zf.write("chart.html", "chart.html")
        if os.path.exists(DAILY_CACHE_FILE):
            zf.write(DAILY_CACHE_FILE, DAILY_CACHE_FILE)
    return buf.getvalue()

with st.sidebar:
    st.markdown("### 📦 Export Project")
    st.download_button(
        label="⬇️ Download main.py + chart.html",
        data=_make_zip(),
        file_name="btc_chart_app.zip",
        mime="application/zip",
        help="Download both files as a ZIP to use on another Replit account",
        use_container_width=True,
    )
    st.caption("Unzip → upload both files → run main.py")


# ─── Live / recent candles (1m, 15m) ─────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def fetch_btc_candles(interval: str = "1m", limit: int = 500) -> list:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=10,
        )
        if r.status_code == 200:
            return [
                {
                    "time":   int(k[0]) // 1000,
                    "open":   round(float(k[1]), 2),
                    "high":   round(float(k[2]), 2),
                    "low":    round(float(k[3]), 2),
                    "close":  round(float(k[4]), 2),
                    "volume": round(float(k[5]), 4),
                }
                for k in r.json()
            ]
    except Exception:
        pass
    return []


# ─── Full daily history helpers ───────────────────────────────────────────
def _fetch_daily_from_api() -> list:
    """Paginate Binance 1d klines back to BTC launch. Returns sorted list."""
    all_klines: list = []
    end_time = None
    try:
        for _ in range(15):
            params = {"symbol": "BTCUSDT", "interval": "1d", "limit": 1000}
            if end_time is not None:
                params["endTime"] = end_time
            r = requests.get(
                "https://api.binance.com/api/v3/klines", params=params, timeout=15
            )
            if r.status_code != 200:
                break
            chunk = r.json()
            if not chunk:
                break
            all_klines = chunk + all_klines
            oldest = int(chunk[0][0])
            if oldest <= BTC_LAUNCH_MS or len(chunk) < 1000:
                break
            end_time = oldest - 1
            time.sleep(0.15)
    except Exception:
        pass

    seen: set = set()
    out: list = []
    for k in all_klines:
        t = int(k[0]) // 1000
        if t in seen:
            continue
        seen.add(t)
        out.append({
            "time":   t,
            "open":   round(float(k[1]), 2),
            "high":   round(float(k[2]), 2),
            "low":    round(float(k[3]), 2),
            "close":  round(float(k[4]), 2),
            "volume": round(float(k[5]), 4),
        })
    out.sort(key=lambda b: b["time"])
    return out


def _save_cache(candles: list) -> None:
    try:
        with open(DAILY_CACHE_FILE, "w") as f:
            json.dump({"fetched_at": int(time.time()), "candles": candles}, f)
    except Exception:
        pass


def _load_cache() -> tuple[list, int]:
    """Returns (candles, fetched_at). fetched_at=0 if file missing/corrupt."""
    try:
        with open(DAILY_CACHE_FILE) as f:
            data = json.load(f)
        return data["candles"], int(data.get("fetched_at", 0))
    except Exception:
        return [], 0


def _background_refresh() -> None:
    """Fetch fresh daily data from API and overwrite cache file silently."""
    fresh = _fetch_daily_from_api()
    if fresh:
        _save_cache(fresh)


def load_daily() -> list:
    """
    Fast path: return cached file instantly.
    If cache is stale (> DAILY_CACHE_TTL), kick off a background refresh
    so next app load gets fresh data — current load is never blocked.
    """
    candles, fetched_at = _load_cache()
    age = time.time() - fetched_at

    if not candles:
        # No cache at all — must fetch synchronously (first-ever run)
        candles = _fetch_daily_from_api()
        if candles:
            _save_cache(candles)
    elif age > DAILY_CACHE_TTL:
        # Cache exists but stale — serve it immediately, refresh in background
        t = threading.Thread(target=_background_refresh, daemon=True)
        t.start()

    return candles


# ─── HTML / JS ───────────────────────────────────────────────────────────
with open("chart.html") as _f:
    _HTML = _f.read()

candles     = fetch_btc_candles("1m",  500)
candles_15m = fetch_btc_candles("15m", 1000)
daily       = load_daily()

html = (
    _HTML
    .replace("__BTC_CANDLES__", json.dumps(candles))
    .replace("__BTC_15M__",     json.dumps(candles_15m))
    .replace("__BTC_DAILY__",   json.dumps(daily))
)
components.html(html, height=920, scrolling=False)
