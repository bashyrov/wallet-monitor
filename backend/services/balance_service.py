"""Fetch balances from providers for one or many wallets."""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.db.models import Wallet, BalanceSnapshot, ProviderErrorLog, BalanceHistory
from backend.domain.models import BalanceResult
from backend.providers.utils import STABLE_COINS
from backend.schemas.portfolio import WalletBalanceResult, AggregatedBalance, BalanceResponse, PnL
from backend.services.price_service import get_usd_value, STABLES as PRICE_STABLES

logger = logging.getLogger("avalant.balance")


def _compute_pnl(current: Decimal, previous: Decimal) -> PnL | None:
    """Return PnL object or None if not enough data."""
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
    """Returns (BalanceResult | None, error_str | None, error_type | None).
    error_type: 'rate_limit' | 'auth' | 'network' | 'unknown' | None
    """
    from backend.crypto import decrypt_credentials
    creds = decrypt_credentials(db_wallet.credentials or {})
    provider_instance = None

    try:
        if db_wallet.wallet_type == "exchange":
            from backend.schemas.wallets import ExchangeWalletSchema
            from backend.domain.models import ExchangeWallet

            wallet_data = {
                "name": db_wallet.name,
                "user": "default",
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
                "name": db_wallet.name,
                "user": "default",
                "chain": db_wallet.type_value,
                "address": creds.get("address", ""),
            }
            validated = ChainWalletSchema.model_validate(wallet_data)
            wallet_obj = ChainWallet(**validated.model_dump())

        elif db_wallet.wallet_type == "perpdex":
            from backend.domain.models import PerpDexWallet

            wallet_obj = PerpDexWallet(
                name=db_wallet.name,
                user="default",
                perp_dex=db_wallet.type_value,
                address=creds.get("address", ""),
                api_key=creds.get("api_key"),
                api_secret=creds.get("api_secret"),
            )

        else:
            return None, f"Unknown wallet type: {db_wallet.wallet_type}", "unknown"

        provider_instance = wallet_obj.provider()
        result = await provider_instance.fetch_balance(wallet=wallet_obj)
        return result, None, None

    except Exception as e:
        msg = str(e)
        logger.error(
            "Provider error for wallet %s (%s/%s): %s",
            db_wallet.id, db_wallet.wallet_type, db_wallet.type_value, msg,
            exc_info=True,
        )
        if "429" in msg or "rate" in msg.lower():
            friendly = "Rate limit exceeded — try again later"
            etype = "rate_limit"
        elif "401" in msg or "403" in msg or "Unauthorized" in msg or "Invalid" in msg:
            friendly = "Invalid API credentials"
            etype = "auth"
        elif "timeout" in msg.lower() or "connect" in msg.lower() or "network" in msg.lower():
            friendly = "Provider unavailable — try again later"
            etype = "network"
        else:
            friendly = "Failed to fetch — try again later"
            etype = "unknown"
        return None, friendly, etype

    finally:
        if provider_instance:
            aclose = getattr(provider_instance, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass


async def fetch_balances(db_wallets: list[Wallet], db: Session) -> BalanceResponse:
    # 1. Load previous snapshots before fetching
    wallet_ids = [w.id for w in db_wallets]
    prev_snapshots: dict[int, BalanceSnapshot] = {
        s.wallet_id: s
        for s in db.query(BalanceSnapshot).filter(BalanceSnapshot.wallet_id.in_(wallet_ids)).all()
    }

    # 2. Fetch current balances
    raw = await asyncio.gather(
        *[_fetch_single(w) for w in db_wallets],
        return_exceptions=True,
    )

    results: list[WalletBalanceResult] = []
    aggregated_totals: dict[str, Decimal] = defaultdict(Decimal)
    prev_agg_stable = Decimal("0")
    curr_agg_stable = Decimal("0")

    now = datetime.utcnow()

    for db_wallet, item in zip(db_wallets, raw):
        if isinstance(item, Exception):
            result, error, etype = None, str(item), "unknown"
        else:
            result, error, etype = item

        prev_snap = prev_snapshots.get(db_wallet.id)
        prev_stable = Decimal(str(prev_snap.stable_total)) if prev_snap else Decimal("0")
        prev_agg_stable += prev_stable if prev_snap else Decimal("0")

        if result is None:
            results.append(WalletBalanceResult(
                wallet_id=db_wallet.id,
                wallet_name=db_wallet.name,
                wallet_type=db_wallet.wallet_type,
                type_value=db_wallet.type_value,
                totals={},
                usd_values={},
                usd_total="0",
                details={},
                error=error or "Unknown error",
                pnl=None,
            ))
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
            curr_agg_stable += curr_stable

            for asset, amt in totals.items():
                aggregated_totals[asset] += Decimal(str(amt))

            # USD values per token
            usd_values: dict[str, str] = {}
            wallet_usd_total = Decimal("0")
            for asset, amt in totals.items():
                uv = get_usd_value(asset, amt)
                if uv is not None:
                    usd_values[asset] = f"{uv:.2f}"
                    wallet_usd_total += Decimal(str(uv))

            pnl = _compute_pnl(curr_stable, prev_stable) if prev_snap else None

            results.append(WalletBalanceResult(
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
            ))

            # 3. Upsert snapshot
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

    # Write balance history for Owner-tagged wallets
    owner_wallets = [w for w in db_wallets if any(t.name == "Owner" for t in (w.tags or []))]
    if owner_wallets:
        owner_ids = {w.id for w in owner_wallets}
        owner_usd = sum(
            Decimal(r.usd_total) for r in results
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

    try:
        db.commit()
    except Exception:
        db.rollback()

    # 4. Aggregate
    stable: dict[str, str] = {}
    other: dict[str, str] = {}
    for asset, amt in aggregated_totals.items():
        normalized = asset.upper().replace("_PERP", "")
        if normalized in STABLE_COINS:
            stable[asset] = str(amt)
        else:
            other[asset] = str(amt)

    stable_total = str(sum(Decimal(v) for v in stable.values()) if stable else Decimal("0"))

    # USD values for each aggregated symbol (computed from total amounts × price)
    agg_usd_values: dict[str, str] = {}
    for asset, amt in aggregated_totals.items():
        uv = get_usd_value(asset, str(amt))
        if uv is not None:
            agg_usd_values[asset] = f"{uv:.2f}"

    # Grand USD total from aggregated USD values (avoids double-counting from per-wallet)
    grand_usd = sum(Decimal(v) for v in agg_usd_values.values())

    # Aggregate PnL — only if at least one wallet had a previous snapshot
    agg_pnl = None
    if any(db_wallet.id in prev_snapshots for db_wallet in db_wallets):
        agg_pnl = _compute_pnl(curr_agg_stable, prev_agg_stable)

    return BalanceResponse(
        results=results,
        aggregated=AggregatedBalance(
            stable=stable,
            other=other,
            stable_total=stable_total,
            usd_total=f"{grand_usd:.2f}",
            usd_values=agg_usd_values,
            pnl=agg_pnl,
        ),
    )
