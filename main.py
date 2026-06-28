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
    page_title="BankNifty Chart",
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
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def _ist_now():
    return datetime.datetime.now(IST)

# ─── BTC (Binance) static fetch ────────────────────────────────────────────────
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

DAILY_CACHE_FILE = "btc_daily_cache.json"
DAILY_CACHE_TTL  = 300

def load_btc_daily() -> list:
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

    chunks = []
    for yr in range(start_year, cur_year + 1):
        chunks.append((f"{yr}-01-01", f"{yr}-06-30"))
        chunks.append((f"{yr}-07-01", f"{yr}-12-31"))

    all_candles = []
    seen_times  = set()

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


# ─── Replay gz endpoint (background HTTP server on side-port) ─────────────────
_REPLAY_ENDPOINT_REGISTERED = False
_REPLAY_ENDPOINT_LOCK = threading.Lock()


def _register_api_route():
    global _REPLAY_ENDPOINT_REGISTERED
    with _REPLAY_ENDPOINT_LOCK:
        if _REPLAY_ENDPOINT_REGISTERED:
            return
        _REPLAY_ENDPOINT_REGISTERED = True

    def _server_loop():
        import http.server, urllib.parse as _up

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parsed = _up.urlparse(self.path)

                # ── Replay .gz data endpoint ───────────────────────────────────
                if parsed.path == "/api/replay_gz":
                    qs2 = _up.parse_qs(parsed.query, keep_blank_values=False)
                    asset = (qs2.get("asset", ["bn"])[0]).lower()
                    tf    = qs2.get("tf", ["1d"])[0]
                    chunk = int(qs2.get("chunk", ["0"])[0])
                    CHUNK_SIZE = 500

                    _GH_RAW_BASE = "https://raw.githubusercontent.com/krishna123814/My-engine/main"
                    _GH_BN_URL   = f"{_GH_RAW_BASE}/banknifty_5m_csv.json.gz"
                    _GH_BTC_URL  = f"{_GH_RAW_BASE}/Bitcoin_BTCUSDT_IST_5m.json.gz"

                    if not hasattr(self.__class__, '_replay_cache'):
                        self.__class__._replay_cache = {}

                    debug_log = []
                    t0 = time.time()

                    try:
                        import gzip as _gz, os as _os, urllib.request as _ur, time as _time

                        fname  = "banknifty_5m_csv.json.gz" if asset == "bn" else "Bitcoin_BTCUSDT_IST_5m.json.gz"
                        gh_url = _GH_BN_URL if asset == "bn" else _GH_BTC_URL

                        script_dir = _os.path.dirname(_os.path.abspath(__file__))
                        search_dirs = [script_dir]
                        mount_src = "/mount/src"
                        if _os.path.isdir(mount_src):
                            for sub in _os.listdir(mount_src):
                                search_dirs.append(_os.path.join(mount_src, sub))

                        gz_path = None
                        for d in search_dirs:
                            candidate = _os.path.join(d, fname)
                            if _os.path.exists(candidate):
                                gz_path = candidate
                                break

                        debug_log.append(f"[1] asset={asset}  tf={tf}  chunk={chunk}")
                        debug_log.append(f"[2] Script dir: {script_dir}")
                        debug_log.append(f"[3] Searched: {search_dirs}")
                        debug_log.append(f"[4] Found at: {gz_path or 'NOT FOUND locally'}")

                        if not gz_path:
                            save_path = _os.path.join(script_dir, fname)
                            debug_log.append(f"[5] Downloading from GitHub…")
                            debug_log.append(f"[5] URL: {gh_url}")
                            try:
                                dl_start = _time.time()
                                req = _ur.Request(gh_url, headers={"User-Agent": "Mozilla/5.0"})
                                with _ur.urlopen(req, timeout=180) as resp:
                                    file_bytes = resp.read()
                                dl_secs  = round(_time.time() - dl_start, 2)
                                dl_kb    = round(len(file_bytes) / 1024, 1)
                                dl_speed = round(dl_kb / max(dl_secs, 0.01), 1)
                                debug_log.append(f"[6] Downloaded: {dl_kb} KB in {dl_secs}s  ({dl_speed} KB/s)")
                                with open(save_path, "wb") as _wf:
                                    _wf.write(file_bytes)
                                gz_path = save_path
                                debug_log.append(f"[7] Saved to: {gz_path}")
                            except Exception as _dl_err:
                                debug_log.append(f"[6] ❌ GitHub download FAILED: {_dl_err}")
                                body = json.dumps({"error": f"github_download_failed: {_dl_err}", "debug": debug_log}).encode()
                                self.send_response(503)
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Access-Control-Allow-Origin", "*")
                                self.send_header("Content-Length", str(len(body)))
                                self.end_headers()
                                self.wfile.write(body)
                                return
                        else:
                            fsize_kb = round(_os.path.getsize(gz_path) / 1024, 1)
                            debug_log.append(f"[5] File found ({fsize_kb} KB) — no download needed")

                        cache = self.__class__._replay_cache
                        if asset not in cache:
                            debug_log.append(f"[8] Loading JSON.gz into memory…")
                            load_start = _time.time()
                            with _gz.open(gz_path, "rb") as _f:
                                cache[asset] = json.load(_f)
                            load_secs = round(_time.time() - load_start, 2)
                            debug_log.append(f"[9] Loaded in {load_secs}s")
                        else:
                            debug_log.append(f"[8] Using cached data (already in memory)")

                        _raw = cache[asset]
                        if asset == "bn" and "data" in _raw and isinstance(_raw["data"], dict):
                            _data = _raw["data"]
                        else:
                            _data = _raw
                        available_keys = [k for k in _data.keys() if k != "meta"]
                        debug_log.append(f"[10] Available TF keys: {available_keys}")

                        tf_list = _data.get(tf)
                        matched_key = tf
                        if tf_list is None:
                            for k in _data.keys():
                                if k.lower() == tf.lower():
                                    tf_list = _data[k]
                                    matched_key = k
                                    debug_log.append(f"[11] '{tf}' matched via lowercase → '{k}'")
                                    break
                        if tf_list is None:
                            debug_log.append(f"[11] ❌ TF '{tf}' NOT found. Keys: {available_keys}")
                            body = json.dumps({"error": "tf_not_found", "tf_requested": tf, "available": available_keys, "debug": debug_log}).encode()
                            self.send_response(404)
                        else:
                            total_rows = len(tf_list)
                            start  = chunk * CHUNK_SIZE
                            end    = min(start + CHUNK_SIZE, total_rows)
                            slice_list = tf_list[start:end]
                            debug_log.append(f"[11] TF='{matched_key}'  rows={total_rows}  chunk={chunk}  slice={start}→{end}")

                            import datetime as _dt
                            candles  = []
                            bad_rows = 0
                            for row in slice_list:
                                try:
                                    ts_str = (row.get("ts") or row.get("Datetime") or "").strip()
                                    if "T" in ts_str:
                                        dt_obj = _dt.datetime.strptime(ts_str[:16], "%Y-%m-%dT%H:%M")
                                        ts = int(dt_obj.replace(tzinfo=_dt.timezone.utc).timestamp())
                                    elif " " in ts_str:
                                        dt_obj = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                                        IST_OFFSET = _dt.timedelta(hours=5, minutes=30)
                                        ts = int((dt_obj - IST_OFFSET).replace(tzinfo=_dt.timezone.utc).timestamp())
                                    else:
                                        dt_obj = _dt.datetime.strptime(ts_str, "%Y-%m-%d")
                                        ts = int(dt_obj.replace(tzinfo=_dt.timezone.utc).timestamp())

                                    o = float(row.get("o") or row.get("Open"))
                                    h = float(row.get("h") or row.get("High"))
                                    l = float(row.get("l") or row.get("Low"))
                                    c = float(row.get("c") or row.get("Close"))

                                    candles.append({
                                        "time":  ts,
                                        "open":  round(o, 2),
                                        "high":  round(h, 2),
                                        "low":   round(l, 2),
                                        "close": round(c, 2),
                                    })
                                except Exception:
                                    bad_rows += 1

                            total_elapsed = round(time.time() - t0, 2)
                            debug_log.append(f"[12] Candles={len(candles)}  bad_rows={bad_rows}  total={total_elapsed}s")

                            result = {
                                "candles":      candles,
                                "chunk":        chunk,
                                "total_rows":   total_rows,
                                "total_chunks": (total_rows + CHUNK_SIZE - 1) // CHUNK_SIZE,
                                "has_more":     end < total_rows,
                                "debug":        debug_log,
                            }
                            body = json.dumps(result).encode()
                            self.send_response(200)

                    except Exception as _ex:
                        import traceback
                        tb = traceback.format_exc()
                        debug_log.append(f"[!!] EXCEPTION: {_ex}")
                        debug_log.append(f"[!!] TRACEBACK:\n{tb}")
                        body = json.dumps({"error": str(_ex), "traceback": tb, "debug": debug_log}).encode()
                        self.send_response(500)

                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.end_headers()

        import socketserver
        for port in range(8502, 8511):
            try:
                srv = socketserver.ThreadingTCPServer(("0.0.0.0", port), _Handler)
                srv.daemon_threads = True
                try:
                    with open(".api_port", "w") as _f:
                        _f.write(str(port))
                except Exception:
                    pass
                srv.serve_forever()
                break
            except OSError:
                continue

    threading.Thread(target=_server_loop, name="ReplayAPI", daemon=True).start()


