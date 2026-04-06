"""Fetch balances from providers for one or many wallets."""
import asyncio
import logging
from collections import defaultdict
from decimal import Decimal

from backend.db.models import Wallet
from backend.domain.models import BalanceResult
from backend.providers.utils import STABLE_COINS
from backend.schemas.portfolio import WalletBalanceResult, AggregatedBalance, BalanceResponse

logger = logging.getLogger("avalant.balance")


async def _fetch_single(db_wallet: Wallet) -> tuple[BalanceResult | None, str | None]:
    """Returns (BalanceResult | None, error_str | None)."""
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
            return None, f"Unknown wallet type: {db_wallet.wallet_type}"

        provider_instance = wallet_obj.provider()
        result = await provider_instance.fetch_balance(wallet=wallet_obj)
        return result, None

    except Exception as e:
        msg = str(e)
        logger.error(
            "Provider error for wallet %s (%s/%s): %s",
            db_wallet.id, db_wallet.wallet_type, db_wallet.type_value, msg,
            exc_info=True,
        )
        if "401" in msg or "403" in msg or "Unauthorized" in msg or "Invalid" in msg:
            friendly = "Invalid API credentials"
        elif "timeout" in msg.lower() or "connect" in msg.lower() or "network" in msg.lower():
            friendly = "Provider unavailable — try again later"
        elif "429" in msg or "rate" in msg.lower():
            friendly = "Rate limit exceeded — try again later"
        else:
            friendly = "Failed to fetch — try again later"
        return None, friendly

    finally:
        if provider_instance:
            aclose = getattr(provider_instance, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass


async def fetch_balances(db_wallets: list[Wallet]) -> BalanceResponse:
    raw = await asyncio.gather(
        *[_fetch_single(w) for w in db_wallets],
        return_exceptions=True,
    )

    results: list[WalletBalanceResult] = []
    aggregated_totals: dict[str, Decimal] = defaultdict(Decimal)

    for db_wallet, item in zip(db_wallets, raw):
        if isinstance(item, Exception):
            result, error = None, str(item)
        else:
            result, error = item

        if result is None:
            results.append(WalletBalanceResult(
                wallet_id=db_wallet.id,
                wallet_name=db_wallet.name,
                wallet_type=db_wallet.wallet_type,
                type_value=db_wallet.type_value,
                totals={},
                details={},
                error=error or "Unknown error",
            ))
        else:
            totals = result.totals or {}
            for asset, amt in totals.items():
                aggregated_totals[asset] += Decimal(str(amt))
            results.append(WalletBalanceResult(
                wallet_id=db_wallet.id,
                wallet_name=db_wallet.name,
                wallet_type=db_wallet.wallet_type,
                type_value=db_wallet.type_value,
                totals=totals,
                details=result.details or {},
                error=None,
            ))

    stable: dict[str, str] = {}
    other: dict[str, str] = {}
    for asset, amt in aggregated_totals.items():
        normalized = asset.upper().replace("_PERP", "")
        if normalized in STABLE_COINS:
            stable[asset] = str(amt)
        else:
            other[asset] = str(amt)

    stable_total = str(sum(Decimal(v) for v in stable.values()) if stable else Decimal("0"))

    return BalanceResponse(
        results=results,
        aggregated=AggregatedBalance(
            stable=stable,
            other=other,
            stable_total=stable_total,
        ),
    )
