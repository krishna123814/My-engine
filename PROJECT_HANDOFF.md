# BankNifty Live Chart App — Project Handoff

**IMPORTANT — WORKFLOW RULE FOR ANY AI CONTINUING THIS PROJECT:**
This project has 18+ files (Python modules + JSON cache/log files). The
user has hit Claude's per-conversation upload limit (max ~17-20 files)
trying to upload them individually. **From now on, always exchange this
project as a single .zip file, both directions:**
- When asking the user for the current code, ask for **one zip** (not a
  list of individual files) and unzip it server-side.
- When handing back any changes, however small, **re-zip the ENTIRE
  current file set** (every .py file, the JSON cache/log files, and this
  updated PROJECT_HANDOFF.md) into one `.zip` and deliver only that zip
  — do not deliver loose individual files, even for a one-file change,
  since the user's workflow depends on always having one current
  up-to-date zip to re-upload next time.
- Always update this PROJECT_HANDOFF.md (append a new dated/numbered
  section, don't rewrite history) before re-zipping, so the next
  session/AI has full continuity.

**Last verified working:** User confirmed on Termux (proot-distro Ubuntu) — app runs correctly
after Phase 1–2 refactor described below. All Python files pass `py_compile` and a static
undefined-name check; a mocked-Streamlit import test also passed cleanly.

---

## 1. Overall goal

The user owns a working BankNifty/BTC live-chart trading app (Streamlit + a custom HTML/JS
chart frontend) that was originally a single ~6,243-line `app.py` file plus a ~41,600-line
`chart.html`. The app runs on a **free-tier Hugging Face Space**.

The user's complaint: **performance/smoothness issues**, and a general sense that the
architecture is not what "a trained developer or big company" would build. They asked for
an assessment (confirmed: yes, it's a functional-but-monolithic prototype, not
production-grade architecture) and a **roadmap** to bring it closer to professional
standards, **without disrupting their live/production usage**.

### Long-term target architecture (agreed roadmap, not yet started)
1. Separate the live-data engine (WebSocket threads, Binance/Fyers polling) from the UI,
   so restarting the UI doesn't kill live data collection.
2. Replace ad-hoc global-variable + manual-lock state with a proper shared store
   (Redis or, given free-tier constraints, lean harder on Supabase).
3. Replace polling (`time.sleep()` loops) with event-driven push (WebSocket) to the browser.
4. Break the 41,600-line `chart.html` into componentized frontend files.
5. Modularize the Python backend (**this is the part actively in progress — see below**).
6. Add observability (structured logs, health checks, error monitoring).

**Eventual "pro" stack discussed:** FastAPI (single port, serves REST + WebSocket + static
frontend from one process) instead of Streamlit + a bolted-on custom mini HTTP server
(`BNHistoryAPI`) that was causing "2 servers in 1 container" port confusion on the HF Space.
This FastAPI/React rewrite is a **large future project**, intentionally deferred — see
Section 5.

### Deployment/staging constraint (important context)
- User is on Hugging Face **free tier**. HF recently (as of this session, mid-2026) changed
  policy: **Docker/Gradio SDK now require a paid/PRO account for free CPU Basic hardware on
  *new* Spaces** — only `Static` SDK is free for new Spaces, and Static can't run Python.
  Duplicating the existing (grandfathered) Space also hit this wall.
- Their `app.py` is a **Streamlit** app, not Gradio — porting to Gradio would mean rewriting
  all UI code (`st.*` → `gr.*`), which was assessed as not worth it just to get free
  hosting.
- **Resolution:** user already had a **Termux (Android)** setup running the app locally via:
  ```
  proot-distro login ubuntu --bind /sdcard -- bash -c 'cd /sdcard/Download && streamlit run app.py'
  ```
  This is now the de facto **safe staging/test environment** — changes are made, given to
  the user, tested on Termux, and only promoted to the live HF Space once confirmed working.
  This avoids all HF free-tier hosting restrictions during development.
- Streamlit Community Cloud was also discussed as a possible alternative host (free, GitHub-based,
  native Streamlit support — no SDK confusion) but not yet set up. Possible future step if
  Termux testing becomes limiting.

---

## 2. What's actively being worked on right now

**Phase 1–2 of the roadmap (Section 5, item "5. Modularize the Python backend") is
in progress:** splitting the monolithic `app.py` into logically separated modules,
**with zero intended behavior change** — this is a pure refactor, not a rewrite. No logic
was altered; code was relocated and re-wired via imports only.

This was done in careful, verified stages (not a blind mechanical split) because the
original file has deep coupling: many functions share global dicts/locks (e.g. `_LIVE`,
`_CANDLE`, `_BN_FEED_DEBUG`, `_SV2_CACHE`, `_SV3_CACHE`) and there were several **genuine
circular dependencies** between logical groups (e.g. `broker_meta` ↔ `binance_rest` both
wanted the same URL constants; `data_updates` ↔ `resample_utils` genuinely call each other
in both directions). These were resolved by:
- Moving truly shared constants into a dependency-free "leaf" module (`config.py`).
- Merging two originally-separate-but-mutually-dependent groups (`data_updates.py` +
  `resample_utils.py`) into one file (`market_data.py`) rather than forcing an artificial
  split that would just recreate the circular import.
- Using a small number of shared-state-only modules (`live_state.py`, `candle_state.py`)
  that both `app.py` and lower-level modules (like `broker_meta.py`) can import without
  cycles.

### Verification method used (important — repeat this for any further changes)
Since this environment has no Streamlit install, no network, and no real Fyers/Binance
credentials, **runtime testing was not possible here**. Instead, three layers of static
verification were used before handing files to the user:
1. `python3 -m py_compile` on every file — catches syntax errors.
2. A custom `ast`-based static analyzer (script was built ad hoc — see reconstruction note
   below) that walks each file's AST, collects all defined names (imports, `def`, `class`,
   assignments, function args, `with`/`for`/`except` targets, comprehension vars) and all
   *used* names, and reports names used-but-never-defined. This caught ~20 real bugs:
   missing imports, names that were moved to another module without updating the importer,
   and genuinely circular imports.
