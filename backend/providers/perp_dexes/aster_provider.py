"""Aster DEX — V3 Pro API with EIP-712 Web3-native authentication.

Auth flow:
  1. User creates an API wallet at asterdex.com/en/api-wallet
  2. api_key   = main account wallet address (login wallet)
  3. api_secret = private key of the API signer wallet
  4. signer address is derived from private key via eth_account

Signing:
  - nonce  = int(time.time()) * 1_000_000  (microseconds)
  - params = urlencode({all params including nonce, user, signer})
  - EIP-712 sign params string → append &signature=0x...
  - Base URL: https://fapi.asterdex.com  (V3 endpoints on fapi, not fapi3)
"""
import threading
import time
import urllib.parse
from decimal import Decimal

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider

BASE = "https://fapi.asterdex.com"

_TYPED_DATA_TEMPLATE = {
    "types": {
        "EIP712Domain": [
            {"name": "name",             "type": "string"},
            {"name": "version",          "type": "string"},
            {"name": "chainId",          "type": "uint256"},
            {"name": "verifyingContract","type": "address"},
        ],
        "Message": [
            {"name": "msg", "type": "string"},
        ],
    },
    "primaryType": "Message",
    "domain": {
        "name": "AsterSignTransaction",
        "version": "1",
        "chainId": 1666,
        "verifyingContract": "0x0000000000000000000000000000000000000000",
    },
    "message": {"msg": ""},
}

# Thread-safe monotonic nonce (microseconds)
_nonce_lock = threading.Lock()
_last_sec = 0
_nonce_counter = 0


def _get_nonce() -> int:
    global _last_sec, _nonce_counter
    with _nonce_lock:
        now = int(time.time())
        if now == _last_sec:
            _nonce_counter += 1
        else:
            _last_sec = now
            _nonce_counter = 0
        return now * 1_000_000 + _nonce_counter


def _eip712_sign(private_key: str, msg: str) -> str:
    td = dict(_TYPED_DATA_TEMPLATE)
    td["message"] = {"msg": msg}
    signed = Account.sign_message(encode_typed_data(full_message=td), private_key=private_key)
    return signed.signature.hex()


class AsterProvider(BaseWalletProvider):
    name = "AsterProvider"
    label = "Aster"
    enabled = True
    needs_api_key = True
    soon = False

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "PythonApp/1.0", "Content-Type": "application/x-www-form-urlencoded"},
        )

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _d(v) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception:
            return Decimal("0")

    async def _signed_get(self, path: str, user: str, signer: str, private_key: str,
                          extra: dict | None = None) -> httpx.Response:
        params: dict = {}
        if extra:
            params.update(extra)
        params["nonce"] = str(_get_nonce())
        params["user"] = user
        params["signer"] = signer
        msg = urllib.parse.urlencode(params)
        sig = _eip712_sign(private_key, msg)
        url = f"{BASE}{path}?{msg}&signature={sig}"
        return await self._client.get(url)

    async def fetch_balance(self, wallet) -> BalanceResult:
        user = wallet.api_key        # main wallet address
        private_key = wallet.api_secret

        if not user or not private_key:
            raise ValueError("Aster requires wallet address (api_key) and API signer private key (api_secret)")

        signer = Account.from_key(private_key).address

        # ── Баланс ───────────────────────────────────────────────────────────
        resp = await self._signed_get("/fapi/v3/balance", user, signer, private_key)
        resp.raise_for_status()
        data = resp.json()

        totals: dict[str, Decimal] = {}
        for item in (data if isinstance(data, list) else []):
            symbol = (item.get("asset") or "").upper()
            bal = self._d(item.get("balance") or item.get("crossWalletBalance") or "0")
            if symbol and bal > 0:
                totals[symbol] = totals.get(symbol, Decimal("0")) + bal

        # ── Открытые позиции ─────────────────────────────────────────────────
        positions = []
        try:
            pos_resp = await self._signed_get("/fapi/v3/positionRisk", user, signer, private_key)
            if pos_resp.is_success:
                for pos in (pos_resp.json() if isinstance(pos_resp.json(), list) else []):
                    notional = self._d(pos.get("notionalValue") or pos.get("notional") or 0)
                    if notional == 0:
                        continue
                    positions.append({
                        "symbol":          pos.get("symbol", ""),
                        "side":            pos.get("positionSide", ""),
                        "notional":        str(notional),
                        "unrealized_pnl":  str(self._d(pos.get("unRealizedProfit") or pos.get("unrealizedProfit") or 0)),
                        "entry_price":     str(self._d(pos.get("entryPrice") or 0)),
                        "mark_price":      str(self._d(pos.get("markPrice") or 0)),
                        "leverage":        pos.get("leverage", 1),
                    })
        except Exception:
            pass

        result = self._build_result(
            wallet, self.name,
            spot={k: v for k, v in totals.items() if v > 0},
            futures={},
            earn={},
        )
        if positions:
            result.details["earn"] = {"positions": positions}
        return result
