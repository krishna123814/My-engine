"""
storage.py — Supabase Storage (.gz) load/upload helpers + local JSON
"state" files (update-status tracking, generic key/value local persistence).
"""
import os
import json
import time
import threading
import requests
import gzip as _gzip
import io as _io
from urllib.parse import quote

from config import _get_secret, IST, _ist_now
from startup_log import _slog, _slog_exception

import gzip as _gzip
import io as _io

_SV2_CACHE: dict = {}   # in-memory cache taaki har rerun pe re-read na ho

# ── Local paths — ye sirf naye-candle-append ke waqt local save + HF Space
# push ke liye use hote hain (write path). READ path (neeche) ab SIRF
# Supabase se hota hai — local-file-read aur GitHub fallback PERMANENTLY
# hata diye gaye hain.
_LOCAL_BN_GZ  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banknifty_5m_csv_json.gz")
_LOCAL_BTC_GZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Bitcoin_BTCUSDT_IST_5m_json.gz")

# Supabase Storage — public bucket ("My 500 stock"), ab ye hi ONLY data source hai.
_SUPABASE_BASE = "https://bybiihyjpdxxobxwyajh.supabase.co/storage/v1/object/public/My%20500%20stock"
_SUPABASE_BN_URL  = f"{_SUPABASE_BASE}/banknifty_5m_csv_json.gz"
_SUPABASE_BTC_URL = f"{_SUPABASE_BASE}/Bitcoin_BTCUSDT_IST_5m_json.gz"

def _sv2_parse_gz_bytes(raw_bytes: bytes) -> list:
    """Raw .gz bytes ko decompress + JSON parse karo."""
    with _gzip.open(_io.BytesIO(raw_bytes), "rb") as f:
        data = json.load(f)
    # Both formats supported: {"meta":..,"data":[..]} or plain list
    return data["data"] if isinstance(data, dict) else data

def _sv2_fetch_gz(label: str, supabase_url: str) -> "tuple[list, str]":
    """SIRF Supabase (public bucket) se fetch karo — koi local-file ya GitHub
    fallback nahi bacha, permanently hata diya gaya hai. Returns
    (rows, source) — source 'supabase'/'failed'."""
    try:
        resp = requests.get(supabase_url, timeout=90)
        resp.raise_for_status()
        rows = _sv2_parse_gz_bytes(resp.content)
        _slog(f"SV2 [{label}] SUPABASE se load hui ({len(rows)} rows) — "
              f"{supabase_url}", level="ok")
        return rows, "supabase"
    except Exception as e:
        _slog(f"SV2 [{label}] Supabase fetch FAIL ({e}) — koi fallback nahi hai (permanently hataya gaya).",
              level="err")
        return [], "failed"

def _sv2_load_bn_gz() -> list:
    """BankNifty 5m candles — SIRF Supabase se, cached."""
    if "bn_raw" in _SV2_CACHE:
        return _SV2_CACHE["bn_raw"]
    rows, source = _sv2_fetch_gz("BankNifty", _SUPABASE_BN_URL)
    _SV2_CACHE["bn_source"] = source
    if not rows:
        _SV2_CACHE["bn_err"] = "GZ_FETCH_FAILED"
        return []
    _SV2_CACHE["bn_raw"] = rows
    return rows

def _sv2_load_btc_gz() -> list:
    """BTC 5m candles — SIRF Supabase se, cached."""
    if "btc_raw" in _SV2_CACHE:
        return _SV2_CACHE["btc_raw"]
    rows, source = _sv2_fetch_gz("BTC", _SUPABASE_BTC_URL)
    _SV2_CACHE["btc_source"] = source
    if not rows:
        _SV2_CACHE["btc_err"] = "GZ_FETCH_FAILED"
        return []
    _SV2_CACHE["btc_raw"] = rows
    return rows

# ─── Supabase WRITE (Storage upload) — service_role key se ─────────────────
# NOTE: service_role key sirf yahan (server-side, app.py) use hoti hai —
# KABHI bhi chart.html/browser mein nahi jaani chahiye (RLS bypass karti
# hai). Anon key (jo chart.html mein already hai) sirf read/auth ke liye hai.
SUPABASE_PROJECT_URL = _get_secret("SUPABASE_URL", "https://bybiihyjpdxxobxwyajh.supabase.co").rstrip("/")
SUPABASE_SERVICE_KEY = _get_secret("SUPABASE_SERVICE_KEY")

# Replay-practice symbols (btcusdt2/3/4/5) — dusra, alag Supabase project
REPLAY_SUPABASE_URL = _get_secret("TEST_SUPABASE_URL").rstrip("/")
REPLAY_SUPABASE_ANON_KEY = _get_secret("TEST_SUPABASE_ANON_KEY")
_SUPABASE_BUCKET = "My 500 stock"

