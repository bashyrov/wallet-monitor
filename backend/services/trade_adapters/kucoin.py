"""KuCoin Futures trade adapter (api-futures.kucoin.com)."""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import math
import time
from typing import Any

import httpx

from backend.providers.exchanges._signing import b64_hmac_sha256

BASE = "https://api-futures.kucoin.com"
logger = logging.getLogger("avalant.trade.kucoin")

_INSTR_CACHE: dict[str, tuple[dict, float]] = {}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()

# BTC → XBT mapping
_BTC_TO_XBT = {"BTC": "XBT"}


def _kc_symbol(s: str) -> str:
    base = s.upper()
    base = _BTC_TO_XBT.get(base, base)
    return base + "USDTM"


async def _instrument_info(symbol: str) -> dict | None:
    now = time.time()
    hit = _INSTR_CACHE.get(symbol)
    if hit and now - hit[1] < _INSTR_TTL:
        return hit[0]
    async with _INSTR_LOCK:
        hit = _INSTR_CACHE.get(symbol)
        if hit and time.time() - hit[1] < _INSTR_TTL:
            return hit[0]
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(f"{BASE}/api/v1/contracts/{symbol}")
                j = r.json()
                data = j.get("data")
                if not data:
                    return None
                info = {
                    "multiplier": float(data.get("multiplier") or 1),
                    "lotSize": int(data.get("lotSize") or 1),
                    "tickSize": float(data.get("tickSize") or 0.01),
                    "maxLeverage": int(float(data.get("maxLeverage") or 100)),
                    "isInverse": bool(data.get("isInverse")),
                    "status": str(data.get("status") or ""),
                }
                _INSTR_CACHE[symbol] = (info, time.time())
                return info
        except Exception as e:
            logger.debug("KuCoin instrument fetch failed %s: %s", symbol, e)
            return None


_KC_FRIENDLY = {
    "100001": "Request too frequent — rate limited.",
    "200004": "Insufficient balance.",
    "300000": "Invalid symbol or not supported.",
    "300003": "Order quantity below minimum.",
    "300012": "Insufficient position to close.",
    "400001": "Invalid API key.",
    "400002": "Signature mismatch.",
    "400003": "Timestamp expired — clock skew.",
    "400005": "API key permissions insufficient.",
    "400100": "Parameter error.",
}


def _friendly_kc(code: str | None, msg: str) -> str:
    if code and code in _KC_FRIENDLY:
        return _KC_FRIENDLY[code]
    return msg or "KuCoin rejected the request."


def _split_code(exc: Exception) -> tuple[str | None, str]:
    import re
    m = re.match(r"KuCoin (\d+): (.*)", str(exc))
    if m:
        return m.group(1), m.group(2)
    return None, str(exc)


