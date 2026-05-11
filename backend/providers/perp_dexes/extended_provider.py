"""Extended (StarkEx perpetuals) — full trading via go-fetcher.

Trading is implemented in `go-fetcher/internal/trade/extended/extended.go`
using Stark L2 signing (Poseidon hash + curve.SignFelts). Python side is
read-only — balance lookups for Portfolio go through the Go HTTP proxy.

Credentials the user supplies (4 fields, all required for trading):
    api_key      — from Extended UI's API Management page
    private_key  — Stark L2 private key (hex)
    api_passphrase — vault / collateral_position_id (decimal int as string)
    address      — Stark L2 public key (hex)  ← stored in `wallet` slot
                   (UI labels this as "public_key" or "wallet")

The Go adapter maps these on the wire as:
    Creds.APIKey      ← api_key
    Creds.APISecret   ← private_key
    Creds.Passphrase  ← vault (api_passphrase)
    Creds.Wallet      ← public_key (address)

Add `extended` to GO_TRADE_VENUES on the web role to route trades via Go;
otherwise the Python adapter returns the readonly-style "trading via Go"
error from the dispatcher.

NOTE: Stark order signing has NO live cross-vector vs the x10 Python SDK
yet — first real order on Extended testnet is the truth check (same caveat
as Paradex)."""

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider


class ExtendedProvider(BaseWalletProvider):
    name = "ExtendedProvider"
    label = "Extended"
    enabled = True
    soon = False             # trading shipped; balance fetch via Go proxy
    needs_api_key = True     # Extended UI API key (HTTP auth on GETs)
    needs_l2_private_key = True   # Stark L2 private key (for signing orders)
    needs_vault = True       # collateral_position_id — int subaccount id

    async def fetch_balance(self, wallet) -> BalanceResult:
        """Read-only balance via the Go /internal/trade/balance endpoint.
        Extended has no public balance REST; the Go adapter handles the
        Stark-signed request. Independent of GO_TRADE_VENUES (that gate
        only controls write-path routing — reads always go via Go since
        there's no Python alternative)."""
        from decimal import Decimal
        from backend.services import trade_proxy
        creds = {
            "api_key":        getattr(wallet, "api_key", None),
            "api_secret":     getattr(wallet, "private_key", None) or getattr(wallet, "api_secret", None),
            "api_passphrase": getattr(wallet, "api_passphrase", None),
            "address":        getattr(wallet, "address", None),
        }
        bal = await trade_proxy.fetch_balance("extended", creds)
        total = Decimal(str(bal.get("usdt") or bal.get("total") or 0))
        # Extended is futures-only — represent the netEquity as a USDC
        # spot-like balance for the portfolio aggregator since BalanceResult
        # expects {asset: amount} maps.
        spot: dict[str, Decimal] = {}
        if total > 0:
            spot["USDC"] = total
        return self._build_result(wallet, self.name, spot=spot, futures={}, earn={})
