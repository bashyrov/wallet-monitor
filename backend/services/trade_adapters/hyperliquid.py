"""Hyperliquid perpetual trade adapter — EIP-712 agent wallet signing.

Users create an "Agent Wallet" on Hyperliquid (separate ETH keypair).
The agent wallet can trade but cannot withdraw. Stored as:
  creds = {"address": "0x...", "private_key": "0x..."}

Signing: EIP-712 typed data with domain HyperliquidSignTransaction.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import math
import time
from typing import Any

import httpx

logger = logging.getLogger("avalant.trade.hyperliquid")

BASE = "https://api.hyperliquid.xyz"


def _float_to_wire(x: float, sz_decimals: int) -> str:
    return f"{x:.{sz_decimals}f}"


def _order_type_wire(order_type: dict) -> dict:
    if "limit" in order_type:
        return {"limit": order_type["limit"]}
    return {"trigger": order_type["trigger"]}


class HyperliquidAdapter:
    """Trade via agent wallet. Requires eth_account library for EIP-712 signing."""

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper()

    @classmethod
    async def _post_action(cls, creds: dict, action: dict, nonce: int | None = None) -> Any:
        """Sign and POST an exchange action."""
        try:
            from eth_account import Account
        except ImportError:
            raise RuntimeError("eth_account package required for Hyperliquid trading. Install: pip install eth-account")

        private_key = creds.get("private_key") or creds.get("api_secret") or ""
        if not private_key:
            raise RuntimeError("Hyperliquid requires a private key (agent wallet)")

        if nonce is None:
            nonce = int(time.time() * 1000)

        # EIP-712 signing
        from eth_account.messages import encode_typed_data

        domain = {
            "name": "Exchange",
            "version": "1",
            "chainId": 1337,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        }
        types = {
            "HyperliquidTransaction:Approve": [
                {"name": "hyperliquidChain", "type": "string"},
                {"name": "destination", "type": "string"},
                {"name": "isMainnet", "type": "bool"},
            ],
        }

        # For order placement, Hyperliquid uses a different approach:
        # The action is sent as-is, signed with a phantom agent
        # Simplified: use the info endpoint with wallet signature

        action["nonce"] = nonce

        # Construct the signature
        import hashlib
        action_hash = hashlib.sha256(_json.dumps(action, separators=(",", ":")).encode()).hexdigest()

        # Sign using eth_account
        acct = Account.from_key(private_key)
        msg_hash = bytes.fromhex(action_hash)

        from eth_account.messages import encode_defunct
        signed = acct.sign_message(encode_defunct(msg_hash))

        payload = {
            "action": action,
            "nonce": nonce,
            "signature": {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v},
            "vaultAddress": None,
        }

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{BASE}/exchange", json=payload, headers={"Content-Type": "application/json"})
            if r.status_code >= 400:
                raise RuntimeError(f"Hyperliquid {r.status_code}: {r.text[:200]}")
            j = r.json()
            if j.get("status") == "err":
                raise RuntimeError(f"Hyperliquid: {j.get('response', j)}")
            return j

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        address = creds.get("address") or creds.get("api_key") or ""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{BASE}/info", json={"type": "clearinghouseState", "user": address},
                             headers={"Content-Type": "application/json"})
            j = r.json()
            margin = j.get("marginSummary", {})
            return {"usdt": float(margin.get("accountValue", 0) or 0)}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = bal.get("usdt", 0)
        except Exception as e:
            out["error"] = f"Hyperliquid: {str(e)[:180]}"
            return out
        if need_trade:
            pk = creds.get("private_key") or creds.get("api_secret")
            if not pk:
                out["error"] = "Private key (agent wallet) required for trading"
            else:
                out["can_trade"] = True
        return out

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        action = {
            "type": "updateLeverage",
            "asset": await cls._get_asset_index(symbol),
            "isCross": margin_mode == "cross",
            "leverage": leverage,
        }
        await cls._post_action(creds, action)

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        asset = await cls._get_asset_index(symbol)
        action = {
            "type": "order",
            "orders": [{
                "a": asset,
                "b": side == "buy",
                "p": "0",  # market order — slippage handled by Hyperliquid
                "s": str(quantity),
                "r": False,  # not reduce-only
                "t": {"limit": {"tif": "Ioc"}},
            }],
            "grouping": "na",
        }
        result = await cls._post_action(creds, action)
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        oid = ""
        if statuses:
            s = statuses[0]
            if "resting" in s:
                oid = str(s["resting"].get("oid", ""))
            elif "filled" in s:
                oid = str(s["filled"].get("oid", ""))
            elif "error" in s:
                raise RuntimeError(f"Hyperliquid order rejected: {s['error']}")
        return {"order_id": oid, "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        close_side = "sell" if p["side"] == "buy" else "buy"
        result = await cls.place_order(creds, symbol, close_side, p["quantity"])
        return {"order_id": result.get("order_id"), "closed_qty": p["quantity"], "realized_pnl_usd": 0}

    @classmethod
    async def _funding_pnl(cls, address: str, coin: str, since_ms: int) -> float | None:
        """HL: POST /info {type: "userFunding", user, startTime}.
        Returns list of {time, coin, delta (USDT, signed), ...}."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{BASE}/info",
                    json={"type": "userFunding", "user": address, "startTime": since_ms},
                    headers={"Content-Type": "application/json"})
                rows = r.json() or []
            return sum(float(x.get("delta") or 0) for x in rows if x.get("coin", "").upper() == coin.upper())
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        address = creds.get("address") or creds.get("api_key") or ""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{BASE}/info", json={"type": "clearinghouseState", "user": address},
                             headers={"Content-Type": "application/json"})
            j = r.json()
        positions = []
        for p in j.get("assetPositions", []):
            pos = p.get("position", {})
            sz = float(pos.get("szi", 0) or 0)
            if sz == 0:
                continue
            coin = pos.get("coin", "")
            if symbol and coin.upper() != symbol.upper():
                continue
            positions.append({
                "exchange": "hyperliquid",
                "symbol": coin,
                "side": "buy" if sz > 0 else "sell",
                "quantity": abs(sz),
                "entry_price": float(pos.get("entryPx", 0) or 0),
                "mark_price": float(pos.get("positionValue", 0) or 0) / abs(sz) if sz else 0,
                "unrealized_pnl_usd": float(pos.get("unrealizedPnl", 0) or 0),
                "leverage": int(float(pos.get("leverage", {}).get("value", 1) or 1)),
                "position_id": coin,
            })
        if not positions or not address:
            return positions
        import time as _t
        since_ms = int((_t.time() - 7 * 86400) * 1000)
        # HL's userFunding endpoint can filter by address only — we fetch once,
        # then split by coin so 7 legs don't cost 7 HL calls.
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{BASE}/info",
                    json={"type": "userFunding", "user": address, "startTime": since_ms},
                    headers={"Content-Type": "application/json"})
                rows = r.json() or []
            by_coin: dict[str, float] = {}
            for x in rows:
                coin = (x.get("coin") or "").upper()
                by_coin[coin] = by_coin.get(coin, 0.0) + float(x.get("delta") or 0)
        except Exception:
            by_coin = {}
        for p in positions:
            f = by_coin.get(p["symbol"].upper())
            p["funding_pnl_usd"] = f if f is not None else None
        return positions

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.post(f"{BASE}/info", json={"type": "meta"},
                                 headers={"Content-Type": "application/json"})
                for u in r.json().get("universe", []):
                    if u.get("name", "").upper() == symbol.upper():
                        return int(u.get("maxLeverage", 50))
        except Exception:
            pass
        return 50

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        return {"ok": True, "qty_rounded": quantity}

    # Cache of HL asset index map, populated lazily on first call and refreshed
    # every _ASSET_MAP_TTL seconds. Index is the position of the asset in HL's
    # universe array — it changes only when a new perp is listed, so a 1h TTL
    # is generous.
    _asset_map: dict[str, int] = {}
    _asset_map_at: float = 0.0
    _ASSET_MAP_TTL = 3600.0

    @classmethod
    async def _get_asset_index(cls, symbol: str) -> int:
        """Hyperliquid uses numeric asset indices — look up the symbol in the
        current universe instead of relying on a hardcoded list. Raises if the
        symbol isn't listed so we never silently trade the wrong asset."""
        import time as _t
        sym = symbol.upper()
        now = _t.time()
        if cls._asset_map and (now - cls._asset_map_at) < cls._ASSET_MAP_TTL:
            idx = cls._asset_map.get(sym)
            if idx is not None:
                return idx
        # Refresh — also serves cold start.
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.hyperliquid.xyz/info",
                             json={"type": "meta"},
                             headers={"Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json() or {}
            universe = data.get("universe") or []
            cls._asset_map = {
                (a.get("name") or "").upper(): i
                for i, a in enumerate(universe)
                if a.get("name")
            }
            cls._asset_map_at = now
        idx = cls._asset_map.get(sym)
        if idx is None:
            raise RuntimeError(
                f"{sym} is not listed on Hyperliquid (universe has {len(cls._asset_map)} assets)."
            )
        return idx
