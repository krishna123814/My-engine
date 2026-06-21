"""
migrate_to_supabase.py
-----------------------
ONE-TIME script: purane local JSON cache files (jo abhi tak app ke
folder me bante aa rahe the) ka data Supabase me upload karta hai.

Chalane ka tareeka:
    python migrate_to_supabase.py

Ye script automatically dhundega:
    - bn_daily_cache.json   -> historical_candles (symbol=NSE:NIFTYBANK-INDEX, timeframe=D)
    - bn_live.json          -> live_ticks (latest snapshot)

Agar koi file exist nahi karti to woh skip ho jayegi, error nahi aayega.
Isko sirf EK BAAR chalana hai migration ke liye. Baad me naya data
app.py khud-ba-khud Supabase me daalta rahega (Step 2/3 wala kaam).
"""

import json
import os
import sys

import supabase_client as supa

SYMBOL = "NSE:NIFTYBANK-INDEX"

BN_DAILY_CACHE = "bn_daily_cache.json"
BN_LIVE_FILE   = "bn_live.json"
BTC_DAILY_CACHE = "btc_daily_cache.json"


def migrate_daily_candles():
    if not os.path.exists(BN_DAILY_CACHE):
        print(f"[skip] {BN_DAILY_CACHE} not found")
        return
    with open(BN_DAILY_CACHE) as f:
        cache = json.load(f)
    candles = cache.get("data", [])
    if not candles:
        print(f"[skip] {BN_DAILY_CACHE} has no candles")
        return
    print(f"[migrate] Uploading {len(candles)} BankNifty daily candles...")
    ok = supa.upsert_historical_candles(SYMBOL, "D", candles)
    print("  -> Success" if ok else "  -> FAILED (check SUPABASE_KEY / network)")


def migrate_btc_daily_candles():
    if not os.path.exists(BTC_DAILY_CACHE):
        print(f"[skip] {BTC_DAILY_CACHE} not found")
        return
    with open(BTC_DAILY_CACHE) as f:
        cache = json.load(f)
    candles = cache.get("data", [])
    if not candles:
        print(f"[skip] {BTC_DAILY_CACHE} has no candles")
        return
    print(f"[migrate] Uploading {len(candles)} BTC daily candles...")
    ok = supa.upsert_historical_candles("BTCUSDT", "1d", candles)
    print("  -> Success" if ok else "  -> FAILED (check SUPABASE_KEY / network)")


def migrate_live_snapshot():
    if not os.path.exists(BN_LIVE_FILE):
        print(f"[skip] {BN_LIVE_FILE} not found")
        return
    with open(BN_LIVE_FILE) as f:
        payload = json.load(f)
    ltp = payload.get("ltp")
    if ltp is None:
        print(f"[skip] {BN_LIVE_FILE} has no ltp")
        return
    print(f"[migrate] Uploading live snapshot (ltp={ltp})...")
    ok = supa.upsert_live_tick(SYMBOL, ltp, payload.get("candle", {}).get("open", ltp))
    print("  -> Success" if ok else "  -> FAILED (check SUPABASE_KEY / network)")


if __name__ == "__main__":
    if not supa.SUPABASE_KEY:
        print("ERROR: SUPABASE_SECRET_KEY not set. Aborting.")
        sys.exit(1)

    print("=== Supabase Migration Start ===")
    migrate_daily_candles()
    migrate_btc_daily_candles()
    migrate_live_snapshot()
    print("=== Done ===")
