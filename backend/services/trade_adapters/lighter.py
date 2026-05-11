"""Lighter zk-perp trade adapter — official lighter-sdk + ZK signing.

Lighter uses ZK-rollup signing — accounts are identified by an integer
`account_index` and trade authority is delegated to one or more API keys
(each indexed 0–255). The `lighter-sdk` package wraps the native signer
into Python via a CGO library shipped per-platform under `lighter/signers/`.

Credentials mapping (so we can reuse the existing api_key/api_secret/
api_passphrase schema without new columns):
    api_key        → account_index   ("12345" — numeric string)
    api_secret     → api_private_key (hex, with 0x prefix)
    api_passphrase → api_key_index   (default "255")

Symbol convention: pure base ticker (e.g. "BTC", "ETH"). The SDK uses
integer market_index — we resolve via /api/v1/orderBookDetails and cache
the symbol → (market_id, size_decimals, price_decimals) tuple for 1h.

Market-order execution uses `create_market_order(market_index,
client_order_index, base_amount_int, avg_execution_price_int, is_ask)`.
The avg_execution_price is a slippage cap; we use last_trade_price ± 5%.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("avalant.trade.lighter")

BASE_URL = "https://mainnet.zklighter.elliot.ai"
DEFAULT_API_KEY_INDEX = 255

# market metadata cache: symbol → (market_id, size_dec, price_dec, last_trade_px, ts_mono)
_meta_cache: dict[str, tuple[int, int, int, float, float]] = {}
_META_TTL_S = 3600.0
_meta_lock = asyncio.Lock()


def _resolve_account_index_from_address(addr: str) -> str | None:
    """Look up Lighter account_index via /api/v1/account?by=l1_address.
    Lighter's API-keys popup only shows the keypair + index; the numeric
    account_index has to be derived from the L1 wallet address."""
    import httpx
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(f"{BASE_URL}/api/v1/account",
                      params={"by": "l1_address", "value": addr.strip()})
        if r.status_code != 200:
            return None
        accs = (r.json() or {}).get("accounts") or []
        if not accs:
            return None
        idx = accs[0].get("index")
        return str(idx) if idx is not None else None
    except Exception:
        return None


def _creds_to_signer_args(creds: dict) -> tuple[int, int, str]:
    """Pull (account_index, api_key_index, api_private_key) out of creds.
    If api_key (account_index) is empty but `address` is set, auto-derive
    the index from Lighter's public account-lookup endpoint."""
    raw_acc = (creds.get("api_key") or "").strip()
    raw_key = (creds.get("api_secret") or "").strip()
    raw_idx = (creds.get("api_passphrase") or "").strip()
    if not raw_acc:
        addr = (creds.get("address") or creds.get("wallet") or "").strip()
        if addr:
            resolved = _resolve_account_index_from_address(addr)
            if resolved:
                raw_acc = resolved
    if not raw_acc or not raw_key:
        raise RuntimeError("Lighter requires account_index (api_key) and api_private_key (api_secret)")
    try:
        account_index = int(raw_acc)
    except ValueError:
        raise RuntimeError("Lighter account_index must be an integer")
    try:
        api_key_index = int(raw_idx) if raw_idx else DEFAULT_API_KEY_INDEX
    except ValueError:
        api_key_index = DEFAULT_API_KEY_INDEX
    pk = raw_key if raw_key.startswith("0x") else "0x" + raw_key
    return account_index, api_key_index, pk


async def _build_signer(creds: dict):
    from lighter import SignerClient
    account_index, api_key_index, pk = _creds_to_signer_args(creds)
    sc = SignerClient(
        url=BASE_URL,
        account_index=account_index,
        api_private_keys={api_key_index: pk},
    )
    return sc, api_key_index


