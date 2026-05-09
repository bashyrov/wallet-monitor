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

    # Real HL signing scheme (matches the official `hyperliquid-py` SDK).
    # The earlier `personal_sign(sha256(json(action)))` form in this file
    # was wrong — HL's exchange endpoint rejects it on real orders.
    #
    # action_hash    = keccak256( msgpack(action) ‖ nonce_be8 ‖ vault_marker )
    # vault_marker   = b"\\x00"                       if vault_address is None
    #                = b"\\x01" ‖ bytes20(vault_addr) otherwise
    # connection_id  = action_hash (32 bytes)
    # typed_data     = EIP-712 over Agent(string source, bytes32 connectionId)
    #                  with source="a" on mainnet, "b" on testnet, and
    #                  domain {name:"Exchange", version:"1",
    #                          chainId:1337, verifyingContract:0x0…0}
    # signature      = sign_typed_data(typed_data) by the agent wallet.
    @staticmethod
    def _sign_action(action: dict, nonce: int, private_key: str,
                     vault_address: str | None = None,
                     is_mainnet: bool = True) -> dict:
        try:
            import msgpack  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "msgpack required for Hyperliquid trading (pip install msgpack)"
            ) from exc
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from eth_utils import keccak  # type: ignore

        packed = msgpack.packb(action, use_bin_type=True)
        packed += nonce.to_bytes(8, "big")
        if vault_address is None:
            packed += b"\x00"
        else:
            packed += b"\x01" + bytes.fromhex(vault_address.removeprefix("0x"))
        connection_id = keccak(packed)  # 32 bytes

        typed_data = {
            "domain": {
                "name": "Exchange",
                "version": "1",
                "chainId": 1337,
                "verifyingContract": "0x0000000000000000000000000000000000000000",
            },
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Agent": [
                    {"name": "source", "type": "string"},
                    {"name": "connectionId", "type": "bytes32"},
                ],
            },
            "primaryType": "Agent",
            "message": {
                "source": "a" if is_mainnet else "b",
                "connectionId": connection_id,
            },
        }
        signed = Account.from_key(private_key).sign_message(
            encode_typed_data(full_message=typed_data)
        )
        return {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}

    @classmethod
    async def _post_action(cls, creds: dict, action: dict, nonce: int | None = None) -> Any:
        private_key = creds.get("private_key") or creds.get("api_secret") or ""
        if not private_key:
            raise RuntimeError("Hyperliquid requires a private key (agent wallet)")
        if nonce is None:
            nonce = int(time.time() * 1000)

        signature = cls._sign_action(action, nonce, private_key,
                                      vault_address=None, is_mainnet=True)

        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
        }

        from backend.services.trade_adapters._http import http_client
        client = http_client(BASE, timeout=10.0)
        r = await client.post("/exchange", json=payload,
                              headers={"Content-Type": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"Hyperliquid {r.status_code}: {r.text[:200]}")
        j = r.json()
        if j.get("status") == "err":
            raise RuntimeError(f"Hyperliquid: {j.get('response', j)}")
        return j

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        address = creds.get("address") or creds.get("api_key") or ""
        from backend.services.trade_adapters._http import http_client
        client = http_client(BASE, timeout=10.0)
        r = await client.post("/info", json={"type": "clearinghouseState", "user": address},
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
        """Reduce-only IoC order. Previous implementation called place_order
        which builds {"r": False} → NOT reduce-only. In hedge mode that would
        open a fresh opposing position; in one-way mode it happened to flatten
        by netting. Now posts the same action with "r": true."""
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        asset = await cls._get_asset_index(symbol)
        close_is_buy = p["side"] == "sell"  # close a short by BUY
        action = {
            "type": "order",
            "orders": [{
                "a": asset,
                "b": close_is_buy,
                "p": "0",
                "s": str(p["quantity"]),
                "r": True,  # reduce-only
                "t": {"limit": {"tif": "Ioc"}},
            }],
            "grouping": "na",
        }
        result = await cls._post_action(creds, action)
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        oid = ""
        if statuses:
            s = statuses[0]
            if "resting" in s: oid = str(s["resting"].get("oid", ""))
            elif "filled" in s: oid = str(s["filled"].get("oid", ""))
            elif "error" in s: raise RuntimeError(f"Hyperliquid: {s['error']}")
        return {
            "order_id": oid,
            "closed_qty": p["quantity"],
            "realized_pnl_usd": p.get("unrealized_pnl_usd", 0),
        }

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
    _asset_meta: dict[str, dict] = {}    # name → full universe entry (szDecimals, maxLeverage, …)
    _asset_map_at: float = 0.0
    _ASSET_MAP_TTL = 3600.0

    @classmethod
    async def _refresh_universe(cls) -> None:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.hyperliquid.xyz/info",
                             json={"type": "meta"},
                             headers={"Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json() or {}
            universe = data.get("universe") or []
        idx_map: dict[str, int] = {}
        meta: dict[str, dict] = {}
        for i, a in enumerate(universe):
            n = (a.get("name") or "").upper()
            if not n:
                continue
            idx_map[n] = i
            meta[n] = a
        import time as _t
        cls._asset_map = idx_map
        cls._asset_meta = meta
        cls._asset_map_at = _t.time()

    @classmethod
    async def _ensure_universe(cls) -> None:
        import time as _t
        if cls._asset_map and (_t.time() - cls._asset_map_at) < cls._ASSET_MAP_TTL:
            return
        await cls._refresh_universe()

    @classmethod
    async def _get_asset_index(cls, symbol: str) -> int:
        """Hyperliquid uses numeric asset indices — look up the symbol in the
        current universe instead of relying on a hardcoded list. Raises if the
        symbol isn't listed so we never silently trade the wrong asset."""
        sym = symbol.upper()
        await cls._ensure_universe()
        idx = cls._asset_map.get(sym)
        if idx is None:
            raise RuntimeError(
                f"{sym} is not listed on Hyperliquid (universe has {len(cls._asset_map)} assets)."
            )
        return idx

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        """HL exposes szDecimals per asset in /info?type=meta — min/step =
        10^-szDecimals coins. No native min_notional; HL enforces value
        check at order time via separate field."""
        try:
            await cls._ensure_universe()
        except Exception:
            return None
        info = cls._asset_meta.get(symbol.upper())
        if not info:
            return None
        sz_dec = int(info.get("szDecimals") or 0)
        step = 10 ** (-sz_dec) if sz_dec >= 0 else 1
        return {
            "min_qty": step,
            "step":    step,
            "max_qty": None,
            "unit": "coin",
        }

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts, *,
                                 market: str = "futures") -> list[dict]:
        """HL fills + funding via /info type=userFillsByTime / userFunding.

        HL spot trading lives on a separate L1 product not exposed via the
        same API; we return [] for spot."""
        from datetime import datetime as _dt
        if market != "futures":
            return []
        address = creds.get("address") or creds.get("api_key") or ""
        if not address:
            return []
        start_ms = int(since_ts.timestamp() * 1000)
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=10) as c:
            try:
                r = await c.post(f"{BASE}/info", json={
                    "type": "userFillsByTime",
                    "user": address,
                    "startTime": start_ms,
                })
                fills = r.json() if r.status_code == 200 else []
            except Exception:
                fills = []
            for r in fills if isinstance(fills, list) else []:
                try:
                    sym = str(r.get("coin") or "").upper()
                    side = "buy" if str(r.get("side") or "") == "B" else "sell"
                    qty = float(r.get("sz") or 0)
                    if qty <= 0:
                        continue
                    ts_ms = int(r.get("time") or 0)
                    if ts_ms <= 0:
                        continue
                    rpnl = r.get("closedPnl")
                    out.append({
                        "symbol": sym,
                        "side": side,
                        "qty": qty,
                        "price": float(r.get("px") or 0),
                        "fee_usd": float(r.get("fee") or 0),
                        "realized_pnl_usd": (float(rpnl)
                                             if rpnl not in (None, "") else None),
                        "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                        "ext_trade_id": str(r.get("tid") or r.get("hash") or ""),
                        "ext_order_id": str(r.get("oid") or "") or None,
                        "kind": "trade",
                    })
                except Exception:
                    continue
            try:
                r = await c.post(f"{BASE}/info", json={
                    "type": "userFunding",
                    "user": address,
                    "startTime": start_ms,
                })
                funds = r.json() if r.status_code == 200 else []
            except Exception:
                funds = []
            for r in funds if isinstance(funds, list) else []:
                try:
                    delta = r.get("delta") or {}
                    sym = str(delta.get("coin") or "").upper()
                    ts_ms = int(r.get("time") or 0)
                    if ts_ms <= 0:
                        continue
                    out.append({
                        "symbol": sym,
                        "side": None,
                        "qty": 0.0, "price": 0.0, "fee_usd": None,
                        "realized_pnl_usd": float(delta.get("usdc") or 0),
                        "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                        "ext_trade_id": str(r.get("hash")
                                            or f"funding-{ts_ms}-{sym}"),
                        "ext_order_id": None,
                        "kind": "funding",
                    })
                except Exception:
                    continue
        return out
