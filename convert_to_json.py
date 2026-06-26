"""
convert_to_json.py
==================
Ek baar local pe chalao — .pkl.gz files ko JSON.gz mein convert karta hai.
Phir yeh JSON.gz files GitHub pe push karo.

Usage:
    python convert_to_json.py

Output files (GitHub pe push karo):
    replay_bn_5m.json.gz
    replay_bn_15m.json.gz
    replay_bn_45m.json.gz
    replay_bn_135m.json.gz
    replay_bn_1d.json.gz
    replay_bn_3d.json.gz
    replay_bn_9d.json.gz
    replay_bn_27d.json.gz
    replay_btc_160m.json.gz
    replay_btc_8h.json.gz
    replay_btc_1d.json.gz
    replay_btc_3d.json.gz
    replay_btc_9d.json.gz
    replay_btc_27d.json.gz
"""

import pickle
import gzip
import json
import os

# ── Config ────────────────────────────────────────────────────────────────────
BN_FILE  = "banknifty_all_tf_pkl.gz"   # uploaded file ka naam
BTC_FILE = "btc_all_tf_pkl.gz"         # uploaded file ka naam

BN_TFS   = ["5m", "15m", "45m", "135m", "1d", "3d", "9d", "27d"]
BTC_TFS  = ["160m", "8h", "1d", "3d", "9d", "27d"]

OUTPUT_DIR = "."   # current folder mein save hoga


def load_pkl_gz(filepath):
    print(f"Loading {filepath} ...")
    with gzip.open(filepath, "rb") as f:
        data = pickle.load(f)
    print(f"  Keys: {list(data.keys())}")
    return data


def df_to_candles(df):
    """DataFrame → [{time, open, high, low, close}, ...] list"""
    candles = []
    bad = 0
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
            candles.append({
                "time":  ts,
                "open":  round(float(row.get("Open",  row.iloc[0])), 2),
                "high":  round(float(row.get("High",  row.iloc[1])), 2),
                "low":   round(float(row.get("Low",   row.iloc[2])), 2),
                "close": round(float(row.get("Close", row.iloc[3])), 2),
            })
        except Exception as e:
            bad += 1
    if bad:
        print(f"    ⚠️  {bad} bad rows skipped")
    return candles


def save_json_gz(candles, filename):
    path = os.path.join(OUTPUT_DIR, filename)
    raw  = json.dumps(candles, separators=(",", ":")).encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(raw)
    size_kb = round(os.path.getsize(path) / 1024, 1)
    print(f"    ✅ Saved: {filename}  ({len(candles)} candles, {size_kb} KB)")


def convert(pkl_file, tfs, prefix):
    if not os.path.exists(pkl_file):
        print(f"❌ File not found: {pkl_file}")
        return
    data = load_pkl_gz(pkl_file)
    for tf in tfs:
        df = data.get(tf)
        if df is None:
            # lowercase fallback
            for k in data.keys():
                if k.lower() == tf.lower():
                    df = data[k]
                    break
        if df is None:
            print(f"    ⚠️  TF '{tf}' not found, skipping")
            continue
        print(f"  Converting {tf}  ({len(df)} rows) ...")
        candles  = df_to_candles(df)
        filename = f"replay_{prefix}_{tf}.json.gz"
        save_json_gz(candles, filename)


if __name__ == "__main__":
    print("=" * 50)
    print("BankNifty convert kar raha hoon...")
    print("=" * 50)
    convert(BN_FILE, BN_TFS, "bn")

    print()
    print("=" * 50)
    print("BTC convert kar raha hoon...")
    print("=" * 50)
    convert(BTC_FILE, BTC_TFS, "btc")

    print()
    print("=" * 50)
    print("✅ Done! In files ko GitHub pe push karo:")
    print("=" * 50)
    for tf in BN_TFS:
        print(f"  replay_bn_{tf}.json.gz")
    for tf in BTC_TFS:
        print(f"  replay_btc_{tf}.json.gz")
