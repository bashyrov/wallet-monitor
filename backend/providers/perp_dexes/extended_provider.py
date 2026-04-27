"""Extended (StarkNet perpetuals) — placeholder provider so the venue
appears in the portfolio picker in parity with the screener feed.

Public balance/account endpoints (`/api/v1/account/*`) currently return
404 — Extended only exposes user data via signed StarkNet typed-data
requests, which we don't have a clean wrapper for yet. Until that's
wired, the provider is shipped with `soon = True`: the wallet form
shows a "Soon" badge on this option and blocks submission. Funding-rate
and orderbook data continue to flow through arbitrage_service /
orderbook_cache untouched."""

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider


class ExtendedProvider(BaseWalletProvider):
    name = "ExtendedProvider"
    label = "Extended"
    enabled = True
    soon = True            # gates the UI: option visible but not connectable
    needs_api_key = False

    async def fetch_balance(self, wallet) -> BalanceResult:
        raise NotImplementedError(
            "Extended portfolio integration is coming soon — public REST "
            "balance API is not yet exposed. Use the screener feed for "
            "Extended funding rates and orderbooks in the meantime."
        )