# ─── Start replay API server ──────────────────────────────────────────────────
_names = {t.name for t in threading.enumerate()}
if "ReplayAPI" not in _names:
    _register_api_route()



# ─── Fetch BTC data ───────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def _get_btc_data():
    btc_1m  = fetch_btc("1m",  1000)
    btc_15m = fetch_btc("15m", 1000)
    btc_day = load_btc_daily()
    return btc_1m, btc_15m, btc_day

btc_1m, btc_15m, btc_day = _get_btc_data()


# ─── Chart HTML builder ───────────────────────────────────────────────────────
def _build_chart_html(btc_1m, btc_15m, btc_day) -> str:
    import os, json as _json

    _html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.html")
    if not os.path.exists(_html_path):
        return "<p style='color:red'>chart.html not found</p>"

    with open(_html_path, "r", encoding="utf-8") as _f:
        html = _f.read()

    def _to_lwc(candles: list) -> str:
        out = []
        for b in candles:
            try:
                if isinstance(b, (list, tuple)):
                    t = int(b[0]) // 1000
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

    # BN data placeholders — empty (replay se aayega)
    html = html.replace("__BN_CANDLES__",  "[]")
    html = html.replace("__BN_5M__",       "[]")
    html = html.replace("__BN_15M__",      "[]")
    html = html.replace("__BN_45M__",      "[]")
    html = html.replace("__BN_DAILY__",    "[]")

    # API port inject
    _api_port = 0
    try:
        if os.path.exists(".api_port"):
            with open(".api_port") as _pf:
                _api_port = int(_pf.read().strip())
    except Exception:
        _api_port = 0
    html = html.replace("__API_PORT__", str(_api_port))

    return html


# ─── Render chart ─────────────────────────────────────────────────────────────
_chart_html = _build_chart_html(btc_1m, btc_15m, btc_day)
components.html(_chart_html, height=950, scrolling=False)
