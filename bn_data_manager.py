"""
BankNifty Historical Data Manager
==================================
• CSV (64MB) → binary gzip (12MB) converter
• Auto-update: Fyers se daily new candles append karo
• 4 PM ke baad ya app open par update hota hai

Binary Format:
  Header: magic(4B) + version(1B) + count(4B) + base_ts(4B) = 13 bytes
  Candle: ts_offset(4B uint32) + O,H,L,C (4x 4B float32) = 20 bytes/candle
  File saved as gzip compressed .bin.gz
"""

import struct, gzip, io, csv, datetime, os, time, logging
import requests

log = logging.getLogger(__name__)

MAGIC    = b'BN1M'
VERSION  = 1
BIN_FILE = os.path.join(os.path.dirname(__file__), "bn_1m.bin.gz")
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

# ─── GitHub Raw URL Config ────────────────────────────────────────────────────
GITHUB_URL = "https://raw.githubusercontent.com/krishna123814/My-engine/main/bn_1m.bin.gz"


def download_from_github(url: str = "", dest: str = "") -> bool:
    """
    GitHub raw URL se bn_1m.bin.gz download karo agar local file nahi hai.
    url:  GitHub raw URL (default: GITHUB_URL)
    dest: local save path (default: BIN_FILE)
    Returns True if download successful.
    """
    src  = url or GITHUB_URL
    path = dest or BIN_FILE

    if os.path.exists(path):
        log.info("bn_1m.bin.gz already exists locally — skip download")
        return True

    log.info(f"GitHub se download ho raha hai... ({src})")

    try:
        resp = requests.get(src, stream=True, timeout=120)

        if resp.status_code != 200:
            log.error(f"GitHub download failed: HTTP {resp.status_code}")
            return False

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        total = 0
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                if chunk:
                    f.write(chunk)
                    total += len(chunk)

        size_mb = total / 1024 / 1024
        log.info(f"✅ Downloaded {size_mb:.1f} MB → {path}")
        print(f"✅ GitHub se download complete: {size_mb:.1f} MB")
        return True

    except Exception as e:
        log.error(f"GitHub download error: {e}")
        if os.path.exists(path):
            os.remove(path)  # Incomplete file delete karo
        return False


def check_github_update(url: str = "") -> dict:
    """
    GitHub par file ka Last-Modified header check karo aur local file se compare karo.

    Returns dict:
      {
        "status": "up_to_date" | "outdated" | "no_local" | "error" | "checking",
        "github_modified": "RFC-date string or None",
        "local_modified":  "datetime string or None",
        "error":           "error message or None",
        "needs_update":    True/False,
      }
    """
    src  = url or GITHUB_URL
    result = {
        "status":          "checking",
        "github_modified": None,
        "local_modified":  None,
        "error":           None,
        "needs_update":    False,
    }

    # Local file exist karta hai?
    if not os.path.exists(BIN_FILE):
        result["status"]       = "no_local"
        result["needs_update"] = True
        return result

    local_mtime = os.path.getmtime(BIN_FILE)
    local_dt    = datetime.datetime.utcfromtimestamp(local_mtime)
    result["local_modified"] = local_dt.strftime("%Y-%m-%d %H:%M UTC")

    # GitHub se HEAD request bhejo (sirf headers, no body download)
    try:
        resp = requests.head(src, timeout=15, allow_redirects=True)

        if resp.status_code == 404:
            result["status"] = "error"
            result["error"]  = f"GitHub file nahi mili (404). URL check karo:\n{src}"
            return result

        if resp.status_code != 200:
            result["status"] = "error"
            result["error"]  = f"GitHub ne HTTP {resp.status_code} diya. Thodi der baad try karo."
            return result

        last_modified_str = resp.headers.get("Last-Modified") or resp.headers.get("last-modified")

        if not last_modified_str:
            # GitHub raw CDN kabhi kabhi Last-Modified nahi bhejta — ETag try karo
            etag = resp.headers.get("ETag") or resp.headers.get("etag") or ""
            result["status"]          = "error"
            result["error"]           = (
                "GitHub ne Last-Modified header nahi bheja (CDN caching issue).\n"
                f"ETag: {etag if etag else 'N/A'}\n"
                "Kuch der baad retry karo ya manually 'Force Download' karo."
            )
            return result

        # Parse RFC-2616 date: "Mon, 01 Jan 2024 12:00:00 GMT"
        import email.utils
        github_dt_tuple = email.utils.parsedate(last_modified_str)
        if github_dt_tuple is None:
            result["status"] = "error"
            result["error"]  = f"GitHub ka date parse nahi hua: '{last_modified_str}'"
            return result

        github_dt = datetime.datetime(*github_dt_tuple[:6])  # naive UTC
        result["github_modified"] = github_dt.strftime("%Y-%m-%d %H:%M UTC")

        # Compare — 60-sec tolerance (CDN rounding)
        diff_secs = (github_dt - local_dt).total_seconds()

        if diff_secs > 60:
            result["status"]       = "outdated"
            result["needs_update"] = True
        else:
            result["status"]       = "up_to_date"
            result["needs_update"] = False

    except requests.exceptions.ConnectionError:
        result["status"] = "error"
        result["error"]  = "Internet connection nahi hai ya GitHub reachable nahi. Network check karo."
    except requests.exceptions.Timeout:
        result["status"] = "error"
        result["error"]  = "GitHub response timeout (15 sec). Thodi der baad retry karo."
    except Exception as e:
        result["status"] = "error"
        result["error"]  = f"Unexpected error: {e}"

    return result


