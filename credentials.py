"""
credentials.py — Fyers (file-based) aur Binance (env-only) credential helpers.
"""
import os
import json
from config import CREDS_FILE


def load_creds() -> dict:
    if os.path.exists(CREDS_FILE):
        try:
            with open(CREDS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_creds(d: dict):
    with open(CREDS_FILE, "w") as f:
        json.dump(d, f)


# ─── Binance API credentials — HF Space secrets ONLY (env vars) ───────────────
# No manual login form, no local-file storage for these two. Set
# BINANCE_API_KEY / BINANCE_SECRET_KEY as HF Space secrets and the app reads
# them fresh every time — nothing to type in, nothing saved to disk.
def _get_binance_creds() -> tuple[str, str]:
    return (
        os.environ.get("BINANCE_API_KEY", "").strip(),
        os.environ.get("BINANCE_SECRET_KEY", "").strip(),
    )
