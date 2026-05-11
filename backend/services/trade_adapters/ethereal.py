"""Ethereal DEX trade adapter — EIP-712 TradeOrder via linked signer.

Auth flow:
  1. User has a main wallet (subaccount owner) on Ethereal.
  2. User creates a "Linked Signer" key locally and pre-authorises it on-chain.
  3. The linked-signer's private key signs trade messages; the main wallet's
     address is the `sender` in every EIP-712 TradeOrder.

creds = {"address": "0x... (main wallet)", "private_key|api_secret": "0x... (linked signer key)"}

Wire shape for /v1/order (market):
  POST {
    data: {
      sender, subaccount (bytes32 hex), quantity (decimal str), side (0|1),
      engineType (0), onchainId, type ("MARKET"), reduceOnly, nonce (str), signedAt (number),
    },
    signature: "0x..."
  }

The EIP-712 TradeOrder schema is pulled live from /v1/rpc/config (cached 1h)
so a server-side domain or type change doesn't silently break signing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("avalant.trade.ethereal")

BASE = "https://api.ethereal.trade"

# ── Caches ──────────────────────────────────────────────────────────────────
_CONFIG_CACHE: dict = {"data": None, "ts": 0}
_PRODUCTS_CACHE: dict = {"data": None, "ts": 0}
_SUBACCOUNT_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 3600.0
_CONFIG_LOCK = asyncio.Lock()
_PRODUCTS_LOCK = asyncio.Lock()


async def _rpc_config() -> dict:
    """{domain, signatureTypes} from /v1/rpc/config — drives EIP-712 signing.
    Cached 1h. Source of truth for chainId / verifyingContract."""
    now = time.time()
    if _CONFIG_CACHE["data"] and now - _CONFIG_CACHE["ts"] < _CACHE_TTL_S:
        return _CONFIG_CACHE["data"]
    async with _CONFIG_LOCK:
        if _CONFIG_CACHE["data"] and time.time() - _CONFIG_CACHE["ts"] < _CACHE_TTL_S:
            return _CONFIG_CACHE["data"]
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{BASE}/v1/rpc/config")
            r.raise_for_status()
            _CONFIG_CACHE["data"] = r.json()
            _CONFIG_CACHE["ts"] = time.time()
            return _CONFIG_CACHE["data"]


async def _products_by_symbol() -> dict[str, dict]:
    """Map symbol ('SOL') → product dict (with onchainId, minQuantity, lotSize)."""
    now = time.time()
    if _PRODUCTS_CACHE["data"] and now - _PRODUCTS_CACHE["ts"] < _CACHE_TTL_S:
        return _PRODUCTS_CACHE["data"]
    async with _PRODUCTS_LOCK:
        if _PRODUCTS_CACHE["data"] and time.time() - _PRODUCTS_CACHE["ts"] < _CACHE_TTL_S:
            return _PRODUCTS_CACHE["data"]
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{BASE}/v1/product")
            r.raise_for_status()
            j = r.json()
            items = j if isinstance(j, list) else j.get("data") or j.get("products") or []
            out: dict[str, dict] = {}
            for p in items:
                # Tickers like "SOLUSD", "BTCUSD". Strip the USD suffix to match
                # our short-form "SOL" / "BTC" symbols.
                t = (p.get("ticker") or "").upper()
                base = t[:-3] if t.endswith("USD") else (p.get("baseTokenName") or "").upper()
                if base:
                    out[base] = p
            _PRODUCTS_CACHE["data"] = out
            _PRODUCTS_CACHE["ts"] = time.time()
            return out


async def _get_subaccount(address: str) -> dict:
    """Cached subaccount lookup. `name` is the bytes32 hex used in EIP-712."""
    addr = (address or "").lower()
    hit = _SUBACCOUNT_CACHE.get(addr)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(f"{BASE}/v1/subaccount", params={"sender": address})
        r.raise_for_status()
        subs = (r.json() or {}).get("data") or []
        if not subs:
            raise RuntimeError(f"Ethereal: no subaccount for {address}")
        sa = subs[0]
        _SUBACCOUNT_CACHE[addr] = (time.time(), sa)
        return sa


def _types_from_config(cfg: dict, primary: str) -> dict:
    """Parse the compact 'name1 type1,name2 type2,...' format into EIP-712
    type lists (eth_account expects list-of-{name,type})."""
    raw = (cfg.get("signatureTypes") or {}).get(primary, "")
    fields = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        toks = part.split()
        if len(toks) < 2:
            continue
        ty, name = toks[0], " ".join(toks[1:])
        fields.append({"name": name, "type": ty})
    return {
        primary: fields,
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
    }


def _q_scaled(value, decimals: int = 9) -> int:
    """Decimal qty/price → scaled uint (Q9 by convention, toGwei in viem)."""
    from decimal import Decimal, ROUND_DOWN
    d = Decimal(str(value))
    scaled = (d * (Decimal(10) ** decimals)).quantize(Decimal(1), rounding=ROUND_DOWN)
    return int(scaled)


class EtherealAdapter:

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper()

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        """Read collateral via /v1/subaccount + /v1/subaccount/balance."""
        address = creds.get("address") or creds.get("api_key") or ""
        if not address:
            return {"usdt": 0.0, "spot_usd": 0.0, "futures_usd": 0.0}
        try:
            sa = await _get_subaccount(address)
        except Exception:
            return {"usdt": 0.0, "spot_usd": 0.0, "futures_usd": 0.0}
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{BASE}/v1/subaccount/balance",
                            params={"subaccountId": sa.get("id")})
            if r.status_code >= 400:
                return {"usdt": 0.0, "spot_usd": 0.0, "futures_usd": 0.0}
            items = (r.json() or {}).get("data") or []
        total = 0.0
        for it in items:
            token = (it.get("tokenName") or "").upper()
            if token not in ("USDT", "USDC", "USD", "USDE", "BUSD"):
                continue
            try:
                avail = float(it.get("available") or 0)
                used = float(it.get("totalUsed") or 0)
                amount = float(it.get("amount") or 0)
                total += (avail + used) or amount
            except (TypeError, ValueError):
                continue
        return {"usdt": total, "spot_usd": 0.0, "futures_usd": total}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = bal.get("usdt", 0)
        except Exception as e:
            out["error"] = f"Ethereal: {str(e)[:180]}"
            return out
        if need_trade:
            pk = creds.get("private_key") or creds.get("api_secret")
            if not pk:
                out["error"] = "Linked signer private key required for trading"
            else:
                out["can_trade"] = True
        return out

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        pass  # Ethereal manages leverage per-position

    # ── EIP-712 TradeOrder ──
    @classmethod
    async def _signed_trade_order(cls, creds: dict, symbol: str, side: str,
                                  quantity: float, reduce_only: bool) -> dict:
        try:
            from eth_account import Account
            try:
                from eth_account.messages import encode_typed_data as _encode
            except ImportError:
                from eth_account.messages import encode_structured_data as _encode
        except ImportError:
            raise RuntimeError("eth_account package required for Ethereal trading")

        priv = creds.get("private_key") or creds.get("api_secret") or ""
        if not priv:
            raise RuntimeError("Ethereal requires a linked signer private key")
        if not priv.startswith("0x"):
            priv = "0x" + priv
        sender = creds.get("address") or creds.get("api_key") or ""
        if not sender:
            raise RuntimeError("Ethereal requires the main wallet address")

        cfg = await _rpc_config()
        domain = cfg.get("domain") or {}
        types = _types_from_config(cfg, "TradeOrder")

        prods = await _products_by_symbol()
        prod = prods.get(symbol.upper())
        if not prod:
            raise RuntimeError(f"Ethereal: product {symbol} not listed")
        onchain_id = int(prod.get("onchainId") or 0)
        min_qty = float(prod.get("minQuantity") or 0)
        lot = float(prod.get("lotSize") or 0.001)
        # Round qty down to lotSize so the venue doesn't reject.
        if lot > 0:
            quantity = (int(quantity / lot)) * lot
        if min_qty and quantity < min_qty:
            raise RuntimeError(f"Ethereal: qty {quantity} below min {min_qty} {symbol}")

        sa = await _get_subaccount(sender)
        subaccount = sa.get("name") or ""  # bytes32 hex string
        if not subaccount.startswith("0x") or len(subaccount) != 66:
            raise RuntimeError(f"Ethereal: bad subaccount bytes32: {subaccount}")

        side_int = 0 if side == "buy" else 1
        nonce_ns = int(time.time() * 1e9)
        signed_at_s = int(time.time())
        qty_scaled = _q_scaled(quantity, 9)
        price_scaled = 0  # MARKET → must be 0 in signature

        message = {
            "sender": sender,
            "subaccount": subaccount,
            "quantity": qty_scaled,
            "price": price_scaled,
            "reduceOnly": bool(reduce_only),
            "side": side_int,
            "engineType": 0,  # PERP
            "productId": onchain_id,
            "nonce": nonce_ns,
            "signedAt": signed_at_s,
        }
        typed_data = {
            "types": types,
            "primaryType": "TradeOrder",
            "domain": domain,
            "message": message,
        }
        try:
            em = _encode(full_message=typed_data)
        except TypeError:
            em = _encode(typed_data)
        signed = Account.sign_message(em, private_key=priv)
        sig_hex = "0x" + signed.signature.hex().lstrip("0x")

        body = {
            "data": {
                "sender": sender,
                "subaccount": subaccount,
                "quantity": str(quantity),
                # no price field for market orders
                "reduceOnly": bool(reduce_only),
                "side": side_int,
                "engineType": 0,
                "onchainId": onchain_id,
                "type": "MARKET",
                "nonce": str(nonce_ns),
                "signedAt": signed_at_s,
            },
            "signature": sig_hex,
        }

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{BASE}/v1/order", json=body,
                              headers={"Content-Type": "application/json"})
            if r.status_code >= 400:
                raise RuntimeError(f"Ethereal {r.status_code}: {r.text[:240]}")
            return r.json()

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        r = await cls._signed_trade_order(creds, symbol, side, quantity, reduce_only=False)
        data = (r or {}).get("data") or r or {}
        return {"order_id": str(data.get("id") or data.get("orderId") or ""),
                "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        close_side = "sell" if p["side"] == "buy" else "buy"
        r = await cls._signed_trade_order(creds, symbol, close_side,
                                          float(p["quantity"]), reduce_only=True)
        data = (r or {}).get("data") or r or {}
        return {
            "order_id": str(data.get("id") or data.get("orderId") or ""),
            "closed_qty": p["quantity"],
            "realized_pnl_usd": p.get("unrealized_pnl_usd", 0),
        }

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        address = creds.get("address") or creds.get("api_key") or ""
        if not address:
            return []
        try:
            sa = await _get_subaccount(address)
        except Exception:
            return []
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{BASE}/v1/position",
                            params={"subaccountId": sa.get("id")})
            if r.status_code >= 400:
                return []
            items = (r.json() or {}).get("data") or []
        out = []
        for p in items:
            qty = float(p.get("size", p.get("quantity", 0)) or 0)
            if qty == 0:
                continue
            # Try several symbol fields
            sym = (p.get("ticker") or p.get("symbol") or p.get("productSymbol") or "")
            sym = sym.upper()
            base = sym[:-3] if sym.endswith("USD") else sym
            if symbol and base != symbol.upper():
                continue
            side_raw = (p.get("side") or "").lower()
            if side_raw in ("buy", "long", "0"):
                pos_side = "buy"
            elif side_raw in ("sell", "short", "1"):
                pos_side = "sell"
            else:
                pos_side = "buy" if qty > 0 else "sell"
            out.append({
                "exchange": "ethereal",
                "symbol": base,
                "side": pos_side,
                "quantity": abs(qty),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "mark_price": float(p.get("markPrice", 0) or 0),
                "unrealized_pnl_usd": float(p.get("unrealizedPnl", 0) or 0),
                "leverage": int(float(p.get("leverage", 1) or 1)),
                "position_id": str(p.get("id") or base),
            })
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        try:
            prods = await _products_by_symbol()
            p = prods.get(symbol.upper())
            if p:
                return int(p.get("maxLeverage") or 20)
        except Exception:
            pass
        return 20

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        try:
            prods = await _products_by_symbol()
            p = prods.get(symbol.upper())
            if p:
                lot = float(p.get("lotSize") or 0.001)
                if lot > 0:
                    quantity = (int(quantity / lot)) * lot
                min_qty = float(p.get("minQuantity") or 0)
                if min_qty and quantity < min_qty:
                    return {"ok": False, "reason": f"Quantity below minimum ({min_qty} {symbol.upper()})."}
        except Exception:
            pass
        return {"ok": True, "qty_rounded": quantity}
