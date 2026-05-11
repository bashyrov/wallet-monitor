"""Backpack Exchange trade adapter (spot + perp where available)."""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE = "https://api.backpack.exchange"
logger = logging.getLogger("avalant.trade.backpack")
RECV_WINDOW = 60000

# ── Instrument cache ──
_MKT_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_MKT_TTL = 600
_MKT_LOCK = asyncio.Lock()


async def _markets() -> dict[str, dict]:
    """Return {symbol: {baseStep, quoteStep, minNotional, ...}}."""
    now = time.time()
    if _MKT_CACHE["data"] and now - _MKT_CACHE["ts"] < _MKT_TTL:
        return _MKT_CACHE["data"]
    async with _MKT_LOCK:
        if _MKT_CACHE["data"] and time.time() - _MKT_CACHE["ts"] < _MKT_TTL:
            return _MKT_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/api/v1/markets")
                body = r.json()
        except Exception as e:
            logger.warning("Backpack markets fetch failed: %s", e)
            return _MKT_CACHE["data"] or {}
        out: dict[str, dict] = {}
        items = body if isinstance(body, list) else body.get("data", [])
        for m in items:
            sym = m.get("symbol")
            if not sym:
                continue
            filters = m.get("filters", {})
            qty_f = filters.get("quantity", {}) if isinstance(filters, dict) else {}
            price_f = filters.get("price", {}) if isinstance(filters, dict) else {}
            out[sym] = {
                "baseStep": float(qty_f.get("stepSize") or m.get("baseStep") or 0),
                "minQty": float(qty_f.get("minQuantity") or m.get("minOrderSize") or 0),
                "minNotional": float(filters.get("minNotional") or 0) if isinstance(filters, dict) else 0,
                "tickSize": float(price_f.get("tickSize") or m.get("quoteStep") or 0),
                "quantityPrecision": int(m.get("quantityPrecision", 4) or 4),
            }
        _MKT_CACHE["data"] = out
        _MKT_CACHE["ts"] = time.time()
        return out


_FRIENDLY = {
    "UNAUTHORIZED": "Invalid API key or signature.",
    "INSUFFICIENT_FUNDS": "Insufficient balance.",
    "INVALID_SYMBOL": "Symbol not listed on Backpack.",
    "INVALID_QUANTITY": "Quantity does not meet exchange requirements.",
}


def _friendly_error(msg: str) -> str:
    for key, friendly in _FRIENDLY.items():
        if key in msg.upper():
            return friendly
    return msg or "Backpack rejected the request."


def _round_qty(qty: float, step: float | None, prec: int) -> float:
    if step and step > 0:
        return math.floor(qty / step) * step
    factor = 10 ** prec
    return math.floor(qty * factor) / factor


def _qty_str(qty: float, prec: int) -> str:
    s = f"{qty:.{max(prec, 0)}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


def _ed25519_sign(message: str, secret_b64: str) -> str:
    seed = base64.b64decode(secret_b64)
    pk = Ed25519PrivateKey.from_private_bytes(seed)
    sig = pk.sign(message.encode())
    return base64.b64encode(sig).decode()


def _build_sign_string(instruction: str, ts: int, params: dict | None = None) -> str:
    parts = [("instruction", instruction)]
    if params:
        parts += sorted((k, str(v)) for k, v in params.items())
    parts += [("timestamp", str(ts)), ("window", str(RECV_WINDOW))]
    return urlencode(parts)


