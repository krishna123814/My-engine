import io
import json
import os
import time
import threading
import hashlib
import hmac
import zipfile
import requests
import streamlit as st
import streamlit.components.v1 as components
import datetime
from urllib.parse import urlencode, quote
from concurrent.futures import ThreadPoolExecutor
import td_symbols as _td
import replay_symbols as _replay

# ─── Split-out modules (Phase 1 refactor) ──────────────────────────────────
# Ye sab pehle isi file (app.py) mein the — ab alag files mein hain taaki
# file chhoti/samajhne mein aasan ho. Behavior EXACTLY same hai, sirf
# location badla hai. Dekho: config.py, hf_admin.py, startup_log.py,
# credentials.py, candle_state.py
from config import (
    _get_secret, FAST2SMS_KEY, HF_TOKEN, HF_SPACE_ID,
    CREDS_FILE, BN_LIVE_FILE, DAILY_CACHE_FILE, BN_DAILY_CACHE,
    DAILY_CACHE_TTL, HIST_CACHE_TTL, IST, _ist_now,
    DEFAULT_APP_ID, DEFAULT_SECRET, DEFAULT_CLIENT_ID, DEFAULT_PASSWORD,
    REDIRECT_URI,
)
from hf_admin import restart_hf_space, set_hf_proxy_variable, pause_hf_space
from startup_log import (
    _STARTUP_LOG_FILE, _STARTUP_LOG_LOCK, _STARTUP_LOG_MAX, _PERSIST_TAGS,
    _STARTUP_LOG_LAST_DISK_ERROR, _STARTUP_LOG,
    _load_startup_log_from_disk, _save_startup_log_to_disk,
    _slog, _slog_exception, _startup_log_snapshot,
)
from credentials import load_creds, save_creds, _get_binance_creds
from candle_state import _update_candle_ltp, _set_candle_from_bar, _CANDLE, _CANDLE_LOCK

# ─── Phase 2: broker/network/storage modules ───────────────────────────────
from broker_meta import (
    fyers_get_access_token, refresh_fyers_meta_cache, refresh_binance_meta_cache,
    fyers_get_option_chain, BINANCE_EAPI_URL,
    OC_FILE, _OC_CACHE, _OC_DEBUG, _OC_LOCK, _OC_TTL,
)
from network_proxy import (
    _proxy_apply, _load_proxy_from_env, _get_ws_proxy, _test_proxy, _PROXY_LOCK,
    _PROXY_CACHE,
)
from binance_rest import (
    _binance_call, binance_get_spot_balance, binance_get_spot_price,
    _btc_sv3_build_and_upload_full, _btc_sv3_incremental_update,
)
from storage import (
    _supabase_upload, _local_state_load_all, _local_state_save,
    _load_update_status, _mark_updated_today, _maybe_launch_background_update,
    FINNHUB_API_KEY, REPLAY_SUPABASE_URL, REPLAY_SUPABASE_ANON_KEY,
    TWELVEDATA_API_KEY,
)
from login_session import (
    _token_monitor_loop, _extract_auth_code, is_session_active, _sess_cache,
)
from fyers_history import (
    _fyers_history, fetch_bn_intraday, load_bn_daily,
    fetch_btc, load_btc_daily,
)
from market_data import (
    _append_new_btc_candles, _replay_master_incremental_update,
    _append_new_bn_candles, _fetch_nifty500_symbols, _nifty500_incremental_update,
    load_sv2_chunk_settings, save_sv2_chunk_settings, _sv2_get_max,
    _sv3_symbol_list, _build_sv3_data, _build_sv3_bulk_all_stocks, _sv3_to_js,
    load_sv3_last_symbol, save_sv3_last_symbol,
    SV2_CHUNK_SETTINGS_FILE, _SV2_MAX_BN_DEFAULT, _SV2_MAX_BTC_DEFAULT,
    _SV2_MAX_BOUNDS, _SV3_CACHE,
)

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

# ── Fresh-boot detection — disk-saved boot_pid se compare hota hai (poori
# detail startup_log.py ke docstring mein). YAHIN capture karna zaroori hai,
# is point se aage koi bhi _slog() call nahi hona chahiye jo isse pehle
# chale — warna PID file update ho jaayegi ek naya check karne se pehle hi.
_prev_boot_pid = None
try:
    with open(_STARTUP_LOG_FILE, "r", encoding="utf-8") as _f_bp:
        _prev_boot_pid = json.load(_f_bp).get("boot_pid")
except Exception:
    _prev_boot_pid = None
_is_fresh_boot = (_prev_boot_pid != os.getpid())

# ─── Global live-tick store, feed-health debug dict ────────────────────────
# (ab live_state.py mein — broker_meta.py ko bhi _LIVE chahiye, isliye shared
# module mein rakha taaki circular import na ho)
from live_state import (
    _LIVE, _LIVE_LOCK, _LAST_TICK_JS, _LAST_TICK_LOCK,
    _BN_FEED_DEBUG, _BN_FEED_DEBUG_LOCK,
)
# ─── Binance FULL option chain (multi-strike, CE/PE grid) — WEBSOCKET LIVE ──
# Stack View 1 bottom-bar "⛓ Chain" panel ke liye — jab top-left symbol BTC ho
# to isi shape ka data Fyers wale option-chain jaisa hi (rows: strike/ce/pe)
# frontend ko milta hai, taaki wahi ek UI dono asset render kar sake.
#
# PEHLE: har 1 second REST se exchangeInfo + ticker + price teeno fetch hote
# the — Binance API limit cross hone ka risk. AB: sirf WebSocket se live data
# aata hai (mark price/bid/ask/greeks/IV + last trade + spot price), aur REST
# sirf 2 jagah, bahut kam frequency par:
#   • exchangeInfo (strikes/expiries list) — har 10 minute mein 1 baar
#     (BINANCE_OC_META_TTL) — ye rarely badalta hai.
#   • 24hr ticker (OI/Volume/Change% — WS pe koi public option-OI stream
#     nahi hai) — har 5 second mein 1 baar, POORE market ke liye EK hi call
#     (per-symbol nahi) — (BINANCE_OC_TICKER_TTL).
# Payload build karne wala background loop (_binance_oc_bg_loop) ab bilkul
# koi network call NAHI karta — sirf in-memory WS data se render karta hai.
from live_engine import (
    _ensure_binance_threads, _ensure_finnhub_ws_thread, _ensure_fyers_threads,
    _get_live_payload, _register_api_route,
    get_cached_binance_option_chain_payload, get_cached_option_chain_payload,
    refresh_market_depth_cache, _MD_PUSHER_DEBUG,
)

# ─── ZIP export ───────────────────────────────────────────────────────────────
def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ("dashboard.py", "chart.html"):
            if os.path.exists(fname):
                zf.write(fname)
    return buf.getvalue()

# ─── Auto-startup: HF Secret "FYERS_LOGIN_URL" se login (Method B ka secret-
# based version — roz subah Google URL ko HF Space Settings → Variables and
# secrets mein paste karo, app boot hote hi khud auth_code exchange kar
# lega). Same auth_code dobara-dobara exchange na ho isliye load_creds()
# mein last-processed code yaad rakha jaata hai. ─────────────────────────────
if not st.session_state.get("_url_secret_login_done"):
    st.session_state["_url_secret_login_done"] = True
    _url_secret = _get_secret("FYERS_LOGIN_URL")
    if _url_secret and not is_session_active():
        _boot_creds2 = load_creds()
        _code2 = _extract_auth_code(_url_secret)
        if _code2 and _code2 != _boot_creds2.get("_last_url_secret_code", ""):
            _app_id2 = _boot_creds2.get("app_id", DEFAULT_APP_ID)
            _secret2 = _boot_creds2.get("secret_key", DEFAULT_SECRET)
            try:
                _ok3, _tok3, _resp3 = fyers_get_access_token(_app_id2, _secret2, _code2)
            except Exception as _e3:
                _slog_exception("LOGIN_URL_SECRET fyers_get_access_token()", _e3)
                _ok3, _tok3, _resp3 = False, str(_e3), {}
            if _ok3:
                save_creds({
                    **_boot_creds2,
                    "app_id": _app_id2, "secret_key": _secret2,
                    "client_id": DEFAULT_CLIENT_ID, "password": DEFAULT_PASSWORD,
                    "access_token": _tok3,
                    "_last_url_secret_code": _code2,   # dobara exchange skip karne ke liye
                })
                _sess_cache.update({"active": False, "ts": time.time()}); st.session_state["_force_active"] = False
                st.session_state["_login_success_msg"] = "🎉 Login successful (FYERS_LOGIN_URL secret se)!"
                _slog("🔐 LOGIN_URL_SECRET: access_token mil gaya, creds save ho gaye — SUCCESS.", level="ok")
            else:
                # Dobara-dobara retry na ho isliye failed code ko bhi "seen" mark kar dete hain
                save_creds({**_boot_creds2, "_last_url_secret_code": _code2})
                _slog(f"🔐 LOGIN_URL_SECRET: access_token exchange FAILED — {_tok3} | raw_response={_resp3}", level="err")
        st.rerun()

# ─── In-chart broker panel: handle query params from iframe form submits ───────
_qp = st.query_params

# Handler 1: Manual Google URL auth_code
if "fyers_code" in _qp:
    _code   = _qp.get("fyers_code",   "").strip()
    _app_id = _qp.get("fyers_app_id", DEFAULT_APP_ID).strip()
    _secret = _qp.get("fyers_secret", DEFAULT_SECRET).strip()
    st.query_params.clear()
    _slog(
        f"🔐 LOGIN_MANUAL: Google-redirect URL se query param mila. "
        f"code_present={'yes' if _code else 'NO'} (len={len(_code)}) "
        f"app_id={'yes' if _app_id else 'NO'} secret={'yes' if _secret else 'NO'}",
        level="info",
    )
    if _code:
        try:
            _ok, _tok, _resp = fyers_get_access_token(_app_id, _secret, _code)
        except Exception as _e_login_manual:
            _slog_exception("LOGIN_MANUAL fyers_get_access_token()", _e_login_manual)
            _ok, _tok, _resp = False, str(_e_login_manual), {}
        if _ok:
            save_creds({
                **load_creds(),
                "app_id": _app_id, "secret_key": _secret,
                "client_id": DEFAULT_CLIENT_ID, "password": DEFAULT_PASSWORD,
                "access_token": _tok,
            })
            _sess_cache.update({"active": False, "ts": time.time()}); st.session_state["_force_active"] = False
            st.session_state["_login_success_msg"] = "🎉 Login successful!"
            _slog("🔐 LOGIN_MANUAL: access_token mil gaya, creds save ho gaye — SUCCESS.", level="ok")
        else:
            _slog(f"🔐 LOGIN_MANUAL: access_token exchange FAILED — {_tok} | raw_response={_resp}", level="err")
    else:
        _slog("🔐 LOGIN_MANUAL: URL me auth_code hi nahi mila — token exchange attempt hi nahi hua.", level="warn")
    st.rerun()