3. A **mocked-Streamlit import test**: created minimal fake `streamlit` (and initially
   `requests`, though real `requests` was actually available) packages under a temp dir,
   prepended to `PYTHONPATH`, then literally ran `import app` in a subprocess. This executes
   all module-level code (every `import`, every decorator like `@st.cache_resource`, every
   top-level assignment) and will raise real `ImportError`/`NameError`/circular-import
   errors that pure `ast` analysis can miss. Iterated on the mock (making `st.*` attributes
   return a universal stub object that supports `__call__`, `__enter__`/`__exit__`,
   `__iter__`, `__getitem__`, `__contains__`) until `import app` succeeded cleanly with no
   traceback. This is strong evidence the module wiring is correct, but **does not**
   validate actual runtime behavior (thread startup, WebSocket connections, Fyers auth
   flow, etc.) — only the user's Termux run can confirm that, and they have now confirmed
   it works.

**Note for whoever continues this:** the ad-hoc `ast` static-checker script was written
inline during the session and not saved as a permanent file. If you need to re-verify after
further changes, recreate it (or write an equivalent) rather than assuming one exists on
disk. Rough spec: collect all `Name` nodes with `ast.Load` context across the file; build a
"defined" set from all imports/def/class/assignment-targets/function-args/comprehension-vars
/`except as`/`with as`/`for` targets, plus `dir(builtins)`; report any used name not in the
defined set. This is a *heuristic*, not a full type/scope checker — it can't see into other
files, so cross-file correctness still requires manually grepping usage vs. the actual
export list of the imported-from module (which is what caught the circular-import cases).

---

## 3. Current file layout (as of last handoff — 17 files total)

All files are plain Python (no packaging/`__init__.py` — flat directory, same as the
original single-file setup). `app.py` is still the Streamlit entrypoint
(`streamlit run app.py`).

