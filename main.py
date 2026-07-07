import io
import json
import os
import time
import threading
import zipfile
import requests
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
DAILY_CACHE_FILE  = "btc_daily_cache.json"
DAILY_CACHE_TTL   = 300        # 5 min — aaj ki candle bhi update rahe
HIST_CACHE_TTL    = 300       # seconds for intraday cache (5 min — reduces API load)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ─── IST helper ───────────────────────────────────────────────────────────────
def _ist_now():
    return datetime.datetime.now(IST)


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
    """BankNifty 5m candles — GitHub se fetch karo (cached)."""
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
    """BN 5m data ko intraday TF mein resample karo.

    .gz timestamps IST-naive hain (9:15 IST stored as 09:15 UTC epoch).
    Per-day anchor: har din 9:15 IST se bucket 0 start hota hai.
    Output timestamps real UTC mein (LightweightCharts + IST timezone ke liye).
    Session filter: sirf 9:15–15:30 IST ke candles.
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
    """BN 5m data ko daily / multi-day candles mein resample karo.

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
    """BTC 5m data ko UTC-anchored TF mein resample karo (24/7 crypto)."""
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
    chalte. Raw 5m .gz data ka source same hai, bas resampling ab repeat
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
        "1D":   _sv2_resample_btc(btc_raw, 1440),
        "3D":   _sv2_resample_btc(btc_raw, 1440 * 3),
        "9D":   _sv2_resample_btc(btc_raw, 1440 * 9),
        "27D":  _sv2_resample_btc(btc_raw, 1440 * 27),
    }
    agg = {
        "bn":  {k: _sv2_trim(v, k) for k, v in bn_tfs.items()},
        "btc": {k: _sv2_trim(v, k) for k, v in btc_tfs.items()},
    }
    _SV2_CACHE["agg"] = agg
    return agg

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


# ─── ZIP export ───────────────────────────────────────────────────────────────
def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ("dashboard.py", "chart.html"):
            if os.path.exists(fname):
                zf.write(fname)
    return buf.getvalue()

# ─── Sidebar (login removed — Stack View 2 GitHub data ke liye koi login nahi chahiye) ──
with st.sidebar:
    st.title("📊 BankNifty Chart")
    st.caption("Stack View 2 — data GitHub se aata hai, koi login zaroori nahi")
    st.markdown("---")
    st.download_button(
        "⬇️ Download Project ZIP",
        data=_make_zip(),
        file_name="banknifty_chart.zip",
        mime="application/zip",
        use_container_width=True,
    )

# ─── Fetch all chart data (no Fyers/broker data — GitHub-based Stack View 2 only) ──
@st.cache_data(ttl=HIST_CACHE_TTL, show_spinner=False)
def _get_chart_data():
    btc_1m  = fetch_btc("1m",  1000)
    btc_15m = fetch_btc("15m", 1000)
    btc_day = load_btc_daily()
    return btc_1m, btc_15m, btc_day

btc_1m, btc_15m, btc_day = _get_chart_data()

# ─── Chart HTML builder — injects live data directly into chart.html ──────────
def _build_chart_html(btc_1m, btc_15m, btc_day) -> str:
    """Read chart.html and replace all __PLACEHOLDERS__ with real data."""
    import os, json as _json

    _html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.html")
    if not os.path.exists(_html_path):
        return "<p style='color:red'>chart.html not found</p>"

    with open(_html_path, "r", encoding="utf-8") as _f:
        html = _f.read()

    def _to_lwc(candles: list) -> str:
        """Convert [[epoch_ms, o, h, l, c, v], ...] to LWC format."""
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
        seen = {}
        for b in out:
            seen[b["time"]] = b
        return _json.dumps(sorted(seen.values(), key=lambda x: x["time"]))

    html = html.replace("__BTC_CANDLES__", _to_lwc(btc_1m))
    html = html.replace("__BTC_15M__",     _to_lwc(btc_15m))
    html = html.replace("__BTC_DAILY__",   _to_lwc(btc_day))
    # No live broker data — Stack View 1 (BankNifty) placeholders stay empty
    html = html.replace("__BN_CANDLES__",  "[]")
    html = html.replace("__BN_5M__",       "[]")
    html = html.replace("__BN_15M__",      "[]")
    html = html.replace("__BN_45M__",      "[]")
    html = html.replace("__BN_DAILY__",    "[]")

    # ── Stack View 2: .gz se pre-resampled data inject karo ─────────────────
    _sv2_err_msg = ""
    try:
        _sv2 = _build_sv2_data()
        _bn  = _sv2["bn"]
        _btc = _sv2["btc"]
        _sv2_debug_info = {
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
        for _ph in ["__SV2_BN_5M__","__SV2_BN_15M__","__SV2_BN_45M__","__SV2_BN_135M__",
                    "__SV2_BN_1D__","__SV2_BN_3D__","__SV2_BN_9D__","__SV2_BN_27D__",
                    "__SV2_BTC_160M__","__SV2_BTC_8H__","__SV2_BTC_1D__",
                    "__SV2_BTC_3D__","__SV2_BTC_9D__","__SV2_BTC_27D__"]:
            html = html.replace(_ph, "[]")
    _sv2_safe = _sv2_err_msg.replace("</", "<\\/")
    html = html.replace("</body>",
        f"<script>window.__SV2_DEBUG={json.dumps(_sv2_safe)};</script>\n</body>", 1)

    # No broker/API sidecar server — always 0
    html = html.replace("__API_PORT__", "0")
    # No broker credentials — clear the panel's stored fields
    html = html.replace("__FYERS_APP_ID__", "")
    html = html.replace("__FYERS_SECRET__",  "")
    html = html.replace("__FYERS_STATUS__",  "disconnected")

    return html


# ─── Main area: embed chart directly (login-gating removed — always shown) ────
st.markdown("## 📊 BankNifty Chart — Stack View 2")

_chart_html = _build_chart_html(btc_1m, btc_15m, btc_day)
components.html(_chart_html, height=950, scrolling=False)