class KuCoinAdapter:
    # Server-time offset cache. KuCoin's signed-request window is fixed
    # ±5s — there's no recvWindow override — so on a Docker host with
    # occasional clock jumps we'd hit "400002 Invalid KC-API-TIMESTAMP".
    # Periodic resync (5 min TTL) of (server_time - local_time) keeps
    # every signed request within their window.
    _TIME_OFFSET_MS: float = 0.0
    _TIME_OFFSET_AT: float = 0.0
    _TIME_OFFSET_TTL_S: float = 300.0

    @classmethod
    async def _server_time_offset_ms(cls) -> float:
        now = time.time()
        if now - cls._TIME_OFFSET_AT < cls._TIME_OFFSET_TTL_S:
            return cls._TIME_OFFSET_MS
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                # Public endpoint, no auth — same as Binance's /time.
                r = await c.get(BASE + "/api/v1/timestamp")
                if r.status_code < 400:
                    j = r.json() or {}
                    server_ms = float(j.get("data") or 0)
                    if server_ms > 0:
                        cls._TIME_OFFSET_MS = server_ms - (time.time() * 1000.0)
                        cls._TIME_OFFSET_AT = time.time()
        except Exception:
            # Keep last good offset; better than zero if local clock drifted
            pass
        return cls._TIME_OFFSET_MS

    @staticmethod
    def _symbol(s: str) -> str:
        return _kc_symbol(s)

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None,
                      body: dict | None = None, host: str | None = None) -> Any:
        offset = await cls._server_time_offset_ms()
        ts = str(int(time.time() * 1000 + offset))
        api_key = creds["api_key"]
        secret = creds["api_secret"]
        passphrase = creds["api_passphrase"]

        if method == "GET" and params:
            query = "&".join(f"{k}={params[k]}" for k in sorted(params))
            url_path = path + "?" + query
            body_str = ""
        elif body is not None:
            url_path = path
            body_str = jsonlib.dumps(body, separators=(",", ":"))
        else:
            url_path = path
            body_str = ""

        # KuCoin sign covers the EXACT body bytes we send. Previous code
        # signed body_str="" but POSTed `content=body_str or "{}"`, which
        # made server see {} but signature over "" — error 400005 on
        # every POST without a body (e.g. /api/v1/bullet-private). Fix:
        # if we're POSTing, ensure send-body == sign-body.
        send_body = body_str
        if method == "POST" and not send_body:
            send_body = "{}"
            body_str = "{}"

        sign_str = ts + method + url_path + body_str
        signature = b64_hmac_sha256(secret, sign_str)
        passphrase_sign = b64_hmac_sha256(secret, passphrase)

        headers = {
            "KC-API-KEY": api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": passphrase_sign,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }
        # Persistent client per host — TLS handshake paid once.
        # KuCoin keeps spot on api.kucoin.com and futures on api-futures.kucoin.com.
        # Same signing scheme; just need to swap the base URL for cross-host reads.
        from backend.services.trade_adapters._http import http_client
        client = http_client(host or BASE, timeout=10.0)
        rel_path = url_path if method == "GET" else path
        if method == "GET":
            r = await client.get(rel_path, headers=headers)
        elif method == "POST":
            r = await client.post(rel_path, content=send_body, headers=headers)
        elif method == "DELETE":
            r = await client.delete(rel_path, headers=headers)
        else:
            raise ValueError(method)

        j = r.json()
        code = str(j.get("code", ""))
        if code != "200000":
            raise RuntimeError(f"KuCoin {code}: {j.get('msg', r.text)}")
        return j.get("data")

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        """KuCoin Futures: account-overview is per-margin-currency. KuCoin
        keeps USDT-, USDC-, and XBT-margined pots independent — without an
        explicit `currency` query the API returns USDT only, which silently
        hides USDC / BTC balances. Fetch all three in parallel.

        Returns the canonical `{usdt: float, ...}` shape the rest of the
        codebase expects: `usdt` is the USDT-margin pot's available balance
        (with equity fallback when funds are tied up in a position), and
        `available_total` / `equity_total` aggregate across all three
        currencies (USDT + USDC + XBT) for views that want "everything I
        can see on this account"."""
        import asyncio as _asyncio

        async def _one(cur: str) -> dict:
            try:
                data = await cls._signed(
                    creds, "GET", "/api/v1/account-overview", {"currency": cur},
                )
            except Exception as e:
                return {"currency": cur, "available": 0.0, "equity": 0.0, "ok": False,
                        "err": str(e)}
            d = data or {}
            return {
                "currency": cur,
                "available": float(d.get("availableBalance") or 0),
                "equity":    float(d.get("accountEquity") or d.get("marginBalance") or 0),
                "ok":        True,
            }

        results = await _asyncio.gather(*(_one(c) for c in ("USDT", "USDC", "XBT")))
        # All three pots failing means we couldn't read the account at all
        # (rate limit / timeout / auth) — raise instead of returning zeros,
        # otherwise callers (trade-status panel, preflight) can't tell
        # "no funds" from "read failed" and show a false 0 USDT.
        if not any(r["ok"] for r in results):
            raise RuntimeError(results[0].get("err") or "KuCoin balance read failed")
        by_cur = {r["currency"]: r for r in results}
        usdt_pot = by_cur.get("USDT", {"available": 0.0, "equity": 0.0})
        usdt_fut = usdt_pot["available"] if usdt_pot["available"] > 0 else usdt_pot["equity"]
        fut_total = sum(r["equity"] for r in results)
        # Spot — KuCoin Spot/Margin/Main lives on api.kucoin.com (the
        # futures adapter's BASE is api-futures.kucoin.com which doesn't
        # expose /accounts). Sum across ALL sub-accounts (no type= filter)
        # so portfolio + arb display the user's true exchange total —
        # otherwise $20 sitting in Main shows as unavailable while it's
        # a one-click transfer from being tradable.
        spot_usd = 0.0
        try:
            data = await cls._signed(creds, "GET", "/api/v1/accounts",
                                     host="https://api.kucoin.com")
            for r in (data or []):
                if (r.get("currency") or "").upper() in ("USDT", "USDC"):
                    try:
                        spot_usd += float(r.get("balance") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
        # `usdt` is the portfolio-level total: ALL futures pots
        # (USDT+USDC+XBT) summed in USD-equivalent, plus spot stables.
        # The previous "USDT pot only" definition silently hid users'
        # USDC-margined futures balances. `futures_usd` mirrors fut_total
        # for consistency with other adapters' {usdt,spot_usd,futures_usd}
        # contract.
        return {
            "usdt":            fut_total + spot_usd,
            "spot_usd":        spot_usd,
            "futures_usd":     fut_total,
            "available":       usdt_pot["available"],
            "equity":          usdt_pot["equity"],
            "available_total": sum(r["available"] for r in results),
            "equity_total":    fut_total,
            "by_currency":     by_cur,
        }

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)
        # KuCoin enforces per-symbol margin mode independently of the order's
        # marginMode field — if they don't match, place_order fails with
        # "The order's margin mode does not match the selected one." Switch
        # the symbol-level mode explicitly via the v2 endpoint before placing.
        target_mode = "ISOLATED" if margin_mode == "isolated" else "CROSS"
        try:
            await cls._signed(creds, "POST", "/api/v2/position/changeMarginMode", body={
                "symbol": sym,
                "marginMode": target_mode,
            })
        except RuntimeError:
            # Already in target mode → KuCoin returns an error code we don't
            # care about (300013 / similar). Non-fatal.
            pass

        try:
            await cls._signed(creds, "POST", "/api/v1/position/risk-limit-level/change", body={
                "symbol": sym,
                "level": 1,
            })
        except RuntimeError:
            pass

        info = await _instrument_info(sym)
        if not info:
            raise RuntimeError(f"{sym} is not listed on KuCoin Futures.")
        if leverage > info.get("maxLeverage", 100):
            raise RuntimeError(f"Max leverage for {sym} is {info['maxLeverage']}x.")

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        info = await _instrument_info(cls._symbol(symbol))
        if not info:
            return None
        mult = float(info.get("multiplier") or 1) or 1
        lot  = float(info.get("lotSize") or 1) or 1
        return {
            "min_qty": lot * mult,
            "step":    lot * mult,
            "max_qty": None,
            "unit": "coin",
        }

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym)
        if not info:
            return {"ok": False, "reason": f"{sym} is not listed on KuCoin Futures."}
        if info.get("status") and info["status"].lower() not in ("open", ""):
            return {"ok": False, "reason": f"{sym} is not trading ({info['status']})."}

        multiplier = info.get("multiplier", 1)
        lot_size = info.get("lotSize", 1)
        # KuCoin uses contracts: size = number of lots, each lot = multiplier units of base
        qty_lots = int(quantity / multiplier) if multiplier else int(quantity)
        qty_lots = (qty_lots // lot_size) * lot_size
        if qty_lots < lot_size:
            return {"ok": False, "reason": f"Quantity below minimum ({lot_size} lot(s), each = {multiplier} {symbol.upper()})."}

        if leverage > info.get("maxLeverage", 100):
            return {"ok": False, "reason": f"Max leverage for {sym} is {info['maxLeverage']}x."}

        # Balance vs required margin. Honor `_cached_balance_usdt` hint from
        # the user-stream snapshot (same as binance/bybit/okx). When the
        # balance can't be read reliably (transient rate limit / timeout —
        # KuCoin REST is regularly slow under the open-order burst), skip
        # the gate and let the venue enforce margin, instead of rejecting
        # with a false "$0.00 USDT".
        cached_bal = creds.get("_cached_balance_usdt")
        if cached_bal is not None:
            bal = float(cached_bal)
        else:
            bal = None
            try:
                bd = await cls.fetch_balance(creds)
                by_cur = bd.get("by_currency") or {}
                if by_cur and all(r.get("ok") for r in by_cur.values()):
                    bal = bd.get("usdt", 0)
            except Exception as e:
                logger.warning("KuCoin preflight balance read failed for %s — skipping margin gate: %s", sym, e)

        mark_price = 0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v1/ticker?symbol={sym}")
                mark_price = float((r.json().get("data") or {}).get("price") or 0)
        except Exception:
            pass
        if bal is not None and mark_price and leverage > 0:
            notional = qty_lots * multiplier * mark_price
            required = notional / max(1, leverage)
            if bal + 0.01 < required:
                # KuCoin separates Main/Spot and Futures balances — funds in
                # Main don't count toward futures margin. Most "insufficient
                # margin" reports trace to a user who has cash but hasn't
                # transferred it to the Futures account. Spell that out.
                return {"ok": False, "reason": (
                    f"KuCoin Futures account has ${bal:.2f} USDT, need ~${required:.2f}. "
                    f"If you have funds in your Main/Spot account, transfer them "
                    f"to Futures on KuCoin (Assets → Transfer)."
                )}

        return {"ok": True, "qty_lots": qty_lots, "multiplier": multiplier, "lot_size": lot_size}

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym) or {}
        multiplier = info.get("multiplier", 1)
        lot_size = info.get("lotSize", 1)
        max_lev = int(info.get("maxLeverage") or 100)
        qty_lots = int(quantity / multiplier) if multiplier else int(quantity)
        qty_lots = (qty_lots // lot_size) * lot_size
        if qty_lots <= 0:
            raise RuntimeError(f"Quantity below minimum for {sym}")
        # Clamp to the per-symbol max. Set a safe default if caller passed 0/neg.
        lev = max(1, min(int(leverage or 1), max_lev))
        # KuCoin requires a unique clientOid per order — without it some
        # accounts return "200002 Parameter error" (vague). Generate a UUID.
        import uuid as _uuid
        body = {
            "clientOid": str(_uuid.uuid4()),
            "symbol": sym,
            "side": "buy" if side == "buy" else "sell",
            "type": "market",
            "size": qty_lots,
            "leverage": lev,
            # KuCoin Futures: marginMode "ISOLATED" | "CROSS" — without this the
            # server may default to whatever the account has cached.
            "marginMode": "ISOLATED" if margin_mode == "isolated" else "CROSS",
        }
        try:
            data = await cls._signed(creds, "POST", "/api/v1/orders", body=body)
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_kc(code, msg))
        return {"order_id": str((data or {}).get("orderId", "")), "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        reduce_side = "sell" if p["side"] == "buy" else "buy"
        import uuid as _uuid
        try:
            data = await cls._signed(creds, "POST", "/api/v1/orders", body={
                "clientOid": str(_uuid.uuid4()),
                "symbol": sym,
                "side": reduce_side,
                "type": "market",
                "closeOrder": True,
                "size": 1,  # closeOrder ignores size, closes entire position
            })
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_kc(code, msg))
        return {
            "order_id": str((data or {}).get("orderId", "")),
            "closed_qty": p["quantity"],
            "realized_pnl_usd": p.get("unrealized_pnl_usd", 0),
        }

    @classmethod
    async def _funding_pnl(cls, creds: dict, api_symbol: str, since_ms: int) -> float | None:
        """Sum `funding` field from /api/v1/funding-history since `since_ms`.
        Positive = received, negative = paid. Returns None on any failure
        so the UI falls back to an em-dash rather than misleading zero."""
        try:
            data = await cls._signed(creds, "GET", "/api/v1/funding-history", {
                "symbol": api_symbol,
                "from": since_ms,
                "maxCount": 200,
            })
            # KuCoin returns {dataList: [{funding, fundingRate, timestamp, ...}]}
            items = (data or {}).get("dataList") if isinstance(data, dict) else data
            return sum(float(x.get("funding") or 0) for x in (items or []))
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        import time as _t
        params = {}
        if symbol:
            params["symbol"] = cls._symbol(symbol)
        data = await cls._signed(creds, "GET", "/api/v1/position" + ("s" if not symbol else ""), params or None)
        items = [data] if isinstance(data, dict) else (data or [])
        # Gather funding history + instrument info per position concurrently.
        pending = []
        for p in items:
            raw_qty = int(p.get("currentQty") or 0)
            if raw_qty == 0:
                continue
            pending.append(p)
        if not pending:
            return []
        # 7-day window for accumulated funding. See binance adapter note.
        since_ms = int((_t.time() - 7 * 86400) * 1000)
        from backend.services.trade_adapters._funding_cache import cached_funding
        api_key = (creds.get("api_key") or "").strip()
        infos, fundings = await asyncio.gather(
            asyncio.gather(*[_instrument_info(p.get("symbol") or cls._symbol(str(p.get("symbol", "")).replace("USDTM", "")))
                             for p in pending], return_exceptions=True),
            asyncio.gather(*[cached_funding(api_key, p.get("symbol") or "",
                                            lambda p=p: cls._funding_pnl(creds, p.get("symbol"), since_ms))
                             for p in pending], return_exceptions=True),
        )
        out = []
        for p, info, funding in zip(pending, infos, fundings):
            raw_qty = int(p.get("currentQty") or 0)
            base_sym = str(p.get("symbol", "")).replace("USDTM", "")
            if base_sym == "XBT":
                base_sym = "BTC"
            multiplier = float((info or {}).get("multiplier") or 1) if not isinstance(info, Exception) else 1.0
            # KuCoin: crossMode flag (true=cross, false=isolated). Some
            # responses use marginMode string instead.
            mm_raw = (p.get("marginMode") or "")
            if mm_raw:
                margin_mode = "isolated" if str(mm_raw).lower().startswith("iso") else "cross"
            elif "crossMode" in p:
                margin_mode = "cross" if bool(p.get("crossMode")) else "isolated"
            else:
                margin_mode = None
            out.append({
                "exchange": "kucoin",
                "symbol": base_sym,
                "side": "buy" if raw_qty > 0 else "sell",
                "quantity": abs(raw_qty) * multiplier,
                "entry_price": float(p.get("avgEntryPrice") or 0),
                "mark_price": float(p.get("markPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealisedPnl") or 0),
                "funding_pnl_usd": funding if isinstance(funding, (int, float)) else None,
                "leverage": int(float(p.get("realLeverage") or p.get("leverage") or 1)),
                "margin_mode": margin_mode,
                "position_id": str(p.get("id", "")),
            })
        return out

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        if not creds.get("api_passphrase"):
            out["error"] = "KuCoin requires a passphrase"
            return out
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "400001" in msg:
                out["error"] = "Invalid API key"
            elif "400002" in msg:
                out["error"] = "Signature mismatch — check API secret and passphrase"
            elif "400005" in msg:
                out["error"] = "Key permissions insufficient"
            else:
                out["error"] = f"KuCoin rejected the key: {msg[:180]}"
            return out
        if need_trade:
            out["can_trade"] = True  # if balance read works, futures key is valid
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        info = await _instrument_info(cls._symbol(symbol))
        if info:
            return info.get("maxLeverage", 100)
        return 100

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts, *,
                                 market: str = "futures") -> list[dict]:
        """KuCoin futures fills + funding since `since_ts`, or spot fills
        if market='spot' (different host: api.kucoin.com).

        Futures: /api/v1/fills?startAt=<ms>&pageSize=200 + /api/v1/funding-history
        Spot:    /api/v1/fills?startAt=<ms>&pageSize=500 on api.kucoin.com
                 (same signing scheme, different host)."""
        from datetime import datetime as _dt
        if market == "spot":
            return await cls._fetch_spot_fills(creds, since_ts)
        if market != "futures":
            return []
        start_ms = int(since_ts.timestamp() * 1000)
        out: list[dict] = []
        page = 1
        for _ in range(20):
            try:
                data = await cls._signed(creds, "GET", "/api/v1/fills", {
                    "startAt": start_ms, "pageSize": 200, "currentPage": page,
                }) or {}
            except Exception as exc:
                logger.info("kucoin fills page=%s failed: %s", page, exc)
                break
            # _signed returns j["data"], so for KuCoin paginated endpoints
            # this is already {currentPage, pageSize, items, totalPage}.
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                rows = []
            if not rows:
                break
            for r in rows:
                try:
                    sym_raw = str(r.get("symbol") or "")
                    sym = sym_raw.replace("XBTUSDTM", "BTC").replace("USDTM", "")
                    side = "buy" if str(r.get("side") or "").lower() == "buy" else "sell"
                    qty = float(r.get("size") or 0)
                    if qty <= 0:
                        continue
                    ts_raw = r.get("tradeTime") or r.get("createdAt")
                    ts_v = float(ts_raw) if ts_raw else 0
                    if ts_v <= 0:
                        continue
                    # tradeTime is nanoseconds, createdAt is ms.
                    ts = ts_v / 1e9 if ts_v > 1e15 else ts_v / 1000.0
                    out.append({
                        "symbol": sym.upper(),
                        "side": side,
                        "qty": qty,
                        "price": float(r.get("price") or 0),
                        "fee_usd": float(r.get("fee") or 0),
                        "realized_pnl_usd": None,
                        "ts": _dt.utcfromtimestamp(ts),
                        "ext_trade_id": str(r.get("tradeId") or r.get("id") or ""),
                        "ext_order_id": str(r.get("orderId") or "") or None,
                        "kind": "trade",
                    })
                except Exception:
                    continue
            total_pages = (data.get("totalPage") or 1)
            page += 1
            if page > int(total_pages):
                break
        try:
            f_data = await cls._signed(creds, "GET", "/api/v1/funding-history", {
                "startAt": start_ms,
            }) or {}
            f_rows = f_data.get("dataList") or f_data.get("data") or []
            if isinstance(f_rows, dict):
                f_rows = f_rows.get("dataList") or []
            for r in f_rows:
                try:
                    sym_raw = str(r.get("symbol") or "")
                    sym = sym_raw.replace("XBTUSDTM", "BTC").replace("USDTM", "")
                    ts_v = float(r.get("timePoint") or 0)
                    if ts_v <= 0:
                        continue
                    out.append({
                        "symbol": sym.upper(),
                        "side": None,
                        "qty": 0.0, "price": 0.0, "fee_usd": None,
                        "realized_pnl_usd": float(r.get("funding") or 0),
                        "ts": _dt.utcfromtimestamp(ts_v / 1000),
                        "ext_trade_id": str(r.get("id")
                                            or f"funding-{int(ts_v)}-{sym}"),
                        "ext_order_id": None,
                        "kind": "funding",
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return out

    @classmethod
    async def _fetch_spot_fills(cls, creds: dict, since_ts) -> list[dict]:
        """KuCoin Spot fills via api.kucoin.com /api/v1/fills (different host
        from futures' api-futures.kucoin.com). Same signing scheme — pass
        the spot host override into _signed."""
        from datetime import datetime as _dt
        start_ms = int(since_ts.timestamp() * 1000)
        end_ms = int(_dt.utcnow().timestamp() * 1000)
        out: list[dict] = []
        page = 1
        for _ in range(10):
            try:
                data = await cls._signed(creds, "GET", "/api/v1/fills",
                                         {"startAt": start_ms, "endAt": end_ms,
                                          "pageSize": 500, "currentPage": page},
                                         host="https://api.kucoin.com") or {}
            except Exception as exc:
                logger.info("kucoin spot fills page=%s failed: %s", page, exc)
                break
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                try:
                    sym_raw = str(r.get("symbol") or "")  # e.g. SOL-USDT
                    if not sym_raw.endswith("-USDT"):
                        continue  # only USDT pairs (matches our short-leg convention)
                    sym = sym_raw.replace("-USDT", "")
                    side = "buy" if str(r.get("side") or "").lower() == "buy" else "sell"
                    qty = float(r.get("size") or 0)
                    if qty <= 0:
                        continue
                    ts_v = float(r.get("createdAt") or 0)
                    if ts_v <= 0:
                        continue
                    out.append({
                        "symbol": sym.upper(),
                        "side": side,
                        "qty": qty,
                        "price": float(r.get("price") or 0),
                        "fee_usd": float(r.get("fee") or 0),
                        "realized_pnl_usd": None,
                        "ts": _dt.utcfromtimestamp(ts_v / 1000.0),
                        "ext_trade_id": str(r.get("tradeId") or r.get("id") or ""),
                        "ext_order_id": str(r.get("orderId") or "") or None,
                        "kind": "trade",
                    })
                except Exception:
                    continue
            total_pages = int(data.get("totalPage") or 1)
            page += 1
            if page > total_pages:
                break
        return out
