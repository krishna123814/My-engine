"""
seed_supabase.py
-----------------
YEH SCRIPT SIRF EK BAAR CHALANI HAI.

Yeh Supabase ko puri tarah fill kar deta hai:
  - BTC 1m  candles  (Binance se — last 1000)
  - BTC 15m candles  (Binance se — last 1000)
  - BTC daily candles (Binance se — 2017 se aaj tak)
  - BankNifty 1m, 5m, 15m, 45m candles (Fyers se)
  - BankNifty daily candles (Fyers se — 2020 se aaj tak)

Chalane ke baad app hamesha Supabase se instant load hogi.
Fyers access_token chahiye BankNifty ke liye.

Usage:
    python seed_supabase.py                    # BTC + BankNifty dono
    python seed_supabase.py --btc-only         # sirf BTC (Fyers ke bina)
    python seed_supabase.py --bn-only          # sirf BankNifty

Environment variables required:
    SUPABASE_URL
    SUPABASE_SECRET_KEY

BankNifty ke liye .fyers_creds.json bhi chahiye (app ke same folder mein).
"""

import os
import sys
import json
import time
import datetime
import requests

# ─── Supabase setup ───────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ ERROR: SUPABASE_URL aur SUPABASE_SECRET_KEY env variables set karo pehle.")
    print("   Example:")
    print('   set SUPABASE_URL=https://xxxx.supabase.co')
    print('   set SUPABASE_SECRET_KEY=sb_secret_xxxx')
    sys.exit(1)

REST_BASE = f"{SUPABASE_URL}/rest/v1"
HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def _ist_now():
    return datetime.datetime.now(IST)

def _ms_to_iso(ts_ms) -> str:
    return datetime.datetime.utcfromtimestamp(int(ts_ms) / 1000).isoformat() + "Z"

# ─── Supabase upsert ──────────────────────────────────────────────────────────
def upsert_candles(symbol: str, timeframe: str, candles: list) -> bool:
    """candles = [[epoch_ms, o, h, l, c, v], ...]"""
    if not candles:
        return False
    rows = []
    for c in candles:
        try:
            rows.append({
                "symbol":      symbol,
                "timeframe":   timeframe,
                "candle_time": _ms_to_iso(c[0]),
                "open":        c[1],
                "high":        c[2],
                "low":         c[3],
                "close":       c[4],
                "volume":      c[5] if len(c) > 5 else None,
            })
        except Exception:
            continue
    if not rows:
        return False

    total = len(rows)
    uploaded = 0
    for i in range(0, total, 500):
        chunk = rows[i:i + 500]
        try:
            resp = requests.post(
                f"{REST_BASE}/historical_candles?on_conflict=symbol,timeframe,candle_time",
                headers=HEADERS,
                json=chunk,
                timeout=30,
            )
            if resp.status_code in (200, 201, 204):
                uploaded += len(chunk)
                print(f"   ✅ {uploaded}/{total} rows uploaded...", end="\r")
            else:
                print(f"\n   ❌ Supabase error {resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"\n   ❌ Network error: {e}")
            return False
    print(f"   ✅ {total}/{total} rows uploaded.     ")
    return True

# ─── Check karo kya data already hai ─────────────────────────────────────────
def count_rows(symbol: str, timeframe: str) -> int:
    try:
        resp = requests.get(
            f"{REST_BASE}/historical_candles",
            headers={**HEADERS, "Prefer": "count=exact"},
            params={"symbol": f"eq.{symbol}", "timeframe": f"eq.{timeframe}",
                    "select": "candle_time", "limit": "1"},
            timeout=10,
        )
        cr = resp.headers.get("Content-Range", "")
        # Content-Range: 0-0/1234  → total = 1234
        if "/" in cr:
            return int(cr.split("/")[1])
        return len(resp.json()) if resp.status_code == 200 else 0
    except Exception:
        return 0

# ─── BTC Candles from Binance ─────────────────────────────────────────────────
def seed_btc_intraday(interval: str, limit: int = 1000):
    symbol = "BTCUSDT"
    existing = count_rows(symbol, interval)
    if existing >= limit * 0.9:
        print(f"   ⏭ BTC {interval}: already {existing} rows — skipping")
        return
    print(f"   📥 Binance se BTC {interval} fetch kar raha hoon...")
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval={interval}&limit={limit}",
            timeout=15,
        ).json()
        candles = [[int(x[0]), float(x[1]), float(x[2]),
                    float(x[3]), float(x[4]), float(x[5])] for x in r]
        print(f"   → {len(candles)} candles mili")
        upsert_candles(symbol, interval, candles)
    except Exception as e:
        print(f"   ❌ Error: {e}")

