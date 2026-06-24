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
GITHUB_BASE = "https://raw.githubusercontent.com/krishna123814/My-engine/main/"

# ─── Precomputed multi-timeframe files ───────────────────────────────────────
# Every entry here is generated FROM bn_1m.bin.gz by regenerate_all_timeframes().
# chart.html loads these directly — no client-side resampling needed.
TF_MINUTES = {
    "5m": 5, "15m": 15, "45m": 45, "135m": 135,
    "1d": 1440, "3d": 4320, "9d": 12960, "27d": 38880,
}
TF_FILE = {
    label: os.path.join(os.path.dirname(__file__), f"bn_{label}.bin.gz")
    for label in TF_MINUTES
}

# ── NSE / BSE official trading holidays 2017-2026 (YYYY-MM-DD) ──
# Mirrors the NSE_HOLIDAYS set in chart.html — keep both in sync.
NSE_HOLIDAYS = {
    # 2017
    '2017-01-26','2017-02-24','2017-03-13','2017-04-04','2017-04-14',
    '2017-06-26','2017-08-15','2017-08-25','2017-10-02','2017-10-19',
    '2017-10-20','2017-11-01','2017-12-25',
    # 2018
    '2018-01-26','2018-03-02','2018-03-29','2018-03-30','2018-05-01',
    '2018-08-15','2018-08-22','2018-09-13','2018-09-20','2018-10-02',
    '2018-11-07','2018-11-08','2018-11-21','2018-12-25',
    # 2019
    '2019-03-04','2019-03-21','2019-04-17','2019-04-19','2019-05-01',
    '2019-06-05','2019-08-12','2019-08-15','2019-09-02','2019-10-02',
    '2019-10-07','2019-10-08','2019-10-28','2019-11-12','2019-12-25',
    # 2020
    '2020-02-21','2020-03-10','2020-04-02','2020-04-06','2020-04-10',
    '2020-04-14','2020-05-01','2020-05-25','2020-07-31','2020-08-15',
    '2020-10-02','2020-11-16','2020-11-30','2020-12-25',
    # 2021
    '2021-01-26','2021-03-11','2021-03-29','2021-04-02','2021-04-14',
    '2021-04-21','2021-05-13','2021-07-21','2021-08-15','2021-09-10',
    '2021-10-02','2021-10-15','2021-11-04','2021-11-05','2021-11-19',
    '2021-12-25',
    # 2022
    '2022-01-26','2022-03-01','2022-03-18','2022-04-14','2022-04-15',
    '2022-05-03','2022-08-09','2022-08-15','2022-10-02','2022-10-05',
    '2022-10-26','2022-10-27','2022-11-08','2022-12-25',
    # 2023
    '2023-01-26','2023-03-07','2023-03-30','2023-04-04','2023-04-07',
    '2023-04-14','2023-04-22','2023-05-01','2023-06-29','2023-08-15',
    '2023-09-19','2023-10-02','2023-10-24','2023-11-14','2023-11-27',
    '2023-12-25',
    # 2024
    '2024-01-22','2024-01-26','2024-03-25','2024-04-01','2024-04-09',
    '2024-04-11','2024-04-14','2024-04-17','2024-06-17','2024-07-17',
    '2024-08-15','2024-10-02','2024-11-01','2024-11-15','2024-12-25',
    # 2025
    '2025-02-26','2025-03-14','2025-03-31','2025-04-10','2025-04-14',
    '2025-04-18','2025-05-01','2025-08-15','2025-08-27','2025-10-02',
    '2025-10-21','2025-10-22','2025-11-05','2025-12-25',
    # 2026 (provisional — verify from NSE circular in Jan 2026)
    '2026-01-26','2026-03-20','2026-04-03','2026-04-14','2026-05-01',
    '2026-08-15','2026-10-02','2026-11-25','2026-12-25',
}


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

    # ── Keep precomputed timeframe files in sync with the new 1m data ───────
    try:
        rows_sec = [(r[0] // 1000, r[1], r[2], r[3], r[4]) for r in all_rows]
        regenerate_all_timeframes(rows_sec)
        log.info("Regenerated all bn_<tf>.bin.gz files after update")
    except Exception as e:
        log.error(f"Timeframe regeneration failed: {e}")

    return {
        "added":     len(fresh),
        "total":     len(all_rows),
        "last_date": today_str,
        "skipped":   False
    }


# ─── Multi-timeframe precompute (ports chart.html's resample logic to Python) ─
#
# These mirror resample() / resampleWorkingDays() / resampleForTF() from
# chart.html EXACTLY so the precomputed files match what the client used to
# compute on the fly. chart.html no longer resamples BankNifty at all — it
# just loads the matching bn_<tf>.bin.gz file directly.

def _utc_date_str(ts_sec: int) -> str:
    return datetime.datetime.utcfromtimestamp(ts_sec).strftime("%Y-%m-%d")


def _resample_nse_session(rows: list, tf_min: int) -> list:
    """NSE session-boundary aware resample (9:15–15:30 IST), tf_min < 1440.
    rows: list of (ts_sec, o, h, l, c) sorted ascending.
    """
    if tf_min <= 1:
        return list(rows)

    IST_OFFSET_SEC = int(5.5 * 3600)
    SESSION_START = 9 * 60 + 15     # 555
    SESSION_END   = 15 * 60 + 30    # 930

    buckets = {}
    order = []
    for ts, o, h, l, c in rows:
        ist_sec = ts + IST_OFFSET_SEC
        day_start = ist_sec - (ist_sec % 86400)
        min_of_day = (ist_sec % 86400) // 60

        if min_of_day < SESSION_START or min_of_day >= SESSION_END:
            continue

        min_since_open = min_of_day - SESSION_START
        bucket_idx = min_since_open // tf_min
        bucket_start_min = SESSION_START + bucket_idx * tf_min
        key = (day_start - IST_OFFSET_SEC) + bucket_start_min * 60

        b = buckets.get(key)
        if b is None:
            buckets[key] = [o, h, l, c]
            order.append(key)
        else:
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c

    order.sort()
    return [(k, *buckets[k]) for k in order]


def _resample_calendar(rows: list, tf_sec: int) -> list:
    """UTC-midnight anchored resample (used for 1D and for crypto)."""
    if tf_sec <= 60:
        return list(rows)

    buckets = {}
    order = []
    for ts, o, h, l, c in rows:
        key = (ts // tf_sec) * tf_sec
        b = buckets.get(key)
        if b is None:
            buckets[key] = [o, h, l, c]
            order.append(key)
        else:
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c

    order.sort()
    return [(k, *buckets[k]) for k in order]


def _resample_working_days(daily_rows: list, n_days: int) -> list:
    """Groups exactly n_days NSE trading-day candles into one bar.
    daily_rows: list of (ts_sec, o, h, l, c) at 1-day granularity.
    Skips Sat/Sun and NSE_HOLIDAYS (defensive — daily_rows should already
    only contain trading days since it's built from intraday 1m data).
    """
    if n_days <= 1:
        return list(daily_rows)

    wd = []
    for ts, o, h, l, c in daily_rows:
        dow = datetime.datetime.utcfromtimestamp(ts).weekday()  # 0=Mon..6=Sun
        if dow >= 5:  # Sat=5, Sun=6
            continue
        if _utc_date_str(ts) in NSE_HOLIDAYS:
            continue
        wd.append((ts, o, h, l, c))

    out = []
    for i in range(0, len(wd), n_days):
        chunk = wd[i:i + n_days]
        if not chunk:
            break
        ts0 = chunk[0][0]
        o0 = chunk[0][1]
        hi = max(r[2] for r in chunk)
        lo = min(r[3] for r in chunk)
        cl = chunk[-1][4]
        out.append((ts0, o0, hi, lo, cl))
    return out


def build_all_timeframes(rows_1m: list) -> dict:
    """rows_1m: list of (ts_sec, o, h, l, c) sorted ascending.
    Returns {label: rows} for every label in TF_MINUTES, computed exactly
    like chart.html's resampleForTF() used to do for the BANKNIFTY asset.
    """
    if not rows_1m:
        return {label: [] for label in TF_MINUTES}

    # Intraday tiers (< 1 day): NSE session-aware
    intraday = {}
    for label, tf_min in TF_MINUTES.items():
        if tf_min < 1440:
            intraday[label] = _resample_nse_session(rows_1m, tf_min)

    # 1D: plain UTC-midnight calendar resample (matches chart.html's
    # resampleForTF, which routes tf==1440 through the calendar branch)
    daily = _resample_calendar(rows_1m, 1440 * 60)
    intraday["1d"] = daily

    # 3D / 9D / 27D: working-day grouping of the daily candles
    for label, tf_min in TF_MINUTES.items():
        if tf_min > 1440:
            n_days = round(tf_min / 1440)
            intraday[label] = _resample_working_days(daily, n_days)

    return intraday


def regenerate_all_timeframes(rows_1m: list = None) -> dict:
    """Recompute and save bn_5m.bin.gz ... bn_27d.bin.gz from the current
    bn_1m.bin.gz (or from rows_1m if explicitly passed, e.g. right after an
    update so we don't have to re-read the file).

    Call this once, right after bn_1m.bin.gz is updated (daily, after market
    close) — NOT on every app load and NOT on every timeframe click.

    Returns {label: candle_count}.
    """
    if rows_1m is None:
        loaded = load_bin(auto_download=False)  # [[ts_ms,o,h,l,c],...]
        rows_1m = [(r[0] // 1000, r[1], r[2], r[3], r[4]) for r in loaded]

    rows_1m = sorted(rows_1m, key=lambda r: r[0])
    all_tf = build_all_timeframes(rows_1m)

    counts = {}
    for label, rows in all_tf.items():
        data = _encode(rows) if rows else b""
        with open(TF_FILE[label], "wb") as f:
            f.write(data)
        counts[label] = len(rows)
        log.info(f"Regenerated bn_{label}.bin.gz → {len(rows):,} candles")

    return counts


def load_timeframe(label: str, auto_download: bool = True) -> list:
    """Load a precomputed timeframe file → [[ts_ms, O, H, L, C], ...]
    (same shape as load_bin(), ready for the existing _to_lwc() in main.py).
    label: one of '5m','15m','45m','135m','1d','3d','9d','27d'
    """
    path = TF_FILE.get(label)
    if not path:
        raise ValueError(f"Unknown timeframe label: {label}")

    if not os.path.exists(path):
        if auto_download:
            download_from_github(url=GITHUB_BASE + f"bn_{label}.bin.gz", dest=path)
        if not os.path.exists(path):
            return []

    with open(path, "rb") as f:
        data = f.read()
    if not data:
        return []
    return _decode(data)


def ensure_all_tf_files() -> bool:
    """Make sure every bn_<tf>.bin.gz exists: try GitHub first, and if any
    are still missing, regenerate them locally from bn_1m.bin.gz.
    Call this once at app startup (like ensure_bin_file()).
    """
    missing = [label for label, path in TF_FILE.items() if not os.path.exists(path)]
    if not missing:
        return True

    for label in missing:
        download_from_github(url=GITHUB_BASE + f"bn_{label}.bin.gz", dest=TF_FILE[label])

    still_missing = [label for label, path in TF_FILE.items() if not os.path.exists(path)]
    if still_missing:
        log.info(f"Timeframe files not on GitHub ({still_missing}) — regenerating locally")
        regenerate_all_timeframes()

    return all(os.path.exists(p) for p in TF_FILE.values())


def load_all_timeframes(auto_download: bool = True) -> dict:
    """Load every precomputed timeframe file. Returns
    {'5m': [...], '15m': [...], ..., '27d': [...]} in LWC ms-format rows.
    """
    return {label: load_timeframe(label, auto_download) for label in TF_MINUTES}


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
