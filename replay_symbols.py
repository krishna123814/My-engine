"""
replay_symbols.py — btcusdt2/3/4/5 ("practice replay" symbols) ka data layer.

DESIGN — v2 (simplified):
  Pehle socha tha BTC jaisa multi-timeframe (5m/8H/1D/3D/9D/27D) khud
  banayenge, lekin chart.html mein already ek GENERIC system hai jo
  Twelve-Data symbols (Gold/Dow/Apple/...) ke liye bana hai — ek hi base
  timeframe ka candles-list do, chart.html khud client-side 1D/3D/9D/27D
  resample kar leta hai (resampleCryptoDaily). Isliye output-shape ab
  EXACT WAISA HI hai jaisa td_symbols.py ka td_all_bucketed() deta hai:

      { key: { label, bucket_min, market, candles: [...] } }

  Isse app.py mein sirf itna karna hai — ye dict, TD wale dict ke saath
  MERGE karke window.__TD_SYMBOLS_DATA__ mein bhej dena. chart.html ka
  poora existing pipeline (ASSET_NAMES, STACK config, symbol-switcher,
  live-poll) generically kaam kar jayega — koi chart.html edit nahi
  chahiye.

  Master file 5-minute native hai, isliye bucket_min = 5 (candles seedha,
  bina resample ke, jaisi hain waisi).

  "market": None rakha hai — chart.html mein null/unknown market ka
  matlab "hamesha open maano" hota hai (isCryptoSymbol jaisa treat hota
  hai), jo replay-symbols ke liye sahi hai.
"""
import bisect
import io
import gzip
import json
import time
import requests