def seed_btc_daily():
    symbol = "BTCUSDT"
    existing = count_rows(symbol, "1d")
    if existing > 2000:
        print(f"   ⏭ BTC 1d: already {existing} rows — skipping")
        return
    print(f"   📥 Binance se BTC daily (2017 se aaj tak) fetch kar raha hoon...")
    today_str = _ist_now().strftime("%Y-%m-%d")
    all_candles = []
    seen = set()
    for yr in range(2017, _ist_now().year + 1):
        for (from_d, to_d) in [(f"{yr}-01-01", f"{yr}-06-30"), (f"{yr}-07-01", f"{yr}-12-31")]:
            if from_d > today_str:
                break
            actual_to = min(to_d, today_str)
            from_ms = int(datetime.datetime.strptime(from_d, "%Y-%m-%d").replace(
                tzinfo=datetime.timezone.utc).timestamp() * 1000)
            to_ms = int(datetime.datetime.strptime(actual_to, "%Y-%m-%d").replace(
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
                        if ts not in seen:
                            seen.add(ts)
                            all_candles.append([ts, float(x[1]), float(x[2]),
                                                float(x[3]), float(x[4]), float(x[5])])
                time.sleep(0.1)
            except Exception as e:
                print(f"   ⚠ {from_d} chunk error: {e}")
    all_candles.sort(key=lambda x: x[0])
    print(f"   → {len(all_candles)} candles mili")
    upsert_candles(symbol, "1d", all_candles)

# ─── BankNifty Candles from Fyers ─────────────────────────────────────────────
def _load_fyers_creds():
    creds_file = ".fyers_creds.json"
    if os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _fyers_fetch(resolution: str, from_date: str, to_date: str, creds: dict) -> list:
    headers = {"Authorization": f"{creds['app_id']}:{creds['access_token']}"}
    params = {
        "symbol": "NSE:NIFTYBANK-INDEX", "resolution": resolution,
        "date_format": "1", "range_from": from_date,
        "range_to": to_date, "cont_flag": "1",
    }
    try:
        res = requests.get(
            "https://api-t1.fyers.in/data/history",
            headers=headers, params=params, timeout=15,
        ).json()
        if res.get("s") == "ok":
            return [[c[0] * 1000, c[1], c[2], c[3], c[4], c[5]]
                    for c in res.get("candles", [])]
    except Exception as e:
        print(f"   ⚠ Fyers fetch error: {e}")
    return []

def seed_bn_intraday(interval_mins: int, creds: dict):
    symbol = "NSE:NIFTYBANK-INDEX"
    tf = str(interval_mins)
    days_map = {1: 10, 5: 30, 15: 60, 45: 90}
    days = days_map.get(interval_mins, 30)

    existing = count_rows(symbol, tf)
    min_expected = days * 375 // interval_mins * 0.5  # 50% threshold
    if existing > min_expected:
        print(f"   ⏭ BankNifty {interval_mins}m: already {existing} rows — skipping")
        return

    today = _ist_now().strftime("%Y-%m-%d")
    from_d = (_ist_now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    print(f"   📥 Fyers se BankNifty {interval_mins}m ({from_d} se {today}) fetch kar raha hoon...")
    candles = _fyers_fetch(tf, from_d, today, creds)
    print(f"   → {len(candles)} candles mili")
    if candles:
        upsert_candles(symbol, tf, candles)
    else:
        print(f"   ⚠ Koi data nahi mila — Fyers token check karo")

def seed_bn_daily(creds: dict):
    symbol = "NSE:NIFTYBANK-INDEX"
    existing = count_rows(symbol, "D")
    if existing > 1000:
        print(f"   ⏭ BankNifty Daily: already {existing} rows — skipping")
        return

    today_str = _ist_now().strftime("%Y-%m-%d")
    print(f"   📥 Fyers se BankNifty Daily (2020 se aaj tak) fetch kar raha hoon...")
    all_candles = []
    seen = set()
    for yr in range(2020, _ist_now().year + 1):
        for (from_d, to_d) in [(f"{yr}-01-01", f"{yr}-06-30"), (f"{yr}-07-01", f"{yr}-12-31")]:
            if from_d > today_str:
                break
            actual_to = min(to_d, today_str)
            chunk = _fyers_fetch("D", from_d, actual_to, creds)
            for c in chunk:
                if c[0] not in seen:
                    seen.add(c[0])
                    all_candles.append(c)
            time.sleep(0.25)
    all_candles.sort(key=lambda x: x[0])
    print(f"   → {len(all_candles)} candles mili")
    if all_candles:
        upsert_candles(symbol, "D", all_candles)
    else:
        print(f"   ⚠ Koi data nahi mila — Fyers token check karo")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    btc_only = "--btc-only" in sys.argv
    bn_only  = "--bn-only"  in sys.argv

    print("=" * 55)
    print("  Supabase Seeder — pura data ek baar mein fill karo")
    print("=" * 55)
    print(f"  Supabase URL : {SUPABASE_URL}")
    print(f"  Key set      : {'YES ✅' if SUPABASE_KEY else 'NO ❌'}")
    print("=" * 55)

    # ── BTC Section ──────────────────────────────────────────────────────────
    if not bn_only:
        print("\n📊 BTC Candles (Binance se — no auth needed)")
        print("-" * 45)

        print("\n[1/3] BTC 1m candles:")
        seed_btc_intraday("1m", 1000)

        print("\n[2/3] BTC 15m candles:")
        seed_btc_intraday("15m", 1000)

        print("\n[3/3] BTC Daily candles (2017 se aaj tak):")
        seed_btc_daily()

    # ── BankNifty Section ────────────────────────────────────────────────────
    if not btc_only:
        print("\n📈 BankNifty Candles (Fyers se)")
        print("-" * 45)

        creds = _load_fyers_creds()
        if not creds.get("access_token"):
            print("   ❌ .fyers_creds.json nahi mila ya access_token missing hai.")
            print("   App mein pehle Fyers login karo, phir yeh script chalao.")
            if not bn_only:
                print("\n✅ BTC data fill ho gaya. BankNifty ke liye Fyers login karo.")
            sys.exit(0)

        print(f"   ✅ Fyers creds loaded (app_id: {creds.get('app_id', '?')})")

        print("\n[4/8] BankNifty 1m candles (last 10 days):")
        seed_bn_intraday(1, creds)

        print("\n[5/8] BankNifty 5m candles (last 30 days):")
        seed_bn_intraday(5, creds)

        print("\n[6/8] BankNifty 15m candles (last 60 days):")
        seed_bn_intraday(15, creds)

        print("\n[7/8] BankNifty 45m candles (last 90 days):")
        seed_bn_intraday(45, creds)

        print("\n[8/8] BankNifty Daily candles (2020 se aaj tak):")
        seed_bn_daily(creds)

    print("\n" + "=" * 55)
    print("  ✅ SEEDING COMPLETE!")
    print("  Ab app hamesha Supabase se instant load hogi.")
    print("  Yeh script dobara chalane ki zaroorat nahi.")
    print("=" * 55)