# Handler 3: SV2 chunk / candle-count settings — Apply (bottom-bar 📦 icon)
if _qp.get("sv2_chunk_trigger") == "1":
    _new_bn, _new_btc = {}, {}
    for _k in _SV2_MAX_BN_DEFAULT.keys():
        _pk = f"sv2bn_{_k}"
        if _pk in _qp:
            try:
                _new_bn[_k] = int(_qp.get(_pk))
            except Exception:
                pass
    for _k in _SV2_MAX_BTC_DEFAULT.keys():
        _pk = f"sv2btc_{_k}"
        if _pk in _qp:
            try:
                _new_btc[_k] = int(_qp.get(_pk))
            except Exception:
                pass
    st.query_params.clear()
    if _new_bn or _new_btc:
        _cs_settings = load_sv2_chunk_settings()
        _cs_settings.setdefault("bn",  {}).update(_new_bn)
        _cs_settings.setdefault("btc", {}).update(_new_btc)
        save_sv2_chunk_settings(_cs_settings)
        # session cache invalidate karo taaki naya value turant lagu ho
        st.session_state.pop("_sv2_max_eff_bn",  None)
        st.session_state.pop("_sv2_max_eff_btc", None)
    st.rerun()

# Handler 3b: REMOVED (user ka explicit ask — no fallback). SV2 (BankNifty/
# BTC replay) pehle ?sv2_load=1 ke zariye poora page-reload + Python-side
# fetch+resample karta tha. Ab SV2 bhi SV3 jaisa hi hai: chart.html seedha
# browser se Supabase Storage ko fetch() karta hai aur resample bhi JS mein
# hi karta hai (dekho chart.html: _sv2EnsureAssetLoaded/_sv2BuildAssetFull),
# koi Streamlit round-trip ya reload nahi — isliye ye query-param handler
# aur uska poora reload-mechanism yahan se hata diya gaya hai.

# Handler 3c: SV3 (Stack View 3 — stock replay) — top-left symbol-picker se
# naya symbol chuna gaya. SV2 jaisa hi lazy-load: jab tak koi symbol pick
# nahi hota, nifty500 .gz kabhi read hi nahi hota — sirf empty placeholders
# inject honge (neeche _build_chart_html mein). Symbol pick hote hi ye
# handler chalta hai: session_state mein save, disk par bhi remember
# (agli baar app khulne par wahi symbol default), aur rerun taaki naya
# resampled data turant inject ho jaaye.
if "sv3_symbol" in _qp:
    _sv3_sym = _qp.get("sv3_symbol", "").strip()
    st.query_params.clear()
    if _sv3_sym:
        st.session_state["_sv3_symbol"]        = _sv3_sym
        st.session_state["_sv3_data_requested"] = True
        save_sv3_last_symbol(_sv3_sym)
    st.rerun()

# Handler 3d: SV3 lazy-load fallback — jab Stack View 3 pehli baar ON
# toggle ho (grid-picker se) aur abhi tak koi symbol select nahi hua ho, JS
# in-chart se ?sv3_load=1 bhejta hai (bilkul sv2_load jaisa) taaki ek default
# symbol (pichli baar wala, ya list ka pehla) load ho jaaye.
if _qp.get("sv3_load") == "1":
    st.query_params.clear()
    if not st.session_state.get("_sv3_symbol"):
        _sv3_default_sym = load_sv3_last_symbol()
        if not _sv3_default_sym:
            _sv3_avail = _sv3_symbol_list()
            _sv3_default_sym = _sv3_avail[0] if _sv3_avail else ""
        st.session_state["_sv3_symbol"] = _sv3_default_sym
    st.session_state["_sv3_data_requested"] = True
    st.rerun()

# Handler 4: SV2 chunk / candle-count settings — Reset to default
if _qp.get("sv2_chunk_reset") == "1":
    st.query_params.clear()
    if os.path.exists(SV2_CHUNK_SETTINGS_FILE):
        try:
            os.remove(SV2_CHUNK_SETTINGS_FILE)
        except Exception:
            pass
    st.session_state.pop("_sv2_max_eff_bn",  None)
    st.session_state.pop("_sv2_max_eff_btc", None)
    st.rerun()

# Handler 5: Market Depth (BTC/NSE) — kaunsa symbol ka depth-sheet khula
# hai, JS side se `depth_symbol` query-param ke zariye batata hai. PEHLE ye
# `form.submit()` se (real browser navigation) trigger hota tha, isliye
# har depth-open/close par poora page reload hota tha (chart iframe bhi
# reload ho jaata). AB chart.html JS side se `history.pushState()` +
# manual `popstate` dispatch use karta hai (dekho _ocPushDepthSymbolToParent
# chart.html mein) — koi real navigation nahi, sirf Streamlit ka apna
# internal query-param→websocket rerun sync trigger hota hai. Isliye
# st.query_params yahan change dikhega, lekin chart iframe reload NAHI
# hoga (koi page-navigation hui hi nahi).
if "depth_symbol" in _qp:
    _dsym = _qp.get("depth_symbol", "").strip()
    st.query_params.clear()
    _dsym_old = st.session_state.get("_active_depth_symbol")
    st.session_state["_active_depth_symbol"] = _dsym if _dsym else None
    _slog(f"[Depth] Handler5 FIRED — depth_symbol query-param = '{_dsym}' | "
          f"session_state: {_dsym_old!r} -> {st.session_state['_active_depth_symbol']!r}")
    st.rerun()

# Handler 6: Local persistent-storage SAVE (state/settings/layout) — ab
# side-port /api/local_state_save POST HF Spaces par unreachable hai (single
# external port), isliye bilkul depth_symbol/sv3_symbol jaisa hi bridge use
# karte hain: chart.html JS side se apne parent (Streamlit) window par
# history.pushState() + manual popstate dispatch karta hai (dekho
# chart.html: _localStatePushSaveToParent) — real navigation nahi, sirf
# Streamlit ka internal query-param→websocket rerun sync trigger hota hai.
# Data JSON-stringified query-param mein jaata hai (URL length ka koi real
# limit yahan nahi lagta kyunki ye asal HTTP request nahi hai — Streamlit
# frontend already-open websocket par bhejta hai, koi proxy/server URL-size
# cap beech mein nahi aata).
if "local_save_kind" in _qp:
    _ls_kind = _qp.get("local_save_kind", "").strip()
    _ls_raw  = _qp.get("local_save_data", "")
    st.query_params.clear()
    _ls_ok, _ls_msg = False, "no data received"
    if _ls_kind:
        try:
            _ls_data = json.loads(_ls_raw) if _ls_raw else None
        except Exception as _e_ls:
            _ls_data = None
            _ls_msg = f"bad JSON in local_save_data: {_e_ls}"
        if _ls_data is not None:
            try:
                _ls_ok, _ls_msg = _local_state_save(_ls_kind, _ls_data)
            except Exception as _e_ls2:
                _slog_exception("Handler6 _local_state_save()", _e_ls2)
                _ls_ok, _ls_msg = False, str(_e_ls2)
    st.session_state["_local_save_last"] = {
        "kind": _ls_kind, "ok": _ls_ok, "msg": _ls_msg, "ts": time.time(),
    }
    if not _ls_ok:
        _slog(f"[LocalSave] Handler6 FAILED — kind={_ls_kind!r} msg={_ls_msg}", level="err")
    st.rerun()

# ── Startup debug: is script-run se pehle process kitni purani hai — agar
# _STARTUP_LOG khaali hai to matlab ye is container/process ka BILKUL PEHLA
# run hai (fresh boot / cold start / restart). Agar pehle se lines hain to
# same process continue ho raha hai (sirf Streamlit rerun hua hai).
# NOTE: _is_fresh_boot yahan dobara compute NAHI karna — file ke bilkul
# shuru mein (_slog infra ke turant baad, kisi bhi _slog() call se pehle)
# ek hi baar sahi tarah capture ho chuka hai. Yahan dobara karne se hamesha
# False aata (kyunki beech mein SV2 diagnostic jaisi cheezein already
# _STARTUP_LOG mein likh chuki hoti hain isi run ke andar). ──────────────
_slog(f"▶ Script run start" + (" — FRESH PROCESS BOOT (cold start/restart)" if _is_fresh_boot else " (rerun, same process)"))

# ── Fresh boot par proxy HF Space secrets (env vars PROXY_HOST/PORT/USER/
# PASS/ON) se RAM mein load karo — koi disk file involved nahi. ────────────
if _is_fresh_boot:
    _load_proxy_from_env()
    with _PROXY_LOCK:
        _px_loaded_host = _PROXY_CACHE["host"]
        _px_loaded_on   = _PROXY_CACHE["enabled"]
    if _px_loaded_host:
        _slog(f"Proxy env-secrets se load hui: {_px_loaded_host}:{_PROXY_CACHE['port']} (enabled={_px_loaded_on})", level="ok")
    else:
        _slog("Proxy env-secrets set nahi hain (PROXY_HOST/PROXY_PORT) — RAM khaali hai, UI se manually Apply karo")

creds      = load_creds()
_bn_api_key_env, _bn_secret_key_env = _get_binance_creds()
_slog(
    "Creds loaded — fyers: app_id=%s secret=%s access_token=%s | binance(env secrets): key=%s secret=%s | proxy: %s" % (
        "yes" if creds.get("app_id") else "NO",
        "yes" if creds.get("secret_key") else "NO",
        "yes" if creds.get("access_token") else "NO",
        "yes" if _bn_api_key_env else "NO",
        "yes" if _bn_secret_key_env else "NO",
        f"{_PROXY_CACHE['host']}:{_PROXY_CACHE['port']}" if _PROXY_CACHE["host"] else "khaali",
    )
)

try:
    sess_active = is_session_active()
except Exception as _e_sess:
    _slog_exception("is_session_active()", _e_sess)
    sess_active = False
_slog(f"sess_active (Fyers session valid) = {sess_active}")

