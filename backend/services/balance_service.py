"""Fetch balances from providers for one or many wallets."""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator

from sqlalchemy.orm import Session

from backend.db.models import Wallet, BalanceSnapshot, ProviderErrorLog, BalanceHistory
from backend.domain.models import BalanceResult
from backend.providers.utils import STABLE_COINS
from backend.schemas.portfolio import WalletBalanceResult, AggregatedBalance, BalanceResponse, PnL
from backend.services.price_service import get_usd_value, STABLES as PRICE_STABLES

logger = logging.getLogger("avalant.balance")


def _compute_pnl(current: Decimal, previous: Decimal) -> PnL | None:
    if previous == 0 and current == 0:
        return None
    diff = current - previous
    if diff == 0:
        return PnL(prev=str(previous), abs="0.00", pct="0.00", direction="flat")
    pct = (diff / previous * 100) if previous != 0 else Decimal("0")
    sign = "+" if diff > 0 else ""
    return PnL(
        prev=f"{previous:.2f}",
        abs=f"{sign}{diff:.2f}",
        pct=f"{sign}{pct:.2f}",
        direction="up" if diff > 0 else "down",
    )


def _stable_sum(totals: dict[str, str]) -> Decimal:
    total = Decimal("0")
    for asset, amt in totals.items():
        if asset.upper().replace("_PERP", "") in STABLE_COINS:
            try:
                total += Decimal(str(amt))
            except Exception:
                pass
    return total


