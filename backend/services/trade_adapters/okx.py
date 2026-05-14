"""OKX USDT-M Swap trade adapter."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json as jsonlib
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx

BASE = "https://www.okx.com"
logger = logging.getLogger("avalant.trade.okx")

_INSTR_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()


async def _instruments() -> dict[str, dict]:
    """Return {instId: {lotSz, minSz, tickSz, ctVal, lever}}."""
    now = time.time()
    if _INSTR_CACHE["data"] and now - _INSTR_CACHE["ts"] < _INSTR_TTL:
        return _INSTR_CACHE["data"]
    async with _INSTR_LOCK:
        if _INSTR_CACHE["data"] and time.time() - _INSTR_CACHE["ts"] < _INSTR_TTL:
            return _INSTR_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/api/v5/public/instruments?instType=SWAP")
                items = r.json().get("data") or []
        except Exception as e:
            logger.warning("OKX instruments fetch failed: %s", e)
            return _INSTR_CACHE["data"] or {}
        out: dict[str, dict] = {}
        for it in items:
            iid = it.get("instId", "")
            if not iid.endswith("-USDT-SWAP"):
                continue
            out[iid] = {
                "lotSz": float(it.get("lotSz") or 1),
                "minSz": float(it.get("minSz") or 1),
                "tickSz": float(it.get("tickSz") or 0.01),
                "ctVal": float(it.get("ctVal") or 1),
                "lever": int(float(it.get("lever") or 125)),
            }
        _INSTR_CACHE["data"] = out
        _INSTR_CACHE["ts"] = time.time()
        return out


_OKX_FRIENDLY = {
    "50000": "Bad request body.",
    "50001": "API key does not match current environment.",
    "50002": "OKX rejected the request — check timestamp.",
    "50004": "Endpoint requires a higher API key permission level.",
    "50011": "Rate limit exceeded — try again in a moment.",
    "50013": "System busy — try again.",
    "50014": "Invalid API key.",
    "50026": "System error — try again.",
    "50111": "Invalid account — check API key scope.",
    "51000": "Parameter error.",
    "51001": "Instrument does not exist on OKX.",
    "51008": "Insufficient balance.",
    "51010": "Account mismatch — key may not have trade permission.",
    "51020": "Order size below minimum.",
    "51100": "Trading account is not activated.",
    "51101": "Too many open orders.",
    "51109": "Order not exist.",
    "51113": "Close position order size exceeds position.",
    "51115": "Leverage cannot exceed the exchange max.",
    "51116": "Cancel order would cause position liquidation.",
    "59000": "Margin mode cannot be changed while holding positions.",
    "59001": "Margin mode already set.",
}


def _friendly_okx(code: str | None, msg: str) -> str:
    if code and code in _OKX_FRIENDLY:
        return _OKX_FRIENDLY[code]
    return msg or "OKX rejected the request."


def _split_code(exc: Exception) -> tuple[str | None, str]:
    import re
    s = str(exc)
    m = re.match(r"OKX (\d+): (.*)", s)
    if m:
        return m.group(1), m.group(2)
    return None, s


def _round_qty_to_step(qty: float, step: float) -> float:
    if step > 0:
        return math.floor(qty / step) * step
    return qty


def _qty_to_str(qty: float) -> str:
    s = f"{qty:.8f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


def _okx_symbol(s: str) -> str:
    return s.upper() + "-USDT-SWAP"


class OKXAdapter:
    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    @staticmethod
    def _sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
        pre = timestamp + method.upper() + path + body
        digest = hmac.new(secret.encode(), pre.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    @classmethod
    async def _req(cls, creds: dict, method: str, path: str, body: dict | None = None) -> Any:
        ts = cls._ts()
        body_str = jsonlib.dumps(body, separators=(",", ":")) if body else ""
        sig = cls._sign(creds["api_secret"], ts, method, path, body_str)
        headers = {
            "OK-ACCESS-KEY": creds["api_key"],
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": creds["api_passphrase"],
            "Content-Type": "application/json",
        }
        # Persistent client per host — avoid TLS handshake on every call.
        from backend.services.trade_adapters._http import http_client
        client = http_client(BASE, timeout=10.0)
        if method == "GET":
            r = await client.get(path, headers=headers)
        else:
            r = await client.post(path, headers=headers, content=body_str)
        if r.status_code >= 400:
            raise RuntimeError(f"OKX HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        code = str(j.get("code", "0"))
        if code != "0":
            msg = j.get("msg") or ""
            # some endpoints put error in data[0].sMsg
            if j.get("data") and isinstance(j["data"], list) and j["data"]:
                sub = j["data"][0]
                msg = msg or sub.get("sMsg") or sub.get("msg") or ""
                code = sub.get("sCode") or code
            raise RuntimeError(f"OKX {code}: {msg}")
        return j.get("data") or []

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        """OKX has a unified trading account (cross-margin pool funds spot AND
        futures together) plus a separate Funding account for idle off-trade
        assets. Report both — `spot_usd` / `futures_usd` are populated with
        the trading-pool total (single pool can margin either side); `usdt`
        sums in funding for the portfolio total."""
        trading = 0.0
        try:
            data = await cls._req(creds, "GET", "/api/v5/account/balance")
            for acct in data:
                for d in acct.get("details", []):
                    if (d.get("ccy") or "").upper() in ("USDT", "USDC", "USDK"):
                        try:
                            trading += float(d.get("cashBal") or d.get("availBal") or 0)
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass
        funding = 0.0
        try:
            data = await cls._req(creds, "GET", "/api/v5/asset/balances")
            for d in data if isinstance(data, list) else []:
                if (d.get("ccy") or "").upper() in ("USDT", "USDC", "USDK"):
                    try:
                        funding += float(d.get("bal") or d.get("availBal") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
        return {
            "usdt": trading + funding,
            "spot_usd": trading,    # unified pool funds spot orders
            "futures_usd": trading, # unified pool funds futures margin
        }

    # ── Leverage + margin mode ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        inst_id = _okx_symbol(symbol)
        mgn = "isolated" if margin_mode == "isolated" else "cross"
        # Set position mode to long_short_mode (hedge) — ignore if already set
        try:
            await cls._req(creds, "POST", "/api/v5/account/set-position-mode", {"posMode": "long_short_mode"})
        except RuntimeError:
            pass
        # In `long_short_mode` + `isolated`, OKX requires `posSide` on the
        # set-leverage call (51000 "Parameter posSide error" otherwise).
        # We have to set leverage for BOTH long and short legs since either
        # could be opened next. In `cross`, posSide is forbidden.
        async def _try(extra: dict) -> None:
            try:
                await cls._req(creds, "POST", "/api/v5/account/set-leverage", {
                    "instId": inst_id,
                    "lever": str(int(leverage)),
                    "mgnMode": mgn,
                    **extra,
                })
            except RuntimeError as e:
                s = str(e)
                # 59001 "Account level too low" / 59000 "Position exists" — non-fatal
                if "59001" in s or "59000" in s:
                    return
                raise
        try:
            if mgn == "isolated":
                await asyncio.gather(_try({"posSide": "long"}), _try({"posSide": "short"}))
            else:
                await _try({})
        except RuntimeError as e:
            raise RuntimeError(_friendly_okx(*_split_code(e)))

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        info = (await _instruments()).get(_okx_symbol(symbol))
        if not info:
            return None
        ct_val = float(info.get("ctVal") or 1) or 1
        lot_sz_contracts = float(info.get("lotSz") or 1)
        min_sz_contracts = float(info.get("minSz") or 1)
        return {
            "min_qty": min_sz_contracts * ct_val,
            "step":    lot_sz_contracts * ct_val,
            "max_qty": None,
            "unit": "coin",
        }

    # ── Preflight ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        inst_id = _okx_symbol(symbol)
        instruments = await _instruments()
        info = instruments.get(inst_id)
        if not info:
            return {"ok": False, "reason": f"{inst_id} is not listed on OKX."}

        ct_val = info["ctVal"]
        lot_sz = info["lotSz"]
        min_sz = info["minSz"]

        # OKX sz is in contracts; 1 contract = ctVal coins
        # Convert coin quantity to contracts
        contracts = quantity / ct_val if ct_val > 0 else quantity
        contracts = _round_qty_to_step(contracts, lot_sz)
        if contracts < min_sz:
            return {"ok": False, "reason": f"Quantity below minimum ({min_sz} contracts = {min_sz * ct_val} {symbol.upper()})."}

        # Mark price
        mark_price = 0.0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v5/public/mark-price?instType=SWAP&instId={inst_id}")
                d = r.json().get("data") or []
                if d:
                    mark_price = float(d[0].get("markPx") or 0)
        except Exception:
            pass

        # Balance check — honor cached hint if available.
        cached_bal = creds.get("_cached_balance_usdt")
        if cached_bal is not None:
            bal = float(cached_bal)
        else:
            try:
                bal = (await cls.fetch_balance(creds)).get("usdt", 0)
            except RuntimeError as e:
                return {"ok": False, "reason": _friendly_okx(*_split_code(e))}

        notional = contracts * ct_val * mark_price
        if mark_price and leverage > 0:
            required = notional / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False, "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}

        return {
            "ok": True,
            "qty_rounded": contracts * ct_val,
            "contracts": contracts,
            "ct_val": ct_val,
            "lot_sz": lot_sz,
            "min_sz": min_sz,
        }

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        inst_id = _okx_symbol(symbol)
        instruments = await _instruments()
        info = instruments.get(inst_id) or {}
        ct_val = info.get("ctVal", 1)
        lot_sz = info.get("lotSz", 1)

        contracts = quantity / ct_val if ct_val > 0 else quantity
        contracts = _round_qty_to_step(contracts, lot_sz)
        if contracts <= 0:
            raise RuntimeError(f"Quantity too small for {inst_id}.")

        try:
            r = await cls._req(creds, "POST", "/api/v5/trade/order", {
                "instId": inst_id,
                "tdMode": "isolated" if margin_mode == "isolated" else "cross",
                "side": "buy" if side == "buy" else "sell",
                "posSide": "long" if side == "buy" else "short",
                "ordType": "market",
                "sz": _qty_to_str(contracts),
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_okx(*_split_code(e)))
        order_id = r[0].get("ordId", "") if r else ""
        # Fast path: return immediately with order_id; avg_price=0 → the
        # user-stream WS supervisor + reconcile worker fill it in within
        # seconds. Previously we slept 300ms + made a second HTTP call
        # (~500ms total added to the user-visible latency) just to grab
        # avgPx — wasteful when WS already pushes that data.
        return {"order_id": str(order_id), "avg_price": 0.0}

    # ── Close position ──
    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        inst_id = _okx_symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        # Match by side — in hedge mode the same instId can have both long
        # and short positions; closing the wrong leg is silently no-op.
        target = next((q for q in positions if (q.get("side") or "").lower() == side.lower()), positions[0])
        p = target
        pos_side = "long" if (p.get("side") or "").lower() == "buy" else "short"
        td_mode = p.get("margin_mode") or "isolated"
        td_mode = "isolated" if td_mode.lower().startswith("iso") else "cross"

        # Use the dedicated close-position endpoint instead of placing an
        # opposing reduce-only market order. The native flatten:
        #   - succeeds in both one-way and hedge mode
        #   - doesn't require us to compute the contract size correctly
        #   - returns cleanly even when the position was just closed by
        #     someone else (idempotent)
        # Contrast: a reduce-only POST /trade/order with sz=contracts would
        # error 51121 "all operations failed" if the cached qty is stale,
        # which happens whenever the user just trimmed the position.
        try:
            r = await cls._req(creds, "POST", "/api/v5/trade/close-position", {
                "instId": inst_id,
                "mgnMode": td_mode,
                "posSide": pos_side,
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_okx(*_split_code(e)))
        order_id = ""
        if r and isinstance(r, list) and r[0].get("clOrdId"):
            order_id = r[0].get("clOrdId", "")
        return {"order_id": str(order_id), "closed_qty": p["quantity"], "realized_pnl_usd": p.get("unrealized_pnl_usd", 0)}

    # ── Positions ──
    @classmethod
    async def _funding_pnl(cls, creds: dict, inst_id: str, since_ms: int) -> float | None:
        """Sum funding bills for `inst_id`. OKX type=8 = funding fee.
        /api/v5/account/bills-archive covers >3 months; /bills covers recent
        3 months. We use /bills since arb positions are usually short-lived."""
        try:
            path = f"/api/v5/account/bills?instType=SWAP&type=8&instId={inst_id}&begin={since_ms}&limit=100"
            data = await cls._req(creds, "GET", path)
            return sum(float(x.get("balChg") or x.get("pnl") or 0) for x in (data or []))
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        path = "/api/v5/account/positions?instType=SWAP"
        if symbol:
            path += f"&instId={_okx_symbol(symbol)}"
        data = await cls._req(creds, "GET", path)
        # Pull instruments cache once — OKX position responses sometimes omit
        # ctVal (and we'd silently default to 1, multiplying quantity by 1
        # instead of e.g. 1000 for DOGE-USDT-SWAP). Quote: BUG: previously
        # qty came back as 0.15 instead of 150 → close-by-coins computed
        # contracts=0.15/1000=0.00015 and rejected as "qty too small".
        instruments = await _instruments()
        positions = []
        for p in data:
            pos = float(p.get("pos") or 0)
            if pos == 0:
                continue
            inst_id = p.get("instId", "")
            # Prefer the position's own ctVal; fall back to instruments cache
            # which is the source of truth from /public/instruments.
            ct_val = float(p.get("ctVal") or 0) or instruments.get(inst_id, {}).get("ctVal", 1)
            qty_coins = abs(pos) * ct_val
            ps = p.get("posSide", "")
            if ps == "long":
                side = "buy"
            elif ps == "short":
                side = "sell"
            else:
                side = "buy" if pos > 0 else "sell"
            sym = inst_id.replace("-USDT-SWAP", "")
            mgn = (p.get("mgnMode") or "").lower()
            margin_mode = "isolated" if mgn.startswith("iso") else ("cross" if mgn else None)
            positions.append({
                "exchange": "okx",
                "symbol": sym,
                "_inst_id": inst_id,
                "side": side,
                "quantity": qty_coins,
                "entry_price": float(p.get("avgPx") or 0),
                "mark_price": float(p.get("markPx") or 0),
                "unrealized_pnl_usd": float(p.get("upl") or 0),
                "leverage": int(float(p.get("lever") or 1)),
                "margin_mode": margin_mode,
                "position_id": inst_id,
            })
        if not positions:
            return []
        import time as _t
        since_ms = int((_t.time() - 7 * 86400) * 1000)
        from backend.services.trade_adapters._funding_cache import cached_funding
        api_key = (creds.get("api_key") or "").strip()
        fundings = await asyncio.gather(*[
            cached_funding(api_key, p["_inst_id"],
                           lambda p=p: cls._funding_pnl(creds, p["_inst_id"], since_ms))
            for p in positions
        ], return_exceptions=True)
        for p, f in zip(positions, fundings):
            p["funding_pnl_usd"] = f if isinstance(f, (int, float)) else None
            p.pop("_inst_id", None)
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
            if "50014" in msg or "Invalid" in msg:
                out["error"] = "Invalid API key"
            elif "50004" in msg or "permission" in msg.lower():
                out["error"] = "API key permissions insufficient"
            else:
                out["error"] = f"OKX rejected the key: {msg[:180]}"
            return out
        if need_trade:
            try:
                cfg = await cls._req(creds, "GET", "/api/v5/account/config")
                if cfg:
                    acct_lv = cfg[0].get("acctLv", "")
                    # acctLv 1=simple, 2=single-ccy margin, 3=multi-ccy, 4=portfolio
                    out["can_trade"] = acct_lv in ("1", "2", "3", "4")
                    if not out["can_trade"]:
                        out["error"] = "Account level does not support futures trading"
            except Exception as e:
                out["error"] = f"Trade-permission probe failed: {str(e)[:180]}"
        return out

    # ── Max leverage ──
    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        instruments = await _instruments()
        info = instruments.get(_okx_symbol(symbol))
        if info:
            return info.get("lever", 125)
        return 125

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts, *,
                                 market: str = "futures") -> list[dict]:
        """OKX fills + funding since `since_ts`.

        /api/v5/trade/fills-history?instType=SWAP|SPOT&begin=<ms> — paginated
        via `after` (the latest billId returned). Funding via
        /api/v5/account/bills-archive?type=8 (settlement type code 8)."""
        from datetime import datetime as _dt
        if market not in ("futures", "spot"):
            return []
        inst_type = "SWAP" if market == "futures" else "SPOT"
        out: list[dict] = []
        begin_ms = int(since_ts.timestamp() * 1000)
        # Pull fills (paginated by `after`).
        after = ""
        for _ in range(20):
            qs = f"instType={inst_type}&begin={begin_ms}&limit=100"
            if after:
                qs += f"&after={after}"
            try:
                rows = await cls._req(creds, "GET",
                                      f"/api/v5/trade/fills-history?{qs}") or []
            except Exception:
                break
            if not rows:
                break
            for r in rows:
                try:
                    inst = str(r.get("instId") or "")  # e.g. BTC-USDT-SWAP / BTC-USDT
                    sym = inst.split("-")[0] if inst else ""
                    side_raw = str(r.get("side") or "").lower()
                    side = "buy" if side_raw == "buy" else "sell"
                    sz = float(r.get("fillSz") or 0)
                    if sz <= 0:
                        continue
                    px = float(r.get("fillPx") or 0)
                    fee_raw = r.get("fee")
                    fee = abs(float(fee_raw)) if fee_raw not in (None, "") else None
                    ts_ms = int(r.get("ts") or 0)
                    if ts_ms <= 0:
                        continue
                    pnl_raw = r.get("fillPnl")
                    rpnl = float(pnl_raw) if pnl_raw not in (None, "") else None
                    # OKX SWAP returns size in CONTRACTS, not coins. Convert.
                    if inst_type == "SWAP":
                        instruments = await _instruments()
                        info = instruments.get(inst) or {}
                        ct_val = float(info.get("ctVal") or 0)
                        if ct_val > 0:
                            sz = sz * ct_val
                    out.append({
                        "symbol": sym.upper(),
                        "side": side,
                        "qty": sz,
                        "price": px,
                        "fee_usd": fee,
                        "realized_pnl_usd": rpnl,
                        "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                        "ext_trade_id": str(r.get("tradeId")
                                            or r.get("billId") or ""),
                        "ext_order_id": str(r.get("ordId") or "") or None,
                        "kind": "trade",
                    })
                except Exception:
                    continue
            after = rows[-1].get("billId") or rows[-1].get("ts") or ""
            if len(rows) < 100:
                break

        if market == "futures":
            after = ""
            for _ in range(20):
                qs = f"type=8&begin={begin_ms}&limit=100"
                if after:
                    qs += f"&after={after}"
                try:
                    rows = await cls._req(creds, "GET",
                                          f"/api/v5/account/bills-archive?{qs}") or []
                except Exception:
                    break
                if not rows:
                    break
                for r in rows:
                    try:
                        inst = str(r.get("instId") or "")
                        sym = inst.split("-")[0] if inst else ""
                        ts_ms = int(r.get("ts") or 0)
                        if ts_ms <= 0:
                            continue
                        out.append({
                            "symbol": sym.upper(),
                            "side": None,
                            "qty": 0.0, "price": 0.0, "fee_usd": None,
                            "realized_pnl_usd": float(r.get("balChg") or 0),
                            "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                            "ext_trade_id": str(r.get("billId")
                                                or f"funding-{ts_ms}-{sym}"),
                            "ext_order_id": None,
                            "kind": "funding",
                        })
                    except Exception:
                        continue
                after = rows[-1].get("billId") or ""
                if len(rows) < 100:
                    break
        return out