def force_download_from_github(url: str = "", dest: str = "") -> dict:
    """
    GitHub se forcefully download karo — local file exist kare ya na kare.
    Returns: {"ok": bool, "size_mb": float, "error": str or None}
    """
    src  = url or GITHUB_URL
    path = dest or BIN_FILE

    log.info(f"Force download from GitHub: {src}")
    try:
        resp = requests.get(src, stream=True, timeout=120)

        if resp.status_code != 200:
            return {"ok": False, "size_mb": 0, "error": f"HTTP {resp.status_code} — GitHub URL sahi hai?"}

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp_path = path + ".tmp"
        total = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)

        if total < 1024:  # < 1 KB = something wrong
            os.remove(tmp_path)
            return {"ok": False, "size_mb": 0, "error": "Downloaded file bahut chhoti hai — corrupt ya wrong URL."}

        # Atomic replace
        import shutil
        shutil.move(tmp_path, path)

        size_mb = total / 1024 / 1024
        log.info(f"✅ Force downloaded {size_mb:.1f} MB → {path}")
        return {"ok": True, "size_mb": round(size_mb, 1), "error": None}

    except requests.exceptions.Timeout:
        return {"ok": False, "size_mb": 0, "error": "Download timeout — file bahut badi hai ya connection slow hai. Retry karo."}
    except Exception as e:
        if os.path.exists(path + ".tmp"):
            try: os.remove(path + ".tmp")
            except: pass
        return {"ok": False, "size_mb": 0, "error": f"Download error: {e}"}


def ensure_bin_file() -> bool:
    """
    Replay/chart mode ke liye: local file check karo, nahi hai to GitHub se lao.
    Returns True if file available hai.
    """
    if os.path.exists(BIN_FILE):
        return True
    log.info("Local bn_1m.bin.gz nahi mili — GitHub se try kar raha hai...")
    return download_from_github()


# ─── Core read/write ──────────────────────────────────────────────────────────

def _ist_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + IST_OFFSET


def _encode(rows: list) -> bytes:
    """rows = list of (ts_unix_sec, open, high, low, close)"""
    if not rows:
        return b''
    base_ts = rows[0][0]
    buf = io.BytesIO()
    buf.write(MAGIC)
    buf.write(struct.pack('B', VERSION))
    buf.write(struct.pack('>I', len(rows)))
    buf.write(struct.pack('>I', base_ts))
    for ts, o, h, l, c in rows:
        buf.write(struct.pack('>I', ts - base_ts))
        buf.write(struct.pack('>ffff', o, h, l, c))
    return gzip.compress(buf.getvalue(), compresslevel=9)


def _decode(data: bytes) -> list:
    """Returns list of [ts_ms, open, high, low, close]  (ts in milliseconds for LWC)"""
    raw = gzip.decompress(data)
    if raw[:4] != MAGIC:
        raise ValueError("Invalid magic bytes")
    version = raw[4]
    count   = struct.unpack('>I', raw[5:9])[0]
    base_ts = struct.unpack('>I', raw[9:13])[0]
    rows = []
    offset = 13
    for _ in range(count):
        ts_off      = struct.unpack('>I', raw[offset:offset+4])[0]
        o, h, l, c  = struct.unpack('>ffff', raw[offset+4:offset+20])
        rows.append([( base_ts + ts_off ) * 1000, o, h, l, c])
        offset += 20
    return rows


