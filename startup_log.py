"""
startup_log.py — App-startup / login debug log.

Login ke turant baad screen chart par redirect ho jaati hai, isliye startup
ke exact steps (creds load, session check, thread launch, koi bhi exception)
yahan capture karte hain — taaki header ke chhote debug icon se poora
startup trace copy karke dekha ja sake, chahe crash/redirect kitna bhi
jaldi ho jaaye.

NOTE (important bug fix): Streamlit HAR script-rerun (button click sahit)
par poori .py file top-se-bottom dobara EXECUTE karta hai — isi process
ke andar, but top-level statements jaise `_STARTUP_LOG = []` HAR rerun par
phir se chalte hain. Matlab RAM-only list sirf ek single rerun ke andar
hi zinda rehti thi — agla rerun (jaise BankNifty Update button ka apna
hi rerun) aate hi khaali ho jaati thi. Isi wajah se purana "fresh boot
detection" (list khaali → fresh boot) HAR baar True aata tha, chahe
process bilkul restart na hua ho — jo ki galat tha.
FIX: ab log disk par ek chhoti JSON file (_STARTUP_LOG_FILE) mein bhi
turant likha jaata hai, aur module load hote hi (yaani har rerun ke
start mein bhi) usi file se wapas load kar liya jaata hai — isliye ab
log sach me kabhi khaali nahi hota (process restart ke baad bhi nahi),
jab tak file delete na ho. Fresh-boot ab OS process-id (`os.getpid()`)
ko file mein save kiye gaye pichhle PID se compare karke detect hota
hai — PID sirf real naye process par badalta hai, Streamlit rerun par
nahi, isliye ye ab sahi tarah "restart hua ya sirf rerun hua" batata hai.
"""
import os
import json
import time
import threading
import datetime

_STARTUP_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup_debug_log.json")
_STARTUP_LOG_LOCK = threading.Lock()
_STARTUP_LOG_MAX  = 1500

# Disk par HAR _slog() call par pura 1500-line file rewrite karna heavy
# I/O hoga (proxy/market-depth jaise background loops bhi _slog use karte
# hain, jo har 1-2s chalte hain). Isliye disk-persist sirf un lines ke liye
# jo permanent debug block me actually dikhti hain (login + BN update +
# errors) — baaki sab RAM me hi rehti hain (is rerun ke liye kaafi hai).
_PERSIST_TAGS = ("SESSDBG", "BN_BTN_CLICK", "sess_active", "LOGIN_MANUAL",
                  "LOGIN_URL_SECRET",
                  "Script run start", "EXCEPTION")
_STARTUP_LOG_DIRTY_COUNT = 0
_STARTUP_LOG_FLUSH_EVERY = 3

_STARTUP_LOG_LAST_DISK_ERROR = None


def _load_startup_log_from_disk() -> list:
    try:
        with open(_STARTUP_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("lines", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _save_startup_log_to_disk(lines: list, boot_pid: int) -> None:
    global _STARTUP_LOG_LAST_DISK_ERROR
    try:
        with open(_STARTUP_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump({"boot_pid": boot_pid, "lines": lines}, f, ensure_ascii=False)
        _STARTUP_LOG_LAST_DISK_ERROR = None
    except Exception as _e_disk:
        # Pehle ye silently swallow ho jaata tha — ab error capture karte
        # hain taaki debug panel mein dikh sake ki disk-persist kyun fail
        # ho raha hai (permission/path/disk-full jaisi wajah).
        _STARTUP_LOG_LAST_DISK_ERROR = f"{type(_e_disk).__name__}: {_e_disk}"


_STARTUP_LOG: list = _load_startup_log_from_disk()


def _slog(msg: str, level: str = "info") -> None:
    """Thread-safe startup/diagnostic log line add karo (RAM hamesha,
    disk sirf login/update/error-relevant lines ke liye — see _PERSIST_TAGS).
    level: info|ok|warn|err"""
    global _STARTUP_LOG_DIRTY_COUNT
    try:
        t = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%H:%M:%S")
    except Exception:
        t = time.strftime("%H:%M:%S")
    line = {"t": t, "level": level, "msg": str(msg)}
    s = str(msg)
    # BN_BTN_CLICK / LOGIN_* / EXCEPTION / err-level → critical, turant flush.
    # SESSDBG / sess_active jaisi baar-baar aane wali lines → throttled flush
    # (kho nahi rahi, RAM me hai, disk-write bas thoda batch hoti hai).
    is_critical = any(tag in s for tag in
                       ("BN_BTN_CLICK", "LOGIN_MANUAL",
                        "LOGIN_URL_SECRET", "EXCEPTION")) or level == "err"
    is_persist_worthy = is_critical or any(tag in s for tag in _PERSIST_TAGS)
    with _STARTUP_LOG_LOCK:
        _STARTUP_LOG.append(line)
        if len(_STARTUP_LOG) > _STARTUP_LOG_MAX:
            del _STARTUP_LOG[: len(_STARTUP_LOG) - _STARTUP_LOG_MAX]
        if is_critical:
            _STARTUP_LOG_DIRTY_COUNT = 0
            _save_startup_log_to_disk(_STARTUP_LOG, os.getpid())
        elif is_persist_worthy:
            _STARTUP_LOG_DIRTY_COUNT += 1
            if _STARTUP_LOG_DIRTY_COUNT >= _STARTUP_LOG_FLUSH_EVERY:
                _STARTUP_LOG_DIRTY_COUNT = 0
                _save_startup_log_to_disk(_STARTUP_LOG, os.getpid())


def _slog_exception(where: str, exc: Exception) -> None:
    """Exception ko poori traceback ke saath log karo — copy-paste karne layak."""
    import traceback
    tb = traceback.format_exc()
    _slog(f"EXCEPTION in {where}: {exc}\n{tb}", level="err")


def _startup_log_snapshot() -> list:
    with _STARTUP_LOG_LOCK:
        return list(_STARTUP_LOG)