# ── AUTO-ENTRY: agar Fyers session pehle se valid hai (token disk pe
# persist hota hai, kabhi manual logout nahi hota — HF Space secrets se
# auto-login) to login page bilkul skip karke seedha Fyers chart mode
# (_fyers_entry_mode) mein le jaate hain. Agar token invalid/expired hai
# (sess_active False) to kuch nahi karte — normal login page hi dikhega.
# Sirf ek baar per browser-session/tab check hota hai (flag guard) taaki
# baad mein user khud Binance Entry / Replay Mode pe manually switch kare
# to wo overwrite na ho is auto-entry se.
if not st.session_state.get("_auto_fyers_entry_checked"):
    st.session_state["_auto_fyers_entry_checked"] = True
    if (
        sess_active
        and not st.session_state.get("_fyers_entry_mode")
        and not st.session_state.get("_binance_entry_mode")
        and not st.session_state.get("_replay_mode")
    ):
        st.session_state["_fyers_entry_mode"] = True
        _slog(
            "AUTO-ENTRY: Fyers session already valid (auto-login) → "
            "login page skip karke seedha chart mode mein bhej rahe hain.",
            level="ok",
        )
        st.rerun()
    else:
        _slog(
            f"AUTO-ENTRY skip kiya — sess_active={sess_active} "
            "(agar False hai to login page par hi rahega, jab tak valid Fyers login na ho).",
        )

# ── Teen entry-point flags (mutually exclusive) ─────────────────────────────
# Login page par ab 3 alag buttons hain: Fyers login ke niche "Fyers Entry",
# Binance login ke niche "Binance Entry", aur standalone "Replay Mode". Jo
# bhi ek dabaya jaata hai wahi True set hota hai, baaki do False kar diye
# jaate hain (dekho neeche button-handlers) — isliye ek samay me sirf ek hi
# entry mode active rehta hai, aur sirf usi se related background threads
# start hote hain (chart ka load kam rehta hai).
_fyers_entry_mode   = st.session_state.get("_fyers_entry_mode", False)
_binance_entry_mode = st.session_state.get("_binance_entry_mode", False)
_replay_mode        = st.session_state.get("_replay_mode", False)
# Chart tabhi render hota hai jab teeno mein se koi ek active ho.
_chart_active = _fyers_entry_mode or _binance_entry_mode or _replay_mode
_slog(
    f"_fyers_entry_mode={_fyers_entry_mode}  _binance_entry_mode={_binance_entry_mode}  "
    f"_replay_mode={_replay_mode}  _chart_active={_chart_active}"
)

# ── AUTO-UPDATE triggers — background, silent, once-per-day (Supabase status
# se track hota hai). Fyers session valid ho to BankNifty + Nifty500; chart
# mode active ho (chahe Fyers ho ya Binance ho ya Replay) to BTC. Har ek
# process-lifetime mein sirf ek baar launch-attempt hota hai (guard andar
# _maybe_launch_background_update mein hai) — Streamlit ke baar-baar rerun
# hone se dobara-dobara thread nahi bante. ────────────────────────────────
if sess_active:
    _maybe_launch_background_update("bn", _append_new_bn_candles)
    _maybe_launch_background_update("nifty500", _nifty500_incremental_update)
if _chart_active:
    _maybe_launch_background_update("btc", _append_new_btc_candles)
    # SV3 BTC 1D (long-term replay ke 3M/9M/... ke liye) — "btc" source-key
    # se ALAG rakha hai jaanbujh kar (wo SV2 ka 5m .gz update hai). Fyers
    # login se independent hai (Binance public klines), isliye 'sess_active'
    # ki jagah 'chart_active' par hi chalta hai — Binance Entry mode mein
    # bhi (jahan Fyers login hota hi nahi) BTC 1D fresh rehna chahiye.
    _maybe_launch_background_update("btc_sv3_1d", _btc_sv3_incremental_update)
    # SV1 replay-symbols (btcusdt2-28) ka shared master file — bilkul SV2
    # BTC jaisa hi automatic-daily pattern (proxy se, background thread,
    # roz-ek-baar). Proxy OFF ho (jaisa user kabhi-kabhi 10 din tak
    # rakhta hai) to _replay_master_incremental_update() andar hi
    # gracefully fail ho jayega (Binance call proxy ke bina blocked hai)
    # — is case mein _mark_updated_today() call hi NAHI hota (dekho
    # _maybe_launch_background_update: sirf ok=True par mark hota hai),
    # isliye agla din/rerun phir se try karega, jab tak proxy wapas ON
    # na ho aur safal na ho jaaye. Koi crash/loop-risk nahi, purana
    # reveal-cron isse bilkul unaffected rehta hai (dekho SUPABASE_SETUP.md).
    _maybe_launch_background_update("sv1_master", _replay_master_incremental_update)
    _maybe_launch_background_update(
        "td_symbols",
        lambda: _td.td_update_all(TWELVEDATA_API_KEY, _supabase_upload, log_fn=_slog),
    )
    # Finnhub LIVE WebSocket — Twelve Data symbols (Dow/GBP-USD/Apple/Amazon)
    # ka live price update ab yahan se aata hai, na ki Twelve Data ke slow
    # REST poll se. Chart active ho (Fyers/Binance/Replay, koi bhi) to yeh
    # chalta hai, kyunki TD symbols SV1 mein hamesha dikhte hain, entry-mode
    # se independent.
    try:
        _ensure_finnhub_ws_thread()
    except Exception as _e_fh:
        _slog_exception("_ensure_finnhub_ws_thread()", _e_fh)

# Sirf jo entry-mode active hai usi ke threads start honge — Replay Mode
# mein koi bhi live thread (Fyers ya Binance) start nahi hota, kyunki wo
# purana .gz data se chalta hai, dono APIs se independent.
if _fyers_entry_mode and sess_active:
    _slog(f"Fyers Entry active (sess_active={sess_active}) → calling _ensure_fyers_threads()")
    try:
        _ensure_fyers_threads()
        _running = sorted(t.name for t in threading.enumerate())
        _slog(f"_ensure_fyers_threads() done. Threads alive now: {_running}", level="ok")
    except Exception as _e_threads:
        _slog_exception("_ensure_fyers_threads()", _e_threads)

if _binance_entry_mode:
    _slog("Binance Entry active → calling _ensure_binance_threads()")
    try:
        _ensure_binance_threads()
        _running = sorted(t.name for t in threading.enumerate())
        _slog(f"_ensure_binance_threads() done. Threads alive now: {_running}", level="ok")
    except Exception as _e_threads:
        _slog_exception("_ensure_binance_threads()", _e_threads)

if _replay_mode:
    _slog("Replay Mode active → koi live thread (Fyers/Binance) start NAHI kiya gaya (jaan-boojh kar).")
    # Fyers/Binance data-threads jaan-boojh kar OFF hain, lekin SV3 "Long Term
    # Replay" ke bulk-preload endpoint (/api/sv3_all_stocks) ke liye side HTTP
    # server chalna hi zaroori hai — ye server koi Fyers/Binance live-data
    # thread nahi hai, sirf already-saved .gz file se JSON serve karta hai.
    try:
        _register_api_route()
    except Exception as _e_api_replay:
        _slog_exception("_register_api_route() (replay mode)", _e_api_replay)

if not (_fyers_entry_mode or _binance_entry_mode or _replay_mode):
    _slog(
        "Koi entry mode active nahi (abhi login page par hai) → "
        "koi bhi background thread is run mein start nahi hua.",
        level="warn",
    )

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

        # Unique nonce per page-load → forces Fyers to generate a FRESH auth_code each time
        import random
        _nonce = str(int(time.time())) + str(random.randint(1000, 9999))
        auth_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={app_id}"
            f"&redirect_uri=https%3A%2F%2Fwww.google.com"
            f"&response_type=code"
            f"&state={_nonce}"
            f"&nonce={_nonce}"
        )

        if has_old_creds:
            st.error("🔴 Token expire ho gaya! Re-login karo")
        else:
            st.warning("⚠️ Login karo")

        # ── Method B: Manual Google URL ─────────────────────────────────────────
        with st.expander("🔗 Google URL Login", expanded=True):
            st.markdown(f"**Step 1 →** [👉 Fyers Fresh Login Link]({auth_url})")
            st.warning("⚠️ Upar wala FRESH link click karo — purana cached URL mat use karo!")
            st.caption("Link click → Google page aayega → us page ka poora URL copy karo")

            url_input = st.text_input(
                "**Step 2 →** Poora URL ya auth_code paste karo",
                placeholder="https://www.google.com/?s=ok&auth_code=eyJ...",
            )

            if st.button("⚡ Connect", use_container_width=True, type="primary"):
                raw = url_input.strip()
                if raw:
                    code = _extract_auth_code(raw)
                    st.caption(f"🔍 Extracted code: `{code[:20]}...`")
                    ok, access_token, full_resp = fyers_get_access_token(app_id, secret, code)
                    if ok:
                        save_creds({
                            **creds,
                            "app_id":       app_id,
                            "secret_key":   secret,
                            "client_id":    DEFAULT_CLIENT_ID,
                            "password":     DEFAULT_PASSWORD,
                            "access_token": access_token,
                        })
                        _sess_cache.update({"active": False, "ts": time.time()}); st.session_state["_force_active"] = False
                        st.success("🎉 Connected!")
                    else:
                        st.error(f"❌ Login Failed: {access_token}")
                        st.markdown("**Full Fyers Response:**")
                        st.code(json.dumps(full_resp, indent=2), language="json")
                else:
                    st.error("URL ya code paste karo pehle")

    st.markdown("---")

    # ── SMS Alert Setup ─────────────────────────────────────────────────────
    with st.expander("📱 SMS Alert Setup", expanded=False):
        st.caption("Token expire hone par SMS aayega 7018093451 par")
        st.success("✅ Fast2SMS connected")

        # Allow changing phone number
        creds_now = load_creds()
        phone_val = creds_now.get("alert_phone", "7018093451")
        new_phone = st.text_input("Alert Phone", value=phone_val, max_chars=12)
        if st.button("💾 Save Phone", use_container_width=True):
            save_creds({**creds_now, "alert_phone": new_phone.strip()})
            st.success(f"Saved: {new_phone}")

    st.markdown("---")
    st.download_button(
        "⬇️ Download Project ZIP",
        data=_make_zip(),
        file_name="banknifty_chart.zip",
        mime="application/zip",
        use_container_width=True,
    )