def load_bin(auto_download: bool = True) -> list:
    """
    Load from .bin.gz → [[ts_ms, O, H, L, C], ...]
    auto_download=True: local file nahi hai to GitHub se download karo.
    """
    if not os.path.exists(BIN_FILE):
        if auto_download:
            log.info("bn_1m.bin.gz nahi mili — GitHub se download try...")
            download_from_github()
        if not os.path.exists(BIN_FILE):
            return []
    with open(BIN_FILE, 'rb') as f:
        return _decode(f.read())


def save_bin(rows: list):
    """Save [[ts_ms, O, H, L, C], ...] to .bin.gz"""
    # convert ms → seconds for encoding
    sec_rows = [(int(r[0]//1000), r[1], r[2], r[3], r[4]) for r in rows]
    data = _encode(sec_rows)
    with open(BIN_FILE, 'wb') as f:
        f.write(data)
    log.info(f"Saved {len(rows)} candles → {len(data)/1024:.0f} KB")


# ─── CSV Import (one-time) ────────────────────────────────────────────────────

def csv_to_bin(csv_path: str) -> str:
    """
    CSV (DD-MM-YYYY, H:MM:SS, IST) → .bin.gz
    Returns path to saved file.
    """
    rows = []
    with open(csv_path, 'r') as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for r in reader:
            date = r[1].strip(); tm = r[2].strip()
            dd, mm, yyyy = date.split('-')
            h, mi, s = tm.split(':')
            dt_ist = datetime.datetime(int(yyyy), int(mm), int(dd),
                                       int(h), int(mi), int(s))
            ts = int((dt_ist - IST_OFFSET -
                       datetime.datetime(1970, 1, 1)).total_seconds())
            o  = float(r[3]); h2 = float(r[4])
            l  = float(r[5]); c  = float(r[6])
            rows.append((ts, o, h2, l, c))

    data = _encode(rows)
    with open(BIN_FILE, 'wb') as f:
        f.write(data)
    print(f"✅ Converted {len(rows):,} candles → {len(data)/1024/1024:.2f} MB → {BIN_FILE}")
    return BIN_FILE


# ─── Fyers Fetch & Append ─────────────────────────────────────────────────────

def _fyers_fetch_range(app_id: str, access_token: str,
                        from_date: str, to_date: str) -> list:
    """
    Returns [[ts_ms, O, H, L, C], ...] from Fyers for given range.
    from_date / to_date: 'YYYY-MM-DD'
    """
    headers = {"Authorization": f"{app_id}:{access_token}"}
    params  = {
        "symbol": "NSE:NIFTYBANK-INDEX",
        "resolution": "1",
        "date_format": "1",
        "range_from": from_date,
        "range_to":   to_date,
        "cont_flag":  "1",
    }
    try:
        res = requests.get("https://api-t1.fyers.in/data/history",
                           headers=headers, params=params, timeout=20).json()
        if res.get("s") == "ok":
            return [[c[0]*1000, c[1], c[2], c[3], c[4]] for c in res.get("candles", [])]
    except Exception as e:
        log.warning(f"Fyers fetch failed: {e}")
    return []


def update_from_fyers(app_id: str, access_token: str,
                       force: bool = False) -> dict:
    """
    Auto-update logic:
    • Load existing .bin.gz
    • Find last candle timestamp
    • Fetch missing days from Fyers (in 59-day chunks max)
    • Append deduplicated candles
    • Save back

    Returns: {'added': N, 'total': M, 'last_date': 'YYYY-MM-DD', 'skipped': bool}
    """
    now_ist = _ist_now()

    # ── Market hours check: only update after 3:35 PM IST (15 min after close) ──
    market_close = now_ist.replace(hour=15, minute=35, second=0, microsecond=0)
    if not force and now_ist < market_close:
        log.info("Market not closed yet, skip update")
        return {"added": 0, "total": 0, "last_date": "", "skipped": True}

    # ── Load existing data ────────────────────────────────────────────────────
    existing = load_bin()
    if existing:
        last_ts_ms = existing[-1][0]
        last_dt_ist = (datetime.datetime.utcfromtimestamp(last_ts_ms // 1000)
                       + IST_OFFSET)
        from_date = (last_dt_ist + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        from_date = "2015-01-01"

    today_str = now_ist.strftime("%Y-%m-%d")

    if from_date > today_str:
        log.info("Data already up to date")
        return {"added": 0, "total": len(existing),
                "last_date": today_str, "skipped": True}

    # ── Fetch in 59-day chunks (Fyers limit) ─────────────────────────────────
    new_candles = []
    from_dt = datetime.datetime.strptime(from_date, "%Y-%m-%d")
    to_dt   = datetime.datetime.strptime(today_str, "%Y-%m-%d")

    chunk_start = from_dt
    while chunk_start <= to_dt:
        chunk_end = min(chunk_start + datetime.timedelta(days=58), to_dt)
        chunk = _fyers_fetch_range(
            app_id, access_token,
            chunk_start.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d")
        )
        new_candles.extend(chunk)
        log.info(f"Fetched {len(chunk)} candles: "
                 f"{chunk_start.date()} → {chunk_end.date()}")
        chunk_start = chunk_end + datetime.timedelta(days=1)
        time.sleep(0.2)  # rate limit

    if not new_candles:
        return {"added": 0, "total": len(existing),
                "last_date": today_str, "skipped": False}

    # ── Deduplicate & sort ────────────────────────────────────────────────────
    existing_ts = {r[0] for r in existing}
    fresh = [r for r in new_candles if r[0] not in existing_ts]
    all_rows = sorted(existing + fresh, key=lambda x: x[0])

    save_bin(all_rows)
    log.info(f"Updated: +{len(fresh)} candles → {len(all_rows):,} total")

    return {
        "added":     len(fresh),
        "total":     len(all_rows),
        "last_date": today_str,
        "skipped":   False
    }


# ─── Stat helper (for display) ────────────────────────────────────────────────

def get_stats() -> dict:
    """Return info about current .bin.gz without loading all data."""
    if not os.path.exists(BIN_FILE):
        return {"exists": False}
    size_mb = os.path.getsize(BIN_FILE) / 1024 / 1024
    rows = load_bin()
    if not rows:
        return {"exists": True, "size_mb": size_mb, "count": 0}
    first = datetime.datetime.utcfromtimestamp(rows[0][0]  // 1000) + IST_OFFSET
    last  = datetime.datetime.utcfromtimestamp(rows[-1][0] // 1000) + IST_OFFSET
    return {
        "exists":   True,
        "size_mb":  round(size_mb, 2),
        "count":    len(rows),
        "first":    first.strftime("%Y-%m-%d %H:%M IST"),
        "last":     last.strftime("%Y-%m-%d %H:%M IST"),
    }


# ─── Auto-update on app open ─────────────────────────────────────────────────
import threading as _threading

_AUTO_UPDATE_LOCK   = _threading.Lock()
_AUTO_UPDATE_STATUS = {
    "running":    False,
    "last_check": 0.0,
    "last_result": None,   # dict from update_from_fyers or None
}

def _auto_update_worker(app_id: str, access_token: str) -> None:
    """Background thread: app open hone par latest Fyers data fetch karo."""
    try:
        now_ist = _ist_now()

        # ── Step 1: local file nahi hai to GitHub se download karo ───────────
        if not os.path.exists(BIN_FILE):
            log.info("[AutoUpdate] Local file missing — GitHub se download...")
            download_from_github()

        # ── Step 2: file ki last candle date check karo ───────────────────────
        existing = load_bin()
        if existing:
            last_ts_ms  = existing[-1][0]
            last_dt_ist = (datetime.datetime.utcfromtimestamp(last_ts_ms / 1000)
                           + IST_OFFSET)
            last_date   = last_dt_ist.date()
        else:
            last_date = datetime.date(2015, 1, 1)

        today = now_ist.date()

        if last_date >= today:
            msg = f"[AutoUpdate] Already up-to-date till {last_date} — skip"
            log.info(msg)
            with _AUTO_UPDATE_LOCK:
                _AUTO_UPDATE_STATUS["last_result"] = {
                    "added": 0, "skipped": True, "last_date": str(last_date)
                }
            return

        # ── Step 3: missing days fetch karo (Fyers, 59-day chunks) ───────────
        from_date = (last_dt_ist + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        to_date   = today.strftime("%Y-%m-%d")
        log.info(f"[AutoUpdate] Fetching {from_date} → {to_date} from Fyers...")

        new_candles: list = []
        from_dt = datetime.datetime.strptime(from_date, "%Y-%m-%d")
        to_dt   = datetime.datetime.strptime(to_date,   "%Y-%m-%d")
        chunk_start = from_dt
        while chunk_start <= to_dt:
            chunk_end = min(chunk_start + datetime.timedelta(days=58), to_dt)
            chunk = _fyers_fetch_range(
                app_id, access_token,
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
            )
            new_candles.extend(chunk)
            log.info(f"[AutoUpdate]   chunk {chunk_start.date()} → {chunk_end.date()}: "
                     f"{len(chunk)} candles")
            chunk_start = chunk_end + datetime.timedelta(days=1)
            import time as _time; _time.sleep(0.3)

        if not new_candles:
            log.info("[AutoUpdate] Fyers se 0 candles — possibly holiday/weekend")
            with _AUTO_UPDATE_LOCK:
                _AUTO_UPDATE_STATUS["last_result"] = {
                    "added": 0, "skipped": False, "last_date": str(last_date)
                }
            return

        # ── Step 4: deduplicate + merge + save ───────────────────────────────
        existing_ts = {r[0] for r in existing}
        fresh       = [r for r in new_candles if r[0] not in existing_ts]
        all_rows    = sorted(existing + fresh, key=lambda x: x[0])
        save_bin(all_rows)

        result = {
            "added":     len(fresh),
            "total":     len(all_rows),
            "last_date": to_date,
            "skipped":   False,
        }
        log.info(f"[AutoUpdate] ✅ Done: +{len(fresh)} candles → {len(all_rows):,} total")
        with _AUTO_UPDATE_LOCK:
            _AUTO_UPDATE_STATUS["last_result"] = result

    except Exception as e:
        log.error(f"[AutoUpdate] ❌ Error: {e}")
        with _AUTO_UPDATE_LOCK:
            _AUTO_UPDATE_STATUS["last_result"] = {"error": str(e)}
    finally:
        with _AUTO_UPDATE_LOCK:
            _AUTO_UPDATE_STATUS["running"]    = False
            _AUTO_UPDATE_STATUS["last_check"] = _time.time()


def start_auto_update(app_id: str, access_token: str,
                      min_interval_sec: int = 300) -> bool:
    """
    App open hone par call karo — background thread mein Fyers se latest data
    fetch karke local .bin.gz update karta hai.

    app_id / access_token : Fyers credentials
    min_interval_sec      : dobara update try karne ka gap (default 5 min)

    Returns True agar thread start hua, False agar already running/recent.
    """
    import time as _time
    with _AUTO_UPDATE_LOCK:
        if _AUTO_UPDATE_STATUS["running"]:
            return False  # already running
        elapsed = _time.time() - _AUTO_UPDATE_STATUS["last_check"]
        if elapsed < min_interval_sec:
            return False  # recently checked, skip
        _AUTO_UPDATE_STATUS["running"] = True

    t = _threading.Thread(
        target=_auto_update_worker,
        args=(app_id, access_token),
        name="BNAutoUpdater",
        daemon=True,
    )
    t.start()
    log.info("[AutoUpdate] Background update thread started")
    return True


def get_auto_update_status() -> dict:
    """Current auto-update status return karo (thread-safe)."""
    with _AUTO_UPDATE_LOCK:
        return dict(_AUTO_UPDATE_STATUS)


# ─── CLI one-shot converter ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1].endswith(".txt"):
        csv_to_bin(sys.argv[1])
        print(get_stats())
    elif len(sys.argv) == 2 and sys.argv[1] == "download":
        # python bn_data_manager.py download
        ok = download_from_github()
        print("✅ Download OK" if ok else "❌ Download failed (GitHub URL check karo)")
        if ok:
            print(get_stats())
    elif len(sys.argv) == 3 and sys.argv[1] == "download":
        # python bn_data_manager.py download CUSTOM_URL
        ok = download_from_github(url=sys.argv[2])
        print("✅ Download OK" if ok else "❌ Download failed")
        if ok:
            print(get_stats())
    else:
        print("Usage:")
        print("  python bn_data_manager.py bank-nifty-1m-data.txt   # CSV → bin.gz")
        print("  python bn_data_manager.py download                  # GitHub se download")
        print("  python bn_data_manager.py download CUSTOM_URL       # Custom URL se")
        print("Stats:", get_stats())