# Twelve Data — external (non-NSE, non-crypto) symbols jaise Gold/Dow Jones.
# Generic registry td_symbols.py mein hai — naya symbol wahan add karo,
# yahan kuch badalne ki zaroorat nahi.
TWELVEDATA_API_KEY = _get_secret("TWELVEDATA_API_KEY")

# Finnhub — LIVE ticks (WebSocket, free-tier) ke liye. Twelve Data se sirf
# historical/backfill hota hai; live-price update yahan se aata hai (dekho
# _finnhub_ws_loop() aur td_symbols.td_apply_live_tick()).
FINNHUB_API_KEY = _get_secret("FINNHUB_API_KEY")

def _supabase_upload(path_in_bucket: str, data_bytes: bytes, content_type: str = "application/json") -> tuple[bool, str]:
    """Supabase Storage mein file upload/overwrite karta hai (service_role
    key se). x-upsert:true se existing file bhi overwrite ho jaati hai
    (naya file banane ki zaroorat nahi)."""
    if not SUPABASE_SERVICE_KEY:
        return False, "SUPABASE_SERVICE_KEY secret nahi mila — HF Space Settings → Variables and secrets mein add karo."
    url = f"{SUPABASE_PROJECT_URL}/storage/v1/object/{quote(_SUPABASE_BUCKET)}/{quote(path_in_bucket)}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    try:
        resp = requests.post(url, headers=headers, data=data_bytes, timeout=60)
        resp.raise_for_status()
        return True, f"{path_in_bucket} Supabase par upload ho gayi."
    except Exception as e:
        _slog_exception(f"_supabase_upload({path_in_bucket})", e)
        return False, f"Supabase upload fail ({path_in_bucket}): {e}"

def _gz_push_to_supabase(local_path: str, filename: str) -> tuple[bool, str]:
    """Local .gz file ko seedha Supabase Storage par push karta hai (ab yahi
    ONLY destination hai — HF Space push PERMANENTLY hataya gaya kyunki
    read path bhi ab Supabase-only hai, HF push se kuch fayda nahi tha)."""
    try:
        with open(local_path, "rb") as f:
            data = f.read()
    except Exception as e:
        return False, f"Local file read fail: {e}"
    return _supabase_upload(filename, data, "application/gzip")

# ─── Local persistent storage — chart save/restore (drawings, Future Line,
# zoom, layouts, per-panel settings). Ye ab Supabase se NAHI hota — Supabase
# egress limit badh jaane ki wajah se HF Space ke bucket-mounted persistent
# volume (Space Settings → Storage → mounted at /data, dekho runtime.volumes
# mein mountPath) par shift kar diya gaya hai. Personal single-user app hai,
# isliye per-device split nahi — sirf 3 chhoti JSON files (state/settings/
# layout), koi bhi browser is Space ko khole to wahi teeno milte hain, koi
# login/auth ki zaroorat nahi. Agar /data mount na mile (jaise local dev
# testing mein), local folder par fallback ho jaata hai taaki crash na ho.
LOCAL_STATE_DIR = "/data/app_state" if os.path.isdir("/data") else os.path.join(os.getcwd(), "_local_app_state")
try:
    os.makedirs(LOCAL_STATE_DIR, exist_ok=True)
except Exception:
    pass

_LOCAL_STATE_KINDS = ("state", "settings", "layout")

def _local_state_file(kind: str) -> str:
    return os.path.join(LOCAL_STATE_DIR, f"{kind}.json")

def _local_state_load_all() -> dict:
    """Teeno kinds (state/settings/layout) disk se ek hi baar mein padh ke
    dict return karta hai — chart.html isko ek hi GET call mein pull karta
    hai (pehle Supabase se 3 alag-alag GET calls lagti thi)."""
    out = {}
    for kind in _LOCAL_STATE_KINDS:
        path = _local_state_file(kind)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    out[kind] = json.load(f)
            else:
                out[kind] = None
        except Exception as e:
            _slog_exception(f"_local_state_load_all({kind})", e)
            out[kind] = None
    return out

def _local_state_save(kind: str, data) -> tuple[bool, str]:
    """Ek kind ko atomically disk par save karta hai — pehle .tmp file mein
    poora likh ke phir os.replace() se rename karte hain, taaki beech mein
    process crash/restart ho jaaye to bhi purani file corrupt na ho, aur
    parallel save-requests aapas mein garbled na likh dein."""
    if kind not in _LOCAL_STATE_KINDS:
        return False, f"unknown kind: {kind}"
    path = _local_state_file(kind)
    tmp  = path + ".tmp"
    try:
        payload = json.dumps(data)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
        return True, f"{kind} saved ({len(payload)} bytes) -> {path}"
    except Exception as e:
        _slog_exception(f"_local_state_save({kind})", e)
        return False, str(e)