# ─── Fetch all chart data ─────────────────────────────────────────────────────
# Cache key includes first 8 chars of token so new token → fresh fetch
@st.cache_data(ttl=HIST_CACHE_TTL, show_spinner=False)
def _get_chart_data(sess: bool, _tok: str = ""):
    _tasks = {
        # BTC: 1H is the smallest TF actually used (min TF = 8H, which is an
        # exact multiple of 60min) — so fetch 1H directly instead of 15m/1m
        # and let the client resample 1H → 8H/1D/3D/9D/27D. Fewer candles
        # over the wire, same result, since 15m/1m were never used anyway.
        "btc_1h":  lambda: fetch_btc("1h", 1000),
        "btc_day": load_btc_daily,
        # BankNifty: 45m is the smallest TF actually used (min TF = 125m,
        # a multiple of 45m) — 1m/5m/15m fetches were dead weight, never
        # read by the chart, so they're removed.
        "bn_45m":  (lambda: fetch_bn_intraday(45)) if sess else (lambda: []),
        "bn_day":  load_bn_daily if sess else (lambda: []),
    }
    with ThreadPoolExecutor(max_workers=8) as _ex:
        _futures = {k: _ex.submit(fn) for k, fn in _tasks.items()}
        _out = {k: f.result() for k, f in _futures.items()}
    return (_out["btc_1h"], _out["btc_day"], _out["bn_45m"], _out["bn_day"])

_tok_hint = creds.get("access_token", "")[:8] if sess_active else ""
btc_1h, btc_day, bn_45m, bn_day = _get_chart_data(sess_active, _tok_hint)

# ─── Chart HTML builder — injects live data directly into chart.html ──────────
def _build_chart_html(
    btc_1h, btc_day,
    bn_45m, bn_day,
    sess_active: bool
) -> str:
    """Read chart.html and replace all __PLACEHOLDERS__ with real data."""
    import os, json as _json

    # Load chart.html from same directory as app.py
    _html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.html")
    if not os.path.exists(_html_path):
        return "<p style='color:red'>chart.html not found</p>"

    with open(_html_path, "r", encoding="utf-8") as _f:
        html = _f.read()

    def _to_lwc(candles: list) -> str:
        """Convert [[epoch_ms, o, h, l, c, v], ...] or [{time,open,...}] to LWC format."""
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
        # Deduplicate by time, keep last
        seen = {}
        for b in out:
            seen[b["time"]] = b
        return _json.dumps(sorted(seen.values(), key=lambda x: x["time"]))

    # ALLDATA[asset] fallback (__BTC_CANDLES__/__BN_CANDLES__) is only ever
    # touched if tf falls below the base TF, which never happens for either
    # asset in real usage — seed it with the base array itself (no extra
    # fetch) instead of leaving it empty, so the fallback stays harmless.
    html = html.replace("__BTC_CANDLES__", _to_lwc(btc_1h))
    html = html.replace("__BTC_1H__",      _to_lwc(btc_1h))
    html = html.replace("__BTC_DAILY__",   _to_lwc(btc_day))
    html = html.replace("__BN_CANDLES__",  _to_lwc(bn_45m))
    html = html.replace("__BN_45M__",      _to_lwc(bn_45m))
    html = html.replace("__BN_DAILY__",    _to_lwc(bn_day))

    # ── Stack View 2: ab SV3 jaisa hi — Python kabhi bhi gz fetch/resample
    # nahi karta. Placeholders hamesha empty inject hote hain; asli data
    # seedha browser se Supabase se fetch() hota hai aur resample bhi JS
    # mein hi hota hai jab user in-chart calendar se date select/resume
    # kare (dekho chart.html: _sv2EnsureAssetLoaded/_sv2BuildAssetFull —
    # koi Streamlit round-trip/reload, koi fallback nahi).
    _sv2_all_placeholders = [
        "__SV2_BN_5M_RAW__","__SV2_BN_125M__",
        "__SV2_BN_1D__","__SV2_BN_3D__","__SV2_BN_9D__","__SV2_BN_27D__",
        "__SV2_BTC_5M_RAW__","__SV2_BTC_8H__","__SV2_BTC_1D__",
        "__SV2_BTC_3D__","__SV2_BTC_9D__","__SV2_BTC_27D__",
    ]
    for _ph in _sv2_all_placeholders:
        html = html.replace(_ph, "[]")
    _sv2_data_loaded_ok = False
    _sv2_err_msg = "NOT_USED_ANYMORE_CLIENT_SIDE_SUPABASE_LOAD"
    # Inject debug info + loaded-flag as JS variables (legacy flag — chart.html
    # ab primarily window.__SV2_LOADED{bn,btc} per-asset object use karta hai)
    _sv2_safe = _sv2_err_msg.replace("</", "<\\/")
    # Bottom-bar "📦 Chunk" icon panel ke liye: current effective candle-count
    # limits (BN + BTC), unki safe bounds, aur current chunk dates.
    _sv2_chunk_ui_info = {
        "bn":            _sv2_get_max("bn"),
        "btc":           _sv2_get_max("btc"),
        "bounds":        _SV2_MAX_BOUNDS,
        "bn_chunk_date":  str(st.session_state.get("_sv2_anchor_date_bn")  or ""),
        "btc_chunk_date": str(st.session_state.get("_sv2_anchor_date_btc") or ""),
    }

    # ── Stack View 3: Nifty500 stock — long-term monthly replay data ────────
    # LAZY LOAD, SV2 jaisa hi: jab tak koi symbol select nahi hota (top-left
    # picker se, ya pehli baar Stack View 3 ON toggle na ho), .gz kabhi
    # padha hi nahi jaata — sirf empty placeholders inject honge. 1D + saari
    # TFs FULL HISTORY jaati hain (ek stock ka 1D max ~30 saal ≈ 7500 rows —
    # BN ke 1D jaisa hi chhota, isliye SV2 ke 5m_raw jaisi anchor-date
    # trimming yahan zaroorat nahi — replay "start point" purely client-side,
    # already-loaded 1D array ke andar ek index hai.)
    _sv3_data_requested = bool(st.session_state.get("_sv3_data_requested"))
    _sv3_symbol_cur = st.session_state.get("_sv3_symbol", "") or ""
    _sv3_all_placeholders = [
        "__SV3_1D__", "__SV3_1M__", "__SV3_3M__", "__SV3_9M__",
        "__SV3_27M__", "__SV3_81M__", "__SV3_243M__",
    ]
    _sv3_err_msg = ""
    _sv3_data_loaded_ok = False
    if not _sv3_data_requested or not _sv3_symbol_cur:
        for _ph in _sv3_all_placeholders:
            html = html.replace(_ph, "[]")
        _sv3_err_msg = "NOT_REQUESTED_YET"
    else:
        try:
            _sv3d = _build_sv3_data(_sv3_symbol_cur)
            html = html.replace("__SV3_1D__",   _sv3_to_js(_sv3d["1D"]))
            html = html.replace("__SV3_1M__",   _sv3_to_js(_sv3d["1M"]))
            html = html.replace("__SV3_3M__",   _sv3_to_js(_sv3d["3M"]))
            html = html.replace("__SV3_9M__",   _sv3_to_js(_sv3d["9M"]))
            html = html.replace("__SV3_27M__",  _sv3_to_js(_sv3d["27M"]))
            html = html.replace("__SV3_81M__",  _sv3_to_js(_sv3d["81M"]))
            html = html.replace("__SV3_243M__", _sv3_to_js(_sv3d["243M"]))
            _sv3_err_msg = json.dumps({
                "symbol": _sv3_symbol_cur,
                "counts": {k: len(v) for k, v in _sv3d.items()},
            })
            _sv3_data_loaded_ok = True
        except Exception as _sv3_ex:
            _sv3_err_msg = f"EXCEPTION: {_sv3_ex} | cache={_SV3_CACHE.get('symbol')}"
            for _ph in _sv3_all_placeholders:
                html = html.replace(_ph, "[]")
    _sv3_safe = _sv3_err_msg.replace("</", "<\\/")
    # CHANGED (user ka explicit ask): pehle yahan _sv3_symbol_list() eagerly
    # call hota tha — matlab chart-page render hote hi Nifty500 naamon ki
    # list fetch ho jaati thi, chahe user symbol-search kholta ya nahi. Ab
    # ye khaali array hi bhejte hain — asli list tabhi fetch hogi jab user
    # khud SV3 symbol-search picker kholega (chart.html: _openSv3SymbolPicker
    # → /api/sv3_symbol_list, dekho neeche local API server mein).
    _sv3_symbol_list_json = "[]"

    # ── App-startup / login debug log — snapshot le lo taaki chart render
    # hone se pehle jitne bhi steps (creds, session check, thread launch,
    # koi exception) hue hain, header ke chhote debug icon se copy kiye
    # ja saken. Render ke turant baad ka bhi ek final marker line daal
    # rahe hain taaki pata chale ye poora startup trace hai.
    try:
        _slog(f"Chart HTML render ho raha hai — sess_active={sess_active} chart_active={_chart_active}", level="ok")
    except Exception:
        pass
    _startup_log_safe = json.dumps(_startup_log_snapshot()).replace("</", "<\\/")
    html = html.replace("</body>",
        f"<script>window.__STARTUP_LOG__={_startup_log_safe};"
        "try{ if (typeof _bootDebugRenderLog === 'function') _bootDebugRenderLog(); }catch(_){}"
        "</script>\n</body>", 1)

    html = html.replace("</body>",
        f"<script>window.__SV2_DEBUG={json.dumps(_sv2_safe)};"
        f"window.__SV2_DATA_LOADED={json.dumps(_sv2_data_loaded_ok)};"
        f"window.__SV2_CHUNK_SETTINGS={json.dumps(_sv2_chunk_ui_info)};</script>\n</body>", 1)

    html = html.replace("</body>",
        f"<script>window.__SV3_DEBUG={json.dumps(_sv3_safe)};"
        f"window.__SV3_DATA_LOADED={json.dumps(_sv3_data_loaded_ok)};"
        f"window.__SV3_SYMBOL={json.dumps(_sv3_symbol_cur)};"
        f"window.__SV3_SYMBOL_LIST={_sv3_symbol_list_json};</script>\n</body>", 1)

    # ── Auto-update status (BankNifty / BTC / Nifty500) — ☁ Supabase debug
    # panel mein dikhane ke liye. Fail-safe: status load hi na ho paaye to
    # bhi khaali dict bhej dete hain, panel "no data" dikha dega.
    try:
        _auto_update_status = _load_update_status()
    except Exception:
        _auto_update_status = {}
    _auto_update_today = _ist_now().strftime("%Y-%m-%d")
    html = html.replace("</body>",
        f"<script>window.__AUTO_UPDATE_STATUS={json.dumps(_auto_update_status)};"
        f"window.__AUTO_UPDATE_TODAY={json.dumps(_auto_update_today)};</script>\n</body>", 1)

    # ── Twelve Data external symbols (Gold/Dow Jones/...) — SV1 ke top-left
    # symbol switcher ke liye generic data. Registry (td_symbols.py) mein
    # jitne bhi symbols hon, ye poore inject ho jaate hain — chart.html
    # generically loop karke unhe ASSET_NAMES mein add karta hai, kahin
    # per-symbol hardcoded code nahi hai. Fail-safe: kuch bhi fail ho to
    # khaali dict — matlab koi external symbol nahi dikhega, BN/BTC untouched.
    try:
        _td_symbols_payload = _td.td_all_bucketed()
    except Exception as _e_td_inject:
        _slog_exception("td_all_bucketed (chart html inject)", _e_td_inject)
        _td_symbols_payload = {}
    # ── Replay symbols (btcusdt2/3/4/5) — 2nd Supabase project se raw+reveal
    # data laata hai, TD symbols jaisa hi shape ({label,bucket_min,market,
    # candles}) deta hai, isliye seedha TD wale dict mein MERGE kar dete
    # hain — chart.html ka existing generic ext_<key> pipeline (ASSET_NAMES,
    # STACK config, symbol-switcher, 1D/3D/9D/27D resample) inhe automatically
    # handle kar lega, koi chart.html change nahi chahiye. Fail-safe: kuch
    # bhi fail ho to kuch add nahi hoga, TD symbols/BTC/BN untouched rahenge.
    # v3 — ON-DEMAND: page-load par ab candles NAHI bhejte (pehle charo
    # symbols ka poora data yahin ban ke chala jaata tha — ~67MB tak ka
    # payload ek symbol ke liye ho sakta tha, aur 1-saal pre-history cap
    # ki wajah se bade-timeframe anchoring bhi mismatch karti thi symbol-
    # se-symbol). Ab sirf halka metadata ({label,bucket_min,market},
    # candles: []) jaata hai — sirf itna kaafi hai symbol-switcher mein
    # naam dikhane ke liye. Jab user in 4 mein se koi symbol select karega,
    # chart.html naya /api/replay_symbol side-API route call karke us
    # symbol ka poora (anchor-consistent) data fresh fetch karega.
    try:
        _replay_payload = _replay.get_replay_symbols_metadata()
        _td_symbols_payload.update(_replay_payload)
    except Exception as _e_replay_inject:
        _slog_exception("replay_symbols metadata inject", _e_replay_inject)
    # NOTE: this must run BEFORE chart.html's main inline <script> (which builds
    # ASSET_NAMES from window.__TD_SYMBOLS_DATA__ at parse time) — so injected
    # right after <head>, not at </body>. Injecting at </body> made the data
    # arrive too late: ASSET_NAMES had already been built with an empty
    # fallback, so Gold/Dow never showed up in SV1's top-left symbol switcher,
    # only BankNifty/BTC.
    html = html.replace("<head>",
        f"<head>\n<script>window.__TD_SYMBOLS_DATA__={json.dumps(_td_symbols_payload)};</script>", 1)

    # ── Local persistent-storage LOAD — ab side-port /api/local_state_load
    # XHR (jo HF Spaces par single-external-port hosting ki wajah se browser
    # se kabhi reachable nahi hota — dekho debug panel "Failed to load"
    # errors) ki jagah, bundle seedha build-time par yahan inject karte hain
    # — bilkul __TD_SYMBOLS_DATA__ jaisa hi pattern. Isse koi extra network
    # round-trip hi nahi lagti (chart.html khud hi is HTML ke saath aa jaata
    # hai), aur HF/local dono jagah equally reliably kaam karta hai. Same
    # bridge-family jo bn_live tick ke liye already use ho rahi hai
    # (Python → JS postMessage push), bas is case mein "push" hamesha
    # page-load ke waqt hi ek baar chahiye hoti hai, isliye alag se
    # fragment/postMessage schedule karne ki zaroorat nahi — seedha
    # inline inject sabse simple aur fastest hai.
    try:
        _local_bundle = _local_state_load_all()
    except Exception as _e_local_bundle:
        _slog_exception("_local_state_load_all() inject", _e_local_bundle)
        _local_bundle = {}
    html = html.replace("<head>",
        f"<head>\n<script>window.__LOCAL_STATE_BUNDLE__={json.dumps(_local_bundle)};</script>", 1)

    # ── Supabase sync (saved layouts / settings / app state) ────────────────
    # URL is not sensitive, but pulling both from Streamlit Cloud secrets
    # (Settings → Secrets) keeps things in one place. Publishable key is
    # safe for the browser (RLS + anonymous auth restrict access per device).
    _sb_url = _get_secret("SUPABASE_URL")
    _sb_key = _get_secret("SUPABASE_ANON_KEY")
    html = html.replace("__SUPABASE_URL__",      _sb_url)
    html = html.replace("__SUPABASE_ANON_KEY__", _sb_key)

    # ── Replay symbols (btcusdt2/3/4/5) — 2nd ("test") Supabase project.
    # FIX: pehle ye sirf Python side-server (/api/replay_symbol) ke through
    # backend se fetch hota tha — HF Spaces par wo internal port bahar se
    # (browser se) reachable nahi hota, isliye candle-data kabhi aata hi
    # nahi tha. Ab BN/BTC (SV2) jaisa hi — browser SEEDHA Supabase se baat
    # karta hai, isliye anon key (read-only, RLS-protected) yahan browser
    # ko bhejna safe hai — SERVICE key kabhi yahan nahi jaani chahiye.
    _replay_sb_url = REPLAY_SUPABASE_URL
    _replay_sb_key = REPLAY_SUPABASE_ANON_KEY
    html = html.replace("__REPLAY_SUPABASE_URL__",      _replay_sb_url)
    html = html.replace("__REPLAY_SUPABASE_ANON_KEY__", _replay_sb_key)

    # Optional auto-login credentials (personal app — same trust model as
    # the existing Fyers app_id/secret injection above). If left blank in
    # secrets, chart.html falls back to showing the manual login form.
    _sb_email = _get_secret("SUPABASE_LOGIN_EMAIL")
    _sb_pass  = _get_secret("SUPABASE_LOGIN_PASSWORD")
    html = html.replace("__SUPABASE_LOGIN_EMAIL__",    _sb_email)
    html = html.replace("__SUPABASE_LOGIN_PASSWORD__", _sb_pass)

    # ── Inject side-API port so chart.html knows which port to call ──────────
    _api_port = 0
    try:
        if os.path.exists(".api_port"):
            with open(".api_port") as _pf:
                _api_port = int(_pf.read().strip())
    except Exception:
        _api_port = 0
    html = html.replace("__API_PORT__", str(_api_port))

    # ── Startup mein last known BN tick inject karo (polling se pehle) ──────
    tick = None
    try:
        if os.path.exists("bn_live.json"):
            with open("bn_live.json") as _tf:
                tick = json.load(_tf)
    except Exception:
        tick = None
    if tick:
        tick_js = json.dumps(tick)
        inject = (
            "\n<script>"
            "(function(){"
            "  setTimeout(function(){"
            "    try{if(typeof _applyBNLiveTick==='function'){_applyBNLiveTick(" + tick_js + ");}}"
            "    catch(_){}"
            "  }, 1200);"
            "})();"
            "</script>"
        )
        html = html.replace("</body>", inject + "\n</body>")

    return html