REPLAY_SYMBOL_REGISTRY = [
    {"key": "btcusdt2", "label": "🔁 BTCUSDT (2017→)", "start_t": 1483228800},  # 2017-01-01 UTC
    {"key": "btcusdt3", "label": "🔁 BTCUSDT (2017-04-19→)", "start_t": 1492560000},  # 2017-04-19 UTC
    {"key": "btcusdt4", "label": "🔁 BTCUSDT (2017-07-09→)", "start_t": 1499558400},  # 2017-07-09 UTC
    {"key": "btcusdt5", "label": "🔁 BTCUSDT (2017-11-21→)", "start_t": 1511222400},  # 2017-11-21 UTC
    {"key": "btcusdt6", "label": "🔁 BTCUSDT (2018-03-09→)", "start_t": 1520553600},  # 2018-03-09 UTC
    {"key": "btcusdt7", "label": "🔁 BTCUSDT (2018-06-25→)", "start_t": 1529884800},  # 2018-06-25 UTC
    {"key": "btcusdt8", "label": "🔁 BTCUSDT (2018-09-14→)", "start_t": 1536883200},  # 2018-09-14 UTC
    {"key": "btcusdt9", "label": "🔁 BTCUSDT (2018-12-31→)", "start_t": 1546214400},  # 2018-12-31 UTC
    {"key": "btcusdt10", "label": "🔁 BTCUSDT (2019-03-22→)", "start_t": 1553212800},  # 2019-03-22 UTC
    {"key": "btcusdt11", "label": "🔁 BTCUSDT (2019-07-08→)", "start_t": 1562544000},  # 2019-07-08 UTC
    {"key": "btcusdt12", "label": "🔁 BTCUSDT (2019-10-24→)", "start_t": 1571875200},  # 2019-10-24 UTC
    {"key": "btcusdt13", "label": "🔁 BTCUSDT (2020-01-13→)", "start_t": 1578873600},  # 2020-01-13 UTC
    {"key": "btcusdt14", "label": "🔁 BTCUSDT (2020-04-03→)", "start_t": 1585872000},  # 2020-04-03 UTC
    {"key": "btcusdt15", "label": "🔁 BTCUSDT (2020-08-16→)", "start_t": 1597536000},  # 2020-08-16 UTC
    {"key": "btcusdt16", "label": "🔁 BTCUSDT (2020-12-02→)", "start_t": 1606867200},  # 2020-12-02 UTC
    {"key": "btcusdt17", "label": "🔁 BTCUSDT (2021-04-16→)", "start_t": 1618531200},  # 2021-04-16 UTC
    {"key": "btcusdt18", "label": "🔁 BTCUSDT (2021-08-02→)", "start_t": 1627862400},  # 2021-08-02 UTC
    {"key": "btcusdt19", "label": "🔁 BTCUSDT (2021-10-22→)", "start_t": 1634860800},  # 2021-10-22 UTC
    {"key": "btcusdt20", "label": "🔁 BTCUSDT (2021-12-15→)", "start_t": 1639526400},  # 2021-12-15 UTC
    {"key": "btcusdt21", "label": "🔁 BTCUSDT (2022-03-06→)", "start_t": 1646524800},  # 2022-03-06 UTC
    {"key": "btcusdt22", "label": "🔁 BTCUSDT (2022-06-22→)", "start_t": 1655856000},  # 2022-06-22 UTC
    {"key": "btcusdt23", "label": "🔁 BTCUSDT (2022-10-08→)", "start_t": 1665187200},  # 2022-10-08 UTC
    {"key": "btcusdt24", "label": "🔁 BTCUSDT (2022-12-28→)", "start_t": 1672185600},  # 2022-12-28 UTC
    {"key": "btcusdt25", "label": "🔁 BTCUSDT (2023-03-19→)", "start_t": 1679184000},  # 2023-03-19 UTC
    {"key": "btcusdt26", "label": "🔁 BTCUSDT (2023-07-05→)", "start_t": 1688515200},  # 2023-07-05 UTC
    {"key": "btcusdt27", "label": "🔁 BTCUSDT (2023-10-21→)", "start_t": 1697846400},  # 2023-10-21 UTC
    {"key": "btcusdt28", "label": "🔁 BTCUSDT (2023-12-14→)", "start_t": 1702512000},  # 2023-12-14 UTC
    {"key": "btcusdt29", "label": "🔁 BTCUSDT (2024-03-04→)", "start_t": 1709510400},  # 2024-03-04 UTC
    {"key": "btcusdt30", "label": "🔁 BTCUSDT (2024-06-20→)", "start_t": 1718841600},  # 2024-06-20 UTC
    {"key": "btcusdt31", "label": "🔁 BTCUSDT (2024-10-06→)", "start_t": 1728172800},  # 2024-10-06 UTC
    {"key": "btcusdt32", "label": "🔁 BTCUSDT (2024-12-26→)", "start_t": 1735171200},  # 2024-12-26 UTC
    {"key": "btcusdt33", "label": "🔁 BTCUSDT (2025-03-17→)", "start_t": 1742169600},  # 2025-03-17 UTC
    {"key": "btcusdt34", "label": "🔁 BTCUSDT (2025-06-06→)", "start_t": 1749168000},  # 2025-06-06 UTC
    {"key": "btcusdt35", "label": "🔁 BTCUSDT (2025-10-19→)", "start_t": 1760832000},  # 2025-10-19 UTC
    {"key": "btcusdt36", "label": "🔁 BTCUSDT (2026-01-08→)", "start_t": 1767830400},  # 2026-01-08 UTC
    {"key": "btcusdt37", "label": "🔁 BTCUSDT (2026-03-30→)", "start_t": 1774828800},  # 2026-03-30 UTC
    {"key": "btcusdt38", "label": "🔁 BTCUSDT (2026-05-23→)", "start_t": 1779494400},  # 2026-05-23 UTC
    {"key": "btcusdt39", "label": "🔁 BTCUSDT (2026-07-16→)", "start_t": 1784160000},  # 2026-07-16 UTC
    {"key": "btcusdt40", "label": "🔁 BTCUSDT (2026-08-12→)", "start_t": 1786492800},  # 2026-08-12 UTC
]

_MASTER_FILENAME = "Bitcoin_BTCUSDT_IST_5m_json.gz"
_BUCKET_NAME     = "market-data"
_REVEAL_TABLE    = "reveal_state"

_MASTER_CACHE: dict = {}
_MASTER_CACHE_TTL = 3600
_STATE_CACHE_TTL  = 20
_state_cache: dict = {"data": None, "ts": 0}

# ── v3 — ON-DEMAND ACTIVE-KEY TRACKING ──────────────────────────────────
# Pehle live-poller (har ~30s) charo symbols ka dirty-check + push karta
# tha, kyunki charo hamesha browser mein load rehte the. Ab sirf jo symbol
# user ne khola hai wahi load hota hai — isliye poller ko bhi sirf USI
# symbol ka push karna chahiye, baaki 3 ka nahi (warna on-demand karne ka
# fayda hi nahi, background mein charo ka data phir bhi banta rahega).
# chart.html jab bhi replay-symbol select karta hai, ek chhota fire-and-
# forget GET (/api/replay_set_active?key=...) bhejta hai jo yahan set kar
# deta hai. Module-level global hai (session_state nahi) — jaisa upar
# _MASTER_CACHE/_last_pushed_t bhi hai, kyunki side-API server thread ka
# st.session_state se access nahi hota.
_active_key: str = ""


