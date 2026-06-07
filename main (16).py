import io
import json
import zipfile
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="BTC/USDT Live", page_icon="₿",
                   layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
#MainMenu,footer,header{visibility:hidden}
.main .block-container{padding:0!important;max-width:100%!important}
.stApp{background:#131722}
</style>""", unsafe_allow_html=True)

BTC_ASSET = "\u20bf BTC/USDT"   # ₿ BTC/USDT


# ─── Download ZIP (sidebar) ───────────────────────────────────────────────
def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write("main.py",    "main.py")
        zf.write("chart.html", "chart.html")
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


@st.cache_data(show_spinner=False, persist="disk")
def fetch_btc_daily_full() -> list:
    """Paginate 1d klines back to BTC launch (Aug 2017), server-side."""
    BTC_LAUNCH_MS = 1502928000000  # 2017-08-17 UTC
    all_klines: list = []
    end_time = None
    try:
        for _ in range(10):  # safety cap; ~4 pages cover 2017→now
            params = {"symbol": "BTCUSDT", "interval": "1d", "limit": 1000}
            if end_time is not None:
                params["endTime"] = end_time
            r = requests.get(
                "https://api.binance.com/api/v3/klines", params=params, timeout=10
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
    except Exception:
        pass

    seen = set()
    out = []
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


# ─── HTML / JS — loaded from chart.html (keeps main.py small) ──────────
with open("chart.html") as _f:
    _HTML = _f.read()


candles     = fetch_btc_candles("1m",  500)
candles_15m = fetch_btc_candles("15m", 500)
daily       = fetch_btc_daily_full()

html = (
    _HTML
    .replace("__BTC_CANDLES__", json.dumps(candles))
    .replace("__BTC_15M__",     json.dumps(candles_15m))
    .replace("__BTC_DAILY__",   json.dumps(daily))
)
components.html(html, height=920, scrolling=False)