# ─── Main area: embed chart directly (no separate API server needed) ─────────
st.markdown("## 📊 BankNifty Live Chart")

# ── Entry-mode flags (_fyers_entry_mode / _binance_entry_mode / _replay_mode)
# ── already defined early, right after sess_active. _chart_active bhi wahin
# defined hai — teeno mein se koi ek active ho to chart render hota hai.

# REMOVED (user ka explicit ask): SV2 (BankNifty/BTC replay) data pehle
# yahan eagerly "requested" set ho jaata tha jab _replay_mode True hota
# (matlab GitHub/Supabase se poora gz fetch + Python-side resample turant
# ho jaata, aur naya chart.html bhi eagerly re-embed hota). Ab SV2 SV3
# jaisa hi hai — koi bhi fetch tabhi hoga jab user khud in-chart calendar
# se date select/resume kare, aur wo bhi seedha browser se Supabase ko
# fetch() kar ke, koi Streamlit round-trip/reload nahi (dekho chart.html:
# _sv2EnsureAssetLoaded / _sv2BuildAssetFull). Isliye yahan koi
# session_state flag set karne ki zaroorat nahi rahi.
_lt_replay_mode = bool(st.session_state.get("_lt_replay_mode"))

# REMOVED (user ka explicit ask): pehle yahan SV3 (Nifty500 stocks) ke liye
# ek DEFAULT symbol eagerly preload hota tha jab bhi _lt_replay_mode True
# hota (matlab "Enter Chart Mode" dabate hi) — isse Supabase se full-history
# data fetch shuru ho jaata, chahe user ne abhi tak koi symbol select na
# kiya ho. Ab koi bhi SV3 data-fetch tabhi hoga jab user khud top-left
# symbol-picker se ek symbol chunega (dekho chart.html: _sv3SelectSymbol →
# _sv3SelectSymbolViaSupabase, jo seedha browser se, sirf usi ek symbol ki
# Supabase file fetch karta hai — koi Streamlit round-trip bhi nahi).
# Isliye "Enter Chart Mode" ab turant khulega, SV3 (Nifty500) hissa khaali/
# picker-prompt state mein khulega jab tak user khud symbol na chuno.