async def _resolve_market(symbol: str) -> tuple[int, int, int, float]:
    """Return (market_id, size_decimals, price_decimals, last_trade_price)."""
    sym = (symbol or "").upper()
    cached = _meta_cache.get(sym)
    if cached and (time.monotonic() - cached[4]) < _META_TTL_S:
        return cached[:4]
    async with _meta_lock:
        cached = _meta_cache.get(sym)
        if cached and (time.monotonic() - cached[4]) < _META_TTL_S:
            return cached[:4]
        from lighter import OrderApi, ApiClient, Configuration
        cfg = Configuration(host=BASE_URL)
        ac = ApiClient(cfg)
        try:
            api = OrderApi(ac)
            books = await api.order_books()
            target_id: int | None = None
            for b in (books.order_books or []):
                if (b.market_type or "").lower() != "perp":
                    continue
                if (b.symbol or "").upper() == sym:
                    target_id = int(b.market_id)
                    break
            if target_id is None:
                raise RuntimeError(f"Lighter: no perp market for {sym}")
            details = await api.order_book_details(market_id=target_id)
            d = details.order_book_details[0]
            size_dec = int(d.size_decimals)
            price_dec = int(d.price_decimals)
            last_px = float(d.last_trade_price or 0)
            _meta_cache[sym] = (target_id, size_dec, price_dec, last_px, time.monotonic())
            return target_id, size_dec, price_dec, last_px
        finally:
            await ac.close()


def _client_order_idx() -> int:
    """Lighter client_order_index must be unique per account; use ms epoch."""
    return int(time.time() * 1000)


