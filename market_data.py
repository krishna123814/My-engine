"""
market_data.py — Daily/incremental data-refresh jobs (BTC, BankNifty
replay-master, Nifty500) + Stack View 2/3 resampling & gap-filling.

NOTE: Ye do pehle-alag-planned modules (data_updates + resample_utils)
genuinely ek-doosre ko call karte hain (Nifty500 update SV3-cache refresh
karta hai, SV3 resampler Nifty500 symbol-list maangta hai) — isliye inhe
ek hi file mein rakha taaki circular-import na ho. Ye do logical sections
hain (comments se clearly divided), do alag files nahi.
"""
import os
import json
import time
import datetime
import requests
import gzip as _gzip
import streamlit as st
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import replay_symbols as _replay
from config import (_get_secret, IST, _ist_now, BTC_1D_SYMBOL,
                     _IST_NAIVE_OFFSET, _GZ_APPEND_OFFSET,
                     _SESSION_START, _SESSION_END)
from startup_log import _slog, _slog_exception
from credentials import load_creds
from network_proxy import _get_proxy_dict
from login_session import is_session_active
from fyers_history import _fyers_history_chunk
from storage import (_SV2_CACHE, _LOCAL_BN_GZ, _LOCAL_BTC_GZ,
                      _supabase_upload, _gz_push_to_supabase, _gz_save_local,
                      _sv2_load_bn_gz, _sv2_load_btc_gz,
                      _local_state_save, _local_state_load_all,
                      _load_update_status, _save_update_status,
                      _already_updated_today, _mark_updated_today,
                      _maybe_launch_background_update,
                      _SUPABASE_BASE, REPLAY_SUPABASE_URL, REPLAY_SUPABASE_ANON_KEY)

