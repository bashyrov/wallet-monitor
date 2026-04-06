import time
from urllib.parse import urlencode

import httpx
from decimal import Decimal
from eth_account import Account
from eth_account.messages import encode_typed_data

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider

BASE = "https://fapi.asterdex.com"

_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
_TYPES = {
    "AsterSignTransaction": [{"name": "msg", "type": "string"}],
}


def _eip712_sign(private_key: str, msg: str) -> str:
    structured = encode_typed_data(
        domain_data=_DOMAIN,
        message_types=_TYPES,
        message_data={"msg": msg},
    )
    signed = Account.sign_message(structured, private_key=private_key)
    return "0x" + signed.signature.hex()


class AsterProvider(BaseWalletProvider):
    name = "AsterProvider"
    label = "Aster"
    enabled = True
    needs_api_key = True
    soon = True

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=20.0)

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _d(value) -> Decimal:
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal("0")

    async def fetch_balance(self, wallet) -> BalanceResult:
        user = wallet.api_key          # main wallet address
        private_key = wallet.api_secret

        if not user or not private_key:
            raise ValueError("Aster requires wallet address (api_key) and API private key (api_secret)")

        signer = Account.from_key(private_key).address
        nonce = int(time.time() * 1_000) * 1_000_000  # microseconds

        params = {"user": user, "signer": signer, "nonce": nonce}
        msg = urlencode(params)
        signature = _eip712_sign(private_key, msg)
        params["signature"] = signature

        resp = await self._client.get(f"{BASE}/fapi/v3/balance", params=params)
        resp.raise_for_status()
        assets = resp.json()

        totals: dict[str, Decimal] = {}
        for item in (assets if isinstance(assets, list) else []):
            symbol = (item.get("asset") or "").upper()
            bal = self._d(item.get("balance") or item.get("crossWalletBalance"))
            if symbol and bal > 0:
                totals[symbol] = totals.get(symbol, Decimal("0")) + bal

        positions = []
        try:
            nonce2 = int(time.time() * 1_000) * 1_000_000
            p2 = {"user": user, "signer": signer, "nonce": nonce2}
            p2["signature"] = _eip712_sign(private_key, urlencode(p2))
            pos_resp = await self._client.get(f"{BASE}/fapi/v3/positionRisk", params=p2)
            if pos_resp.is_success:
                for pos in (pos_resp.json() if isinstance(pos_resp.json(), list) else []):
                    notional = self._d(pos.get("notionalValue") or pos.get("notional"))
                    if notional == 0:
                        continue
                    positions.append({
                        "symbol": pos.get("symbol", ""),
                        "side": pos.get("positionSide", ""),
                        "notional": str(notional),
                        "unrealized_pnl": str(self._d(pos.get("unRealizedProfit") or pos.get("unrealizedProfit"))),
                        "entry_price": str(self._d(pos.get("entryPrice"))),
                        "mark_price": str(self._d(pos.get("markPrice"))),
                        "leverage": pos.get("leverage", 1),
                    })
        except Exception:
            pass

        totals_str = {k: str(v) for k, v in totals.items() if v > 0}
        return self._build_result(
            wallet, self.name,
            spot=totals_str,
            futures={},
            earn={"positions": positions} if positions else {},
        )