async def _fetch_single(db_wallet: Wallet) -> tuple[BalanceResult | None, str | None, str | None]:
    """Returns (BalanceResult | None, error_str | None, error_type | None)."""
    from backend.crypto import decrypt_credentials
    creds = decrypt_credentials(db_wallet.credentials or {})
    provider_instance = None

    try:
        if db_wallet.wallet_type == "exchange":
            from backend.schemas.wallets import ExchangeWalletSchema
            from backend.domain.models import ExchangeWallet
            wallet_data = {
                "name": db_wallet.name, "user": "default",
                "exchange": db_wallet.type_value,
                "api_key": creds.get("api_key", ""),
                "api_secret": creds.get("api_secret", ""),
                "api_passphrase": creds.get("api_passphrase"),
            }
            validated = ExchangeWalletSchema.model_validate(wallet_data)
            wallet_obj = ExchangeWallet(**validated.model_dump())

        elif db_wallet.wallet_type == "chain":
            from backend.schemas.wallets import ChainWalletSchema
            from backend.domain.models import ChainWallet
            wallet_data = {
                "name": db_wallet.name, "user": "default",
                "chain": db_wallet.type_value,
                "address": creds.get("address", ""),
            }
            validated = ChainWalletSchema.model_validate(wallet_data)
            wallet_obj = ChainWallet(**validated.model_dump())

        elif db_wallet.wallet_type == "perpdex":
            from backend.domain.models import PerpDexWallet
            wallet_obj = PerpDexWallet(
                name=db_wallet.name, user="default",
                perp_dex=db_wallet.type_value,
                address=creds.get("address", ""),
                api_key=creds.get("api_key"),
                api_secret=creds.get("api_secret"),
            )
        else:
            return None, f"Unknown wallet type: {db_wallet.wallet_type}", "unknown"

        provider_instance = wallet_obj.provider()
        # Hard cap per-wallet fetch — no single exchange should hold up a user
        # for more than ~25s. Retries + slow APIs otherwise stack to minutes.
        result = await asyncio.wait_for(
            provider_instance.fetch_balance(wallet=wallet_obj),
            timeout=25.0,
        )
        return result, None, None

    except asyncio.TimeoutError:
        logger.warning(
            "Provider timeout for wallet %s (%s/%s): exceeded 25s",
            db_wallet.id, db_wallet.wallet_type, db_wallet.type_value,
        )
        return None, "Provider took too long (>25s) — try again later", "network"
    except Exception as e:
        msg = str(e)
        logger.error(
            "Provider error for wallet %s (%s/%s): %s",
            db_wallet.id, db_wallet.wallet_type, db_wallet.type_value, msg,
            exc_info=True,
        )
        if "429" in msg or "rate" in msg.lower():
            return None, "Rate limit exceeded — try again later", "rate_limit"
        elif "401" in msg or "403" in msg or "Unauthorized" in msg or "Invalid" in msg:
            return None, "Invalid API credentials", "auth"
        elif "timeout" in msg.lower() or "connect" in msg.lower() or "network" in msg.lower():
            return None, "Provider unavailable — try again later", "network"
        else:
            return None, "Failed to fetch — try again later", "unknown"

    finally:
        if provider_instance:
            aclose = getattr(provider_instance, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass


def _build_wallet_result(
    db_wallet: Wallet,
    result: BalanceResult | None,
    error: str | None,
    etype: str | None,
    prev_snap: BalanceSnapshot | None,
    now: datetime,
) -> tuple[WalletBalanceResult, Decimal]:
    """Build WalletBalanceResult; returns (result, curr_stable)."""
    if result is None:
        return WalletBalanceResult(
            wallet_id=db_wallet.id,
            wallet_name=db_wallet.name,
            wallet_type=db_wallet.wallet_type,
            type_value=db_wallet.type_value,
            totals={}, usd_values={}, usd_total="0",
            details={}, error=error or "Unknown error", pnl=None,
        ), Decimal("0")

    totals = result.totals or {}
    curr_stable = _stable_sum(totals)
    prev_stable = Decimal(str(prev_snap.stable_total)) if prev_snap else Decimal("0")

    usd_values: dict[str, str] = {}
    wallet_usd_total = Decimal("0")
    for asset, amt in totals.items():
        uv = get_usd_value(asset, amt)
        if uv is not None:
            usd_values[asset] = f"{uv:.2f}"
            wallet_usd_total += Decimal(str(uv))

    pnl = _compute_pnl(curr_stable, prev_stable) if prev_snap else None

    return WalletBalanceResult(
        wallet_id=db_wallet.id,
        wallet_name=db_wallet.name,
        wallet_type=db_wallet.wallet_type,
        type_value=db_wallet.type_value,
        totals=totals,
        usd_values=usd_values,
        usd_total=f"{wallet_usd_total:.2f}",
        details=result.details or {},
        error=None,
        pnl=pnl,
    ), curr_stable


async def fetch_balances_stream(
    db_wallets: list[Wallet],
    db: Session,
) -> AsyncIterator[dict]:
    """
    Async generator. Yields dicts:
      {"type": "wallet", "result": WalletBalanceResult-dict}  — one per wallet as it completes
      {"type": "done",   "aggregated": AggregatedBalance-dict} — final, after all done
    """
    now = datetime.utcnow()

    # Load previous snapshots once upfront
    wallet_ids = [w.id for w in db_wallets]
    prev_snapshots: dict[int, BalanceSnapshot] = {
        s.wallet_id: s
        for s in db.query(BalanceSnapshot).filter(BalanceSnapshot.wallet_id.in_(wallet_ids)).all()
    }

    # Queue receives (wallet, result, error, etype) as each finishes
    queue: asyncio.Queue = asyncio.Queue()

    async def _work(wallet: Wallet) -> None:
        res, err, etype = await _fetch_single(wallet)
        await queue.put((wallet, res, err, etype))

    tasks = [asyncio.create_task(_work(w)) for w in db_wallets]

    collected: list[tuple] = []  # (db_wallet, wallet_result, result, error, etype, prev_snap)
    aggregated_totals: dict[str, Decimal] = defaultdict(Decimal)
    curr_agg_stable = Decimal("0")
    prev_agg_stable = Decimal("0")

    for prev_snap in prev_snapshots.values():
        prev_agg_stable += Decimal(str(prev_snap.stable_total))

    for _ in db_wallets:
        db_wallet, result, error, etype = await queue.get()
        prev_snap = prev_snapshots.get(db_wallet.id)

        wallet_result, curr_stable = _build_wallet_result(
            db_wallet, result, error, etype, prev_snap, now
        )
        collected.append((db_wallet, wallet_result, result, error, etype, prev_snap))
        curr_agg_stable += curr_stable

        if result is not None:
            for asset, amt in (result.totals or {}).items():
                aggregated_totals[asset] += Decimal(str(amt))

        yield {"type": "wallet", "result": wallet_result.model_dump()}

    # All done — write to DB
    try:
        for db_wallet, wallet_result, result, error, etype, prev_snap in collected:
            if result is None:
                try:
                    db.add(ProviderErrorLog(
                        wallet_type=db_wallet.wallet_type,
                        type_value=db_wallet.type_value,
                        error_type=etype or "unknown",
                        created_at=now,
                    ))
                except Exception:
                    pass
            else:
                totals = result.totals or {}
                curr_stable = _stable_sum(totals)
                try:
                    if prev_snap:
                        prev_snap.totals = totals
                        prev_snap.stable_total = float(curr_stable)
                        prev_snap.snapshot_at = now
                    else:
                        db.add(BalanceSnapshot(
                            wallet_id=db_wallet.id,
                            user_id=db_wallet.user_id,
                            totals=totals,
                            stable_total=float(curr_stable),
                            snapshot_at=now,
                        ))
                except Exception:
                    pass

        # Balance history for Owner-tagged wallets
        owner_wallets = [w for w, *_ in collected if any(t.name == "Owner" for t in (w.tags or []))]
        if owner_wallets:
            owner_ids = {w.id for w in owner_wallets}
            owner_usd = sum(
                Decimal(r.usd_total) for _, r, *_ in collected
                if r.wallet_id in owner_ids and not r.error
            )
            try:
                db.add(BalanceHistory(
                    user_id=owner_wallets[0].user_id,
                    usd_total=float(owner_usd),
                    snapshot_at=now,
                ))
            except Exception:
                pass

        db.commit()
    except Exception:
        db.rollback()

    # Compute aggregated
    stable: dict[str, str] = {}
    other: dict[str, str] = {}
    for asset, amt in aggregated_totals.items():
        normalized = asset.upper().replace("_PERP", "")
        if normalized in STABLE_COINS:
            stable[asset] = str(amt)
        else:
            other[asset] = str(amt)

    stable_total = str(sum(Decimal(v) for v in stable.values()) if stable else Decimal("0"))

    agg_usd_values: dict[str, str] = {}
    for asset, amt in aggregated_totals.items():
        uv = get_usd_value(asset, str(amt))
        if uv is not None:
            agg_usd_values[asset] = f"{uv:.2f}"

    grand_usd = sum(Decimal(v) for v in agg_usd_values.values())

    agg_pnl = None
    if prev_snapshots:
        agg_pnl = _compute_pnl(curr_agg_stable, prev_agg_stable)

    aggregated = AggregatedBalance(
        stable=stable, other=other,
        stable_total=stable_total,
        usd_total=f"{grand_usd:.2f}",
        usd_values=agg_usd_values,
        pnl=agg_pnl,
    )
    yield {"type": "done", "aggregated": aggregated.model_dump()}

    # Cleanup tasks (they should all be done)
    await asyncio.gather(*tasks, return_exceptions=True)


async def fetch_balances(db_wallets: list[Wallet], db: Session) -> BalanceResponse:
    """Non-streaming wrapper — collects all results then returns."""
    results: list[WalletBalanceResult] = []
    aggregated = None
    async for event in fetch_balances_stream(db_wallets, db):
        if event["type"] == "wallet":
            results.append(WalletBalanceResult(**event["result"]))
        elif event["type"] == "done":
            aggregated = AggregatedBalance(**event["aggregated"])
    return BalanceResponse(results=results, aggregated=aggregated)