class LighterAdapter:
    EXCHANGE = "lighter"
    DISPLAY = "Lighter"

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        # PUBLIC read — no signing required. We only need account_index
        # (or an L1 address from which to derive it). Don't go through
        # _creds_to_signer_args which insists on api_secret too.
        from lighter import AccountApi, ApiClient, Configuration
        raw_acc = (creds.get("api_key") or "").strip()
        addr = (creds.get("address") or creds.get("wallet") or "").strip()
        if not raw_acc and addr:
            resolved = _resolve_account_index_from_address(addr)
            if resolved:
                raw_acc = resolved
        if not raw_acc:
            return {"usdt": 0.0, "spot_usd": 0.0, "futures_usd": 0.0}
        try:
            account_index = int(raw_acc)
        except ValueError:
            return {"usdt": 0.0, "spot_usd": 0.0, "futures_usd": 0.0}
        cfg = Configuration(host=BASE_URL)
        ac = ApiClient(cfg)
        try:
            api = AccountApi(ac)
            res = await api.account(by="index", value=str(account_index))
            acc = (res.accounts or [None])[0]
            if acc is None:
                return {"usdt": 0.0, "spot_usd": 0.0, "futures_usd": 0.0}
            # Lighter reports the user's total USD collateral at the account
            # level (`collateral` field). The per-asset `assets[].balance`
            # is a position-token quantity, NOT USD — summing it gave $0.01
            # while the real collateral was $40.
            #
            # account_type=1 is the perp sub-account (collateral is futures
            # margin); account_type=2 is the spot sub-account. We surface
            # whichever this index represents in the matching field.
            collateral = 0.0
            c = getattr(acc, "collateral", None)
            if c is not None:
                try:
                    collateral = float(c)
                except (TypeError, ValueError):
                    pass
            if collateral == 0.0:
                # Fallback to per-asset USDC/USDT sum if `collateral` is missing.
                for a in (acc.assets or []):
                    sym = (a.symbol or "").upper()
                    if sym in ("USDC", "USDT"):
                        try:
                            collateral += float(a.balance or 0) + float(a.locked_balance or 0)
                        except (TypeError, ValueError):
                            pass
            acct_type = getattr(acc, "account_type", None)
            is_spot = str(acct_type).lower() == "spot" or acct_type == 2
            if is_spot:
                return {"usdt": collateral, "spot_usd": collateral, "futures_usd": 0.0}
            return {"usdt": collateral, "spot_usd": 0.0, "futures_usd": collateral}
        finally:
            await ac.close()

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}

        def _friendly(e, fallback: str) -> str:
            msg = str(e)
            ml = msg.lower()
            if "account_index" in ml and ("integer" in ml or "invalid" in ml):
                return "account_index must be a positive integer (the numeric Lighter account ID)"
            if "api_private_key" in ml or "missing api_secret" in ml:
                return "Missing api_private_key — paste the hex-encoded ZK signing key (with or without 0x)"
            if "account not found" in ml or "21100" in ml:
                return "Lighter account not found — double-check the account_index"
            if "signature" in ml or "verify" in ml:
                return "Signing failed — api_private_key doesn't match this account_index"
            if "401" in ml or "403" in ml or "unauthorized" in ml:
                return "Lighter rejected the key — verify it's an active API key for this account"
            return f"{fallback}: {msg[:140]}"

        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            out["error"] = _friendly(e, "Lighter rejected the key")
            return out
        if need_trade:
            sc = None
            try:
                sc, _ = await _build_signer(creds)
                # SDK version drift: check_client() may return a str directly
                # in newer releases, or a coroutine in older ones. Handle both.
                import inspect
                raw = sc.check_client()
                err = await raw if inspect.iscoroutine(raw) else raw
                if err:
                    out["error"] = _friendly(err, "Lighter signer check failed")
                else:
                    out["can_trade"] = True
            except Exception as e:
                out["error"] = _friendly(e, "Lighter signer init failed")
            finally:
                if sc is not None:
                    try:
                        # close() may also be sync depending on SDK version
                        import inspect as _ins
                        rc = sc.close()
                        if _ins.iscoroutine(rc):
                            await rc
                    except Exception:
                        pass
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        return 25  # Lighter generally supports up to 25× on majors; venue-side capped per market

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str,
                           leverage: int, margin_mode: str) -> None:
        sc, api_key_index = await _build_signer(creds)
        market_id, _, _, _ = await _resolve_market(symbol)
        try:
            mode = (
                sc.ISOLATED_MARGIN_MODE if (margin_mode or "").lower().startswith("iso")
                else sc.CROSS_MARGIN_MODE
            )
            tx_or_err = await sc.update_leverage(
                market_index=market_id,
                fraction=int(max(1, leverage)),
                margin_mode=mode,
                api_key_index=api_key_index,
            )
            # SDK returns (tx, resp, err) tuple — surface err if present
            err = tx_or_err[-1] if isinstance(tx_or_err, tuple) and len(tx_or_err) >= 1 else None
            if err:
                raise RuntimeError(f"Lighter update_leverage: {err}")
        finally:
            try:
                await sc.close()
            except Exception:
                pass

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sc, api_key_index = await _build_signer(creds)
        try:
            market_id, size_dec, price_dec, last_px = await _resolve_market(symbol)
            is_ask = (side or "").lower() in ("sell", "short", "ask")
            base_amount = int(round(float(quantity) * (10 ** size_dec)))
            if base_amount <= 0:
                raise RuntimeError(f"Lighter: quantity {quantity} rounds to 0 at {size_dec} decimals")
            # Slippage cap: ±5% on last_trade_price. Buyer accepts up to last*1.05,
            # seller accepts down to last*0.95.
            if last_px <= 0:
                raise RuntimeError(f"Lighter: no last_trade_price for {symbol}")
            slip = 0.95 if is_ask else 1.05
            avg_px = int(round(last_px * slip * (10 ** price_dec)))
            tx, resp, err = await sc.create_market_order(
                market_index=market_id,
                client_order_index=_client_order_idx(),
                base_amount=base_amount,
                avg_execution_price=avg_px,
                is_ask=is_ask,
                api_key_index=api_key_index,
            )
            if err:
                raise RuntimeError(f"Lighter place_order: {err}")
            tx_hash = getattr(resp, "tx_hash", None) or getattr(resp, "code", None)
            return {"order_id": str(tx_hash or ""), "avg_price": last_px}
        finally:
            try:
                await sc.close()
            except Exception:
                pass

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        positions = await cls.list_positions(creds, symbol=symbol)
        match = next((p for p in positions if (p.get("symbol") or "").upper() == symbol.upper()), None)
        if not match:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        qty = abs(float(match.get("quantity") or 0))
        if qty <= 0:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        # opposite side closes
        opposite = "sell" if (match.get("side") or "").lower() == "buy" else "buy"
        sc, api_key_index = await _build_signer(creds)
        try:
            market_id, size_dec, price_dec, last_px = await _resolve_market(symbol)
            base_amount = int(round(qty * (10 ** size_dec)))
            slip = 0.95 if opposite == "sell" else 1.05
            avg_px = int(round(last_px * slip * (10 ** price_dec)))
            tx, resp, err = await sc.create_market_order(
                market_index=market_id,
                client_order_index=_client_order_idx(),
                base_amount=base_amount,
                avg_execution_price=avg_px,
                is_ask=(opposite == "sell"),
                reduce_only=True,
                api_key_index=api_key_index,
            )
            if err:
                raise RuntimeError(f"Lighter close_position: {err}")
            tx_hash = getattr(resp, "tx_hash", None) or getattr(resp, "code", None)
            return {
                "order_id": str(tx_hash or ""),
                "closed_qty": qty,
                "realized_pnl_usd": float(match.get("unrealized_pnl_usd") or 0),
            }
        finally:
            try:
                await sc.close()
            except Exception:
                pass

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        from lighter import AccountApi, ApiClient, Configuration
        account_index, _, _ = _creds_to_signer_args(creds)
        cfg = Configuration(host=BASE_URL)
        ac = ApiClient(cfg)
        try:
            api = AccountApi(ac)
            res = await api.account(by="index", value=str(account_index))
            acc = (res.accounts or [None])[0]
            if acc is None:
                return []
            out: list[dict] = []
            for p in (acc.positions or []):
                sym = (p.symbol or "").upper()
                if symbol and sym != symbol.upper():
                    continue
                try:
                    qty = float(p.position or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                if qty == 0:
                    continue
                side = "buy" if (p.sign == 1 or qty > 0) else "sell"
                try:
                    entry = float(p.avg_entry_price or 0)
                except (TypeError, ValueError):
                    entry = 0.0
                try:
                    upnl = float(p.unrealized_pnl or 0)
                except (TypeError, ValueError):
                    upnl = 0.0
                try:
                    funding = float(getattr(p, "realized_funding", None) or 0)
                except (TypeError, ValueError):
                    funding = 0.0
                out.append({
                    "exchange": "lighter",
                    "symbol": sym,
                    "side": side,
                    "quantity": abs(qty),
                    "entry_price": entry,
                    "unrealized_pnl_usd": upnl,
                    "funding_pnl_usd": funding,
                    "leverage": int(p.allocated_margin or 0) or None,
                    "margin_mode": "cross",
                })
            return out
        finally:
            await ac.close()

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        try:
            _mid, size_dec, _, _ = await _resolve_market(symbol)
        except Exception:
            return None
        step = 10 ** (-int(size_dec)) if size_dec > 0 else None
        return {
            "min_qty": step or 0,   # one lot minimum
            "step":    step,
            "max_qty": None,
            "unit": "coin",
        }

    @classmethod
    async def preflight(cls, creds, symbol, quantity, leverage):
        try:
            mid, size_dec, _, _ = await _resolve_market(symbol)
            base_amount = int(round(float(quantity) * (10 ** size_dec)))
            if base_amount <= 0:
                return {"ok": False, "reason": f"qty {quantity} below {size_dec}-dec lot size"}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:180]}

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts: float, *, market: str = "futures") -> list[dict]:
        """Pull fills from Lighter /api/v1/fills — unsigned read endpoint."""
        import httpx
        account_index, _, _ = _creds_to_signer_args(creds)
        params = {
            "accountIndex": str(account_index),
            "limit": "200",
            "startTime": str(int(since_ts * 1000)),  # ms
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{BASE_URL}/api/v1/fills", params=params)
            if r.status_code != 200:
                return []
            data = r.json()
        except Exception:
            return []

        fills = data if isinstance(data, list) else data.get("fills") or []
        out: list[dict] = []
        for f in fills:
            try:
                ts = float(f.get("createdAt") or f.get("timestamp") or 0) / 1000
                qty = float(f.get("baseAmount") or f.get("quantity") or 0)
                px = float(f.get("price") or 0)
                sym = (f.get("symbol") or f.get("market") or "").upper()
                if sym.endswith("-USDC") or sym.endswith("-USDT"):
                    sym = sym.rsplit("-", 1)[0]
                side = "sell" if f.get("isAsk") else "buy"
                ext_id = str(f.get("orderId") or f.get("id") or "")
                fee = float(f.get("fee") or 0)
                pnl = float(f.get("realizedPnl") or 0)
                if qty <= 0 or px <= 0 or ts <= 0:
                    continue
                out.append({
                    "ts": ts,
                    "symbol": sym,
                    "side": side,
                    "quantity": qty,
                    "price": px,
                    "fee_usd": fee,
                    "realized_pnl_usd": pnl,
                    "ext_trade_id": ext_id,
                    "kind": "trade",
                    "market": "futures",
                })
            except (TypeError, ValueError, KeyError):
                continue
        return out
