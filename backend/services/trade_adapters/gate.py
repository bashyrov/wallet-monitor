"""Gate.io USDT Futures trade adapter (v4)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json as jsonlib
import logging
import math
import time
from typing import Any

import httpx

BASE = "https://api.gateio.ws"
logger = logging.getLogger("avalant.trade.gate")

_CONTRACT_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_CONTRACT_TTL = 600
_CONTRACT_LOCK = asyncio.Lock()


async def _contracts() -> dict[str, dict]:
    """Return {contract: {quanto_multiplier, order_size_min, leverage_min, leverage_max, mark_price_round}}."""
    now = time.time()
    if _CONTRACT_CACHE["data"] and now - _CONTRACT_CACHE["ts"] < _CONTRACT_TTL:
        return _CONTRACT_CACHE["data"]
    async with _CONTRACT_LOCK:
        if _CONTRACT_CACHE["data"] and time.time() - _CONTRACT_CACHE["ts"] < _CONTRACT_TTL:
            return _CONTRACT_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/api/v4/futures/usdt/contracts")
                items = r.json()
        except Exception as e:
            logger.warning("Gate contracts fetch failed: %s", e)
            return _CONTRACT_CACHE["data"] or {}
        out: dict[str, dict] = {}
        if not isinstance(items, list):
            return _CONTRACT_CACHE["data"] or {}
        for it in items:
            name = it.get("name", "")
            out[name] = {
                "quanto_multiplier": float(it.get("quanto_multiplier") or 1),
                "order_size_min": int(it.get("order_size_min") or 1),
                "order_size_max": int(it.get("order_size_max") or 1000000),
                "leverage_min": int(it.get("leverage_min") or 1),
                "leverage_max": int(it.get("leverage_max") or 100),
                "mark_price_round": float(it.get("mark_price_round") or 0.01),
                "order_price_round": float(it.get("order_price_round") or 0.01),
            }
        _CONTRACT_CACHE["data"] = out
        _CONTRACT_CACHE["ts"] = time.time()
        return out


_GATE_FRIENDLY = {
    "INVALID_KEY": "Invalid API key.",
    "INVALID_SIGNATURE": "Signature mismatch — API secret is wrong.",
    "INVALID_TIMESTAMP": "Clock skew — try again.",
    "KEY_EXPIRED": "API key has expired.",
    "FORBIDDEN": "API key does not have required permissions.",
    "BALANCE_NOT_ENOUGH": "Insufficient balance for margin.",
    "ORDER_SIZE_MIN": "Order size below minimum.",
    "ORDER_SIZE_MAX": "Order size exceeds maximum.",
    "CONTRACT_NOT_FOUND": "Contract not found on Gate.io Futures.",
    "RISK_LIMIT_EXCEEDED": "Risk limit exceeded.",
    "LEVERAGE_TOO_HIGH": "Leverage exceeds the exchange maximum.",
    "POSITION_NOT_FOUND": "No open position found.",
    "DUAL_SIDE_NOT_ALLOWED": "Dual-side mode not supported in current state.",
}


def _friendly_gate(label: str | None, msg: str) -> str:
    if label and label in _GATE_FRIENDLY:
        return _GATE_FRIENDLY[label]
    return msg or "Gate.io rejected the request."


def _split_label(exc: Exception) -> tuple[str | None, str]:
    import re
    s = str(exc)
    m = re.match(r"Gate (\S+): (.*)", s)
    if m:
        return m.group(1), m.group(2)
    return None, s


def _gate_symbol(s: str) -> str:
    return s.upper() + "_USDT"


class GateAdapter:
    @staticmethod
    def _sign(secret: str, method: str, path: str, query: str, body: str, ts: str) -> str:
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        pre = f"{method}\n{path}\n{query}\n{body_hash}\n{ts}"
        return hmac.new(secret.encode(), pre.encode(), hashlib.sha512).hexdigest()

    @classmethod
    async def _req(cls, creds: dict, method: str, path: str,
                   query: dict | None = None, body: dict | None = None) -> Any:
        ts = str(int(time.time()))
        query_str = "&".join(f"{k}={query[k]}" for k in sorted(query)) if query else ""
        body_str = jsonlib.dumps(body, separators=(",", ":")) if body else ""
        sig = cls._sign(creds["api_secret"], method, path, query_str, body_str, ts)
        headers = {
            "KEY": creds["api_key"],
            "SIGN": sig,
            "Timestamp": ts,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Persistent client per host — TLS handshake paid once.
        from backend.services.trade_adapters._http import http_client
        client = http_client(BASE, timeout=10.0)
        url_path = path + ("?" + query_str if query_str else "")
        if method == "GET":
            r = await client.get(url_path, headers=headers)
        elif method == "POST":
            r = await client.post(url_path, headers=headers, content=body_str)
        elif method == "DELETE":
            r = await client.delete(url_path, headers=headers)
        else:
            raise ValueError(method)
        if r.status_code >= 400:
            label = None
            msg = r.text
            try:
                j = r.json()
                label = j.get("label")
                msg = j.get("message") or j.get("detail") or r.text
            except Exception:
                pass
            raise RuntimeError(f"Gate {label or r.status_code}: {msg}")
        if r.status_code == 204 or not r.text.strip():
            return {}
        return r.json()

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._req(creds, "GET", "/api/v4/futures/usdt/accounts")
        return {"usdt": float(data.get("available") or 0)}

    # ── Leverage ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        contract = _gate_symbol(symbol)
        # Gate's leverage endpoint takes `leverage` as a QUERY parameter, not
        # a JSON body — sending it in the body produces "Missing required
        # parameter: leverage" even though the spec is unambiguous.
        # 0 = cross, >0 = isolated with that leverage.
        lev_val = int(leverage) if margin_mode == "isolated" else 0
        try:
            await cls._req(creds, "POST",
                           f"/api/v4/futures/usdt/positions/{contract}/leverage",
                           query={"leverage": lev_val})
        except RuntimeError as e:
            s = str(e)
            if "not changed" not in s.lower() and "same" not in s.lower():
                raise RuntimeError(_friendly_gate(*_split_label(e)))
        if margin_mode == "isolated":
            try:
                await cls._req(creds, "POST",
                               f"/api/v4/futures/usdt/positions/{contract}/leverage",
                               query={"leverage": int(leverage), "cross_leverage_limit": "0"})
            except RuntimeError:
                pass

    # ── Public qty limits ──
    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        """Min / max / step qty for `symbol` (in coin units, not contracts).
        Used by the trade panel to render an inline "min 0.01 SPACEX, step
        0.001" hint and reject sub-min orders before they hit preflight.
        Returns None when the symbol isn't on Gate."""
        contract = _gate_symbol(symbol)
        all_contracts = await _contracts()
        info = all_contracts.get(contract)
        if not info:
            return None
        quanto = float(info.get("quanto_multiplier") or 1)
        return {
            "min_qty": float(info.get("order_size_min") or 1) * quanto,
            "max_qty": float(info.get("order_size_max") or 0) * quanto if info.get("order_size_max") else None,
            "step":    quanto,    # 1 contract = quanto coins → smallest increment
            "unit":    "coin",
        }

    # ── Preflight ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        contract = _gate_symbol(symbol)
        all_contracts = await _contracts()
        info = all_contracts.get(contract)
        if not info:
            return {"ok": False, "reason": f"{contract} is not listed on Gate.io Futures."}

        quanto = info["quanto_multiplier"]
        min_size = info["order_size_min"]

        # Convert coin quantity → contracts: 1 contract = quanto_multiplier coins
        if quanto > 0:
            num_contracts = int(quantity / quanto)
        else:
            num_contracts = int(quantity)

        if num_contracts < min_size:
            return {"ok": False,
                    "reason": f"Quantity below minimum ({min_size} contracts = {min_size * quanto} {symbol.upper()})."}

        if num_contracts > info["order_size_max"]:
            return {"ok": False, "reason": f"Order size exceeds maximum ({info['order_size_max']} contracts)."}

        # Mark price
        mark_price = 0.0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v4/futures/usdt/contracts/{contract}")
                d = r.json()
                mark_price = float(d.get("mark_price") or d.get("last_price") or 0)
        except Exception:
            pass

        # Balance
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            return {"ok": False, "reason": _friendly_gate(*_split_label(e))}

        notional = num_contracts * quanto * mark_price
        if mark_price and leverage > 0:
            required = notional / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False,
                        "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}

        max_lev = info.get("leverage_max", 100)
        if leverage > max_lev:
            return {"ok": False, "reason": f"Leverage {leverage}x exceeds max {max_lev}x for {contract}."}

        return {
            "ok": True,
            "qty_rounded": num_contracts * quanto,
            "contracts": num_contracts,
            "quanto_multiplier": quanto,
            "min_size": min_size,
        }

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        contract = _gate_symbol(symbol)
        all_contracts = await _contracts()
        info = all_contracts.get(contract) or {}
        quanto = info.get("quanto_multiplier", 1)

        num_contracts = int(quantity / quanto) if quanto > 0 else int(quantity)
        if num_contracts <= 0:
            raise RuntimeError(f"Quantity too small for {contract}.")

        # Gate: positive size = long, negative = short
        size = num_contracts if side == "buy" else -num_contracts

        try:
            r = await cls._req(creds, "POST", "/api/v4/futures/usdt/orders", body={
                "contract": contract,
                "size": size,
                "price": "0",
                "tif": "ioc",
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_gate(*_split_label(e)))
        order_id = str(r.get("id", ""))
        fill_price = float(r.get("fill_price") or 0)
        return {"order_id": order_id, "avg_price": fill_price}

    # ── Close position ──
    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        contract = _gate_symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}

        # Find the position matching the side we want to close. In dual-mode
        # there can be both long+short on the same contract — pick the right one.
        target = None
        for p in positions:
            if (p.get("side") or "").lower() == side.lower():
                target = p
                break
        if target is None:
            target = positions[0]

        # Gate has two close paths:
        #   single-mode (default): `close: true, size: 0` auto-flattens whichever
        #     direction holds an open position. Fails with POSITION_DUAL_MODE
        #     if the account has dual-mode enabled.
        #   dual-mode: must use `auto_size: "close_long"` or `"close_short"`
        #     (size still 0). Identifies which leg to flatten explicitly.
        # Try the single-mode path first; on dual-mode error, retry with auto_size.
        body_single = {
            "contract": contract,
            "size": 0,
            "price": "0",
            "tif": "ioc",
            "close": True,
        }
        try:
            r = await cls._req(creds, "POST", "/api/v4/futures/usdt/orders", body=body_single)
        except RuntimeError as e:
            label, msg = _split_label(e)
            if label == "POSITION_DUAL_MODE":
                # Dual-mode: explicitly specify which leg to close.
                auto_size = "close_long" if (target.get("side") or "").lower() == "buy" else "close_short"
                body_dual = {
                    "contract": contract,
                    "size": 0,
                    "price": "0",
                    "tif": "ioc",
                    "auto_size": auto_size,
                    "reduce_only": True,
                }
                try:
                    r = await cls._req(creds, "POST", "/api/v4/futures/usdt/orders", body=body_dual)
                except RuntimeError as e2:
                    raise RuntimeError(_friendly_gate(*_split_label(e2)))
            else:
                raise RuntimeError(_friendly_gate(label, msg))
        order_id = str(r.get("id", ""))
        return {"order_id": order_id, "closed_qty": target["quantity"], "realized_pnl_usd": target.get("unrealized_pnl_usd", 0)}

    # ── Positions ──
    @classmethod
    async def _funding_pnl(cls, creds: dict, contract: str, since_s: int) -> float | None:
        """Sum `change` from /api/v4/futures/usdt/account_book?type=fund."""
        try:
            data = await cls._req(creds, "GET",
                f"/api/v4/futures/usdt/account_book?type=fund&contract={contract}&from={since_s}&limit=100")
            return sum(float(x.get("change") or 0) for x in (data or []))
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        data = await cls._req(creds, "GET", "/api/v4/futures/usdt/positions")
        if not isinstance(data, list):
            data = []
        all_contracts = await _contracts()
        positions = []
        for p in data:
            size = int(p.get("size") or 0)
            if size == 0:
                continue
            cname = p.get("contract", "")
            if symbol and cname != _gate_symbol(symbol):
                continue
            quanto = all_contracts.get(cname, {}).get("quanto_multiplier", 1)
            qty_coins = abs(size) * quanto
            sym = cname.replace("_USDT", "")
            # Gate: leverage=0 means cross margin; >0 means isolated with
            # that leverage. There's no separate marginMode field.
            lev_val = float(p.get("leverage") or 0)
            margin_mode = "isolated" if lev_val > 0 else "cross"
            positions.append({
                "exchange": "gate",
                "symbol": sym,
                "_contract": cname,
                "side": "buy" if size > 0 else "sell",
                "quantity": qty_coins,
                "entry_price": float(p.get("entry_price") or 0),
                "mark_price": float(p.get("mark_price") or 0),
                "unrealized_pnl_usd": float(p.get("unrealised_pnl") or 0),
                "leverage": int(lev_val) if lev_val > 0 else int(float(p.get("cross_leverage_limit") or 1)),
                "margin_mode": margin_mode,
                "position_id": cname,
            })
        if not positions:
            return []
        since_s = int(time.time() - 7 * 86400)
        from backend.services.trade_adapters._funding_cache import cached_funding
        api_key = (creds.get("api_key") or "").strip()
        fundings = await asyncio.gather(*[
            cached_funding(api_key, p["_contract"],
                           lambda p=p: cls._funding_pnl(creds, p["_contract"], since_s))
            for p in positions
        ], return_exceptions=True)
        for p, f in zip(positions, fundings):
            p["funding_pnl_usd"] = f if isinstance(f, (int, float)) else None
            p.pop("_contract", None)
        return positions

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
            if "INVALID_KEY" in msg:
                out["error"] = "Invalid API key"
            elif "INVALID_SIGNATURE" in msg:
                out["error"] = "Signature mismatch — API secret is wrong"
            elif "FORBIDDEN" in msg:
                out["error"] = "API key permissions insufficient"
            else:
                out["error"] = f"Gate.io rejected the key: {msg[:180]}"
            return out
        if need_trade:
            # Gate doesn't have a dedicated permissions endpoint;
            # try placing a tiny order that will fail on size to confirm trade access
            try:
                await cls._req(creds, "POST", "/api/v4/futures/usdt/orders", body={
                    "contract": "BTC_USDT",
                    "size": 0,
                    "price": "0",
                    "tif": "ioc",
                })
                out["can_trade"] = True
            except RuntimeError as e:
                msg = str(e)
                if "FORBIDDEN" in msg or "permission" in msg.lower():
                    out["error"] = "Key has no Futures trading permission"
                else:
                    # Any other error (like ORDER_SIZE_MIN) means the key CAN trade
                    out["can_trade"] = True
        return out

    # ── Max leverage ──
    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        all_contracts = await _contracts()
        info = all_contracts.get(_gate_symbol(symbol))
        if info:
            return info.get("leverage_max", 100)
        return 100

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts, *,
                                 market: str = "futures") -> list[dict]:
        """Gate fills + funding since `since_ts`.

        Futures: /api/v4/futures/usdt/my_trades?from=<sec>&limit=1000 + funding
        via /api/v4/futures/usdt/account_book?type=fund.
        Spot: /api/v4/spot/my_trades. Spot side requires a `currency_pair`
        param for some accounts; we sweep top USDT pairs from balances."""
        from datetime import datetime as _dt
        from_s = int(since_ts.timestamp())
        out: list[dict] = []
        if market == "futures":
            try:
                rows = await cls._req(creds, "GET",
                                      "/api/v4/futures/usdt/my_trades",
                                      query={"from": from_s, "limit": 1000}) or []
            except Exception:
                rows = []
            # Build per-contract qty multiplier (size is in contracts).
            try:
                contracts = await _contracts()
            except Exception:
                contracts = {}
            for r in rows:
                try:
                    contract = str(r.get("contract") or "")
                    sym = contract.replace("_USDT", "")
                    sz_contracts = float(r.get("size") or 0)
                    side = "buy" if sz_contracts > 0 else "sell"
                    info = contracts.get(contract) or {}
                    multiplier = float(info.get("quanto_multiplier") or 1.0) or 1.0
                    qty = abs(sz_contracts) * multiplier
                    if qty <= 0:
                        continue
                    ts_raw = r.get("create_time")
                    ts_s = float(ts_raw) if ts_raw else 0
                    if ts_s <= 0:
                        continue
                    out.append({
                        "symbol": sym.upper(),
                        "side": side,
                        "qty": qty,
                        "price": float(r.get("price") or 0),
                        "fee_usd": None,
                        "realized_pnl_usd": None,
                        "ts": _dt.utcfromtimestamp(ts_s),
                        "ext_trade_id": str(r.get("id") or ""),
                        "ext_order_id": str(r.get("order_id") or "") or None,
                        "kind": "trade",
                    })
                except Exception:
                    continue
            try:
                fund_rows = await cls._req(creds, "GET",
                                           "/api/v4/futures/usdt/account_book",
                                           query={"from": from_s, "type": "fund",
                                                  "limit": 1000}) or []
            except Exception:
                fund_rows = []
            for r in fund_rows:
                try:
                    contract = str(r.get("text") or "")  # contract goes in "text"
                    sym = contract.replace("_USDT", "")
                    ts_s = float(r.get("time") or 0)
                    if ts_s <= 0:
                        continue
                    out.append({
                        "symbol": sym.upper(),
                        "side": None,
                        "qty": 0.0, "price": 0.0, "fee_usd": None,
                        "realized_pnl_usd": float(r.get("change") or 0),
                        "ts": _dt.utcfromtimestamp(ts_s),
                        "ext_trade_id": str(r.get("id") or f"funding-{ts_s}-{sym}"),
                        "ext_order_id": None,
                        "kind": "funding",
                    })
                except Exception:
                    continue
            return out
        if market == "spot":
            # Sweep spot pairs from non-USDT balances.
            try:
                balances = await cls._req(creds, "GET", "/api/v4/spot/accounts") or []
            except Exception:
                return out
            stables = {"USDT", "USDC", "BUSD", "DAI"}
            for b in balances:
                cur = str(b.get("currency") or "").upper()
                total = float(b.get("available") or 0) + float(b.get("locked") or 0)
                if cur in stables or total <= 0:
                    continue
                pair = f"{cur}_USDT"
                try:
                    rows = await cls._req(creds, "GET",
                                          "/api/v4/spot/my_trades",
                                          query={"currency_pair": pair,
                                                 "from": from_s,
                                                 "limit": 1000}) or []
                except Exception:
                    continue
                for r in rows:
                    try:
                        sz = float(r.get("amount") or 0)
                        if sz <= 0:
                            continue
                        side = "buy" if str(r.get("side") or "") == "buy" else "sell"
                        ts_raw = r.get("create_time")
                        ts_s = float(ts_raw) if ts_raw else 0
                        if ts_s <= 0:
                            continue
                        out.append({
                            "symbol": cur,
                            "side": side,
                            "qty": sz,
                            "price": float(r.get("price") or 0),
                            "fee_usd": float(r.get("fee") or 0),
                            "realized_pnl_usd": None,
                            "ts": _dt.utcfromtimestamp(ts_s),
                            "ext_trade_id": str(r.get("id") or ""),
                            "ext_order_id": str(r.get("order_id") or "") or None,
                            "kind": "trade",
                        })
                    except Exception:
                        continue
            return out
        return out
