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

# ─── Google Drive Config ──────────────────────────────────────────────────────
# Yahan apna Google Drive File ID daalo (share link se milta hai)
# Format: https://drive.google.com/file/d/FILE_ID_YAHAN/view
GDRIVE_FILE_ID = os.environ.get("BN_GDRIVE_FILE_ID", "1fEC-AMsI3Ke-he2M7bak31TUeLIjVwQ3")


def download_from_gdrive(file_id: str = "", dest: str = "") -> bool:
    """
    Google Drive se bn_1m.bin.gz download karo agar local file nahi hai.
    file_id: Google Drive file ID (share link wala)
    dest: local save path (default: BIN_FILE)
    Returns True if download successful.
    """
    fid  = file_id or GDRIVE_FILE_ID
    path = dest or BIN_FILE

    if not fid:
        log.warning("GDRIVE_FILE_ID set nahi hai — download skip")
        return False

    if os.path.exists(path):
        log.info(f"bn_1m.bin.gz already exists locally — skip download")
        return True

    log.info(f"Google Drive se download ho raha hai... (file_id={fid[:12]}...)")

    # Google Drive direct download URL
    url = f"https://drive.google.com/uc?export=download&id={fid}&confirm=t"

    try:
        session = requests.Session()
        resp = session.get(url, stream=True, timeout=60)

        # Large file ke liye confirm token handle karo
        if "Content-Disposition" not in resp.headers:
            # Confirmation page mili — token dhundho
            for key, val in resp.cookies.items():
                if "download_warning" in key.lower() or key.startswith("download_warning"):
                    url = f"https://drive.google.com/uc?export=download&id={fid}&confirm={val}"
                    resp = session.get(url, stream=True, timeout=120)
                    break
            else:
                # Token cookies mein nahi mila — direct try karo
                url2 = f"https://drive.google.com/uc?export=download&id={fid}&confirm=1"
                resp2 = session.get(url2, stream=True, timeout=120)
                if resp2.status_code == 200 and len(resp2.content) > 1000:
                    resp = resp2

        if resp.status_code != 200:
            log.error(f"Drive download failed: HTTP {resp.status_code}")
            return False

        # File save karo
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        total = 0
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                if chunk:
                    f.write(chunk)
                    total += len(chunk)

        size_mb = total / 1024 / 1024
        log.info(f"✅ Downloaded {size_mb:.1f} MB → {path}")
        print(f"✅ Google Drive se download complete: {size_mb:.1f} MB")
        return True

    except Exception as e:
        log.error(f"Google Drive download error: {e}")
        if os.path.exists(path):
            os.remove(path)  # Incomplete file delete karo
        return False


def ensure_bin_file(gdrive_file_id: str = "") -> bool:
    """
    Replay/chart mode ke liye: local file check karo, nahi hai to Drive se lao.
    Returns True if file available hai.
    """
    if os.path.exists(BIN_FILE):
        return True
    log.info("Local bn_1m.bin.gz nahi mili — Google Drive se try kar raha hai...")
    return download_from_gdrive(file_id=gdrive_file_id)


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
    auto_download=True: local file nahi hai to Google Drive se download karo.
    """
    if not os.path.exists(BIN_FILE):
        if auto_download and GDRIVE_FILE_ID:
            log.info("bn_1m.bin.gz nahi mili — Drive se download try...")
            download_from_gdrive()
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


# ─── CLI one-shot converter ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1].endswith(".txt"):
        csv_to_bin(sys.argv[1])
        print(get_stats())
    elif len(sys.argv) == 2 and sys.argv[1] == "download":
        # python bn_data_manager.py download
        ok = download_from_gdrive()
        print("✅ Download OK" if ok else "❌ Download failed (GDRIVE_FILE_ID check karo)")
        if ok:
            print(get_stats())
    elif len(sys.argv) == 3 and sys.argv[1] == "download":
        # python bn_data_manager.py download FILE_ID
        ok = download_from_gdrive(file_id=sys.argv[2])
        print("✅ Download OK" if ok else "❌ Download failed")
        if ok:
            print(get_stats())
    else:
        print("Usage:")
        print("  python bn_data_manager.py bank-nifty-1m-data.txt   # CSV → bin.gz")
        print("  python bn_data_manager.py download                  # Drive se download")
        print("  python bn_data_manager.py download FILE_ID          # Specific file")
        print("Stats:", get_stats())