class BackpackAdapter:
    """Backpack Exchange adapter. Supports both spot and perp symbols."""

    @classmethod
    async def _req(cls, creds: dict, method: str, path: str,
                   instruction: str, params: dict | None = None,
                   body: dict | None = None) -> Any:
        ts = int(time.time() * 1000)
        sign_str = _build_sign_string(instruction, ts, params or body)
        sig = _ed25519_sign(sign_str, creds["api_secret"])
        headers = {
            "X-API-Key": creds["api_key"],
            "X-Signature": sig,
            "X-Timestamp": str(ts),
            "X-Window": str(RECV_WINDOW),
        }
        from backend.services.trade_adapters._http import http_client
        client = http_client(BASE, timeout=10.0)
        if method == "GET":
            r = await client.get(path, params=params, headers=headers)
        elif method == "POST":
            headers["Content-Type"] = "application/json"
            r = await client.post(path, json=body, headers=headers)
        elif method == "DELETE":
            r = await client.delete(path, params=params, headers=headers)
        else:
            raise ValueError(method)
        if r.status_code >= 400:
            msg = r.text
            try:
                j = r.json()
                msg = str(j.get("message") or j.get("error") or r.text)
            except Exception:
                pass
            raise RuntimeError(f"Backpack {r.status_code}: {msg}")
        if not r.content:
            return {}
        return r.json()

    @staticmethod
    def _symbol(s: str) -> str:
        # Backpack perpetuals are USDC-margined: SOL_USDC_PERP, BTC_USDC_PERP, etc.
        # Spot pairs use SOL_USDC. We trade perps here.
        return s.upper() + "_USDC_PERP"

    @staticmethod
    def _spot_symbol(s: str) -> str:
        return s.upper() + "_USDC"

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        """Backpack stores futures collateral and spot capital on separate
        endpoints. /capital/collateral is the futures-margin pool (netEquity);
        /capital is the spot wallet."""
        fut_usd = 0.0
        try:
            col = await cls._req(creds, "GET", "/api/v1/capital/collateral", "collateralQuery")
            if isinstance(col, dict):
                ne = col.get("netEquity")
                if ne is not None:
                    fut_usd = float(ne or 0)
        except Exception:
            pass
        spot_usd = 0.0
        try:
            spot = await cls._req(creds, "GET", "/api/v1/capital", "balanceQuery")
            if isinstance(spot, dict):
                for asset in ("USDT", "USDC", "USD"):
                    entry = spot.get(asset, {}) or {}
                    try:
                        spot_usd += float(entry.get("available") or 0) + float(entry.get("locked") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
        return {"usdt": fut_usd + spot_usd, "spot_usd": spot_usd, "futures_usd": fut_usd}

    # ── Leverage (Backpack spot has no leverage API — stub) ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        pass  # Backpack does not support per-symbol leverage setting

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        info = (await _markets()).get(cls._symbol(symbol))
        if not info:
            return None
        return {
            "min_qty": float(info.get("minQty") or 0),
            "step":    float(info.get("baseStep") or 0) or None,
            "max_qty": None,
            "unit": "coin",
        }

    # ── Preflight ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = (await _markets()).get(sym)
        if not info:
            return {"ok": False, "reason": f"Symbol {sym} not listed on Backpack."}
        prec = info.get("quantityPrecision", 4)
        step = info.get("baseStep") or 0
        min_qty = info.get("minQty") or 0
        qty_r = _round_qty(quantity, step, prec)
        if qty_r <= 0 or qty_r < min_qty:
            return {"ok": False, "reason": f"Quantity below minimum ({min_qty} {symbol.upper()})."}
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            return {"ok": False, "reason": _friendly_error(str(e))}
        return {"ok": True, "qty_rounded": qty_r, "precision": prec,
                "min_qty": min_qty, "step_size": step}

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = (await _markets()).get(sym) or {}
        prec = info.get("quantityPrecision", 4)
        step = info.get("baseStep") or 0
        qty_r = _round_qty(quantity, step, prec)
        qty_s = _qty_str(qty_r, prec)
        order_body = {
            "symbol": sym,
            "side": "Bid" if side == "buy" else "Ask",
            "orderType": "Market",
            "quantity": qty_s,
        }
        try:
            r = await cls._req(creds, "POST", "/api/v1/order", "orderExecute", body=order_body)
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(str(e)))
        return {"order_id": str(r.get("id") or r.get("orderId", "")),
                "avg_price": float(r.get("price") or r.get("avgPrice", 0) or 0)}

    # ── Close position ──
    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        # Backpack spot: sell the full base asset balance to "close"
        sym = cls._symbol(symbol)
        base_asset = symbol.upper()
        data = await cls._req(creds, "GET", "/api/v1/capital", "balanceQuery")
        entry = (data or {}).get(base_asset, {})
        amt = float(entry.get("available") or 0)
        if amt <= 0:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        info = (await _markets()).get(sym) or {}
        prec = info.get("quantityPrecision", 4)
        step = info.get("baseStep") or 0
        qty_r = _round_qty(amt, step, prec)
        qty_s = _qty_str(qty_r, prec)
        reduce_side = "Ask" if side == "buy" else "Bid"
        try:
            r = await cls._req(creds, "POST", "/api/v1/order", "orderExecute", body={
                "symbol": sym,
                "side": reduce_side,
                "orderType": "Market",
                "quantity": qty_s,
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(str(e)))
        return {"order_id": str(r.get("id", "")), "closed_qty": qty_r, "realized_pnl_usd": 0.0}

    # ── Positions (spot: non-zero balances as pseudo-positions) ──
    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        data = await cls._req(creds, "GET", "/api/v1/capital", "balanceQuery")
        out = []
        for asset, entry in (data or {}).items():
            if asset in ("USDT", "USDC"):
                continue
            total = float(entry.get("available") or 0) + float(entry.get("locked") or 0)
            if total <= 0:
                continue
            if symbol and asset.upper() != symbol.upper():
                continue
            out.append({
                "exchange": "backpack",
                "symbol": asset,
                "side": "buy",
                "quantity": total,
                "entry_price": 0,
                "mark_price": 0,
                "unrealized_pnl_usd": 0,
                "leverage": 1,
                "position_id": f"{asset}_USDT",
            })
        return out

    # ── Validate key ──
    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "UNAUTHORIZED" in msg.upper() or "401" in msg:
                out["error"] = "Invalid API key or signature"
            else:
                out["error"] = f"Backpack rejected the key: {msg[:180]}"
            return out
        if need_trade:
            out["can_trade"] = True
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        # Backpack perpetuals support up to 50x. The market filter
        # `maxMultiplier` is for price banding, not leverage.
        return 50

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts, *, market: str = "futures") -> list[dict]:
        """Backpack spot fills since `since_ts`.
        Endpoint: GET /api/v1/history/fills?symbol=X&limit=1000
        Backpack is spot-only; ignore market != 'spot'."""
        from datetime import datetime as _dt
        if market not in ("futures", "spot"):
            return []
        since_ms = int(since_ts.timestamp() * 1000)
        out: list[dict] = []
        # Fetch all markets to know what symbols to sweep. Backpack
        # perpetuals are _USDC_PERP; spot is _USDC. Pull both flavours
        # so we capture spot_short pair legs even if user is mostly perp.
        try:
            mkts = await _markets()
            if market == "futures":
                symbols = [s for s in mkts if s.endswith("_USDC_PERP")]
            else:
                symbols = [s for s in mkts if s.endswith("_USDC") and not s.endswith("_PERP")]
        except Exception:
            symbols = []
        for sym in symbols:
            base = sym.replace("_USDC_PERP", "").replace("_USDC", "").replace("_USDT", "")
            try:
                data = await cls._req(creds, "GET", "/api/v1/history/fills",
                                       "fillHistoryQueryAll",
                                       params={"symbol": sym, "limit": 1000})
                rows = data if isinstance(data, list) else (data or {}).get("fills") or []
                for r in rows:
                    try:
                        ts_str = r.get("timestamp") or r.get("createdAt") or ""
                        if ts_str:
                            from datetime import timezone
                            ts = _dt.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc)
                        else:
                            continue
                        ts_ms = int(ts.timestamp() * 1000)
                        if ts_ms < since_ms:
                            continue
                        qty = float(r.get("quantity") or r.get("qty") or 0)
                        price = float(r.get("price") or 0)
                        fee = float(r.get("fee") or 0)
                        side_raw = (r.get("side") or "").lower()
                        side = "buy" if side_raw in ("bid", "buy") else "sell"
                        out.append({
                            "symbol": base,
                            "side": side,
                            "qty": qty,
                            "price": price,
                            "fee_usd": fee,
                            "realized_pnl_usd": None,
                            "ts": ts.replace(tzinfo=None),
                            "ext_trade_id": str(r.get("id") or r.get("tradeId") or ""),
                            "ext_order_id": str(r.get("orderId") or "") or None,
                            "kind": "trade",
                        })
                    except Exception:
                        continue
            except Exception as exc:
                logger.debug("backpack fills %s failed: %s", sym, exc)
                continue
        return out
