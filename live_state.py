"""
live_state.py — Global live-tick store + feed-health debug dict.
WebSocket thread inhe update karta hai, UI/other modules inhe padhte hain.
"""
import threading

# ─── Global live-tick store (updated by WebSocket thread) ─────────────────
_LIVE: dict = {
    "ltp":       None,
    "prev_close": None,
    "ts":        0,
    "source":    None,   # "ws" (Fyers WebSocket push) | "rest" (1s REST-poll fallback)
}
# threading.Condition() wraps a lock internally and is fully drop-in
# compatible with existing `with _LIVE_LOCK:` usage everywhere else in the
# codebase (Condition supports the context-manager protocol via its
# underlying RLock) — so this change alone breaks nothing. What it adds:
# `.wait()`/`.notify_all()`, which the new SSE push endpoint (live_engine.py
# /api/bn_tick_stream) uses to sleep until an actual new tick arrives instead
# of polling on a fixed interval. This is what makes the tick delivery
# genuinely event-driven rather than "push-flavored polling in disguise".
_LIVE_LOCK = threading.Condition()

# Latest tick JSON string — postMessage injector ise padh ke iframe ko bhejta hai
_LAST_TICK_JS: dict = {"json": ""}
_LAST_TICK_LOCK = threading.Lock()

# ─── BankNifty WS/REST feed health — DEBUGGING ke liye ─────────────────────
_BN_FEED_DEBUG: dict = {
    "ws_connected":        False,
    "ws_last_connect_ts":  0,
    "ws_last_message_ts":  0,
    "ws_last_error":       None,
    "ws_last_close":       None,
    "rest_last_attempt_ts": 0,
    "rest_last_success_ts": 0,
    "rest_last_error":     None,
}
_BN_FEED_DEBUG_LOCK = threading.Lock()