def replay_set_active_key(key: str) -> None:
    """chart.html se aata hai jab bhi koi replay-symbol select/switch
    hota hai. Khaali string = 'koi replay symbol abhi active nahi'."""
    global _active_key
    _active_key = key if key in {e["key"] for e in REPLAY_SYMBOL_REGISTRY} else ""


def replay_get_active_key() -> str:
    return _active_key


# Live-push ke liye: har symbol ka aakhri "browser ko bhej chuke" pointer.
# _live_data_pusher (app.py) periodically replay_pop_dirty_keys() call
# karega — jis symbol ka revealed_up_to_t badla hai, uski key wapas
# milegi, sirf usi ka fresh data dobara bhejna padega (poori 4 symbols
# baar-baar re-bhejne ki zaroorat nahi).
_last_pushed_t: dict = {}

# Pre-history cap PERMANENTLY REMOVED (v3 — on-demand model):
# Pehle sirf 1-saal peeche tak ka context bheja jaata tha (poori 2017 se
# history ek saath, upfront, bahut bhaari thi — btcusdt4 akela ~67 MB ban
# raha tha). Ab candles UPFRONT nahi bhejte — sirf jab user symbol par
# click kare tab ek symbol ka data on-demand fetch hota hai (app.py ka
# naya /api/replay_symbol side-API route dekho). Isliye size-cap ki
# zaroorat khatam ho gayi — ab har symbol HAMESHA master file ke bilkul
# start (2017-01-01, ts[0]) se hi bheja jaata hai. Ye zaroori hai taaki
# 3D/9D/27D jaisi badi-timeframe candles sabhi 4 symbols ke liye EK HI
# fixed anchor (2017-01-01) se align hon — warna alag-alag start_t ki
# wajah se alag anchor-date ban jaati thi aur bade-timeframe ki candle
# symbol-se-symbol mismatch karti thi (index-based resampleCryptoDaily
# grouping, chart.html mein — 1-saal-cap hi is mismatch ka root cause tha).

# ── Debug state (top-header "🔁 Replay Symbols" popup ke liye) ─────────────
# Purana _last_pushed_t sirf internal dirty-check ke kaam aata tha — bahar
# se koi ye nahi dekh sakta tha ki "live push kyun nahi aayi". Yahan har
# relevant moment (Supabase fetch, baseline-reset, dirty-check) apna
# snapshot record hota hai, taaki replay_get_debug_state() se poora
# "kahan atka" turant pata chale — guess karne ki zaroorat na pade.
_debug_state: dict = {
    "last_fetch_err": None,     # last _fetch_reveal_state() exception (ya None)
    "last_fetch_ok_at": 0,      # last SUCCESSFUL Supabase fetch ka wall-clock time
    "last_full_rerun_at": 0,    # get_all_replay_symbols() (= poora chart-html
                                 # rebuild) aakhri baar kab chala — ye hi
                                 # baseline-reset ka trigger hai
    # ── NAYE fields (round 2) — pichli baar "in sync" hamesha true dikhta
    # tha chahe push ho ya na ho (kyunki baseline usi push-function ke
    # andar hi resync ho jaata tha, debug-capture se PEHLE). Ab seedha
    # record karte hain: "kya is baar dirty mila?" aur "push karte waqt
    # koi exception to nahi aayi?" — ye 2 cheezein hi asli gap dikhayengi. ──
    "last_dirty_check_at": 0,     # replay_pop_dirty_keys() aakhri baar kab chala
    "last_dirty_keys": [],        # us check mein kaunsi keys DIRTY mili (khaali = kuch nahi badla)
    "last_dirty_keys_raw": {},    # us check ke exact cur_t values (per key) — taaki cron-tick capture ho raha hai ya nahi seedha dikhe
    "push_error": None,           # replay_get_bucketed() mein aakhri exception (agar koi hui)
    "push_error_at": 0,
    "push_success_count": 0,      # kitni baar push (candles build) successfully complete hua — lifetime counter
    "per_key": {
        e["key"]: {"db_value": None, "baseline": None, "last_check_at": 0}
        for e in REPLAY_SYMBOL_REGISTRY
    },
}


def _log(msg: str, level: str = "info", log_fn=None):
    if log_fn:
        try:
            log_fn(f"[replay_symbols] {msg}", level=level)
        except Exception:
            pass


