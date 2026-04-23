"""Dedicated arb-compute subprocess.

Motivation
----------
`_compute_arb_sync` is 0.7-4.7 s of pure-Python O(N²) work every refresh
tick. Running it in-master (even via `asyncio.to_thread`) keeps it under
the master process's GIL, which blocks the fetcher's asyncio loop from
servicing WS frames and REST-fetch timers. Every minute or two we saw a
burst of `Screener <ex> fetch timeout (>10.0s)` as queued tasks fired
their timers while the loop was stalled.

Moving compute to its own OS process gives it its own GIL and its own
CPU slice — the master's loop stays responsive throughout. We tried
`ProcessPoolExecutor` first; it failed because pickling 4-5 k rows of
funding data + 500-row result through the `multiprocessing.Queue` was
too expensive (compute jumped from ~1 s → 11-48 s). This version swaps
the IPC path entirely: the subprocess reads `funding.json` from the
shared tmpfs file cache (already written by the master) and writes
`arbitrage.json` back to the same cache. No pickling, no queue.

Safety
------
Gated by `AVALANT_ARB_COMPUTE_MODE`:
  * unset / "inline" (default) — legacy path: master's screener refresh
    loop runs compute via `asyncio.to_thread` as before.
  * "subprocess" — start this service; master's refresh loop detects the
    subprocess is alive and skips its own compute call.

Failure modes roll back cleanly: if the subprocess dies or AVALANT_
ARB_COMPUTE_MODE is changed back to "inline", the master resumes
computing itself.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import time

logger = logging.getLogger("avalant.arb_compute")

_CACHE_DIR = "/tmp/avalant_cache"
_ADMIN_STATE_FILE = os.path.join(_CACHE_DIR, "admin_state.json")
_COMPUTE_LIVE_FILE = os.path.join(_CACHE_DIR, "arb_compute.alive")

# How stale `funding.json` is allowed to be before we skip a cycle. A cold
# cache at fetcher startup takes a couple of seconds to populate; after
# that, funding.json is rewritten every ~2 s by get_funding_data's fast
# path. 30 s matches existing reader tolerance across the codebase.
_FUNDING_MAX_AGE_S = 30.0


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def is_subprocess_mode() -> bool:
    return (os.environ.get("AVALANT_ARB_COMPUTE_MODE") or "").strip().lower() == "subprocess"


def is_worker_alive() -> bool:
    """Master-side check: is the compute subprocess writing its heartbeat?

    Returns True if `/tmp/avalant_cache/arb_compute.alive` was touched in
    the last ~5 s. The subprocess bumps it at the top of every cycle.
    """
    try:
        age = time.time() - os.path.getmtime(_COMPUTE_LIVE_FILE)
        return age < 5.0
    except OSError:
        return False


# ── Subprocess entry-point ───────────────────────────────────────────────────
def _compute_loop_process(stop_evt) -> None:
    """Runs inside the compute subprocess. Own Python interpreter + GIL.

    Imports are lazy so the fork-copy of master's memory doesn't force
    re-initialisation; we only need a handful of helpers here.
    """
    import json as _json
    import time as _time
    # Per-process logger — fork inherits handlers from master, so log lines
    # funnel through the same rotating-file setup.
    from backend.logging_config import setup_logging
    setup_logging("arb_compute")
    log = logging.getLogger("avalant.arb_compute")

    from backend.services.arbitrage_service import (
        _read_file_cache, _write_file_cache, _drop_price_outliers,
        _compute_arb_sync, _slim_arb_for_file, FETCHERS,
    )
    from backend.services import token_registry as _tr
    _tr._load_from_disk()   # warm the registry from disk persist

    refresh_interval = _env_float("AVALANT_REFRESH_INTERVAL", 0.6)
    local_stale_max = 15.0
    log.info("arb compute subprocess started (interval=%.1fs)", refresh_interval)

    last_good_count = 0
    last_write_ts = 0.0
    tick = 0
    last_log = 0.0

    while not stop_evt.is_set():
        t0 = _time.time()
        # Heartbeat file — master uses this to detect the subprocess is alive.
        try:
            with open(_COMPUTE_LIVE_FILE, "w") as f:
                f.write(str(t0))
        except OSError:
            pass

        try:
            # 1. Read admin filter state (written by master periodically).
            disabled_ex: set[str] = set()
            hidden_sym: set[str] = set()
            min_volume = 0.0
            exclude_ex: set[str] = set()
            try:
                with open(_ADMIN_STATE_FILE) as f:
                    state = _json.load(f) or {}
                disabled_ex = set(state.get("disabled_exchanges") or [])
                hidden_sym  = set(state.get("hidden_symbols") or [])
                min_volume  = float(state.get("arb_min_volume_usd") or 0.0)
                exclude_ex  = set(state.get("arb_exclude_exchanges") or [])
            except (FileNotFoundError, _json.JSONDecodeError, OSError):
                pass  # First run: no filter, re-read next tick.

            # 2. Pull rows from funding.json (master writes it every ~2s).
            shared = _read_file_cache("funding.json", max_age=_FUNDING_MAX_AGE_S) or {}
            rows = list(shared.get("rows") or [])
            rows = [r for r in rows if r.get("exchange") not in disabled_ex]

            def _keep(r: dict) -> bool:
                if hidden_sym and r.get("symbol") in hidden_sym:
                    return False
                v = r.get("volume_usd")
                rate = r.get("rate")
                if v is None or rate is None:
                    return False
                try:
                    if float(rate) == 0.0:
                        return False
                    return float(v) >= min_volume
                except (TypeError, ValueError):
                    return False

            rows = [r for r in rows if _keep(r)]
            rows = _drop_price_outliers(rows)

            if rows:
                result = _compute_arb_sync(rows, _time.time(), exclude=exclude_ex)
                new_count = len(result.get("opportunities", []))
                now = _time.time()

                # Anti-flicker mirrors the master's logic — skip writes that
                # would bomb the UI with a transient <50%-count result.
                too_thin = (
                    last_good_count > 10
                    and (now - last_write_ts) < 5.0
                    and new_count < last_good_count * 0.5
                )
                if too_thin:
                    log.info("arb anti-flicker: skipped (prev=%d new=%d)",
                             last_good_count, new_count)
                else:
                    _write_file_cache("arbitrage.json", _slim_arb_for_file(result))
                    if new_count > 0:
                        last_good_count = new_count
                        last_write_ts = now

                tick += 1
                if now - last_log >= 30.0:
                    log.info(
                        "compute tick #%d: %.2fs opps=%d rows=%d",
                        tick, _time.time() - t0, new_count, len(rows),
                    )
                    last_log = now
        except Exception as exc:
            log.warning("compute cycle error: %s", exc)

        elapsed = _time.time() - t0
        remaining = max(0.1, refresh_interval - elapsed)
        stop_evt.wait(remaining)

    try:
        os.remove(_COMPUTE_LIVE_FILE)
    except OSError:
        pass


# ── Master-side lifecycle ────────────────────────────────────────────────────
_proc: multiprocessing.Process | None = None
_stop_evt: "multiprocessing.synchronize.Event | None" = None  # type: ignore[name-defined]


def _persist_admin_state_once() -> None:
    """Write a snapshot of admin filter state to disk so the subprocess has
    something to read on its next tick. Called at startup and every 30 s from
    a small daemon thread on master."""
    import json as _json
    try:
        from backend.services import admin_settings
        state = {
            "disabled_exchanges":    sorted(admin_settings.get_disabled_exchanges()),
            "hidden_symbols":        sorted(admin_settings.get_hidden_symbols()),
            "arb_min_volume_usd":    float(admin_settings.get_arb_min_volume_usd()),
            "arb_exclude_exchanges": sorted(admin_settings.get_arb_exclude_exchanges()),
        }
    except Exception as exc:
        logger.debug("admin-state read failed: %s", exc)
        return
    os.makedirs(_CACHE_DIR, exist_ok=True)
    tmp = _ADMIN_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            _json.dump(state, f)
        os.replace(tmp, _ADMIN_STATE_FILE)
    except OSError as exc:
        logger.debug("admin-state write failed: %s", exc)


def _admin_state_loop(stop_evt) -> None:
    logger.info("admin-state persistence thread started (every 30s)")
    while not stop_evt.is_set():
        _persist_admin_state_once()
        stop_evt.wait(30.0)


def start_arb_compute_process() -> None:
    """Spawn the compute subprocess. Idempotent — safe to call at startup.
    Respects AVALANT_ARB_COMPUTE_MODE: runs only when set to 'subprocess'."""
    global _proc, _stop_evt
    if not is_subprocess_mode():
        return
    if _proc is not None and _proc.is_alive():
        return

    # Prime the admin-state file before the subprocess reads it so it's not
    # starved for a whole cycle at startup.
    _persist_admin_state_once()

    _stop_evt = multiprocessing.Event()
    _proc = multiprocessing.Process(
        target=_compute_loop_process,
        args=(_stop_evt,),
        name="arb-compute",
        daemon=True,
    )
    _proc.start()
    logger.info("arb compute subprocess PID=%s", _proc.pid)

    # Start a thread on master to keep admin_state.json refreshed.
    import threading
    admin_stop = threading.Event()
    # Close over master's threading.Event so stop_arb_compute_process can set
    # both together.
    _admin_state_holder["stop"] = admin_stop
    t = threading.Thread(target=_admin_state_loop, args=(admin_stop,),
                         name="admin-state-persist", daemon=True)
    t.start()
    _admin_state_holder["thread"] = t


_admin_state_holder: dict = {"stop": None, "thread": None}


def stop_arb_compute_process(timeout: float = 5.0) -> None:
    global _proc, _stop_evt
    # Stop the admin-state persistence thread.
    admin_stop = _admin_state_holder.get("stop")
    if admin_stop is not None:
        admin_stop.set()
    # Stop the compute subprocess.
    if _stop_evt is not None:
        _stop_evt.set()
    if _proc and _proc.is_alive():
        _proc.join(timeout=timeout)
        if _proc.is_alive():
            _proc.terminate()
    _proc = None
    _stop_evt = None