# ═══════════════════════ SECTION A: Data-update jobs ═══════════════════════
def _append_new_btc_candles() -> tuple[bool, str]:
    """Binance REST (public klines, 5m) se local .gz ke aakhri candle ke baad
    ka naya data fetch karo, purane data ke saath append karo, local file
    update karo, phir Supabase Storage par push karo. Binance call proxy se
    jaati hai (Binance is Space se blocked hai); Supabase upload proxy se NAHI
    jaata."""
    rows = _sv2_load_btc_gz()
    if not rows:
        return False, "Purana BTC data hi load nahi ho paaya — pehle wo theek karo."
    last_t = max(r["t"] for r in rows)   # BTC .gz timestamps real UTC hain (offset nahi)
    start_ms = (last_t + 300) * 1000     # agla 5-min candle
    end_ms   = int(time.time() * 1000)
    if start_ms >= end_ms:
        return True, "BTC data already up-to-date hai, naya candle abhi bana nahi."
    try:
        new_rows = []
        cursor = start_ms
        while cursor < end_ms:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "5m",
                        "startTime": cursor, "endTime": end_ms, "limit": 1000},
                proxies=_get_proxy_dict(), timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for k in batch:
                new_rows.append({"t": int(k[0] // 1000), "o": float(k[1]),
                                  "h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
            cursor = batch[-1][0] + 300_000   # next candle after last openTime
            if len(batch) < 1000:
                break
        if not new_rows:
            return True, "Binance se koi naya candle nahi mila (already latest)."
        merged_map = {r["t"]: r for r in rows}
        for r in new_rows:
            merged_map[r["t"]] = r
        merged = sorted(merged_map.values(), key=lambda r: r["t"])
        _gz_save_local(_LOCAL_BTC_GZ, merged)
        _SV2_CACHE["btc_raw"] = merged   # is session mein turant reflect ho
        ok, msg = _gz_push_to_supabase(_LOCAL_BTC_GZ, "Bitcoin_BTCUSDT_IST_5m_json.gz")
        return ok, f"BTC: {len(new_rows)} naye candles append hue. {msg}"
    except Exception as e:
        _slog_exception("BTC_BTN_CLICK _append_new_btc_candles()", e)
        return False, f"BTC append fail: {e} (poori traceback debug block me EXCEPTION tag ke saath)"


# ─── SV1 Replay Master File — daily incremental update ────────────────────
# NAYA: SV1 (btcusdt2-28) ka master file (`Bitcoin_BTCUSDT_IST_5m_json.gz`,
# alag "Btc"/REPLAY_SUPABASE_URL project mein) ab isse update ho sakta hai
# — bilkul _append_new_btc_candles() (upar) jaisa hi pattern (Binance se
# naye 5m candles, proxy ke through), bas destination alag project hai
# aur write TEMP-upload+SWAP se hoti hai (replay_symbols.py, race-safe —
# dekho wahan ka comment). Purana `reveal_candles_sql_every_5min` cron
# is function se KABHI seedha touch nahi hota — cron sirf DB-tables
# (reveal_state, master_file_meta) padhta hai, is function ka aakhri
# kaam bhi wahi hai: naya `data_upto_t` likh dena taaki cron ko pata
# chale "asal data ab yahan tak hai".
def _replay_master_incremental_update() -> tuple[bool, str]:
    """SV1 replay-symbols ka master file update — proxy se Binance se
    naye 5m candles khinchta hai, purane master ke saath merge karta
    hai, TEMP-upload + atomic-swap se live file update karta hai, aur
    aakhri mein reveal-cron ka dynamic cap (`master_file_meta`) bhi
    naye data ke hisaab se sync kar deta hai. Proxy OFF ho ya kai din
    na chalaya jaaye to bhi safe hai — bas 'kitna gap hai utna fetch
    karega', koi jump/reset nahi hota (self-correcting, jaisa reveal
    formula khud hai)."""
    if not (REPLAY_SUPABASE_URL and REPLAY_SUPABASE_ANON_KEY):
        return False, "REPLAY_SUPABASE_URL / TEST_SUPABASE_ANON_KEY set nahi hain (HF secrets check karo)."
    _replay_service_key = _get_secret("TEST_SUPABASE_SERVICE_KEY")
    if not _replay_service_key:
        return False, "TEST_SUPABASE_SERVICE_KEY secret nahi mila — HF Space Settings → Variables and secrets mein add karo (Btc project ki service_role key, anon key se ALAG)."

    master = _replay.replay_get_master_rows(REPLAY_SUPABASE_URL, log_fn=_slog)
    if not master:
        return False, "SV1 master file load hi nahi ho payi (Supabase se) — pehle proxy/connection check karo."

    last_t = master[-1]["t"]           # master gapless hai, last row hi asli 'abhi tak' hai
    start_ms = (last_t + 300) * 1000   # agla 5-min candle
    end_ms = int(time.time() * 1000)
    if start_ms >= end_ms:
        return True, "SV1 master file already up-to-date hai, naya candle abhi bana nahi."

    try:
        new_rows = []
        cursor = start_ms
        while cursor < end_ms:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "5m",
                        "startTime": cursor, "endTime": end_ms, "limit": 1000},
                proxies=_get_proxy_dict(), timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for k in batch:
                new_rows.append({"t": int(k[0] // 1000), "o": float(k[1]),
                                  "h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
            cursor = batch[-1][0] + 300_000
            if len(batch) < 1000:
                break

        if not new_rows:
            return True, "Binance se koi naya candle nahi mila (already latest)."

        # ── merge (dedupe-safe via dict, phir sorted) — same pattern jo
        # _append_new_btc_candles() mein hai, taaki koi duplicate/overlap
        # candle na ho ────────────────────────────────────────────────
        merged_map = {r["t"]: r for r in master}
        for r in new_rows:
            merged_map[r["t"]] = r
        merged = sorted(merged_map.values(), key=lambda r: r["t"])
        gz_bytes = _gzip.compress(json.dumps(merged).encode("utf-8"))

        # ── Step A: TEMP filename se upload (live file abhi untouched) ──
        ok_up, up_msg = _replay.replay_master_upload_temp(
            REPLAY_SUPABASE_URL, _replay_service_key, gz_bytes, log_fn=_slog)
        if not ok_up:
            return False, f"Temp upload fail (live file untouched hai, safe hai): {up_msg}"

        # ── Step B: atomic swap (live ab naya data serve karega) ────────
        ok_swap, swap_msg = _replay.replay_master_swap(
            REPLAY_SUPABASE_URL, _replay_service_key, log_fn=_slog)
        if not ok_swap:
            return False, f"Swap fail: {swap_msg}"

        # ── Step C: cron ka dynamic cap sync (SABSE AAKHRI, file swap ke
        # baad hi — warna cron aisi jagah reveal maang sakta hai jahan
        # abhi data nahi pahuncha) ───────────────────────────────────────
        new_last_t = merged[-1]["t"]
        ok_cap, cap_msg = _replay.replay_update_master_cap(
            REPLAY_SUPABASE_URL, _replay_service_key, new_last_t, log_fn=_slog)
        if not ok_cap:
            return True, (f"⚠️ Master file update ho gayi ({len(new_rows)} naye candles, "
                           f"ab tak: {new_last_t}) LEKIN reveal-cap update fail hui: {cap_msg} — "
                           f"cron abhi bhi PURANI cap tak hi reveal karega jab tak ye manually "
                           f"SQL Editor se fix na ho: update public.master_file_meta set "
                           f"data_upto_t = {new_last_t} where id = 1;")

        return True, (f"✅ SV1 Master file update ho gayi — {len(new_rows)} naye candles jode gaye "
                       f"(ab data yahan tak hai: {new_last_t}), reveal-cron ka cap bhi sync ho gaya. "
                       f"Purana cron job bilkul untouched raha.")
    except Exception as e:
        _slog_exception("_replay_master_incremental_update", e)
        return False, f"SV1 master update fail: {e} (poori traceback debug block me EXCEPTION tag ke saath)"

def _append_new_bn_candles() -> tuple[bool, str]:
    """Fyers history (5m, direct — koi proxy nahi) se local BankNifty .gz ke
    aakhri candle ke baad ka naya data fetch karo, append + local update +
    Supabase Storage par push."""
    creds = load_creds()
    if not creds.get("access_token"):
        return False, "Fyers login nahi hai — pehle Fyers se login karo, tabhi BankNifty data mil sakta hai."
    # FIX: sirf access_token ki *presence* check karna kaafi nahi hai — wo
    # expire ho chuka ho tab bhi file mein maujood rehta hai. Isse pehle
    # ek false-positive bug tha: expired token ke saath Fyers API call
    # silently khaali candles [] return karta tha, aur wo code "already
    # up-to-date" samajh ke True (success) bol deta tha — jabki asal mein
    # koi fetch hua hi nahi tha aur .gz kabhi update nahi hota tha. Ab
    # is_session_active() se live validity check karke hi aage badhte hain.
    # NOTE: update_cache=False — ye sirf fetch ke liye "token filhaal valid
    # hai ya nahi" jaanna chahta hai. Agar True yahan bhi global page-
    # routing cache mein likh diya jaata, to isi button ko dabane se poora
    # app "logged in" (chart mode) mein switch ho jaata — asal login-flow
    # (TOTP/manual) complete kiye bina hi. Wahi purana bug tha.
    if not is_session_active(update_cache=False):
        return False, "Fyers token expire ho chuka hai — sidebar se re-login karo, phir BankNifty Update Karo dabao."
    rows = _sv2_load_bn_gz()
    if not rows:
        return False, "Purana BankNifty data hi load nahi ho paaya — pehle wo theek karo."
    last_t_naive = max(r["t"] for r in rows)             # IST-naive stored value
    last_t_real  = last_t_naive - _GZ_APPEND_OFFSET       # real UTC epoch
    from_d = datetime.datetime.fromtimestamp(last_t_real, tz=IST).strftime("%Y-%m-%d")
    to_d   = _ist_now().strftime("%Y-%m-%d")
    try:
        # raise_on_error=True: agar Fyers API call hi fail ho (network/timeout/
        # non-ok response), yahan exception aayega — "koi naya candle nahi tha"
        # (genuine) se "fetch hi fail hua" (fault) ab mix nahi honge.
        candles = _fyers_history_chunk("5", from_d, to_d, raise_on_error=True)   # [ [epoch_sec, o,h,l,c,v], ... ]
        if not candles:
            return True, "Fyers se koi naya candle nahi mila (already latest, ya market band hai)."
        new_rows = []
        for c in candles:
            t_real = c[0] // 1000 if c[0] > 10_000_000_000 else c[0]  # ms→s agar zaroorat ho
            t_naive = t_real + _GZ_APPEND_OFFSET
            if t_naive <= last_t_naive:
                continue   # already maujood
            new_rows.append({"t": t_naive, "o": c[1], "h": c[2], "l": c[3], "c": c[4]})
        if not new_rows:
            return True, "BankNifty data already up-to-date hai."
        merged_map = {r["t"]: r for r in rows}
        for r in new_rows:
            merged_map[r["t"]] = r
        merged = sorted(merged_map.values(), key=lambda r: r["t"])
        _gz_save_local(_LOCAL_BN_GZ, merged)
        _SV2_CACHE["bn_raw"] = merged
        ok, msg = _gz_push_to_supabase(_LOCAL_BN_GZ, "banknifty_5m_csv_json.gz")
        return ok, f"BankNifty: {len(new_rows)} naye candles append hue. {msg}"
    except Exception as e:
        _slog_exception("BN_BTN_CLICK _append_new_bn_candles()", e)
        return False, f"BankNifty fetch/append fail — Fyers API call safal nahi hua: {e} (poori traceback debug block me EXCEPTION tag ke saath)"

# ─── Underlying stocks — poori (jitni available) daily history .gz ────────
# Index (NIFTYBANK-INDEX) nahi, balki individual stocks ka 1D candle data —
# ek hi .gz file mein, starting page se "File Banao" + "Download .gz" se.

# ── Nifty 500 symbol list: ab NSE se NAHI, Supabase se (_index.json) ──────
# NSE ki live CSV fetch hata di gayi (slow/unreliable — 15-20s tak lag jaata
# tha, kabhi-kabhi fail bhi hoti thi). Wahi "My 500 stock" Supabase bucket
# mein ek chhota _index.json file already maujood hai jisme har symbol ke
# per-symbol data-file ka mapping hai ({"symbol":"NSE:XXX-EQ","file":"..."}).
# Ye file already-loaded stocks ka accurate, single-fetch source hai — NSE
# jaisa alag network-dependency/retry-loop nahi chahiye.
_NIFTY500_INDEX_URL = f"{_SUPABASE_BASE}/_index.json"

@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_nifty500_symbols() -> list:
    """Nifty500 symbol list — Supabase '_index.json' se (ek hi chhota file,
    turant). 1hr cache hai taaki har rerun par dobara fetch na ho. Isse
    NSE dependency poori tarah hat gayi hai."""
    try:
        resp = requests.get(_NIFTY500_INDEX_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        symbols = [row["symbol"] for row in data if isinstance(row, dict) and row.get("symbol")]
        if len(symbols) >= 400:   # sanity check
            return symbols
    except Exception:
        pass
    return []



def _fyers_history_symbol_chunk(symbol: str, resolution: str, from_date: str, to_date: str) -> list:
    """_fyers_history_chunk jaisa hi hai, bas symbol parameterized hai — kisi
    bhi NSE:XXXX-EQ symbol (BankNifty stocks) ka history layega, index ke
    hardcoded NIFTYBANK-INDEX ki jagah."""
    creds = load_creds()
    if not creds.get("access_token"):
        return []
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol": symbol, "resolution": resolution,
        "date_format": "1", "range_from": from_date, "range_to": to_date, "cont_flag": "1",
    }
    try:
        resp = requests.get("https://api-t1.fyers.in/data/history",
                             headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        res = resp.json()
        if res.get("s") == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4], c[5]] for c in res.get("candles", [])]
        return []   # "no_data" (stock is period mein listed nahi tha) ya koi aur non-ok — dono skip
    except Exception:
        return []

def _normalize_daily_candles(all_candles: list) -> list:
    """Raw Fyers daily candles ([epoch_ms, o,h,l,c,v], ...) ko 9:15 AM IST
    ke consistent convention mein normalize karta hai (same jo
    _fetch_symbol_full_daily aur _append_new_bn_candles use karte hain)."""
    _IST_OFF, _NSE_OPEN = 19800, 33300
    normalized = []
    for c in all_candles:
        t_sec        = int(c[0]) // 1000
        ist_sec      = t_sec + _IST_OFF
        ist_midnight = ist_sec - (ist_sec % 86400)
        t_fixed      = (ist_midnight - _IST_OFF) + _NSE_OPEN
        normalized.append({"t": t_fixed, "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
    return normalized

def _fetch_symbol_full_daily(symbol: str, years: int = 30, max_workers: int = 3) -> list:
    """Ek stock ka poora available daily (1D) history — koi upper cap nahi,
    jitna bhi Fyers de utna poora (stock jitni purani listed hai utna hi
    milega; kam purani ho to kam hi aayega — koi error nahi).

    max_workers kam rakha gaya hai (aur caller symbols ke beech bhi thoda
    rukta hai) taaki Fyers ka per-second rate-limit na lage — pehle 6 workers
    x 14 symbols ek saath fire hone se end ke symbols ke liye Fyers silently
    empty/reject deta tha (list ke aakhri stocks ka data hi nahi aata tha)."""
    today = _ist_now()
    chunk_days  = 360   # Fyers daily-resolution max safe range per call
    start_limit = today - datetime.timedelta(days=years * 365)
    ranges = []
    end = today
    while end > start_limit:
        start = max(end - datetime.timedelta(days=chunk_days), start_limit)
        ranges.append((start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
        end = start - datetime.timedelta(days=1)

    all_candles: list = []
    seen: set = set()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(lambda r: _fyers_history_symbol_chunk(symbol, "D", r[0], r[1]), ranges))

    # Kuch chunks khali aa sakte hain sirf rate-limit ki wajah se (na ki
    # genuinely "stock is period mein listed nahi tha") — unhe alag se,
    # dheere-dheere (serial + delay) dobara try karo. Baaki asli-empty
    # (listing-se-pehle wale) chunks dobara try karne par bhi khali hi
    # aayenge, koi harm nahi.
    empty_idxs = [i for i, chunk in enumerate(results) if not chunk]
    for i in empty_idxs:
        time.sleep(0.8)
        retry_chunk = _fyers_history_symbol_chunk(symbol, "D", ranges[i][0], ranges[i][1])
        if retry_chunk:
            results[i] = retry_chunk

    for chunk in results:
        for c in chunk:
            if c[0] not in seen:
                seen.add(c[0])
                all_candles.append(c)
    all_candles.sort(key=lambda x: x[0])
    return _normalize_daily_candles(all_candles)

def _fetch_symbol_incremental_daily(symbol: str, from_date: str, to_date: str) -> list:
    """Ek symbol ka daily data sirf ek chhoti date-range ke liye (naye/missing
    candles ke liye) — poore-30-saal wale _fetch_symbol_full_daily se alag,
    ye sirf gap fill karta hai isliye bahut halka/fast hai. 360-din se badi
    range ho (jaise pehli baar ya lambe gap ke baad) to zaroorat padne par
    chunks mein tod deta hai."""
    start = datetime.datetime.strptime(from_date, "%Y-%m-%d").date()
    end   = datetime.datetime.strptime(to_date,   "%Y-%m-%d").date()
    if start > end:
        return []
    chunk_days = 360
    ranges = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + datetime.timedelta(days=chunk_days), end)
        ranges.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + datetime.timedelta(days=1)

    all_candles: list = []
    for r in ranges:
        all_candles.extend(_fyers_history_symbol_chunk(symbol, "D", r[0], r[1]))
        time.sleep(0.15)   # Fyers rate-limit ke liye halka sa gap
    return _normalize_daily_candles(all_candles)

def _nifty500_incremental_update(max_workers: int = 4) -> tuple[bool, str]:
    """Nifty500 ke SAARE stocks ka daily data — sirf MISSING/naye candles
    fetch karke Supabase (per-symbol JSON files) par push karta hai. Poora
    rebuild NAHI karta (bahut slow/rate-limit-heavy hota), sirf har symbol
    ke last-available-date ke baad ka gap fill hota hai.

    Naye symbols (jinke abhi Supabase par koi file hi nahi) ke liye poori
    30-saal history fetch hoti hai (_fetch_symbol_full_daily) — ye sirf
    tab hoga jab NSE ki live list mein koi bilkul naya stock aaya ho,
    isliye rare hai.

    Fyers login zaroori hai (BankNifty jaisa hi)."""
    if not is_session_active(update_cache=False):
        return False, "Fyers login/token valid nahi hai — Nifty500 auto-update skip."

    symbols = _fetch_nifty500_symbols()
    if not symbols:
        return False, "Nifty500 symbol list nahi mil paayi (Supabase _index.json) — auto-update skip."

    existing = _sv3_load_all_from_supabase()   # {symbol: rows}, already Supabase se
    today_str = _ist_now().strftime("%Y-%m-%d")

    def _one(sym):
        rows = existing.get(sym, []) or []
        try:
            if rows:
                last_t = max(r["t"] for r in rows)   # IST-naive epoch (real wall-clock IST as UTC)
                last_date = datetime.datetime.utcfromtimestamp(last_t).date()
                from_d = (last_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                if from_d > today_str:
                    return sym, None, "up-to-date"
                new_candles = _fetch_symbol_incremental_daily(sym, from_d, today_str)
            else:
                new_candles = _fetch_symbol_full_daily(sym, years=30)
            if not new_candles:
                return sym, None, "no-new"
            merged_map = {r["t"]: r for r in rows}
            for r in new_candles:
                merged_map[r["t"]] = r
            merged = sorted(merged_map.values(), key=lambda r: r["t"])
            return sym, merged, "updated"
        except Exception as e:
            return sym, None, f"error: {e}"

    updated, failed, errored = 0, 0, 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for sym, merged, status in ex.map(_one, symbols):
            if status == "updated" and merged is not None:
                ok, _msg = _supabase_upload(f"{sym.replace(':', '_')}.json",
                                             json.dumps(merged).encode("utf-8"),
                                             "application/json")
                if ok:
                    updated += 1
                else:
                    failed += 1
            elif status.startswith("error"):
                errored += 1

    # Underlying Supabase data badal gaya — in-memory/cached views ko force-refresh karo.
    try:
        _sv3_load_all_from_supabase.clear()
    except Exception:
        pass
    _SV3_BULK_CACHE["key"]  = None
    _SV3_BULK_CACHE["data"] = None
    _SV3_CACHE.clear()

    msg = (f"Nifty500 incremental update: {updated}/{len(symbols)} symbols updated, "
           f"{failed} upload-fail, {errored} fetch-error.")
    return True, msg

# REMOVED (user ka explicit ask — 'File Banao' feature permanently
# delete): _fmt_secs / _load_stocks_gz_dict / _next_batch_symbols /
# build_stocks_gz — sab sirf us hataye-gaye card ke liye the. Live
# chart data hamesha Supabase se aata hai (auto-updated background
# thread se), inki koi zaroorat nahi thi.

## ── IST-naive timestamp constants ───────────────────────────────────────────
## .gz data mein timestamps IST-naive hain: 9:15 IST ko 09:15 UTC ki tarah
## store kiya gaya hai. LightweightCharts real UTC chahta hai (IST timezone ke
## sath display karta hai: UTC + 5:30). Fix: har output time se 19800 subtract karo.
_IST_NAIVE_OFFSET = 19800   # 5.5 * 3600 — IST-naive → real UTC conversion
_SESSION_START    = 33300   # 9:15 IST = 9*3600 + 15*60 seconds from midnight
_SESSION_END      = 55800   # 15:30 IST = 15*3600 + 30*60 seconds from midnight


# ═══════════════════════ SECTION B: Resample / gap-fill utils ═══════════════
def _sv2_fill_bn_gaps(rows: list) -> list:
    """BN 5m raw data mein missing 5-min slots (no-trade gaps) forward-fill karo.

    Vendor ka 5m data kai jagah beech mein slots miss karta hai (illiquid /
    no-trade moments). Agar poore 125m/etc bucket ke saare 5m-slots missing
    hon, to us bucket ka candle hi resample output se gayab ho jaata hai —
    chart mein genuine "candles ke beech gap" dikhta hai. Fix: har trading
    session (9:15–15:29:XX IST, 375 minutes/day = 75 slots of 5m) ke liye
    poori 5-min sequence banao — jo slot missing ho use pichle available
    close se flat candle (o=h=l=c=prev_close) se bhar do. Isse koi bhi
    downstream resample bucket kabhi khaali nahi rahega.

    NOTE: raw .gz ab 5m granularity hai (pehle 1m thi) — isliye step 300
    seconds (5 min) hai, 60 seconds (1 min) nahi.
    """
    if not rows:
        return rows
    by_day: dict = {}
    for r in rows:
        t   = r["t"]
        mod = t % 86400
        if mod < _SESSION_START or mod >= _SESSION_END:
            continue
        day_start = t - mod
        by_day.setdefault(day_start, {})[t] = r

    out = []
    prev_close = None
    for day_start in sorted(by_day.keys()):
        day_rows = by_day[day_start]
        for sec_off in range(_SESSION_START, _SESSION_END, 300):
            t = day_start + sec_off
            if t in day_rows:
                r = day_rows[t]
                out.append(r)
                prev_close = r["c"]
            elif prev_close is not None:
                out.append({"t": t, "o": prev_close, "h": prev_close,
                            "l": prev_close, "c": prev_close})
            # agar dataset ke bilkul shuru mein hi pehla slot missing ho
            # (prev_close abhi None hai), to use silently skip karo — us
            # point tak koi reference close available hi nahi hai.
    return out

def _sv2_resample_bn_intraday(rows: list, tf_min: int) -> list:
    """BN 5m data ko intraday TF mein resample karo.

    .gz timestamps IST-naive hain (9:15 IST stored as 09:15 UTC epoch).
    Per-day anchor: har din 9:15 IST se bucket 0 start hota hai.
    Output timestamps real UTC mein (LightweightCharts + IST timezone ke liye).
    Session filter: sirf 9:15–15:30 IST ke candles.

    NOTE: raw .gz ab 5m granularity hai (pehle 1m thi). Isliye passthrough
    (bina bucketing) sirf tf_min<=5 par hota hai — 125m ab yahin se actual
    bucket-resample hoke banta hai (375-min session / 125m = 3 buckets/din).
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
    """BN 1m data ko daily / multi-day candles mein resample karo.

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
    """BTC 5m data ko UTC-anchored TF mein resample karo (24/7 crypto).

    NOTE: sirf 8H (intraday, tf_min < 1440) ke liye use karo (160m band kar
    diya gaya hai). Daily+
    (1D/3D/9D/27D) ke liye _sv2_resample_btc_daily() use karo — wo epoch
    (1970) anchor ki jagah data ke apne Day-1 se index-based chunking
    karta hai, jisse 3D/9D/27D hamesha same date se sync start hote hain.

    NOTE: raw .gz 5m granularity hai (BankNifty 1m par hai, BTC 5m par
    wapas revert kar diya gaya hai). Isliye passthrough (bina bucketing)
    tf_min<=5 par hota hai.
    """
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

def _sv2_resample_btc_daily(rows: list, n_days: int = 1) -> list:
    """BTC 5m data ko daily / multi-day candles mein resample karo.

    Crypto 24/7 hai (koi session/weekday filter nahi) — sirf UTC
    calendar-day buckets banao, phir un dailies ko INDEX se (BN ke
    _sv2_resample_bn_daily jaisa: array index-0 = data ka pehla din)
    groups of n_days mein chunk karo.

    Ye zaroori hai kyunki purana _sv2_resample_btc() epoch (1 Jan 1970)
    se seedha `(t // (n_days*86400)) * (n_days*86400)` karta tha — us
    approach mein 3D/9D/27D ke cycle-boundaries data-start (2017) se
    alag-alag remainder dete hain, isliye teeno TF alag-alag calendar
    dates se start hote the. Index-based chunking (yahan) sabko data ke
    Day-1 se hi sync rakhta hai — BN aur SV2 replay (_liveAggregateDailyPlus,
    jo already index-based hai) dono ke saath consistent.
    """
    day_buckets: dict = {}
    for r in rows:
        t = r["t"]
        day_start = (t // 86400) * 86400          # UTC calendar-day start
        if day_start not in day_buckets:
            day_buckets[day_start] = {"time": day_start,
                                       "open": r["o"], "high": r["h"],
                                       "low": r["l"], "close": r["c"]}
        else:
            b = day_buckets[day_start]
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

def _sv2_date_to_anchor_epoch(d) -> int:
    """Calendar date (datetime.date) ko wahi 'IST-naive-as-UTC' epoch scheme
    mein convert karo jisme SV2 .gz data ke timestamps stored hain (jaise
    9:15 IST ko 09:15 UTC ki tarah store kiya gaya hai — is file ke top
    comments dekho: _IST_NAIVE_OFFSET)."""
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp())

# ─── Mobile ke liye max candles per TF (chunked inject) ───────────────────────
# Ab BankNifty aur BTC ke liye ALAG-ALAG default hain (pehle "1D/3D/9D/27D"
# jaisi daily labels dono asset ke beech shared hoti thi — ab har asset ki
# apni settings hain). Ye "factory defaults" hain; asli effective values
# _sv2_get_max() se aati hain, jo isme user ke saved overrides (bottom-bar
# ke 📦 Chunk icon se set kiye gaye) merge karta hai.
_SV2_MAX_BN_DEFAULT = {
    "5m_raw": 12000,
}
_SV2_MAX_BTC_DEFAULT = {
    # 160m removed (no longer used); 8H/1D/3D/9D/27D are now FULL-HISTORY
    # (no chunking/trim) for BTC — see _build_sv2_data(). Only 5m_raw (used
    # for forming-candle interpolation smoothness) is still size-limited.
    "5m_raw": 64000,
}
# Safe min/max bounds per label — user chahe jitna bhi likhe, isi range mein
# clamp ho jaayega (mobile hang / bahut kam data dono se bachne ke liye).
_SV2_MAX_BOUNDS = {
    "5m_raw": (500, 120000),
}

SV2_CHUNK_SETTINGS_FILE = "sv2_chunk_settings.json"
SV2_LAST_DATE_FILE      = "sv2_last_dates.json"

def load_sv2_last_dates() -> dict:
    """Pichli baar starting-page pe select ki gayi chunk dates (asset-wise) —
    ye disk pe save hoti hain taaki naya browser session khulne par bhi
    date-input pehle se usi date pe fill mile (load abhi bhi automatic
    NAHI hota, button dabana zaroori hai — sirf field pre-filled rehti hai)."""
    if os.path.exists(SV2_LAST_DATE_FILE):
        try:
            with open(SV2_LAST_DATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sv2_last_dates(d: dict):
    with open(SV2_LAST_DATE_FILE, "w") as f:
        json.dump(d, f)

def _sv2_default_date(asset: str):
    """Date-input widget ka default value: is session mein already select
    kiya ho to wahi, warna last-saved (disk se), warna aaj ki date."""
    sess_key = f"_sv2_anchor_date_{asset}"
    if st.session_state.get(sess_key):
        return st.session_state[sess_key]
    _saved = load_sv2_last_dates().get(asset)
    if _saved:
        try:
            return datetime.date.fromisoformat(_saved)
        except Exception:
            pass
    return datetime.date.today()

def _sv2_remember_dates(bn_date, btc_date):
    """Har rerun pe current widget selection ko disk pe likh do — isse agli
    baar (naya session/browser refresh) pe bhi wahi date pehle se fill milti
    hai, chahe 'Chart Kholo' button dabaya ho ya nahi."""
    save_sv2_last_dates({"bn": str(bn_date), "btc": str(btc_date)})

def load_sv2_chunk_settings() -> dict:
    """Saved overrides load karo: {"bn": {...}, "btc": {...}}."""
    if os.path.exists(SV2_CHUNK_SETTINGS_FILE):
        try:
            with open(SV2_CHUNK_SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sv2_chunk_settings(d: dict):
    with open(SV2_CHUNK_SETTINGS_FILE, "w") as f:
        json.dump(d, f)

def _sv2_get_max(asset: str) -> dict:
    """Asset ('bn'/'btc') ki effective per-TF candle-count limits: factory
    default + user ke saved overrides (clamped to _SV2_MAX_BOUNDS). Session
    ke andar cache hota hai taaki har rerun pe file dobara na padhni pade."""
    cache_key = f"_sv2_max_eff_{asset}"
    if cache_key not in st.session_state:
        default = _SV2_MAX_BN_DEFAULT if asset == "bn" else _SV2_MAX_BTC_DEFAULT
        saved   = load_sv2_chunk_settings().get(asset, {})
        eff = dict(default)
        for k, v in saved.items():
            if k in eff:
                try:
                    lo, hi = _SV2_MAX_BOUNDS.get(k, (10, 200000))
                    eff[k] = max(lo, min(hi, int(v)))
                except Exception:
                    pass
        st.session_state[cache_key] = eff
    return st.session_state[cache_key]

def _sv2_trim(data: list, label: str, anchor_epoch: int = None, asset: str = "bn") -> list:
    """Chunk select karo (mobile hang prevention, per-asset per-TF limits).

    - anchor_epoch None  → purana default: sirf LAST N candles (recent data).
    - anchor_epoch diya  → us date ke aas-paas se window nikaalo: thoda
      history anchor se PEHLE ka (context ke liye) aur baaki (zyada hissa)
      anchor ke BAAD ka (kyunki replay ko aage badhne ke liye future candles
      chahiye) — total count wahi N jo _sv2_get_max(asset) se aata hai.
    """
    n = _sv2_get_max(asset).get(label, 2000)
    if len(data) <= n:
        return data
    if anchor_epoch is None:
        return data[-n:]
    # Binary search: pehla index jahan bar['time'] >= anchor_epoch
    lo, hi = 0, len(data)
    while lo < hi:
        mid = (lo + hi) // 2
        if data[mid]["time"] < anchor_epoch:
            lo = mid + 1
        else:
            hi = mid
    idx    = lo
    before = n // 4                       # ~25% budget anchor se pehle
    start  = max(0, idx - before)
    end    = min(len(data), start + n)
    start  = max(0, end - n)              # series ke end tak clip ho to peeche khiskao
    return data[start:end]

def _sv2_to_js(data: list) -> str:
    """List of dicts → compact JSON string for inline JS."""
    return json.dumps(data, separators=(",", ":"))

# ─── Stack View 3: Nifty 500 stock — long-term MONTHLY replay ─────────────
# SV2 (BankNifty/BTC, days-based: 1D→3D→9D→27D) ka hi index-chunk resample
# pattern hai, bas ek DYNAMIC stock symbol par aur MONTHS ki unit mein:
# 1D (already nifty500 .gz mein hai) → calendar-month buckets (1M, internal
# reference table — user ko TF ke roop mein nahi dikhta) → phir index-chunk
# karke 3M/9M/27M/81M/243M (SV2 ke 1D→3D→9D→27D jaisa hi, Month-1 se hamesha
# sync, calendar-quarter/financial-year se independent).
SV3_LAST_SYMBOL_FILE = "sv3_last_symbol.json"
_SV3_CACHE: dict = {}   # single-slot in-memory cache: {"symbol":…, "data":{…}}
SV3_TF_MONTHS = {"3M": 3, "9M": 9, "27M": 27, "81M": 81, "243M": 243}

def _sv3_month_key(t: int):
    """IST-naive epoch (jaise nifty500 .gz mein store hota hai) → (year,
    month) tuple — calendar-month bucket ki pehchan. Baaki .gz code (jaise
    upar _sv2_resample_btc_daily) jis IST-naive convention se calendar-din
    nikalta hai, wahi utcfromtimestamp() yahan bhi use kiya hai."""
    dt = datetime.datetime.utcfromtimestamp(t)
    return (dt.year, dt.month)

def _sv3_bucket_monthly(rows: list) -> list:
    """Raw 1D rows ({'t','o','h','l','c'} short-keys, nifty500 .gz format) ko
    calendar-month candles mein group karta hai — LWC {'time','open','high',
    'low','close'} long-keys output (jaisa _sv2_resample_btc_daily upar karta
    hai). Ek mahine ke saare trading days mil kar ek hi "1M" candle ban jaate
    hain (open=mahine ka pehla open, close=mahine ka last close, high/low=
    mahine ka max/min)."""
    month_buckets: dict = {}
    order: list = []
    for r in rows:
        try:
            t = int(r["t"]); o = float(r["o"]); h = float(r["h"])
            l = float(r["l"]); c = float(r["c"])
        except Exception:
            continue
        key = _sv3_month_key(t)
        if key not in month_buckets:
            month_buckets[key] = {"time": t, "open": o, "high": h, "low": l, "close": c}
            order.append(key)
        else:
            b = month_buckets[key]
            b["high"]  = max(b["high"], h)
            b["low"]   = min(b["low"],  l)
            b["close"] = c
    return [month_buckets[k] for k in order]

def _sv3_resample_monthly(monthly: list, n_months: int) -> list:
    """Pehle-se-bucketed 1M candles (_sv3_bucket_monthly ka output) ko
    groups of n_months mein INDEX-chunk karta hai — bilkul SV2 ke
    1D→3D/9D/27D wale index-chunk jaisa (Month-1 se hamesha sync, calendar
    quarter/financial-year se independent)."""
    if n_months <= 1:
        return monthly
    out = []
    for i in range(0, len(monthly), n_months):
        chunk = monthly[i:i + n_months]
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

def _sv3_daily_to_lwc(rows: list) -> list:
    """Raw nifty500 .gz 1D rows (short-keys) → LWC long-keys, bina resample
    kiye — ye khud base/reveal series banti hai (SV2_BN['1D'] jaisa role,
    replay isi se ek-ek din reveal hoti hai)."""
    out = []
    for r in rows:
        try:
            out.append({
                "time":  int(r["t"]),   "open": float(r["o"]), "high": float(r["h"]),
                "low":   float(r["l"]), "close": float(r["c"]),
            })
        except Exception:
            continue
    return out

def _sv3_supabase_symbol_url(sym: str) -> str:
    """Per-symbol JSON file URL — SAME naming convention jo chart.html
    (browser JS, _sv3SupabaseFileUrl) already use karta hai:
    'NSE:ABDL-EQ' → 'NSE_ABDL-EQ.json', urlencoded."""
    fname = sym.replace(":", "_") + ".json"
    return f"{_SUPABASE_BASE}/{quote(fname)}"

def _sv3_fetch_symbol_from_supabase(sym: str) -> list:
    """Ek symbol ka daily raw rows seedha Supabase se (plain JSON file, gzip
    nahi — SV2 .gz files se alag format). Fail ho to khaali list, koi
    fallback nahi."""
    try:
        resp = requests.get(_sv3_supabase_symbol_url(sym), timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data.get("data", []) or []
        return data if isinstance(data, list) else []
    except Exception:
        return []

@st.cache_resource(show_spinner=False, ttl=300)
def _sv3_load_all_from_supabase() -> dict:
    """SAARE Nifty500 symbols ka daily data — SIRF Supabase (per-symbol JSON
    files) se, concurrently fetch karke {symbol: rows} dict banata hai. Koi
    local .gz ya HF Space read yahan nahi hoti — permanently hataya gaya.
    5-min TTL cache (st.cache_resource) taaki har rerun par 500 network
    calls na hon; TTL khatam hone par khud-ba-khud refresh ho jaata hai."""
    symbols = _fetch_nifty500_symbols()
    if not symbols:
        _slog("SV3 Supabase bulk-load: Nifty500 symbol list (_index.json) nahi mili.", level="err")
        return {}

    def _one(sym):
        return sym, _sv3_fetch_symbol_from_supabase(sym)

    out: dict = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        for sym, rows in ex.map(_one, symbols):
            if rows:
                out[sym] = rows
    _slog(f"SV3 Supabase bulk-load: {len(out)}/{len(symbols)} symbols mile.",
          level="ok" if out else "err")
    return out

def _sv3_symbol_list() -> list:
    """Top-left picker ki symbol-name list — CHANGED: ab ye Nifty500 ki
    live NSE list (_fetch_nifty500_symbols, 1hr-cached, sirf naam, koi price
    data nahi) se aati hai, NA ki Supabase se saare 500 stocks ka data load
    karke. Pehle ye function _sv3_load_all_from_supabase() call karta tha —
    matlab sirf DROPDOWN mein naam dikhane ke liye bhi 500 individual
    Supabase JSON-file requests ho jaate the. Ab koi bhi Supabase data-call
    nahi hoti jab tak user khud koi symbol select na kare.

    BTC_1D_SYMBOL yahan explicitly PINNED hai — Nifty500 ke _index.json
    (_fetch_nifty500_symbols) mein NAHI hai, isliye list ke aage manually
    add kiya jaata hai taaki SV3 symbol-search picker mein bhi dikhe. BTC
    ka data/update pipeline (_btc_sv3_*, Binance-based) Nifty500 ke Fyers-
    based pipeline se poori tarah independent hai."""
    try:
        syms = sorted(_fetch_nifty500_symbols())
    except Exception:
        syms = []
    return [BTC_1D_SYMBOL] + syms

def _build_sv3_data(symbol: str) -> dict:
    """Ek symbol ke liye 1D (base) + 1M (internal reference, UI mein nahi
    dikhta) + 3M/9M/27M/81M/243M (visible TFs) — sab ek saath resample karke
    return karta hai. Same symbol dobara maange to single-slot cache se
    turant milta hai (rerun-safe, Streamlit ke baar-baar top-se-bottom
    re-execute hone par bhi dobara compute nahi hota)."""
    if _SV3_CACHE.get("symbol") == symbol and _SV3_CACHE.get("data"):
        return _SV3_CACHE["data"]
    # CHANGED: pehle yahan _sv3_load_all_from_supabase() se SAARE 500
    # stocks ka data khinch kar phir isme se ek symbol nikaala jaata tha —
    # matlab ek symbol select karne par bhi 500 network calls ho jaate
    # the. Ab seedha sirf isi symbol ki Supabase file fetch hoti hai
    # (single request), baaki 499 symbols ko bilkul touch nahi kiya jaata.
    raw_rows = _sv3_fetch_symbol_from_supabase(symbol) or []
    monthly  = _sv3_bucket_monthly(raw_rows)
    out = {"1D": _sv3_daily_to_lwc(raw_rows), "1M": monthly}
    for label, n in SV3_TF_MONTHS.items():
        out[label] = _sv3_resample_monthly(monthly, n)
    _SV3_CACHE["symbol"] = symbol
    _SV3_CACHE["data"]   = out
    return out

# ─── SV3 bulk (ALL stocks at once) — "Long Term Replay" instant-switch ────────
_SV3_BULK_CACHE: dict = {"key": None, "data": None}

def _build_sv3_bulk_all_stocks() -> dict:
    """Supabase se load hue SAARE symbols ke liye SIRF visible full-history
    timeframes (3M/9M/27M/81M/243M) resample karke ek dict return karta hai:
    {symbol: {"3M":[...], "9M":[...], ...}}.

    IMPORTANT: 1D (raw daily) yahan JAAN-BOOJH KAR shamil NAHI hai. 500
    stocks * ~30 saal ka daily data ek saath JSON mein bhejna 100+ MB ban
    jaata — browser ke liye impractical/slow. Isliye:
      - Poori history ke 5 timeframes (3M...243M) sabhi stocks ke liye
        instant switch ho jaate hain (chhota payload, few MB).
      - Din-ba-din REPLAY (jisko 1D chahiye) us particular stock ko pehli
        baar "Start/Resume" dabane par normal (chhota, single-symbol)
        fresh-load se milta hai — jaisa pehle se hota tha.

    Underlying Supabase data ki symbol-count ke hisaab se in-memory cache
    hota hai (underlying data khud 5-min TTL par refresh hoti hai — dekho
    _sv3_load_all_from_supabase), taaki har API-call par saare symbols
    dobara resample na karne padein."""
    stocks = _sv3_load_all_from_supabase()
    key = len(stocks)
    if _SV3_BULK_CACHE["key"] == key and _SV3_BULK_CACHE["data"] is not None:
        return _SV3_BULK_CACHE["data"]

    out: dict = {}
    for sym, raw_rows in stocks.items():
        if not raw_rows:
            continue
        try:
            monthly = _sv3_bucket_monthly(raw_rows)
            sym_out = {}
            for label, n in SV3_TF_MONTHS.items():
                sym_out[label] = _sv3_resample_monthly(monthly, n)
            out[sym] = sym_out
        except Exception:
            continue
    _SV3_BULK_CACHE["key"]  = key
    _SV3_BULK_CACHE["data"] = out
    return out

def _sv3_to_js(data: list) -> str:
    return json.dumps(data, separators=(",", ":"))

def load_sv3_last_symbol() -> str:
    """Pichli baar select kiya gaya symbol — disk se (naya browser session
    mein bhi wahi symbol default mile)."""
    if os.path.exists(SV3_LAST_SYMBOL_FILE):
        try:
            with open(SV3_LAST_SYMBOL_FILE) as f:
                return (json.load(f) or {}).get("symbol", "") or ""
        except Exception:
            pass
    return ""

def save_sv3_last_symbol(symbol: str):
    try:
        with open(SV3_LAST_SYMBOL_FILE, "w") as f:
            json.dump({"symbol": symbol}, f)
    except Exception:
        pass

def _build_sv2_data(bn_anchor: int = None, btc_anchor: int = None) -> dict:
    """Dono .gz files se sab TFs ka data return karo, ek diye gaye date
    ("chunk") ke aas-paas trim karke.

    IMPORTANT: fetch + resample (GitHub se .gz download + TF resampling)
    sirf EK BAAR hota hai process/session mein pehli dafa (result
    _SV2_CACHE["bn_tfs_full"] / ["btc_tfs_full"] mein cache hota hai — full,
    untrimmed). Uske baad, chahe user koi bhi naya "chunk date" chune, sirf
    halka-sa trim step (_sv2_trim) dobara chalta hai — GitHub fetch ya
    resample dobara NAHI hota.
    """
    if "bn_tfs_full" not in _SV2_CACHE or "btc_tfs_full" not in _SV2_CACHE:
        bn_raw  = _sv2_fill_bn_gaps(_sv2_load_bn_gz())
        btc_raw = _sv2_load_btc_gz()

        _SV2_CACHE["bn_tfs_full"] = {
            "5m_raw": _sv2_resample_bn_intraday(bn_raw, 5),
            "125m": _sv2_resample_bn_intraday(bn_raw,  125),
            "1D":   _sv2_resample_bn_daily   (bn_raw,  1),
            "3D":   _sv2_resample_bn_daily   (bn_raw,  3),
            "9D":   _sv2_resample_bn_daily   (bn_raw,  9),
            "27D":  _sv2_resample_bn_daily   (bn_raw,  27),
        }
        _SV2_CACHE["btc_tfs_full"] = {
            "5m_raw": _sv2_resample_btc(btc_raw, 5),
            "8H":   _sv2_resample_btc(btc_raw, 480),
            "1D":   _sv2_resample_btc_daily(btc_raw, 1),
            "3D":   _sv2_resample_btc_daily(btc_raw, 3),
            "9D":   _sv2_resample_btc_daily(btc_raw, 9),
            "27D":  _sv2_resample_btc_daily(btc_raw, 27),
        }

    bn_tfs  = _SV2_CACHE["bn_tfs_full"]
    btc_tfs = _SV2_CACHE["btc_tfs_full"]
    # Ab BN aur BTC dono ke liye SAME pattern: sirf "5m_raw" (forming-candle
    # interpolation ke liye) trimmed/chunked rehta hai; baaki saare TFs
    # (125m/1D/3D/9D/27D for BN, 8H/1D/3D/9D/27D for BTC) ab FULL-HISTORY
    # untrimmed jaate hain — koi bar-replay chunk-limit nahi (bade TFs hain,
    # candle-count kam hoti hai, phone hang nahi karta).
    agg = {
        "bn":  {k: (_sv2_trim(v, k, bn_anchor, "bn") if k == "5m_raw" else v)
                for k, v in bn_tfs.items()},
        "btc": {k: (_sv2_trim(v, k, btc_anchor, "btc") if k == "5m_raw" else v)
                for k, v in btc_tfs.items()},
    }
    return agg

# ─── Fyers historical data ─────────────────────────────────────────────────────
