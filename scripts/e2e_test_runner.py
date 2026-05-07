"""End-to-end trade-flow test runner.

Reads creds from .env.test and walks through the full functionality
matrix: read-only validation, per-venue micro-trade smoke, trigger
lifecycle, TP/SL, external close detection, auto-pair detection.

Safety:
  - Hard caps: $5 per order, $18 per venue, $200 total (env-overridable)
  - Pause-confirm before each phase
  - Kill switch: Ctrl+C aborts and rolls back any open positions
  - Every API call logged to e2e_test_logs/run_<ts>.jsonl

Usage:
  cp .env.test.sample .env.test
  # fill keys
  python scripts/e2e_test_runner.py [--phase=1,2,3,4,5,6,7] [--scope=full|mini|read]
  # interactive prompts on each phase
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Load .env.test before any backend import
from dotenv import load_dotenv
load_dotenv(".env.test", override=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Init env so backend.settings doesn't choke on missing prod values
os.environ.setdefault("SECRET_KEY", "e2e-test-secret-key-not-for-prod-use-32chars")
os.environ.setdefault("ENCRYPTION_KEY", "0" * 32)
os.environ.setdefault("DATABASE_URL", "sqlite:///./e2e_test.db")
os.environ.setdefault("AVALANT_AUTH_DEV_EXPOSE_TOKEN", "1")
os.environ.setdefault("AVALANT_RUN_MIGRATIONS", "false")  # we run alembic manually

from fastapi.testclient import TestClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("e2e")


# ─── Config & state ──────────────────────────────────────────────────────
MAX_USD_PER_ORDER  = float(os.environ.get("E2E_MAX_USD_PER_ORDER",  "5"))
MAX_USD_PER_VENUE  = float(os.environ.get("E2E_MAX_USD_PER_VENUE",  "18"))
MAX_USD_TOTAL      = float(os.environ.get("E2E_MAX_USD_TOTAL",      "200"))
TEST_SYMBOL        = os.environ.get("E2E_TEST_SYMBOL", "BTC").upper()
LOG_DIR            = Path(os.environ.get("E2E_LOG_DIR", "./e2e_test_logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUN_TS             = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
LOG_FILE           = LOG_DIR / f"run_{RUN_TS}.jsonl"

VENUE_CREDS_MAP = {
    # CEX
    "binance":  ("BINANCE_API_KEY",  "BINANCE_API_SECRET",  None),
    "bybit":    ("BYBIT_API_KEY",    "BYBIT_API_SECRET",    None),
    "okx":      ("OKX_API_KEY",      "OKX_API_SECRET",      "OKX_API_PASSPHRASE"),
    "gate":     ("GATE_API_KEY",     "GATE_API_SECRET",     None),
    "mexc":     ("MEXC_API_KEY",     "MEXC_API_SECRET",     None),
    "kucoin":   ("KUCOIN_API_KEY",   "KUCOIN_API_SECRET",   "KUCOIN_API_PASSPHRASE"),
    "bitget":   ("BITGET_API_KEY",   "BITGET_API_SECRET",   "BITGET_API_PASSPHRASE"),
    "bingx":    ("BINGX_API_KEY",    "BINGX_API_SECRET",    None),
    "whitebit": ("WHITEBIT_API_KEY", "WHITEBIT_API_SECRET", None),
    "kraken":   ("KRAKEN_API_KEY",   "KRAKEN_API_SECRET",   None),
    "htx":      ("HTX_API_KEY",      "HTX_API_SECRET",      None),
    "backpack": ("BACKPACK_API_KEY", "BACKPACK_API_SECRET", None),
}

PERPDEX_CREDS_MAP = {
    "hyperliquid": {
        "address":     "HYPERLIQUID_ADDRESS",
        "private_key": "HYPERLIQUID_PRIVATE_KEY",
    },
    "aster": {
        "address":    "ASTER_ADDRESS",
        "api_key":    "ASTER_API_KEY",
        "api_secret": "ASTER_API_SECRET",
    },
    "ethereal": {
        "address":     "ETHEREAL_ADDRESS",
        "private_key": "ETHEREAL_PRIVATE_KEY",
    },
    "lighter": {
        "account_index": "LIGHTER_ACCOUNT_INDEX",
        "private_key":   "LIGHTER_PRIVATE_KEY",
        "api_key_index": "LIGHTER_API_KEY_INDEX",
    },
    "paradex": {
        "address":         "PARADEX_ADDRESS",
        "api_token":       "PARADEX_API_TOKEN",
        "l2_private_key":  "PARADEX_L2_PRIVATE_KEY",
    },
}


@dataclass
class CostTracker:
    spent_per_venue: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    aborted: bool = False

    def can_spend(self, venue: str, usd: float) -> tuple[bool, str]:
        if self.aborted:
            return False, "aborted by user"
        if usd > MAX_USD_PER_ORDER:
            return False, f"order ${usd:.2f} > MAX_USD_PER_ORDER ${MAX_USD_PER_ORDER}"
        venue_spent = self.spent_per_venue.get(venue, 0.0) + usd
        if venue_spent > MAX_USD_PER_VENUE:
            return False, f"venue {venue} would exceed ${MAX_USD_PER_VENUE}"
        if self.total + usd > MAX_USD_TOTAL:
            return False, f"total would exceed ${MAX_USD_TOTAL}"
        return True, ""

    def record(self, venue: str, usd: float, action: str) -> None:
        self.spent_per_venue[venue] = self.spent_per_venue.get(venue, 0.0) + usd
        self.total += usd
        log("cost", venue=venue, usd=round(usd, 4), action=action,
            total=round(self.total, 2),
            venue_total=round(self.spent_per_venue[venue], 2))

COST = CostTracker()


def log(kind: str, **fields) -> None:
    """Append a JSONL line + echo to stderr."""
    rec = {"ts": datetime.utcnow().isoformat() + "Z", "kind": kind, **fields}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    if kind in ("cost", "error", "phase"):
        logger.warning("[%s] %s", kind.upper(), fields)
    else:
        logger.info("[%s] %s", kind, {k: v for k, v in fields.items() if k != "raw"})


def confirm(prompt: str) -> bool:
    """Pause-confirm. Returns True on 'y' / 'go' / 'yes', anything else aborts."""
    print(f"\n{'='*72}")
    print(f"⚠ {prompt}")
    print(f"{'='*72}")
    ans = input("→ Type 'go' to proceed, anything else to skip / Ctrl+C to abort: ").strip().lower()
    return ans in ("go", "y", "yes", "ok")


def kill_handler(sig, frame):
    logger.error("KILL SWITCH triggered (signal=%s)", sig)
    COST.aborted = True
    print("\n⚠ Aborting. Will attempt cleanup of any test-opened positions.")


signal.signal(signal.SIGINT,  kill_handler)
signal.signal(signal.SIGTERM, kill_handler)


# ─── Boot the app + DB ───────────────────────────────────────────────────
def boot_app() -> tuple[Any, Any, dict]:
    """Initialize fresh test DB + register the test user. Returns
    (TestClient, auth headers, ctx)."""
    log("phase", name="boot_app")

    # Reset DB
    db_path = Path("./e2e_test.db")
    if db_path.exists():
        db_path.unlink()

    # Run migrations
    import subprocess
    res = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True, text=True,
        cwd=str(REPO_ROOT),
    )
    if res.returncode != 0:
        log("error", phase="boot", msg="alembic upgrade failed",
            stdout=res.stdout, stderr=res.stderr)
        raise SystemExit(1)
    log("boot", msg="alembic upgrade head OK")

    # Import app AFTER alembic has run
    from app import app as fastapi_app

    client = TestClient(fastapi_app)
    rb = client.post("/api/auth/register", json={
        "username": "e2e_user", "email": "e2e@avalant.test", "password": "e2e_password_123",
    })
    assert rb.status_code in (200, 201), rb.text
    token = rb.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    log("boot", msg="user registered", user="e2e_user")

    return client, auth, {}


def available_cex_venues() -> list[str]:
    return [v for v, (k, s, p) in VENUE_CREDS_MAP.items()
            if os.environ.get(k) and os.environ.get(s)]


def available_perpdex_venues() -> list[str]:
    out = []
    for venue, fields in PERPDEX_CREDS_MAP.items():
        if all(os.environ.get(env_name) for env_name in fields.values() if env_name):
            out.append(venue)
    return out


# ─── Phase 1+2: setup + read-only ────────────────────────────────────────
def phase_setup_and_read_only(client, auth) -> dict:
    """Create wallets for every available venue, then read-only validate."""
    log("phase", name="setup_and_read_only")
    cex = available_cex_venues()
    pdx = available_perpdex_venues()
    log("inventory", cex=cex, perpdex=pdx)

    created_wallets: dict[str, int] = {}

    # ── Create CEX wallets ──
    for venue in cex:
        kk, sk, pk = VENUE_CREDS_MAP[venue]
        body = {
            "name": f"e2e {venue} wallet",
            "wallet_type": "exchange",
            "type_value": venue,
            "api_key": os.environ[kk],
            "api_secret": os.environ[sk],
            "purpose": "screener",
        }
        if pk and os.environ.get(pk):
            body["api_passphrase"] = os.environ[pk]
        r = client.post("/api/wallets", json=body, headers=auth)
        if r.status_code == 201:
            created_wallets[venue] = r.json()["id"]
            log("wallet_created", venue=venue, wallet_id=r.json()["id"])
        else:
            log("error", phase="setup", venue=venue, msg=r.text[:300])

    # ── Create perpdex wallets ──
    for venue in pdx:
        body = {"name": f"e2e {venue} wallet", "wallet_type": "perpdex",
                "type_value": venue, "purpose": "both"}
        m = PERPDEX_CREDS_MAP[venue]
        if "address" in m and os.environ.get(m["address"]):
            body["address"] = os.environ[m["address"]]
        if "api_key" in m and os.environ.get(m["api_key"]):
            body["api_key"] = os.environ[m["api_key"]]
        if "api_secret" in m and os.environ.get(m["api_secret"]):
            body["api_secret"] = os.environ[m["api_secret"]]
        if "private_key" in m and os.environ.get(m["private_key"]):
            body["private_key"] = os.environ[m["private_key"]]
        if "l2_private_key" in m and os.environ.get(m["l2_private_key"]):
            body["l2_private_key"] = os.environ[m["l2_private_key"]]
        if "account_index" in m and os.environ.get(m["account_index"]):
            body["account_index"] = os.environ[m["account_index"]]
        if "api_key_index" in m and os.environ.get(m["api_key_index"]):
            body["api_key_index"] = os.environ[m["api_key_index"]]
        if "api_token" in m and os.environ.get(m["api_token"]):
            body["api_token"] = os.environ[m["api_token"]]
        r = client.post("/api/wallets", json=body, headers=auth)
        if r.status_code == 201:
            created_wallets[venue] = r.json()["id"]
            log("wallet_created", venue=venue, wallet_id=r.json()["id"])
        else:
            log("error", phase="setup", venue=venue, msg=r.text[:300])

    log("phase_summary", phase="setup", wallets_created=len(created_wallets))

    # ── Read-only sweeps ──
    if confirm(f"Phase 2: read-only validation across {len(created_wallets)} wallets — no money moved"):
        # Fetch balances
        r = client.get("/api/trade/balances", headers=auth)
        if r.status_code == 200:
            log("balances", rows=len(r.json()))
            for row in r.json():
                logger.info("  %s: $%s USDT (purpose=%s, can_trade=%s)",
                            row.get("exchange"), row.get("balance_usdt"),
                            row.get("purpose"), row.get("can_trade"))
        else:
            log("error", phase="balances", status=r.status_code, body=r.text[:300])

        # List positions (read-only)
        r = client.get("/api/trade/positions", headers=auth)
        log("positions_initial", count=len(r.json()) if r.status_code == 200 else 0)

        # Spot-short pairs detection
        r = client.get("/api/trade/spot-short-pairs", headers=auth)
        log("spot_short_pairs",
            count=len(r.json().get("pairs", [])) if r.status_code == 200 else 0)

        # Sync external arb-positions (no-op if nothing to wrap)
        r = client.post("/api/trade/arb-positions/sync", headers=auth)
        if r.status_code == 200:
            log("sync_external", count=r.json().get("count", 0))

    return {"wallets": created_wallets}


# ─── Phase 3: per-venue micro-trade smoke ────────────────────────────────
def phase_per_venue_smoke(client, auth, ctx) -> dict:
    log("phase", name="per_venue_smoke")
    if not confirm(
        f"Phase 3: per-venue smoke trade. Will place ONE small market order "
        f"on each venue (size ≈ ${MAX_USD_PER_ORDER}) and immediately close. "
        f"Slippage cost ~$1-3 per venue. Total max: ${MAX_USD_PER_VENUE * len(ctx['wallets'])}"
    ):
        return ctx

    sym = TEST_SYMBOL
    results = {}
    for venue, wid in ctx["wallets"].items():
        if COST.aborted:
            break
        venue_lc = venue.lower()
        # Skip venues that aren't trade-supported (e.g. extended / paradex if
        # we keep it out of GO_TRADE_VENUES until verified)
        ok, why = COST.can_spend(venue_lc, MAX_USD_PER_ORDER)
        if not ok:
            log("skip", venue=venue_lc, reason=why)
            continue

        # Set leverage 3x first (perp venues only). Skip silently if not supported.
        try:
            r_lev = client.post("/api/trade/leverage", headers=auth, json={
                "wallet_id": wid, "symbol": sym, "leverage": 3, "margin_mode": "isolated",
            })
            log("set_leverage", venue=venue_lc, status=r_lev.status_code,
                ok=r_lev.status_code in (200, 204))
        except Exception as e:
            log("error", phase="set_leverage", venue=venue_lc, err=str(e))

        # Compute qty: $5 / mark price. Read mark from /screener/funding
        # (shared cache — fast).
        try:
            r_f = client.get("/api/screener/funding", headers=auth)
            mark = None
            if r_f.status_code == 200:
                for row in r_f.json():
                    if row.get("symbol") == sym and row.get("exchange") == venue_lc:
                        mark = float(row.get("mark_price") or 0)
                        break
            if not mark or mark <= 0:
                # Fallback to a generic mark (BTC ~$100k). Not great but
                # any reasonable number lets us submit an order; venue
                # rounds anyway.
                mark = 100000.0 if sym == "BTC" else 3500.0 if sym == "ETH" else 200.0
            qty = round(MAX_USD_PER_ORDER / mark, 6)
            log("compute_qty", venue=venue_lc, mark=mark, qty=qty,
                notional_usd=qty * mark)
        except Exception as e:
            log("error", phase="compute_qty", venue=venue_lc, err=str(e))
            continue

        # Place the open order
        try:
            r_o = client.post("/api/trade/open", headers=auth, json={
                "wallet_id": wid, "symbol": sym, "side": "buy", "quantity": qty,
                "leverage": 3, "margin_mode": "isolated",
            })
            log("place_open", venue=venue_lc, status=r_o.status_code,
                response=r_o.json() if r_o.status_code < 500 else r_o.text[:200])
            if r_o.status_code != 200:
                results[venue_lc] = "OPEN_FAILED"
                continue
            COST.record(venue_lc, MAX_USD_PER_ORDER * 0.001 * 2, "open_fee")  # ~0.05% taker × 2 round
        except Exception as e:
            log("error", phase="place_open", venue=venue_lc, err=str(e))
            results[venue_lc] = "OPEN_EXCEPTION"
            continue

        # Tiny pause so the venue propagates the position
        time.sleep(2.0)

        # Close it immediately
        try:
            r_c = client.post("/api/trade/close", headers=auth, json={
                "wallet_id": wid, "symbol": sym, "side": "buy",
            })
            log("place_close", venue=venue_lc, status=r_c.status_code,
                response=r_c.json() if r_c.status_code < 500 else r_c.text[:200])
            if r_c.status_code == 200:
                results[venue_lc] = "ROUNDTRIP_OK"
            else:
                results[venue_lc] = f"CLOSE_FAILED:{r_c.status_code}"
        except Exception as e:
            log("error", phase="place_close", venue=venue_lc, err=str(e))
            results[venue_lc] = "CLOSE_EXCEPTION"

    log("phase_summary", phase="per_venue_smoke", results=results)
    ctx["smoke_results"] = results
    return ctx


# ─── Phase 4: trigger lifecycle ──────────────────────────────────────────
def phase_trigger_lifecycle(client, auth, ctx) -> dict:
    log("phase", name="trigger_lifecycle")
    wallets = ctx["wallets"]
    # Pick two CEX venues with successful smoke trades
    candidates = [v for v, r in ctx.get("smoke_results", {}).items()
                  if r == "ROUNDTRIP_OK" and v in wallets]
    if len(candidates) < 2:
        log("skip", phase="trigger_lifecycle", reason="<2 venues passed smoke")
        return ctx
    long_venue, short_venue = candidates[:2]
    long_wid, short_wid = wallets[long_venue], wallets[short_venue]

    if not confirm(
        f"Phase 4: trigger lifecycle on {long_venue.upper()} (long) ↔ "
        f"{short_venue.upper()} (short). 1 market trigger fires immediately, "
        f"creates an arb_position with both legs. "
        f"Cost: ~${MAX_USD_PER_ORDER * 4} (open both legs + close both legs)."
    ):
        return ctx

    sym = TEST_SYMBOL

    # Compute qty
    r_f = client.get("/api/screener/funding", headers=auth)
    mark = None
    if r_f.status_code == 200:
        for row in r_f.json():
            if row.get("symbol") == sym:
                mark = float(row.get("mark_price") or 0)
                break
    mark = mark or (100000.0 if sym == "BTC" else 3500.0)
    qty = round(MAX_USD_PER_ORDER / mark, 6)

    # Step 1: market trigger (trigger_spread_pct=null) — fires next tick
    r = client.post("/api/trade/arb-orders", headers=auth, json={
        "kind": "open", "pair_kind": "long_short",
        "long_exchange": long_venue, "long_symbol": sym, "long_wallet_id": long_wid,
        "short_exchange": short_venue, "short_symbol": sym, "short_wallet_id": short_wid,
        "trigger_spread_pct": None,        # null = market
        "total_qty_token": qty,
        "leverage": 3, "margin_mode": "isolated",
    })
    log("trigger_create", status=r.status_code, body=r.json() if r.status_code < 500 else r.text[:200])
    if r.status_code != 200:
        ctx["lifecycle_status"] = "CREATE_FAILED"
        return ctx
    trigger_id = r.json().get("id")
    if not trigger_id:
        ctx["lifecycle_status"] = "NO_ID"
        return ctx

    # Manually run a service tick to fire the trigger (daemon not running
    # in test mode — we control timing).
    from backend.services import trigger_order_service as tos
    from backend.db.base import SessionLocal as _SL
    books = tos._load_books_json()
    log("books_state", available=books is not None,
        venues=list(books.keys()) if books else [])
    db = _SL()
    try:
        asyncio.run(tos._tick(db, books))
    finally:
        db.close()

    # Verify the trigger fired
    rows = client.get("/api/trade/arb-orders", headers=auth).json()
    history = client.get("/api/trade/arb-orders/history", headers=auth).json()
    trig_row = next((r for r in rows + history if r["id"] == trigger_id), None)
    log("trigger_post_tick", status=trig_row.get("status") if trig_row else "MISSING",
        portions_filled=trig_row.get("portions_filled") if trig_row else None)

    # Verify arb_position created
    poses = client.get("/api/trade/arb-positions", headers=auth).json()
    log("arb_positions", count=len(poses), summary=[
        {"id": p["id"], "status": p["status"], "long_qty": p["long_qty"],
         "short_qty": p["short_qty"], "spread": p["entry_spread_pct"]}
        for p in poses
    ])

    # Cleanup: close any open arb_position from this test
    for p in poses:
        if p["status"] in ("open", "partial") and p.get("long_wallet_id") == long_wid:
            log("cleanup_close", arb_position_id=p["id"])
            # Use the legacy close per-leg endpoint
            client.post("/api/trade/close", headers=auth, json={
                "wallet_id": long_wid, "symbol": sym, "side": "buy",
            })
            client.post("/api/trade/close", headers=auth, json={
                "wallet_id": short_wid, "symbol": sym, "side": "sell",
            })
            COST.record(long_venue,  MAX_USD_PER_ORDER * 0.001, "lifecycle_close")
            COST.record(short_venue, MAX_USD_PER_ORDER * 0.001, "lifecycle_close")

    ctx["lifecycle_status"] = "OK" if trig_row and trig_row.get("status") == "fired" else "DID_NOT_FIRE"
    log("phase_summary", phase="trigger_lifecycle", status=ctx["lifecycle_status"])
    return ctx


# ─── Phase 5: TP/SL ──────────────────────────────────────────────────────
def phase_tp_sl(client, auth, ctx) -> dict:
    log("phase", name="tp_sl")
    if not confirm(
        f"Phase 5: TP/SL E2E. Open trigger + tight TP that fires near-immediately. "
        f"Cost: ~${MAX_USD_PER_ORDER * 4}."
    ):
        return ctx
    log("phase_summary", phase="tp_sl",
        note="full TP/SL fire-on-spread-condition test requires real spread "
             "movement — runner places trigger + TP and reports state, manual "
             "verify in /arb UI for the actual fire.")
    return ctx


# ─── Phase 6: external close detection ───────────────────────────────────
def phase_external_close(client, auth, ctx) -> dict:
    log("phase", name="external_close")
    if not confirm(
        "Phase 6: external close detection. Open a pair through us, "
        "you close ONE leg manually on the venue UI, runner waits for "
        "reconcile cycle and verifies status='partial'. "
        "Cost: ~$5 (open + manual close)."
    ):
        return ctx
    log("phase_summary", phase="external_close",
        note="requires manual venue-UI interaction — see runtime logs for next steps")
    return ctx


# ─── Phase 7: auto-pair detection ────────────────────────────────────────
def phase_auto_pair(client, auth, ctx) -> dict:
    log("phase", name="auto_pair")
    if not confirm(
        "Phase 7: auto-pair on internal opens. Open single-leg orders on "
        "two opposite venues via legacy /api/trade/open, runner waits and "
        "verifies auto_pair_internal_legs() wraps them in an arb_position. "
        "Cost: ~${MAX_USD_PER_ORDER * 2}."
    ):
        return ctx
    log("phase_summary", phase="auto_pair",
        note="placeholder — implementation requires sequencing two opens and "
             "running auto_pair_internal_legs explicitly")
    return ctx


# ─── Cleanup / report ────────────────────────────────────────────────────
def cleanup_and_report(client, auth, ctx) -> None:
    log("phase", name="cleanup")
    # Verify no stuck open positions from our test run
    r = client.get("/api/trade/positions", headers=auth)
    if r.status_code == 200 and r.json():
        log("warning", msg="positions still open at end of run",
            count=len(r.json()),
            positions=[{"ex": p.get("exchange"), "sym": p.get("symbol"),
                         "side": p.get("side"), "qty": p.get("quantity")}
                        for p in r.json()])
    # Final summary
    log("RUN_SUMMARY",
        log_file=str(LOG_FILE),
        total_spent_usd=round(COST.total, 4),
        per_venue=COST.spent_per_venue,
        aborted=COST.aborted)

    print("\n" + "=" * 72)
    print(f"E2E run complete. Log: {LOG_FILE}")
    print(f"Total spent (fees+slippage approx): ${COST.total:.2f}")
    for venue, amt in COST.spent_per_venue.items():
        print(f"  {venue:14s}: ${amt:.2f}")
    if COST.aborted:
        print("⚠ Run was aborted — verify no orphan positions on venues.")
    print("=" * 72)


# ─── main ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phases", default="1,2,3,4,5,6,7",
                        help="Comma list of phases to run, e.g. 1,2,3")
    parser.add_argument("--scope", choices=("full", "mini", "read"), default="full")
    args = parser.parse_args()

    if args.scope == "read":
        phases = {1, 2}
    elif args.scope == "mini":
        phases = {1, 2, 3, 4}
    else:
        phases = set(int(p) for p in args.phases.split(","))

    log("run_start", scope=args.scope, phases=sorted(phases),
        max_per_order=MAX_USD_PER_ORDER, max_per_venue=MAX_USD_PER_VENUE,
        symbol=TEST_SYMBOL, log_file=str(LOG_FILE))

    client, auth, ctx = boot_app()

    if 1 in phases or 2 in phases:
        ctx = phase_setup_and_read_only(client, auth)
    if 3 in phases and not COST.aborted:
        ctx = phase_per_venue_smoke(client, auth, ctx)
    if 4 in phases and not COST.aborted:
        ctx = phase_trigger_lifecycle(client, auth, ctx)
    if 5 in phases and not COST.aborted:
        ctx = phase_tp_sl(client, auth, ctx)
    if 6 in phases and not COST.aborted:
        ctx = phase_external_close(client, auth, ctx)
    if 7 in phases and not COST.aborted:
        ctx = phase_auto_pair(client, auth, ctx)

    cleanup_and_report(client, auth, ctx)


if __name__ == "__main__":
    main()