| File | Responsibility | Depends on (imports from) |
|---|---|---|
| `config.py` | Secrets/env-var reading (`_get_secret`), file-path constants, cache TTLs, IST timezone helper, and **shared cross-module constants** (Binance URLs, `BN_OC_SYMBOL`, `BTC_1D_SYMBOL`, session time-offsets) that were centralized here specifically to break circular imports. Zero internal deps — the base leaf module. | — |
| `startup_log.py` | Disk-persisted startup/debug log (`_slog`, `_slog_exception`), survives Streamlit reruns via PID-tagged JSON file on disk. | — |
| `credentials.py` | Fyers creds file load/save (`load_creds`/`save_creds`), Binance creds from env (`_get_binance_creds`). | `config` |
| `hf_admin.py` | HF Space restart/pause/set-proxy-variable via `huggingface_hub` API. | `config` |
| `candle_state.py` | Shared 1-minute candle tracker (`_CANDLE` dict + lock), fed by both WS ticks and REST fallback. | — |
| `live_state.py` | Shared live-tick store (`_LIVE`), last-tick-JSON cache, BN feed-health debug dict (`_BN_FEED_DEBUG`). Split out specifically because `broker_meta.py` also needs `_LIVE`, and `app.py` needs it too — avoids a cycle. | — |
| `network_proxy.py` | Optional outbound proxy config/test (host/port/user/pass), used by all Binance REST + WS calls. | `config` (for URL constants used in the proxy self-test) |
| `broker_meta.py` | Fyers login/token exchange, Fyers/Binance account-balance + nearest-strike caching, full Fyers option-chain fetch. | `credentials`, `config`, `live_state`, `login_session` (for `_write_login_log`), `binance_rest` (for `binance_get_spot_balance`) |
| `binance_rest.py` | Binance REST: server time, request signing, spot balance/price, kline fetch (full+incremental), nearest-option lookup, option premium, SV3 BTC daily build/upload. | `config`, `network_proxy`, `storage` (for `_supabase_upload`), `market_data` (for SV3 cache/helpers) |
| `storage.py` | Supabase Storage (.gz) load/upload, local JSON "state" files (update-status tracking, generic persistence). Also holds several API-key constants (`FINNHUB_API_KEY`, `REPLAY_SUPABASE_*`, `TWELVEDATA_API_KEY`) that were originally defined inline here. | `config`, `startup_log` |
| `login_session.py` | Login-attempt logging, SMS alert on login, background token-expiry monitor, `is_session_active()` (the auth gate used everywhere in the UI). | `config`, `startup_log`, `credentials` |
| `fyers_history.py` | Fyers historical-candle fetchers (BankNifty intraday/daily, raw-chunk + full-load variants), BTC daily/intraday loaders, OHLC normalizer. | `config`, `credentials` |
| `market_data.py` | **Merged module** (see circular-dependency note above). Section A: daily/incremental data-refresh jobs (BTC, BankNifty replay-master, Nifty500). Section B: Stack View 2/3 resampling + gap-filling utilities. | `config`, `startup_log`, `credentials`, `network_proxy`, `login_session`, `fyers_history`, `storage` |
| `app.py` | Streamlit entrypoint. Still contains: page config/CSS, fresh-boot detection, the **BN Binance WebSocket engine** (mark/trade/spot threads + watchdog), Finnhub WS, Fyers option-chain background cache, Fyers WebSocket engine (`_on_ws_message`, `_start_ws`, `_rest_live_loop`), the internal `BNHistoryAPI` mini HTTP server, ZIP export, and the actual chart-HTML builder + main UI script body. ~1,500 lines (down from 6,243). | imports from all of the above |
| `td_symbols.py`, `replay_symbols.py` | Unchanged from original upload — symbol list helpers. | — |
| `chart.html` | **Completely unchanged** (~41,600 lines). Not yet touched — frontend split (roadmap item 4) has not started. | — |