# ─── Daily update-status tracker — Supabase par hi persist hota hai (local
# disk/session_state nahi, kyunki HF Space restart par wo reset ho sakte
# hain) — taaki "aaj already ho chuka hai" cross-restart bhi pata chale. ──
_UPDATE_STATUS_FILENAME = "update_status.json"
_UPDATE_STATUS_URL = f"{_SUPABASE_BASE}/{_UPDATE_STATUS_FILENAME}"

def _load_update_status() -> dict:
    """Public bucket se seedha GET (read, no auth chahiye). File na ho ya
    corrupt ho to khaali dict — matlab "kabhi update nahi hua"."""
    try:
        resp = requests.get(_UPDATE_STATUS_URL, timeout=20)
        if resp.status_code == 200:
            return resp.json() or {}
    except Exception:
        pass
    return {}

def _save_update_status(status: dict) -> tuple[bool, str]:
    payload = json.dumps(status).encode("utf-8")
    return _supabase_upload(_UPDATE_STATUS_FILENAME, payload, "application/json")

def _already_updated_today(source: str) -> bool:
    """source: 'bn' / 'btc' / 'nifty500'."""
    status = _load_update_status()
    today_str = _ist_now().strftime("%Y-%m-%d")
    return status.get(source, {}).get("last_date") == today_str

def _mark_updated_today(source: str) -> None:
    status = _load_update_status()
    status[source] = {"last_date": _ist_now().strftime("%Y-%m-%d"), "last_ts": int(time.time())}
    ok, msg = _save_update_status(status)
    _slog(f"AUTO_UPDATE [{source}] status-mark: {msg}", level="ok" if ok else "warn")

# ─── Auto-update orchestration — background thread, once-per-process-launch
# guard + Supabase-persisted daily skip. ────────────────────────────────────
_AUTO_UPDATE_LAUNCHED: dict = {}   # this-process guard, thread-safe se
_AUTO_UPDATE_LOCK = threading.Lock()

def _maybe_launch_background_update(source: str, fn) -> None:
    """source: 'bn' / 'btc' / 'nifty500'. fn: no-arg callable → (ok, msg).
    Ek hi baar per-process launch hota hai (thread already chal raha ho to
    dobara nahi); thread ke andar Supabase status-file check hota hai —
    agar aaj already ho chuka hai to silently skip, warna update chalake
    Supabase par status mark karta hai. Chart mode block nahi hota — ye
    poora kaam background thread mein hota hai."""
    with _AUTO_UPDATE_LOCK:
        if _AUTO_UPDATE_LAUNCHED.get(source):
            return
        _AUTO_UPDATE_LAUNCHED[source] = True

    def _runner():
        try:
            if _already_updated_today(source):
                _slog(f"AUTO_UPDATE [{source}]: aaj already ho chuka hai — skip.", level="info")
                return
            _slog(f"AUTO_UPDATE [{source}]: background update shuru ho raha hai.", level="info")
            ok, msg = fn()
            _slog(f"AUTO_UPDATE [{source}]: {msg}", level="ok" if ok else "err")
            if ok:
                _mark_updated_today(source)
        except Exception as e:
            _slog_exception(f"AUTO_UPDATE [{source}]", e)

    threading.Thread(target=_runner, daemon=True, name=f"auto_update_{source}").start()

# ─── Append naye candles .gz files mein (local file update + Supabase push) ─
_GZ_APPEND_OFFSET = 19800   # BankNifty ke liye real-UTC → IST-naive (same as _IST_NAIVE_OFFSET, upar define se pehle yahan bhi chahiye)

def _gz_save_local(path: str, rows: list) -> None:
    """Rows (list of {t,o,h,l,c} dicts) ko gzip-compressed JSON bana ke local
    path par likho — .gz file ka wahi format jisse app already padhti hai."""
    payload = json.dumps(rows).encode("utf-8")
    with _gzip.open(path, "wb") as f:
        f.write(payload)

# REMOVED (user ka explicit ask): _gz_push_to_hf_space / _gz_pull_from_hf_space
# — ye sirf "File Banao" (Nifty500 local .gz) legacy feature ke liye the,
# jo poori tarah hata di gayi hai (SV3 chart Supabase se aata hai, HF Space
# repo wale .gz se nahi).

