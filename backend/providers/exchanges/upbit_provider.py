"""
Upbit exchange provider (Korean CEX, spot-only).
Auth: JWT (HS256) in `Authorization: Bearer <jwt>`.
JWT payload: {access_key, nonce (UUID4)} — plus
{query_hash: SHA512(query string), query_hash_alg: "SHA512"} when the
request carries query params. /v1/accounts is parameterless.
"""
import hashlib
import uuid
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

from jose import jwt

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider
from backend.providers.http import RetryClient

UPBIT_BASE = "https://api.upbit.com"


def upbit_auth_headers(api_key: str, api_secret: str, params: dict | None = None) -> dict[str, str]:
    payload: dict = {"access_key": api_key.strip(), "nonce": str(uuid.uuid4())}
    if params:
        qs = urlencode(params, doseq=True)
        payload["query_hash"] = hashlib.sha512(qs.encode()).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    token = jwt.encode(payload, api_secret.strip(), algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


class UpbitProvider(BaseWalletProvider):
    name = "UpbitProvider"
    label = "Upbit"
    enabled = True
    needs_passphrase = False
    needs_api_key = True
    base_url = UPBIT_BASE

    def __init__(self) -> None:
        self._http = RetryClient(timeout=12)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        headers = upbit_auth_headers(wallet.api_key, wallet.api_secret)
        r = await self._http.get(f"{self.base_url}/v1/accounts", headers=headers)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Upbit API error: {data['error']}")

        spot: defaultdict = defaultdict(Decimal)
        for row in data or []:
            ccy = (row.get("currency") or "").strip().upper()
            if not ccy:
                continue
            amt = Decimal(str(row.get("balance") or "0")) + Decimal(str(row.get("locked") or "0"))
            if amt != 0:
                spot[ccy] += amt

        return self._build_result(wallet, self.name, dict(spot), {}, {})