### Dependency graph (must stay a DAG — no cycles)
```
config, startup_log, credentials, candle_state, live_state   (leaves)
        ↓
network_proxy, hf_admin, login_session, fyers_history, storage
        ↓
binance_rest, market_data
        ↓
broker_meta
        ↓
app.py  (imports everything)
```
**If adding new cross-module calls, always check this direction.** A module lower in this
list must never import from a module higher up, or it recreates a circular import (this
happened twice already during this refactor and had to be fixed — see `broker_meta.py`
originally trying to define its own copy of `BINANCE_BASE_URL`/`BINANCE_EAPI_URL` while
`binance_rest.py` also needed them, and `data_updates.py`/`resample_utils.py` calling each
other, which is why they're now merged into `market_data.py`).

---

## 4. What has NOT been done yet (honest gaps)

1. **The live WebSocket engine is still one big block inside `app.py`.** This is the
   highest-value, highest-risk remaining piece to modularize (it's the actual "why is my
   app not smooth" root cause — see Section 6 for the original diagnosis). It was
   deliberately left alone because it has the deepest interdependency (many functions share
   `_BN_LIVE_QUOTES`-style `@st.cache_resource` singletons, watchdog loops, etc.) and because
   breaking live-data collection would be the worst possible outcome of a refactor mistake.
   **Recommended approach if continuing this:** extract it as one cohesive module (not many
   small files) to preserve internal correctness, the same way `market_data.py` was merged
   rather than force-split. Apply the same 3-layer verification (compile → static
   undefined-name check → mocked-import test) before handing to the user.
2. **`chart.html` (41,600 lines) has not been touched at all.**
3. **No actual performance optimization has happened** — this whole phase has been pure
   reorganization for maintainability. The `time.sleep()` polling loops, the dual
   Streamlit-server + `BNHistoryAPI`-server pattern, and the full-script Streamlit rerun
   model are all still exactly as they were. Smoothness complaints from the original ask
   are **not yet addressed** by anything done so far — this was explicitly framed to the
   user as "Phase 1: organize safely" before "Phase 2+: actually optimize."
4. **No FastAPI/React migration has started** — still just discussed as the eventual
   target, explicitly deferred as a large separate project.
5. **No automated test suite exists.** All verification has been static (see Section 2).
   Real functional testing has happened exactly once, informally, by the user on Termux,
   and they reported "sab sahi h" (everything's fine) — but no specific feature-by-feature
   checklist was confirmed (login flow, live tick updates, option chain, chart rendering,
   BTC vs BankNifty toggle, daily-update buttons, etc. were not individually verified by the
   user in the conversation — only a general "it works").

---

## 5. Recommended next steps, in priority order

1. **Get a more specific confirmation from the user** on exactly what they tested on
   Termux (did they check login, live ticks, option chain, both BTC and BankNifty modes,
   the daily-update buttons, HF admin restart/pause buttons?) before assuming the refactor
   is 100% correct — "sab sahi h" is a good signal but not a full regression check.
2. Once confirmed solid, **promote these 17 files to the actual HF Space** (replacing the
   original monolithic files there), keeping the original files backed up somewhere first
   (e.g. ask the user to zip/download the current HF Space repo before overwriting, or use
   HF's git history to be able to revert).
3. **Then**, and only then, consider tackling the live WebSocket engine extraction from
   `app.py` (Section 4, item 1) — same careful methodology.
4. **In parallel or after**, start on actual performance fixes (this was the user's
   original complaint and hasn't been touched yet): replacing polling loops with
   push-based updates, evaluating whether the dual-server (`BNHistoryAPI`) pattern can be
   collapsed.
5. `chart.html` componentization and the FastAPI/React rewrite remain the largest, longest
   deferred items — do not attempt these until the smaller, lower-risk items above are
   solid and the user has a comfortable staging workflow (Termux is currently that; a
   GitHub + Streamlit Community Cloud setup was discussed as a possible upgrade to that
   workflow but not yet built).

---

## 7. Phase 3 update (this session) — WebSocket engine extraction + first performance fixes

**Status: done, static-verified (same 3-layer method as Phase 1-2), NOT yet
Termux-confirmed for these specific changes as of writing this.**

1. **Live-data engine extracted from `app.py` into a new `live_engine.py`**
   (Section 4 item 1 — was the highest-value, highest-risk remaining piece).
   Moved as ONE cohesive module (not split further), per the earlier
   recommendation: Binance option-chain WS engine (mark/trade/spot +
   watchdog), Finnhub WS, Fyers option-chain background cache, Fyers WS
   engine + REST fallback poller, market-depth caching, and the
   `BNHistoryAPI` mini HTTP server registration. `app.py` dropped from
   ~3,559 lines to ~1,574 lines; `live_engine.py` is ~2,073 lines.
   `app.py` now does `from live_engine import (...)` for the 8
   names the remaining UI code still calls (`_ensure_binance_threads`,
   `_ensure_finnhub_ws_thread`, `_ensure_fyers_threads`,
   `_get_live_payload`, `_register_api_route`,
   `get_cached_binance_option_chain_payload`,
   `get_cached_option_chain_payload`, `refresh_market_depth_cache`,
   `_MD_PUSHER_DEBUG`). Dependency graph: `live_engine.py` sits between
   `broker_meta.py` and `app.py` (imports from broker_meta, market_data,
   binance_rest, login_session, storage, fyers_history, credentials,
   network_proxy, config, candle_state, live_state — no new cycles).
   **Zero intended behavior change** in this step — pure move.

2. **First real performance fix: `_rest_live_loop` (Fyers REST fallback)
   lightened.** Previously: woke every 1s regardless of WS health, and
   once the WS went stale, hit Fyers' full-day history endpoint every 1s
   forever with no backoff — real risk of worsening a Fyers outage via
   rate-limiting. Now: sleeps 2s when WS is fresh (cheap check, fewer
   wakeups), and applies exponential backoff (1s→2s→4s...capped 10s,
   with jitter) on consecutive REST failures, resetting to fast polling
   the moment either the WS recovers or a REST call succeeds. Mirrors the
   `_bn_ws_backoff_sleep` pattern already used for the Binance WS
   reconnects, for consistency. New constants:
   `FYERS_REST_BACKOFF_BASE/MAX`, `FYERS_REST_IDLE_SLEEP`. **Same
   eventual data freshness/behavior**, just fewer wasted calls.

3. **`BNHistoryAPI` dual-server pattern — investigated, deliberately NOT
   collapsed.** Assessed and explained to the user: this second HTTP
   server (port 8502-8510, inside the same process as Streamlit) is not
   an accident — `chart.html` polls it directly every ~300ms for live
   ticks specifically to bypass Streamlit's slow full-script rerun model
   (was the "2-3s lag" fix). The code's own docstring already records
   that hooking routes into Streamlit's internal Tornado server was tried
   earlier and rejected as unreliable across Streamlit versions. A true
   collapse to one process/one port is the FastAPI rewrite — already the
   intentionally-deferred large item (Section 5, item 5) — and should
   not be attempted as an incremental patch on a live production app.
   **What was done instead (small, safe fix for the actual "port
   dikkat" complaint):** the port-selection loop (tries 8502..8510) was
   silent before — if every candidate port was busy, the side-server
   thread died with zero log/error and chart.html would just silently
   stop getting fast ticks, with no diagnostic trail. Added:
   `_API_PORT_STATE` dict (chosen port, bind timestamp, per-port
   attempt/error log) for runtime introspection, an `_slog` info line on
   successful bind, and an `_slog` **error** line if all 9 ports are
   busy (previously invisible). No endpoint/route logic touched.

**Next recommended steps (updates Section 5):**
1. Get Termux confirmation specifically for the Phase 3 changes above
   (WebSocket engine extraction + REST-poll backoff + port diagnostics)
   before promoting to the live HF Space — same discipline as Phase 1-2.
2. Once confirmed, promote to HF Space (back up originals first).
3. Remaining performance items not yet touched: full audit of any other
   `time.sleep()` loops.
4. `chart.html` componentization and the FastAPI/React rewrite remain
   the largest, longest-deferred items — unchanged from before.

### 7b. Follow-up (same session) — Binance option-chain bg-loop disk I/O fix

Audited `_binance_oc_meta_bg_loop` and `_binance_oc_ticker_bg_loop` —
both already TTL-guarded correctly, no change needed. Found and fixed a
real issue in `_binance_oc_bg_loop` (the 300ms in-memory payload
rebuild loop): it was **also writing the full payload to
`binance_optionchain.json` on every single 300ms tick — ~3.3 disk
writes/sec, 24/7**, even though grepping the whole codebase confirms
that file is never read anywhere (`/api/binance_optionchain` already
serves straight from the in-memory `_BINANCE_OC_LAST_PAYLOAD` cache).
Fix: in-memory update still happens every 300ms (needed for the fast
poll routes), but the disk write is now throttled to once per ~2s —
kept alive for any legacy consumer, ~85% less I/O. Same 3-layer
verification passed.

### 7c. Follow-up (same session) — full `time.sleep()` audit across all files

Grepped every `.py` file for `time.sleep(` and reviewed each. Result:
apart from the two fixes above (7/7b), everything else was already
sound — `_bn_watchdog_loop` (5s, lightweight timestamp checks),
`_binance_oc_meta_bg_loop`/`_ticker_bg_loop` (TTL-guarded correctly),
`_option_chain_bg_loop` (Fyers, TTL-guarded, disk-write only on actual
refresh — did NOT have the Binance loop's bug), the Binance-WS and
Finnhub-WS reconnect backoffs (already exponential + jitter), the
`login_session.py` token monitor (5 min, a real necessary network
check), and the small `time.sleep(0.15-0.2)` gaps in `binance_rest.py`/
`market_data.py`/`td_symbols.py` (legitimate rate-limit spacing inside
batch-fetch loops, not standing background pollers). **No further
changes made** — audit is complete, nothing else needed fixing.

### 7d. Follow-up (same session) — observability / health endpoint (roadmap item 6)

Roadmap item 6 ("add observability: structured logs, health checks,
error monitoring") had not been started. Structured logging already
existed in reasonable form via `startup_log.py` (`_slog`/`_slog_exception`,
level-tagged, persisted for critical/persist-worthy lines). What was
missing was a **single consolidated health view** — previously you had
to check several separate debug dicts (`_BN_THREAD_HEARTBEAT`,
`_BN_WS_STATE`, `_FYERS_OC_HEARTBEAT`, `_BN_FEED_DEBUG`,
`_FINNHUB_WS_STATE`, `_API_PORT_STATE`) individually to know if the app
was actually healthy.

**Added (purely additive — reads existing state, touches nothing else):**
- `_build_health_snapshot()` in `live_engine.py` — aggregates all of the
  above into one dict: Fyers WS/REST feed status, Binance WS
  mark/trade/spot status, background-loop heartbeats (meta/ticker/
  payload/option-chain) with per-component staleness thresholds,
  Finnhub WS status, the side-server's chosen port + bind history, and
  a live thread-name/count list. Computes an overall `"ok"` /
  `"degraded"` verdict.
- New route **`GET /api/health`** on the existing `BNHistoryAPI`
  side-server — returns the snapshot as JSON, HTTP 200 if `"ok"`, 503
  if `"degraded"`. Usable both for manual checking and for an external
  uptime-monitor (e.g. UptimeRobot) pointed at
  `http://<host>:<api_port>/api/health`.

No existing endpoint, thread, or data structure was modified — this
only reads. Functionally exercised in this session (called directly
post-import, returned clean JSON with an accurate "degraded" verdict
since no background threads were actually running in the static test
environment — expected).

---

## 8. Phase 4 update (this session) — polling → event-driven push (SSE) for BankNifty ticks

**Status: done, static-verified (compile + ast undefined-name check + mocked-Streamlit
import for `live_engine.py`, `node --check` for all inline JS in `chart.html`). NOT yet
Termux-confirmed for these specific changes as of writing this.**

**Goal:** replace the browser polling `/api/bn_tick` every 300ms with a real push
mechanism, per the roadmap's item 3 ("Replace polling (`time.sleep()` loops) with
event-driven push"). Scoped to the BankNifty live-tick path only (the highest-frequency
poll in the app); other polling (option chain, market depth, history) untouched.

1. **`live_state.py`: `_LIVE_LOCK` changed from `threading.Lock()` to
   `threading.Condition()`.** `Condition` wraps a lock internally and is a drop-in
   replacement for every existing `with _LIVE_LOCK:` call site elsewhere in the codebase
   (`app.py`, `live_engine.py`'s REST loop) — nothing else needed to change for those.
   What it adds: `.wait()` / `.notify_all()`, used below to make a listener actually block
   until a new tick lands instead of waking on a timer.

2. **`live_engine.py`: `_LIVE_LOCK.notify_all()` added at both places `_LIVE` gets a new
   tick** — the Fyers WS message handler (`_on_ws_message`) and the REST fallback loop
   (`_rest_live_loop`). This is the actual "push" trigger; both paths (WS and REST
   fallback) wake listeners equally, so a fallback tick isn't delivered any slower than a
   WS tick.

3. **New route `GET /api/bn_tick_stream`** on the existing `BNHistoryAPI` side-server —
   Server-Sent Events (SSE). On connect: sends SSE headers, then loops
   `with _LIVE_LOCK: _LIVE_LOCK.wait(timeout=_SSE_HEARTBEAT_SECONDS)`, and on wake either
   writes a real `data: {...}\n\n` tick (if `_LIVE["ts"]` actually changed since last send)
   or a `: hb\n\n` comment-line heartbeat (used only to detect a dead socket early via the
   write exception — never sent as fake tick data). Hard-capped at
   `_SSE_MAX_CONN_SECONDS` (6h) per connection as a safety net against a client that never
   sends TCP FIN; the browser's own EventSource reconnects well before that in practice.
   `ThreadingTCPServer` already gives every connection its own thread, so one long-lived
   SSE thread per open chart tab costs the same as any other concurrent request on this
   server — no new architecture needed. **`/api/bn_tick` (the old 300ms-poll endpoint) is
   left completely untouched** and is now the fallback path.

4. **`chart.html`: new `_startBNTickStream()`**, called at the same place
   `_startBNFastPolling()` used to be called directly. Opens an `EventSource` to
   `/api/bn_tick_stream`, applies ticks via the same `_applyBNLiveTick()` + `ts`-dedupe
   logic the old poller used. Falls back to `_startBNFastPolling()` (unchanged, still
   present) in two cases only: side-port unreachable / no `EventSource` support at all, or
   the `EventSource` reaches `readyState === CLOSED` (i.e. its own built-in reconnect
   attempts have already been exhausted — transient blips are retried natively by the
   browser before that point, so this fallback only fires on a real, sustained failure).

**What this does NOT touch (deliberately, to keep this change scoped and low-risk):**
- Option-chain, market-depth, and history polling in `chart.html` — all still poll as
  before.
- `/api/bn_tick` itself — kept byte-for-byte as it was, purely as the fallback target.
- Nothing about the dual-server (`BNHistoryAPI` + Streamlit) architecture changed.

**Bug found and fixed in passing (unrelated to the SSE work, discovered because the
mocked-import test in Phase 4's verification caught it):** `network_proxy.py` line 12 was
still doing `from broker_meta import BINANCE_BASE_URL, BINANCE_EAPI_URL` — a leftover from
before Phase 1's centralization of those constants into `config.py`. Both `broker_meta.py`
and `binance_rest.py` correctly import them from `config` already; `network_proxy.py` was
the one straggler still pointing at `broker_meta`, which recreates exactly the
`network_proxy → broker_meta → binance_rest → network_proxy` cycle Phase 1's handoff
describes fixing. **Confirmed via a clean re-extract of the previous zip (untouched) that
this cycle already existed there** — i.e. it predates this session and is not something
the SSE work introduced. Given the Section 2 dependency graph already says
`network_proxy` sits below `broker_meta`, this was a one-line, zero-ambiguity fix: changed
that one import to `from config import BINANCE_BASE_URL, BINANCE_EAPI_URL`. This also means
`import app` (and therefore `streamlit run app.py`) would have failed outright before this
fix — **this should be treated as the single highest-priority thing to re-verify on
Termux**, above the SSE change itself, since it's a hard import-time failure rather than a
runtime behavior change.

**Next recommended steps (updates Section 5):**
1. **Termux-confirm the `network_proxy.py` circular-import fix first** — if the app
   wouldn't even start before, that's the most urgent thing to verify launches cleanly now.
2. Termux-confirm the SSE tick delivery specifically: open the BankNifty chart, watch ticks
   update, then simulate a dropped connection (e.g. toggle airplane mode briefly) and
   confirm it recovers — either via EventSource's own reconnect or via the poll fallback —
   without a page reload.
3. Once both are confirmed, promote to HF Space (back up originals first, same discipline
   as every prior phase).
4. Remaining polling not yet converted: option-chain and market-depth polling in
   `chart.html` are candidates for the same SSE treatment if this pattern proves solid in
   production, but were deliberately left alone this round to keep the change scoped.
5. `chart.html` componentization and the FastAPI/React rewrite remain the largest,
   longest-deferred items — unchanged from before.

---

## 6. Original problem diagnosis (for context — informs future performance work)

This was the assessment given to the user near the start of the conversation, explaining
*why* the app has smoothness/performance issues (separate from the modularization work
described above, which does not by itself fix these):

- Single ~6,243-line `app.py` mixed UI, business logic, WebSocket handling, HF admin,
  and proxy management together (now improved — see Section 3).
- Global Python variables + ~15 manual `threading.Lock()`s for shared state instead of a
  real store.
- Local JSON files used as an ad-hoc database instead of a real DB (Supabase is used for
  some things, but not consistently).
- ~15+ background threads running inside one Streamlit process.
- `time.sleep()`-based polling in multiple places instead of event-driven push.
- Streamlit's rerun model re-executes the whole script top-to-bottom on every interaction —
  a 6,000-line file re-running is inherently slower than it needs to be (this file-size
  problem is what Section 3's split addresses, but the *rerun model itself* is unchanged and
  is a Streamlit-architectural limitation, not something the file split fixes).
- The internal `BNHistoryAPI` mini HTTP server + Streamlit's own server = two servers
  competing for a port inside one HF Space container (this was the user's original "port
  dikkat" question) — the long-term fix discussed is a FastAPI rewrite where one process,
  one port, serves everything (REST + WebSocket + static frontend), eliminating the need
  for a bolted-on second server entirely.
- `chart.html` is a single 41,638-line static file loaded in an iframe, reloaded in full
  rather than updated incrementally.