def _load_master(supabase_url: str, log_fn=None) -> list:
    now = time.time()
    cached = _MASTER_CACHE.get("rows")
    if cached is not None and (now - _MASTER_CACHE.get("ts", 0)) < _MASTER_CACHE_TTL:
        return cached
    url = f"{supabase_url.rstrip('/')}/storage/v1/object/public/{_BUCKET_NAME}/{_MASTER_FILENAME}"
    try:
        resp = requests.get(url, timeout=90)
        resp.raise_for_status()
        with gzip.open(io.BytesIO(resp.content), "rb") as f:
            rows = json.load(f)
        rows = rows["data"] if isinstance(rows, dict) else rows
        _MASTER_CACHE["rows"] = rows
        _MASTER_CACHE["ts"] = now
        _log(f"master file loaded ({len(rows)} rows)", "ok", log_fn)
        return rows
    except Exception as e:
        _log(f"master file load FAILED: {e}", "err", log_fn)
        return cached or []


def _fetch_reveal_state(supabase_url: str, anon_key: str, log_fn=None) -> dict:
    now = time.time()
    if _state_cache["data"] is not None and (now - _state_cache["ts"]) < _STATE_CACHE_TTL:
        return _state_cache["data"]
    url = f"{supabase_url.rstrip('/')}/rest/v1/{_REVEAL_TABLE}?select=symbol,revealed_up_to_t"
    headers = {"apikey": anon_key, "Authorization": f"Bearer {anon_key}"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        rows = resp.json()
        state = {r["symbol"]: r["revealed_up_to_t"] for r in rows}
        _state_cache["data"] = state
        _state_cache["ts"] = now
        # ── debug: fetch cleanly succeeded ──
        _debug_state["last_fetch_err"] = None
        _debug_state["last_fetch_ok_at"] = now
        return state
    except Exception as e:
        _log(f"reveal_state fetch FAILED: {e}", "err", log_fn)
        # ── debug: record WHY the fetch failed, so the popup can show it
        # directly instead of just a stale/frozen dbValue ──
        _debug_state["last_fetch_err"] = f"{type(e).__name__}: {e}"
        return _state_cache["data"] or {}


def _resample_8h(rows: list) -> list:
    """5m raw rows ko 8-hour (480 min) buckets mein jodta hai — BTC ke
    _sv2_resample_btc jaisa hi logic (epoch-aligned, gapless-safe)."""
    sec = 480 * 60
    buckets: dict = {}
    order = []
    for r in rows:
        key = (r["t"] // sec) * sec
        if key not in buckets:
            buckets[key] = {"time": key, "open": r["o"], "high": r["h"],
                             "low": r["l"], "close": r["c"]}
            order.append(key)
        else:
            b = buckets[key]
            b["high"] = max(b["high"], r["h"])
            b["low"] = min(b["low"], r["l"])
            b["close"] = r["c"]
    return [buckets[k] for k in sorted(order)]


def _build_candles(entry: dict, master: list, ts: list, revealed_up_to_t: int) -> list:
    """Ek symbol-entry ke liye final candles list (8H buckets) — MASTER
    FILE KE BILKUL START (anchor, 2017-01-01) se leke live-revealed tak.
    Dono jagah (single-key on-demand fetch aur live push) se yehi reuse
    hota hai, taaki logic ek hi jagah rahe. Top-cell BTC jaisa "8H"
    dikhaye — isliye yahin resample kar dete hain (raw 5m seedha
    chart.html ko nahi bhejte).

    NOTE (v3 fix): pehle yahan `entry["start_t"] - _PRE_HISTORY_SECONDS`
    se ek per-symbol alag "show_from_t" nikalta tha — jo har symbol ka
    alag anchor-date bana deta tha, aur isi wajah se bade-timeframe
    (3D/9D/27D) resample mismatch karta tha symbol-se-symbol (chahe
    underlying master-file same ho). Ab hardcode `master_start_t` hi
    show_from_t hai — sabhi symbols ka data hamesha EK HI fixed date
    (2017-01-01) se shuru hota hai, isliye chart.html ki index-based
    resampleCryptoDaily grouping bhi sabke liye consistent banti hai."""
    master_start_t = ts[0] if ts else 0
    idx_start = bisect.bisect_left(ts, master_start_t)
    idx_end = bisect.bisect_right(ts, revealed_up_to_t)
    rows = master[idx_start:idx_end]
    return _resample_8h(rows)


def get_replay_symbols_metadata() -> dict:
    """v3 — ON-DEMAND MODEL: page-load (Enter Chart click) par ab ye call
    hota hai, get_all_replay_symbols() nahi. Sirf {label, bucket_min,
    market} deta hai, candles: [] (khaali) — taaki symbol-switcher mein
    naam turant dikh jaayen, lekin bhaari candle-data tab tak na aaye
    jab tak user khud us symbol par click na kare. Koi Supabase call
    nahi karta (registry se hi static info hai), isliye instant/free hai.
    chart.html jab is symbol ko select karega, ye khaali candles dekh kar
    naya /api/replay_symbol side-API call karega (fresh fetch)."""
    out = {}
    for entry in REPLAY_SYMBOL_REGISTRY:
        out[entry["key"]] = {
            "label": entry["label"],
            "bucket_min": 480,
            "market": None,
            "candles": [],
        }
    return out


def get_all_replay_symbols(supabase_url: str, anon_key: str, log_fn=None) -> dict:
    """{key: {label, bucket_min, market, candles}} — bilkul td_all_bucketed()
    jaisa hi shape, taaki app.py ismein aur TD wale dict ko merge karke
    ek hi window.__TD_SYMBOLS_DATA__ mein bhej sake."""
    if not supabase_url or not anon_key:
        return {}
    master = _load_master(supabase_url, log_fn)
    if not master:
        return {}
    state = _fetch_reveal_state(supabase_url, anon_key, log_fn)
    ts = [r["t"] for r in master]

    # ── debug: is call ka matlab hai poora chart-html rebuild ho raha hai
    # (full Streamlit script-rerun) — YAHI wo moment hai jo baseline
    # (_last_pushed_t) ko "abhi ka" value par reset kar deta hai. Isliye
    # is timestamp ko alag se record karte hain — agar future mein live-
    # push kaam na kare, popup mein turant dikh jayega ki full-rerun kitni
    # baar-baar ho raha hai (baseline ko settle hi nahi hone de raha).
    _debug_state["last_full_rerun_at"] = time.time()

    out = {}
    for entry in REPLAY_SYMBOL_REGISTRY:
        revealed_up_to_t = state.get(entry["key"], entry["start_t"] - 300)
        candles = _build_candles(entry, master, ts, revealed_up_to_t)
        if not candles:
            continue
        out[entry["key"]] = {
            "label": entry["label"],
            "bucket_min": 480,
            "market": None,
            "candles": candles,
            "revealed_up_to_t": revealed_up_to_t,  # FIX: countdown-timer ke liye zaroori — dekho chart.html _calcRemainingReplay()
        }
        _last_pushed_t[entry["key"]] = revealed_up_to_t  # baseline set — pehli page-load par hi ye data chala gaya
        # ── debug snapshot ──
        pk = _debug_state["per_key"].setdefault(entry["key"], {})
        pk["db_value"] = revealed_up_to_t
        pk["baseline"] = revealed_up_to_t
    return out


def replay_pop_dirty_keys(supabase_url: str, anon_key: str, log_fn=None) -> list:
    """Live-pusher (_live_data_pusher) periodically ye call karega. Jin
    symbols ka revealed_up_to_t pichhli baar se badal gaya hai, unki
    keys return karta hai — sirf unhi ka fresh data browser ko dobara
    bhejna padega (TD symbols ke td_pop_dirty_keys() jaisa hi pattern)."""
    if not supabase_url or not anon_key:
        return []
    state = _fetch_reveal_state(supabase_url, anon_key, log_fn)
    now = time.time()
    dirty = []
    raw_snapshot = {}
    for entry in REPLAY_SYMBOL_REGISTRY:
        key = entry["key"]
        cur_t = state.get(key)
        raw_snapshot[key] = cur_t
        # ── debug snapshot — record HAR check par (dirty mile ya na mile),
        # taaki popup mein dikh sake "DB kya keh raha hai" vs "baseline
        # kya hai" vs "aakhri baar check kab hua" — bina isse pata nahi
        # chalta tha ki check chal bhi raha hai ya nahi. ──
        pk = _debug_state["per_key"].setdefault(key, {})
        pk["db_value"] = cur_t
        pk["baseline"] = _last_pushed_t.get(key)
        pk["last_check_at"] = now
        if cur_t is None:
            continue
        if _last_pushed_t.get(key) != cur_t:
            dirty.append(key)
    # ── NAYA (round 2): ye exact snapshot record karo ki is check ne
    # kaunsi keys dirty paayi (aur cur_t raw values) — agar cron sach mein
    # tick karta hai lekin ye hamesha khaali list dikhaye, to seedha pata
    # chal jayega ki comparison-logic mein hi gadbad hai, guess nahi rahega.
    _debug_state["last_dirty_check_at"] = now
    _debug_state["last_dirty_keys"] = list(dirty)
    _debug_state["last_dirty_keys_raw"] = raw_snapshot
    return dirty


def replay_get_bucketed(key: str, supabase_url: str, anon_key: str, log_fn=None) -> dict:
    """Ek single symbol ka fresh {candles, revealed_up_to_t} — dirty-key
    push ke waqt use hota hai (poori 4 symbols dobara build karne ki
    zaroorat nahi).

    FIX: pehle sirf `candles` (list) return hota tha — countdown-timer
    (chart.html _calcRemainingReplay) ko `revealed_up_to_t` bhi chahiye
    (asli reveal-pointer, jisse remaining-time calculate hota hai), isliye
    ab dict return hota hai. Callers (app.py) ko bhi update kiya gaya hai."""
    try:
        entry = next((e for e in REPLAY_SYMBOL_REGISTRY if e["key"] == key), None)
        if not entry:
            return {"candles": [], "revealed_up_to_t": None}
        master = _load_master(supabase_url, log_fn)
        if not master:
            # ── NAYA: agar master file load hi na ho paayi (network/
            # Supabase storage issue), pehle ye SILENTLY khaali list
            # laut jaata tha — app.py isse "candles: []" bhej deta,
            # jo shayad frontend chup-chaap ignore kar deta (empty
            # array par kuch bhi update nahi hota). Ab is exact wajah
            # ko record karte hain. ──
            _debug_state["push_error"] = f"{key}: _load_master returned empty (Supabase storage fetch failed?)"
            _debug_state["push_error_at"] = time.time()
            return {"candles": [], "revealed_up_to_t": None}
        ts = [r["t"] for r in master]
        state = _fetch_reveal_state(supabase_url, anon_key, log_fn)
        revealed_up_to_t = state.get(key, entry["start_t"] - 300)
        candles = _build_candles(entry, master, ts, revealed_up_to_t)
        _last_pushed_t[key] = revealed_up_to_t
        # ── debug: baseline ab is fresh push ke baad update ho gaya ──
        pk = _debug_state["per_key"].setdefault(key, {})
        pk["baseline"] = revealed_up_to_t
        # ── debug: successful push count (lifetime) — agar ye number
        # popup mein badhte hue dikhe to matlab push function khud
        # kaamyabi se chal raha hai; agar ye kabhi na badhe to gadbad
        # yahin (candle-building) mein hai. ──
        _debug_state["push_success_count"] += 1
        return {"candles": candles, "revealed_up_to_t": revealed_up_to_t}
    except Exception as e:
        # ── NAYA: pehle koi bhi exception yahan se seedha upar (app.py)
        # tak propagate ho jaati thi, jahan _slog_exception() sirf
        # server-side startup-log mein likh deta tha — popup mein kabhi
        # nahi dikhta tha ki push fail kyun hui. Ab exact error yahin
        # capture karke record karte hain, taaki popup se hi turant
        # pata chale, phir bhi re-raise karte hain taaki app.py ka
        # existing behaviour (skip is push, baaki sab untouched) same rahe.
        _debug_state["push_error"] = f"{key}: {type(e).__name__}: {e}"
        _debug_state["push_error_at"] = time.time()
        raise


def replay_get_debug_state() -> dict:
    """Poora debug-snapshot — top-header ke '🔁 Replay Symbols' popup ke
    naye fields ke liye. Ye sirf ab-tak record hui values lautata hai
    (koi naya network call nahi karta), isliye har 1s bhi call karna
    safe/sasta hai. Har field batata hai 'kahan atka':
      - last_fetch_err     : Supabase reveal_state read hi fail ho raha hai?
      - last_fetch_ok_at   : aakhri successful fetch kab hua
      - last_full_rerun_at : poora chart-html rebuild (jo baseline reset
                             karta hai) aakhri baar kab hua
      - keys[key].db_value : Supabase mein abhi revealed_up_to_t
      - keys[key].baseline : Python RAM mein 'aakhri push hui' value
                             (db_value != baseline => push pending/dirty)
      - keys[key].last_check_at : dirty-check aakhri baar kab chala
    """
    now = time.time()
    out = {
        "last_fetch_err": _debug_state.get("last_fetch_err"),
        "last_fetch_ok_at": _debug_state.get("last_fetch_ok_at", 0),
        "last_full_rerun_at": _debug_state.get("last_full_rerun_at", 0),
        # ── round-2 fields ──
        "last_dirty_check_at": _debug_state.get("last_dirty_check_at", 0),
        "last_dirty_keys": _debug_state.get("last_dirty_keys", []),
        "last_dirty_keys_raw": _debug_state.get("last_dirty_keys_raw", {}),
        "push_error": _debug_state.get("push_error"),
        "push_error_at": _debug_state.get("push_error_at", 0),
        "push_success_count": _debug_state.get("push_success_count", 0),
        "server_now": now,
        "keys": {},
    }
    for entry in REPLAY_SYMBOL_REGISTRY:
        k = entry["key"]
        pk = _debug_state["per_key"].get(k, {})
        out["keys"][k] = {
            "db_value": pk.get("db_value"),
            "baseline": pk.get("baseline"),
            "last_check_at": pk.get("last_check_at", 0),
        }
    return out


# ═════════════════════════════════════════════════════════════════════════
# ── MASTER FILE — DAILY UPDATE (write-side) ─────────────────────────────
# ═════════════════════════════════════════════════════════════════════════
# NAYA (SV1 ko "updatable" banane ke liye, jaisa SV2 BTC/BankNifty roz
# update hoti hai). Purane cron (`reveal_candles_sql_every_5min`) ko
# YE POORA SECTION KABHI TOUCH NAHI KARTA — cron sirf `reveal_state` +
# `master_file_meta.data_upto_t` (2 tables) padhta hai, master file ke
# CONTENT se cron ka koi seedha connection nahi hai. Isliye master file
# update karna cron ke liye pura invisible hai (jab tak `data_upto_t`
# sahi sync rahe — wahi cap hai jo cron ko batata hai "asal data kahan
# tak hai", dekho migration Section 0 / 2026-09-03).
#
# RACE-CONDITION SAFETY: chart.html browser se SEEDHA is master file ko
# Supabase Storage se fetch karta hai (server-cache ke through nahi —
# dekho SUPABASE_SETUP.md Section 7). Isliye asli filename ko kabhi
# seedha overwrite (re-upload) nahi karte — warna kisi bhi user ka
# browser exact usi second half-written/partial gzip utha sakta hai.
# Iski jagah:
#   1. Naya data ek TEMP filename se upload hota hai (poora naya file,
#      asli filename abhi tak untouched)
#   2. Phir Supabase Storage "move" (rename) se 2 fast metadata-level
#      operations mein swap hota hai (live→backup, temp→live) — move
#      poori file dobara stream nahi karta, isliye race-window seconds
#      ki jagah milliseconds ka reh jaata hai.
#   3. Agar step 2 ka doosra move fail ho jaaye, backup se turant
#      ROLLBACK ho jaata hai — live filename kabhi bhi "missing" state
#      mein nahi rehta.
# Service-role key chahiye in sabke liye (write access) — anon key sirf
# read ke liye hai, isse kabhi write nahi ho sakta (RLS).

_MASTER_TEMP_NAME   = _MASTER_FILENAME.replace(".gz", "_new.gz")
_MASTER_BACKUP_NAME = _MASTER_FILENAME.replace(".gz", "_backup.gz")


def replay_get_master_rows(supabase_url: str, log_fn=None) -> list:
    """Public wrapper — poora master file (existing 1hr-cache respect
    karta hai) return karta hai. Update-flow (app.py) isse 'abhi ka
    aakhri candle kahan tak hai' (last_t) nikalne ke liye use karta hai."""
    return _load_master(supabase_url, log_fn)


def replay_master_upload_temp(supabase_url: str, service_key: str,
                               data_bytes: bytes, log_fn=None) -> tuple:
    """Naya poora master-data (gzip bytes) ek TEMP filename se upload
    karta hai — asli live filename ko is step mein bilkul touch nahi
    karta. x-upsert:true, taaki baar-baar retry safe rahe (purani temp
    file ho to overwrite ho jaayegi, koi accumulation nahi)."""
    if not supabase_url or not service_key:
        return False, "supabase_url ya service_key missing."
    url = f"{supabase_url.rstrip('/')}/storage/v1/object/{_BUCKET_NAME}/{_MASTER_TEMP_NAME}"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/gzip",
        "x-upsert": "true",
    }
    try:
        resp = requests.post(url, headers=headers, data=data_bytes, timeout=120)
        resp.raise_for_status()
        _log(f"temp master upload OK ({_MASTER_TEMP_NAME}, {len(data_bytes)} bytes)", "ok", log_fn)
        return True, _MASTER_TEMP_NAME
    except Exception as e:
        _log(f"temp master upload FAILED: {e}", "err", log_fn)
        return False, f"{type(e).__name__}: {e}"


def _replay_storage_move(supabase_url: str, service_key: str,
                          source: str, dest: str, log_fn=None) -> tuple:
    """Ek generic Supabase Storage 'move' (rename) call — fast metadata
    operation, poori file dobara upload/stream nahi karta."""
    url = f"{supabase_url.rstrip('/')}/storage/v1/object/move"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
    }
    body = {"bucketId": _BUCKET_NAME, "sourceKey": source, "destinationKey": dest}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        _log(f"storage move OK: {source} -> {dest}", "ok", log_fn)
        return True, "ok"
    except Exception as e:
        _log(f"storage move FAILED ({source} -> {dest}): {e}", "err", log_fn)
        return False, f"{type(e).__name__}: {e}"