if _chart_active:
    if not sess_active:
        if _replay_mode and _lt_replay_mode:
            st.success("🚀 Chart Mode active — BTC/BankNifty aur Nifty500 stocks, symbol select karte hi on-demand load honge")
        elif _replay_mode:
            st.success("📼 Replay Mode active — BTC/BankNifty data date/symbol select karte hi on-demand load hoga")
        elif _binance_entry_mode:
            st.info("🟡 Binance Chart mode — BankNifty data available nahi (Fyers login nahi hai)")

    _chart_html = _build_chart_html(
        btc_1h, btc_day,
        bn_45m, bn_day,
        sess_active,
    )
    components.html(_chart_html, height=950, scrolling=False)

    # ── Combined Live-Data Pusher (option chain, meta, balance, depth) ──────
    # PEHLE: 5 alag @st.fragment (option chain, fyers-meta, binance-option-
    # chain, binance-meta, market-depth) — har ek apna khud ka hidden iframe
    # mount karta tha, har 1s/5s par. Wo pushMessage traffic zyada nahi tha,
    # lekin har fragment ka apna Streamlit-rerun + components.html() call +
    # naya iframe mount overhead tha — 4-5 alag mounts/sec chart ke upar.
    # AB: ek hi @st.fragment(run_every=1) sab data ikattha karta hai aur EK
    # hi <script> block mein saare postMessage bhej deta hai — sirf ek
    # iframe mount/sec, JS side listener wahi rehta hai (kuch badalne ki
    # zaroorat nahi — har message apne purane 'type' ke saath hi aata hai).
    # 5s-cadence wali cheezein (fyers-meta, binance-meta) counter se skip
    # hoti hain taaki unki API-cost badhe nahi.
    #
    # NOTE (REVERSED — see debug session ~12:03): purana _bn_tick_pusher
    # (postMessage BN-tick relay) pehle yahan se hata diya gaya tha, is
    # assumption par ki chart.html ka /api/bn_tick side-port poll (300ms,
    # primary) aur bn_live.json fallback (800ms) hi kaafi hain. Lekin
    # single-external-port hosting (jaise HF Spaces) mein browser side-port
    # (8502-8510) tak pahunch hi nahi paata — debug panel ne khud confirm
    # kiya: "Poll (backup): FAILING — fetch error: Failed to fetch". Aur
    # bn_live.json bhi Streamlit ke static file server se seedha serve nahi
    # hota (comment dekho _write_live_json() ke paas). Matlab dono fallback
    # is hosting mein hamesha silently fail hote hain, aur WS+REST backend
    # ekdum healthy hone ke bawajood BankNifty tick 30-80s tak stale reh
    # jaata tha kyunki koi channel actually deliver hi nahi kar pa raha tha.
    # FIX: postMessage relay wapas add kiya — ye wahi proven-working bridge
    # hai jisse option_chain/market_depth is hosting mein already reliably
    # chal rahe hain (Backend heartbeat sections mein "ALIVE" dikh raha
    # tha). JS listener (`window.addEventListener('message', ...)`,
    # `msg.type === 'bn_live'` → `_applyBNLiveTick(msg.data)`) already
    # wired tha aur bas isi push ka intezaar kar raha tha — koi JS/chart.html
    # change nahi karni padi. Side-port + bn_live.json fallbacks bhi rehne
    # diye (jahan wo kaam karte hain, jaise local dev, wahan fast 300ms poll
    # ab bhi primary rahega — postMessage sirf tab activate hota hai jab
    # wo dono quiet ho jaayein, dekho JS ka `_bnLastAppliedWallTs` throttle).
    st.session_state.setdefault("_live_pusher_tick", 0)

    if _chart_active:
        @st.fragment(run_every=1)
        def _live_data_pusher():
            st.session_state["_live_pusher_tick"] += 1
            _tick_n = st.session_state["_live_pusher_tick"]
            _do_5s  = (_tick_n % 5 == 0)   # har 5th run par hi 5s-cadence wali cheezein refresh

            _messages = []  # list of (type, payload_dict)

            # BankNifty live tick (1s) — postMessage fallback, see NOTE
            # above. `_get_live_payload()` None hota hai sirf first-ever
            # tick se pehle (process boot ke turant baad) — us case mein
            # bhejne layak kuch nahi hota, isliye skip.
            if sess_active:
                _bn_live_payload = _get_live_payload()
                if _bn_live_payload is not None:
                    _messages.append(("bn_live", _bn_live_payload))

            # Fyers: option chain (1s) + balance/meta (5s)
            if sess_active:
                _oc = get_cached_option_chain_payload()
                if not _oc:
                    _oc = {"error": "Kuch data nahi mila (unknown reason)"}
                _messages.append(("option_chain", _oc))

                if _do_5s:
                    _messages.append(("fyers_meta", refresh_fyers_meta_cache()))

            # Binance: option chain (1s) + balance/meta (5s)
            if _binance_entry_mode:
                _boc = get_cached_binance_option_chain_payload()
                if not _boc:
                    _boc = {"error": "Kuch data nahi mila (unknown reason)"}
                _messages.append(("binance_option_chain", _boc))

                if _do_5s:
                    _messages.append(("binance_meta", refresh_binance_meta_cache()))

            # Market Depth (BTC/NSE) — har run heartbeat + jab depth-sheet
            # khula ho tab actual data (dekho purani detailed note upar wale
            # commit mein — refresh_market_depth_cache ka apna 1.5s TTL hai).
            global _MD_PUSHER_DEBUG
            _MD_PUSHER_DEBUG["runs"] += 1
            _dsym = st.session_state.get("_active_depth_symbol")
            _MD_PUSHER_DEBUG["last_active_symbol"] = _dsym
            _MD_PUSHER_DEBUG["last_run_ts"] = time.time()
            if not _dsym:
                _dpayload = {
                    "_heartbeat": True,
                    "active_symbol_on_backend": None,
                    "runs": _MD_PUSHER_DEBUG["runs"],
                }
            else:
                _dpayload = refresh_market_depth_cache(_dsym)
                _dpayload["_heartbeat"] = False
                _dpayload["active_symbol_on_backend"] = _dsym
                _dpayload["runs"] = _MD_PUSHER_DEBUG["runs"]
            _messages.append(("market_depth", _dpayload))

            # ── Local persistent-storage SAVE ack — Handler 6 (query-param
            # bridge) ne jo bhi last save process kiya, uska result yahan se
            # postMessage ke through JS ko wapas bhejte hain, taaki debug
            # panel ko pata chale save safal hua ya nahi (side-port wale
            # doLocalSave() ke purane onreadystatechange ki jagah, jo HF par
            # kabhi fire hi nahi hota tha). ──
            _ls_last = st.session_state.get("_local_save_last")
            if _ls_last:
                _messages.append(("local_save_ack", _ls_last))

            # ── Twelve Data external symbols (Gold/Dow Jones/...) — LIVE
            # ab Finnhub WebSocket se aata hai (dekho _finnhub_ws_loop),
            # yahan sirf jo keys abhi-abhi tick se update hui hain unhe
            # browser ko push karte hain — poori registry scan nahi karni
            # padti. ~2s cadence rakha hai (har tick pe nahi) taaki chart
            # rebuild (JS side full-candles replace + redraw) bahut zyada
            # baar-baar na ho — Finnhub push khud milliseconds-level hai,
            # display-refresh 2s kaafi hai "real live" feel ke liye.
            if _tick_n % 2 == 0:
                try:
                    for _td_key in _td.td_pop_dirty_keys():
                        _messages.append(("td_live", {
                            "key": _td_key,
                            "candles": _td.td_get_bucketed(_td_key),
                        }))
                except Exception as _e_fh_push:
                    _slog_exception("td_pop_dirty_keys push", _e_fh_push)

            # ── Replay symbols (btcusdt2/3/4/5) — naya candle Supabase-side
            # cron har 5 min reveal karta hai; yahan har ~30s check karte
            # hain kisi symbol ka pointer badla to nahi (halka REST call,
            # reveal_state chhoti table hai). Badla ho to sirf usi symbol
            # ka fresh data browser ko bhejte hain (same td_live channel
            # reuse — chart.html ke liye ye TD symbol jaisa hi dikhta hai).
            if REPLAY_SUPABASE_URL and REPLAY_SUPABASE_ANON_KEY and _tick_n % 30 == 0:
                try:
                    # v3 — ON-DEMAND: pehle yahan dirty mila har symbol
                    # push ho jaata tha (charo hamesha loaded the). Ab
                    # sirf jo symbol chart.html mein abhi khula/active hai
                    # (dekho /api/replay_set_active) usi ka push bhejte
                    # hain — baaki 3 symbols agar load hi nahi hain to
                    # unka data banane/bhejne ka koi fayda nahi.
                    _replay_active = _replay.replay_get_active_key()
                    for _replay_key in _replay.replay_pop_dirty_keys(
                        REPLAY_SUPABASE_URL, REPLAY_SUPABASE_ANON_KEY, log_fn=_slog
                    ):
                        if _replay_active and _replay_key != _replay_active:
                            continue  # ye symbol abhi screen par nahi hai — skip
                        _rpush = _replay.replay_get_bucketed(
                            _replay_key, REPLAY_SUPABASE_URL, REPLAY_SUPABASE_ANON_KEY, log_fn=_slog
                        )
                        _messages.append(("td_live", {
                            "key": _replay_key,
                            "candles": _rpush["candles"],
                            "revealed_up_to_t": _rpush["revealed_up_to_t"],  # FIX: countdown-timer ke liye
                        }))
                except Exception as _e_replay_push:
                    _slog_exception("replay_pop_dirty_keys push", _e_replay_push)

                # ── Replay Symbols DEBUG snapshot — top-header popup ke
                # naye fields (dbValue/baseline/lastCheck/fetchErr/fullRerun)
                # yahin se aate hain. Isi 30-tick cadence par bhejte hain
                # (jitni baar dirty-check chalta hai utni baar hi kaafi
                # hai — real-time-per-second granularity ki zaroorat nahi,
                # ye sirf diagnosis ke liye hai). Fail-safe: fail ho to
                # bas is ek push ko skip karo, baaki sab untouched. ──
                try:
                    _messages.append(("replay_debug", _replay.replay_get_debug_state()))
                except Exception as _e_replay_dbg:
                    _slog_exception("replay_get_debug_state push", _e_replay_dbg)

            # ── Twelve Data REST poll — ab sirf SLOW BACKUP/reconciliation
            # hai (Finnhub WS drop/gap ho jaaye to bhi data stale na rahe).
            # auto-scaling interval (registry-size ke hisaab se, free-plan
            # rate-limit respect karte hue — dekho td_recommended_poll_
            # interval_sec). Har tick pe check, lekin actual API call sirf
            # jab interval poora ho. ──────────────────────────────────────
            _td_interval = _td.td_recommended_poll_interval_sec()
            if _tick_n % _td_interval == 0:
                try:
                    _td_updated = _td.td_live_poll_batch(TWELVEDATA_API_KEY, log_fn=_slog)
                    for _td_key in _td_updated:
                        _messages.append(("td_live", {
                            "key": _td_key,
                            "candles": _td.td_get_bucketed(_td_key),
                        }))
                except Exception as _e_td:
                    _slog_exception("td_live_poll_batch", _e_td)

            # ── Ek hi script mein saare postMessage bhejo ────────────────────
            _posts = "\n".join(
                "  try { frames[i].contentWindow.postMessage(JSON.stringify(%s), '*'); } catch(e) {}"
                % json.dumps({"type": _mtype, "data": _mdata})
                for _mtype, _mdata in _messages
            )
            _combined_script = f"""
<script>
(function() {{
  var frames = window.parent.document.querySelectorAll('iframe');
  for (var i = 0; i < frames.length; i++) {{
{_posts}
  }}
}})();
</script>
"""
            components.html(_combined_script, height=0, scrolling=False)

        _live_data_pusher()