def replay_master_swap(supabase_url: str, service_key: str, log_fn=None) -> tuple:
    """TEMP file (_new.gz) ko asli live filename par SWAP karta hai —
    2-step move (live->backup, temp->live). Beech mein kabhi bhi live
    filename 'missing' nahi hota (move se pehle purana backup delete
    karte hain taaki move fail na ho 'destination already exists' se,
    lekin agar delete fail bhi ho jaaye to bhi aage badhte hain — kuch
    Supabase versions overwrite allow karte hain).

    Agar doosra move (temp->live) fail ho jaaye, PEHLE move ko turant
    ROLLBACK karte hain (backup->live) — taaki live filename kabhi bhi
    khaali/missing na rahe."""
    if not supabase_url or not service_key:
        return False, "supabase_url ya service_key missing."

    # purana backup hata do (best-effort — na ho paaye to bhi aage badhte hain)
    del_url = f"{supabase_url.rstrip('/')}/storage/v1/object/{_BUCKET_NAME}/{_MASTER_BACKUP_NAME}"
    headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
    try:
        requests.delete(del_url, headers=headers, timeout=20)
    except Exception:
        pass

    ok1, msg1 = _replay_storage_move(supabase_url, service_key, _MASTER_FILENAME, _MASTER_BACKUP_NAME, log_fn)
    if not ok1:
        return False, f"live->backup move fail (live file abhi bhi untouched hai): {msg1}"

    ok2, msg2 = _replay_storage_move(supabase_url, service_key, _MASTER_TEMP_NAME, _MASTER_FILENAME, log_fn)
    if not ok2:
        # ROLLBACK — backup wapas live filename par
        ok3, msg3 = _replay_storage_move(supabase_url, service_key, _MASTER_BACKUP_NAME, _MASTER_FILENAME, log_fn)
        if ok3:
            return False, f"temp->live move fail, lekin ROLLBACK safal (live file purani wali hi hai): {msg2}"
        return False, f"temp->live move fail AUR rollback bhi fail — MANUALLY check karo Storage mein! move-err: {msg2} | rollback-err: {msg3}"

    # swap safal — is process ki in-memory cache turant invalidate karo,
    # taaki agla replay_get_master_rows() call fresh (nayi) file laaye,
    # purani cached (RAM) copy 1hr tak stale serve na ho.
    _MASTER_CACHE["rows"] = None
    _MASTER_CACHE["ts"] = 0
    return True, "Master file swap ho gayi (live filename ab naya data serve kar raha hai)."


def replay_update_master_cap(supabase_url: str, service_key: str,
                              new_data_upto_t: int, log_fn=None) -> tuple:
    """`master_file_meta.data_upto_t` update karta hai — SQL migration
    (2026-09-03) ke baad cron ka LEAST()-cap yehi column padhta hai.
    Ye is poore update-flow ka AAKHRI step hona chahiye (file swap ke
    baad) — warna cron file mein data hone se PEHLE hi us waqt tak
    reveal karne ki koshish kar sakta hai."""
    if not supabase_url or not service_key:
        return False, "supabase_url ya service_key missing."
    url = f"{supabase_url.rstrip('/')}/rest/v1/master_file_meta?id=eq.1"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        resp = requests.patch(url, headers=headers, json={"data_upto_t": new_data_upto_t}, timeout=20)
        resp.raise_for_status()
        _log(f"master_file_meta.data_upto_t updated -> {new_data_upto_t}", "ok", log_fn)
        return True, "cap updated"
    except Exception as e:
        _log(f"master_file_meta update FAILED: {e}", "err", log_fn)
        return False, f"{type(e).__name__}: {e}"