else:
    # ─── Main area inline Login Panel ─────────────────────────────────────────
    _creds_main = load_creds()
    _has_old    = bool(_creds_main.get("access_token")) and not sess_active
    _app_id_m   = _creds_main.get("app_id",    DEFAULT_APP_ID)
    _secret_m   = _creds_main.get("secret_key", DEFAULT_SECRET)
    import random as _rand
    _nonce_m = str(int(time.time())) + str(_rand.randint(1000, 9999))
    _auth_url_m = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={_app_id_m}"
        f"&redirect_uri=https%3A%2F%2Fwww.google.com"
        f"&response_type=code"
        f"&state={_nonce_m}"
        f"&nonce={_nonce_m}"
    )

    st.markdown("""
    <style>
    .login-card{
        background:#1e222d;border:1px solid #2a2e3e;border-radius:14px;
        padding:32px 28px;max-width:620px;margin:30px auto;
    }
    .login-title{color:#e0e3eb;font-size:1.5rem;font-weight:700;margin-bottom:4px;}
    .login-sub{color:#848da0;font-size:0.9rem;margin-bottom:24px;}
    .method-label{
        color:#a3aabf;font-size:0.78rem;font-weight:600;letter-spacing:.08em;
        text-transform:uppercase;margin-bottom:8px;
    }
    .step-badge{
        background:#1a73e8;color:#fff;border-radius:50%;
        width:22px;height:22px;display:inline-flex;align-items:center;
        justify-content:center;font-size:.75rem;font-weight:700;margin-right:8px;
    }
    </style>
    """, unsafe_allow_html=True)

    # ── TOP "Enter" button — login page ke sabse upar. Fyers/Binance login
    # ho ya na ho, seedha chart mode mein le jaata hai. Dono replay data
    # sources (SV2 = BTC/BankNifty, SV3 = Nifty500 stocks) ek saath
    # preload hote hain, taaki chart ke andar switch karte waqt/symbol
    # select karte waqt kabhi reload na ho (dono ab Supabase se aate hain).
    st.markdown("<div style='max-width:620px;margin:0 auto 8px;'>", unsafe_allow_html=True)
    st.markdown('''<div class="login-card" style="padding:20px 24px;">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title" style="font-size:1.15rem;margin-bottom:2px;">🚀 Enter</div>''', unsafe_allow_html=True)
    st.markdown(
        "<div style='color:#848da0;font-size:0.82rem;margin-bottom:14px;'>"
        "Login ho ya na ho — seedha chart mode kholo. BTC/BankNifty aur "
        "Nifty500 stocks, dono ka replay data chart ke andar hi available "
        "rahega.</div>",
        unsafe_allow_html=True,
    )
    if st.button("🚀 Enter Chart Mode", use_container_width=True, key="top_enter_chart_btn"):
        _slog("👉 'Enter Chart Mode' (top button) clicked → _replay_mode=True, _lt_replay_mode=True set kiya, st.rerun().")
        st.session_state["_replay_mode"]        = True
        st.session_state["_lt_replay_mode"]     = True
        st.session_state["_fyers_entry_mode"]   = False
        st.session_state["_binance_entry_mode"] = False
        st.rerun()
    st.markdown('''</div>''', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    if _has_old:
        st.error("🔴 Fyers token expire ho gaya — dobara login karo")

    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🔑 Fyers Login</div>''', unsafe_allow_html=True)
    st.markdown('''<div class="login-sub">Login karo — phir live BankNifty chart khulega</div>''', unsafe_allow_html=True)

    # ── METHOD B: Google URL ───────────────────────────────────────────────────
    st.markdown('''<div class="method-label">🔗 Google URL Login</div>''', unsafe_allow_html=True)

    st.markdown(
        f'''<p style="margin:6px 0 10px;">'''
        f'''<span class="step-badge">1</span>'''
        f'''<a href="{_auth_url_m}" target="_blank" style="color:#1a73e8;font-weight:600;">'''
        f'''👉 Yahan click karo — Fyers Fresh Login Link</a></p>''',
        unsafe_allow_html=True,
    )
    st.caption("⚠️ Link click karo → Google page khulega → us page ka poora URL copy karo")

    _url_inp_m = st.text_input(
        "Step 2 → Poora Google URL ya sirf auth_code paste karo",
        placeholder="https://www.google.com/?s=ok&auth_code=eyJ...",
        key="main_url_inp",
    )

    if st.button("⚡ Connect", use_container_width=True, type="primary", key="main_url_connect"):
        _raw_m = _url_inp_m.strip()
        if _raw_m:
            _code_m = _extract_auth_code(_raw_m)
            _ok_u, _tok_u, _resp_u = fyers_get_access_token(_app_id_m, _secret_m, _code_m)
            if _ok_u:
                save_creds({
                    **_creds_main,
                    "app_id":       _app_id_m,
                    "secret_key":   _secret_m,
                    "client_id":    DEFAULT_CLIENT_ID,
                    "password":     DEFAULT_PASSWORD,
                    "access_token": _tok_u,
                })
                st.session_state["_force_active"] = False
                _sess_cache.update({"active": False, "ts": time.time()})
                st.success("🎉 Connected!")
            else:
                st.error(f"❌ Login Failed: {_tok_u}")
                with st.expander("Full Fyers Response"):
                    st.code(json.dumps(_resp_u, indent=2), language="json")
        else:
            st.warning("URL ya auth_code paste karo pehle")

    if sess_active:
        st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
        if st.button("📈 Fyers Entry — Chart Kholo", use_container_width=True, key="fyers_entry_btn"):
            # Sirf Fyers-related threads chalenge (REST poller, token monitor,
            # Fyers option-chain, Fyers WS) — Binance ka koi thread nahi.
            _slog("👉 'Fyers Entry' clicked → _fyers_entry_mode=True set kiya (Binance/Replay off), st.rerun().")
            st.session_state["_fyers_entry_mode"]   = True
            st.session_state["_binance_entry_mode"] = False
            st.session_state["_replay_mode"]        = False
            st.session_state["_lt_replay_mode"]     = False
            st.rerun()
    else:
        st.markdown(
            "<div style='color:#555;font-size:0.8rem;margin-top:10px;'>"
            "Chart kholne ke liye pehle login karo.</div>",
            unsafe_allow_html=True,
        )

    st.markdown('''</div>''', unsafe_allow_html=True)

    # ── Binance Status Card (no manual login) ──────────────────────────────────
    #    API Key/Secret sirf HF Space secrets (env vars) se aate hain —
    #    BINANCE_API_KEY / BINANCE_SECRET_KEY. Koi text input, koi save-to-disk
    #    nahi. Background thread (BinanceOptionChainBG) inhi env creds ko
    #    directly _get_binance_creds() se padhta hai.
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🟡 Binance</div>''', unsafe_allow_html=True)
    st.markdown('''<div class="login-sub">Keys HF Space secrets se aati hain (BINANCE_API_KEY / BINANCE_SECRET_KEY) — yahan kuch bharne ki zaroorat nahi</div>''', unsafe_allow_html=True)

    _bn_api_key, _bn_secret_key = _get_binance_creds()
    if _bn_api_key and _bn_secret_key:
        st.markdown(
            "<div style='padding:8px 0;color:#26a69a;font-size:0.85rem;font-weight:600;'>"
            "🟢 Secrets mile — dono keys set hain</div>",
            unsafe_allow_html=True,
        )
    else:
        _missing = []
        if not _bn_api_key: _missing.append("BINANCE_API_KEY")
        if not _bn_secret_key: _missing.append("BINANCE_SECRET_KEY")
        st.markdown(
            f"<div style='padding:8px 0;color:#ef5350;font-size:0.85rem;'>"
            f"⚠️ Missing: {', '.join(_missing)} — HF Space Settings → Variables and secrets me daalo</div>",
            unsafe_allow_html=True,
        )

    if st.button("🔎 Verify Binance Connection", use_container_width=True, key="binance_verify_btn"):
        _slog("👉 'Verify Binance Connection' button clicked")
        if not (_bn_api_key and _bn_secret_key):
            st.session_state["binance_logged_in"] = False
            _slog("Binance verify blocked — env secrets khaali the", level="warn")
            st.error("Pehle HF Space secrets me BINANCE_API_KEY / BINANCE_SECRET_KEY daalo")
        else:
            with st.spinner("Binance keys verify ho rahi hain…"):
                try:
                    _ok_login, _login_result = binance_get_spot_balance(_bn_api_key, _bn_secret_key)
                except Exception as _e_bn_login:
                    _slog_exception("Binance Verify → binance_get_spot_balance()", _e_bn_login)
                    _ok_login, _login_result = False, str(_e_bn_login)

            st.session_state["binance_logged_in"] = _ok_login
            if _ok_login:
                _slog("Binance keys verified OK", level="ok")
                st.success("✅ Binance keys valid hain")
            else:
                _slog(f"Binance verify FAILED — {_login_result}", level="err")
                st.error(f"❌ Verify failed: {_login_result}")

    if _bn_api_key and _bn_secret_key:
        st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
        if st.button("📈 Binance Entry — Chart Kholo", use_container_width=True, key="binance_entry_btn"):
            # Sirf Binance-related threads chalenge (option-chain BG + saare
            # Binance WS loops) — Fyers ka koi thread nahi.
            _slog("👉 'Binance Entry' clicked → _binance_entry_mode=True set kiya (Fyers/Replay off), st.rerun().")
            st.session_state["_binance_entry_mode"] = True
            st.session_state["_fyers_entry_mode"]   = False
            st.session_state["_replay_mode"]        = False
            st.session_state["_lt_replay_mode"]     = False
            st.rerun()

    st.markdown('''</div>''', unsafe_allow_html=True)


    # ── Proxy Settings Card ────────────────────────────────────────────────────
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🌐 Proxy Settings <span style="font-size:0.75rem;color:#555;font-weight:400;">(toggle HF Space Variable mein persist hota hai — restart/sleep ke baad bhi yehi value load hogi)</span></div>''', unsafe_allow_html=True)

    # ── RAM se current state padho ───────────────────────────────────────────
    with _PROXY_LOCK:
        _ram_host = _PROXY_CACHE["host"]
        _ram_port = _PROXY_CACHE["port"]
        _ram_user = _PROXY_CACHE["user"]
        _ram_pwd  = _PROXY_CACHE["password"]
        _ram_on   = _PROXY_CACHE["on"]

    # ── Toggle — sabse upar ──────────────────────────────────────────────────
    _proxy_toggle_col, _proxy_status_col = st.columns([1, 2])
    with _proxy_toggle_col:
        _proxy_on = st.toggle(
            "Proxy Use Karo",
            value=_ram_on,
            key="proxy_on_toggle",
            help="ON = sabhi Binance requests proxy se jayengi\nOFF = direct Binance connection",
        )
    with _proxy_status_col:
        if _proxy_on and _ram_host and _ram_port:
            st.markdown(
                f"<div style='padding:8px 0;color:#26a69a;font-size:0.85rem;font-weight:600;'>"
                f"🟢 Active — {_ram_host}:{_ram_port}</div>",
                unsafe_allow_html=True,
            )
        elif _proxy_on and not (_ram_host and _ram_port):
            st.markdown(
                "<div style='padding:8px 0;color:#ef5350;font-size:0.85rem;'>"
                "⚠️ ON hai par address save nahi — neeche bhar ke Apply karo</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='padding:8px 0;color:#555;font-size:0.85rem;'>"
                "⚪ OFF — Direct Binance connection</div>",
                unsafe_allow_html=True,
            )

    # Toggle flip hone par: RAM turant update (fields turant active/disable
    # ho jaayein) + HF Space 'PROXY_ON' Variable bhi update (persist ho jaaye,
    # restart/sleep ke baad bhi yahi value load ho). Variable change hote hi
    # HF khud Space rebuild kar deta hai — isliye purani websocket threads
    # (Binance) bhi is rebuild mein khud khatam ho jaayengi, alag se restart
    # button dabane ki zaroorat nahi.
    if _proxy_on != _ram_on:
        _proxy_apply(_ram_host, _ram_port, _ram_user, _ram_pwd, _proxy_on)
        _ram_on = _proxy_on
        with st.spinner("PROXY_ON variable HF Space par save ho raha hai…"):
            _pxvar_ok, _pxvar_msg = set_hf_proxy_variable(_proxy_on)
        if _pxvar_ok:
            st.success(f"✅ {_pxvar_msg}")
            _slog(f"PROXY_ON variable updated via toggle: {_pxvar_msg}", level="ok")
        else:
            st.warning(f"⚠️ RAM mein change ho gaya, lekin HF par persist nahi hua: {_pxvar_msg}")
            _slog(f"PROXY_ON variable update FAILED: {_pxvar_msg}", level="err")

    # ── Fields — RAM se pre-fill, toggle OFF ho to disable ──────────────────
    st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
    _px_col1, _px_col2 = st.columns([3, 1])
    with _px_col1:
        _proxy_host = st.text_input(
            "Proxy Address",
            value=_ram_host,
            placeholder="3.216.155.203",
            key="proxy_host_inp",
            disabled=not _proxy_on,
        )
    with _px_col2:
        _proxy_port = st.text_input(
            "Port",
            value=_ram_port,
            placeholder="8080",
            key="proxy_port_inp",
            disabled=not _proxy_on,
        )

    _px_col3, _px_col4 = st.columns(2)
    with _px_col3:
        _proxy_user = st.text_input(
            "Username (optional)",
            value=_ram_user,
            placeholder="myuser",
            key="proxy_user_inp",
            disabled=not _proxy_on,
        )
    with _px_col4:
        _proxy_pass = st.text_input(
            "Password (optional)",
            value=_ram_pwd,
            placeholder="••••••",
            type="password",
            key="proxy_pass_inp",
            disabled=not _proxy_on,
        )

    _px_btn_col1, _px_btn_col2 = st.columns(2)
    with _px_btn_col1:
        if st.button("✅ Apply", use_container_width=True, key="proxy_save_btn", disabled=not _proxy_on):
            if _proxy_host.strip() and _proxy_port.strip():
                # sirf RAM mein daal do — koi file nahi
                _proxy_apply(_proxy_host, _proxy_port, _proxy_user, _proxy_pass, True)
                st.success(f"✅ Proxy RAM mein set: {_proxy_host.strip()}:{_proxy_port.strip()}")
                _slog(f"Proxy applied to RAM: {_proxy_host.strip()}:{_proxy_port.strip()}", level="ok")
            else:
                st.warning("⚠️ Address aur Port dono bharo")

    with _px_btn_col2:
        if st.button("🔍 Test", use_container_width=True, key="proxy_test_btn", disabled=not _proxy_on):
            # Test ke liye RAM mein fields daal ke test karo
            _proxy_apply(_proxy_host, _proxy_port, _proxy_user, _proxy_pass, True)
            with st.spinner("Test ho raha hai…"):
                _test_ok, _test_msg = _test_proxy()
            if _test_ok:
                st.success(_test_msg)
                _slog(f"Proxy test SUCCESS: {_test_msg}", level="ok")
            else:
                st.error(_test_msg)
                _slog(f"Proxy test FAILED: {_test_msg}", level="err")

    st.markdown('''</div>''', unsafe_allow_html=True)

    # ── SV3 BTC 1D History Card ─────────────────────────────────────────────
    # Ye sirf ONE-TIME bootstrap ke liye hai (turant BTCUSDT.json bana kar
    # Supabase par daalne ke liye) — uske baad daily gap-fill khud background
    # mein automatically ho jaata hai (_maybe_launch_background_update
    # "btc_sv3_1d" source, upar chart-active hote hi launch hota hai), isliye
    # roz-roz ye button dabane ki zaroorat nahi.
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🪙 SV3 — BTC 1D History <span style="font-size:0.75rem;color:#555;font-weight:400;">(one-time bootstrap — Binance se poora history khinch ke Supabase par BTCUSDT.json banata hai)</span></div>''', unsafe_allow_html=True)
    if st.button("📥 BTC 1D History Banao (Supabase Par Upload)", use_container_width=True, key="btc_sv3_build_btn"):
        with st.spinner("Binance se BTCUSDT ka poora 1D history khinch ke Supabase par upload ho raha hai… (proxy ON hona chahiye, ismein 1-2 minute lag sakte hain)"):
            _btc3_ok, _btc3_msg = _btc_sv3_build_and_upload_full()
        if _btc3_ok:
            st.success(f"✅ {_btc3_msg}")
            _slog(f"SV3 BTC 1D manual build: {_btc3_msg}", level="ok")
        else:
            st.error(f"❌ {_btc3_msg}")
            _slog(f"SV3 BTC 1D manual build FAILED: {_btc3_msg}", level="err")
    st.markdown('''</div>''', unsafe_allow_html=True)

    # ── SV1 Replay Master File — daily update card ──────────────────────────
    # Master file (Bitcoin_BTCUSDT_IST_5m_json.gz, alag "Btc"/REPLAY project)
    # ab manually is button se update ho sakti hai — proxy se naye 5m candles
    # khinch ke TEMP-upload + atomic-swap se live file update karta hai, aur
    # reveal-cron ka dynamic cap bhi sync kar deta hai. Purana cron
    # (reveal_candles_sql_every_5min) is button se KABHI touch nahi hota.
    st.markdown('''<div class="login-card">''', unsafe_allow_html=True)
    st.markdown('''<div class="login-title">🔁 SV1 — Replay Master File Update <span style="font-size:0.75rem;color:#555;font-weight:400;">(btcusdt2-28 ka shared master file — proxy se naye candles khinch ke Supabase par safe-swap se update karta hai)</span></div>''', unsafe_allow_html=True)
    if st.button("🔄 SV1 Master File Update Karo", use_container_width=True, key="sv1_master_update_btn"):
        with st.spinner("Binance se naye candles khinch rahe hain aur Supabase par safe-swap ho raha hai… (proxy ON hona chahiye, ismein 1-2 minute lag sakte hain)"):
            _rmu_ok, _rmu_msg = _replay_master_incremental_update()
        if _rmu_ok:
            st.success(_rmu_msg)
            _slog(f"SV1 master manual update: {_rmu_msg}", level="ok")
        else:
            st.error(f"❌ {_rmu_msg}")
            _slog(f"SV1 master manual update FAILED: {_rmu_msg}", level="err")
    st.markdown('''</div>''', unsafe_allow_html=True)

    # ── Restart Space / Pause Space buttons PERMANENTLY REMOVED — user
    # inhe HF dashboard / UI se hi manually kar leta hai.

    # ── Replay Data Update card (BTC Update Karo / BankNifty Update Karo)
    # PERMANENTLY REMOVED — app khud hi background mein auto-update kar
    # rahi thi, isliye ye manual buttons redundant the — user ke explicit
    # ask par hata diya gaya.

    # ── "Nifty 500 Stocks Data" (File Banao) card PERMANENTLY REMOVED ──
    # Ye legacy/backup mechanism tha (poore-500-stocks ka ek combined local
    # .gz file, HF Space repo mein push hota tha) — live chart iss se kabhi
    # padhta nahi tha. SV3 chart hamesha Supabase per-symbol files
    # (_index.json + NSE_XXX-EQ.json) se aata hai, jo background auto-update
    # (_nifty500_incremental_update, roz-ek-baar) khud-ba-khud fresh rakhta
    # hai. Isliye ye manual button redundant tha — user ke explicit ask par
    # hata diya gaya.

    # ── Long Term Stock Replay (SV3) shortcut hata diya — ab is jagah top
    # wala "Enter" button (login page ke sabse upar) dono (SV2 BTC/BankNifty
    # + SV3 Nifty500 stocks) ek saath preload karke chart mode kholta hai.
    st.markdown('''</div>''', unsafe_allow_html=True)

