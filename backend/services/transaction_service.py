"""Fetch last 5 transactions from providers for a single wallet."""
import base64
import time
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from backend.providers.http import RetryClient

from backend.db.models import Wallet
from backend.schemas.portfolio import Transaction, TransactionResponse

LIMIT = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(ts) -> str:
    """Convert a unix timestamp (ms or s) or ISO string to ISO-8601 string."""
    if ts is None:
        return ""
    if isinstance(ts, str):
        # already ISO or close enough
        if ts.isdigit() or (ts.replace(".", "", 1).isdigit()):
            ts = float(ts)
        else:
            return ts[:19]
    ts = float(ts)
    # heuristic: if > 1e12 it's milliseconds
    if ts > 1e12:
        ts = ts / 1000
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _tx(tx_id: str, ttype: str, asset: str, amount, ts, status: str = "completed",
        address: str | None = None, network: str | None = None) -> Transaction:
    return Transaction(
        tx_id=str(tx_id or ""),
        type=ttype,
        asset=str(asset or ""),
        amount=str(amount or "0"),
        timestamp=_iso(ts),
        status=status,
        address=address or None,
        network=network or None,
    )


# ---------------------------------------------------------------------------
# Exchange fetchers
# ---------------------------------------------------------------------------

async def _binance_txs(creds: dict) -> list[Transaction]:
    from backend.providers.exchanges._signing import hex_hmac_sha256, ms

    spot_base    = "https://api.binance.com"
    futures_base = "https://fapi.binance.com"
    headers = {"X-MBX-APIKEY": creds["api_key"]}

    async def signed_get(base: str, path: str, params: dict) -> list | dict:
        p = dict(params)
        p["timestamp"] = int(ms())
        p.setdefault("recvWindow", 5000)
        qs = urlencode(p, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)
        url = f"{base}{path}?{qs}&signature={sig}"
        async with RetryClient(timeout=15) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            return r.json()

    txs: list[Transaction] = []

    # 1. Spot deposits
    try:
        data = await signed_get(spot_base, "/sapi/v1/capital/deposit/hisrec", {"limit": LIMIT})
        deposits = data if isinstance(data, list) else []
        for d in deposits[:LIMIT]:
            txs.append(_tx(d.get("id") or d.get("txId", ""), "deposit",
                           d.get("coin", ""), d.get("amount", "0"),
                           d.get("insertTime"), _deposit_status(d.get("status", 1)),
                           address=d.get("address"), network=d.get("network")))
    except Exception:
        pass

    # 2. Spot withdrawals
    try:
        data = await signed_get(spot_base, "/sapi/v1/capital/withdraw/history", {"limit": LIMIT})
        withdrawals = data if isinstance(data, list) else []
        for w in withdrawals[:LIMIT]:
            txs.append(_tx(w.get("id", ""), "withdraw",
                           w.get("coin", ""), w.get("amount", "0"),
                           w.get("applyTime"), _withdraw_status(w.get("status", 6)),
                           address=w.get("address"), network=w.get("network")))
    except Exception:
        pass

    # 3. Futures income (funding fees, realized PnL, etc.)
    try:
        data = await signed_get(futures_base, "/fapi/v1/income", {"limit": LIMIT})
        income = data if isinstance(data, list) else []
        for item in income[:LIMIT]:
            income_type = item.get("incomeType", "TRANSFER")
            ttype = "trade" if income_type in ("REALIZED_PNL", "COMMISSION") else "transfer"
            amount = item.get("income", "0")
            if float(amount) == 0:
                continue
            txs.append(_tx(
                item.get("tranId", item.get("time", "")),
                ttype,
                item.get("asset", "USDT"),
                amount,
                item.get("time"),
                "completed",
            ))
    except Exception:
        pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


def _deposit_status(code) -> str:
    mapping = {0: "pending", 1: "completed", 6: "pending", 7: "completed"}
    return mapping.get(int(code or 1), "completed")


def _withdraw_status(code) -> str:
    mapping = {0: "pending", 1: "pending", 2: "failed", 3: "pending",
               4: "pending", 5: "failed", 6: "completed", 7: "pending"}
    return mapping.get(int(code or 6), "completed")


async def _okx_txs(creds: dict) -> list[Transaction]:
    from backend.providers.exchanges._signing import b64_hmac_sha256

    base = "https://www.okx.com"

    async with RetryClient(timeout=15) as c:
        r = await c.get(f"{base}/api/v5/public/time")
        ts_ms = int(r.json()["data"][0]["ts"])
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        ts_iso = dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        def _headers(path_with_qs: str) -> dict:
            prehash = f"{ts_iso}GET{path_with_qs}"
            sign = b64_hmac_sha256(creds["api_secret"], prehash)
            return {
                "OK-ACCESS-KEY": creds["api_key"],
                "OK-ACCESS-SIGN": sign,
                "OK-ACCESS-TIMESTAMP": ts_iso,
                "OK-ACCESS-PASSPHRASE": creds["api_passphrase"],
                "Content-Type": "application/json",
            }

        txs: list[Transaction] = []

        # 1. Asset deposits
        try:
            path = f"/api/v5/asset/deposit-history?limit={LIMIT}"
            r = await c.get(f"{base}{path}", headers=_headers(path))
            data = r.json()
            for d in (data.get("data") or [])[:LIMIT]:
                txs.append(_tx(d.get("depId", d.get("txId", "")), "deposit",
                               d.get("ccy", ""), d.get("amt", "0"),
                               d.get("ts"), "completed" if str(d.get("state")) == "2" else "pending",
                               address=d.get("to"), network=d.get("chain")))
        except Exception:
            pass

        # 2. Asset withdrawals
        try:
            path = f"/api/v5/asset/withdrawal-history?limit={LIMIT}"
            r = await c.get(f"{base}{path}", headers=_headers(path))
            data = r.json()
            for w in (data.get("data") or [])[:LIMIT]:
                txs.append(_tx(w.get("wdId", w.get("txId", "")), "withdraw",
                               w.get("ccy", ""), w.get("amt", "0"),
                               w.get("ts"), "completed" if str(w.get("state")) == "2" else "pending",
                               address=w.get("to"), network=w.get("chain")))
        except Exception:
            pass

        # 3. Recent trade fills
        if len(txs) < LIMIT:
            try:
                path = f"/api/v5/trade/fills?limit={LIMIT}"
                r = await c.get(f"{base}{path}", headers=_headers(path))
                data = r.json()
                for f_ in (data.get("data") or [])[:LIMIT - len(txs)]:
                    side = f_.get("side", "buy")
                    asset = f_.get("instId", "").split("-")[0]
                    txs.append(_tx(f_.get("tradeId", f_.get("billId", "")), "trade",
                                   asset, f_.get("fillSz", "0"), f_.get("ts")))
            except Exception:
                pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


async def _bybit_txs(creds: dict) -> list[Transaction]:
    from backend.providers.exchanges._signing import hex_hmac_sha256, ms

    base = "https://api.bybit.com"
    recv_window = "5000"

    def bybit_headers(qs: str) -> dict:
        ts = ms()
        sign = hex_hmac_sha256(creds["api_secret"], f"{ts}{creds['api_key']}{recv_window}{qs}")
        return {
            "X-BAPI-API-KEY": creds["api_key"],
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

    txs: list[Transaction] = []
    async with RetryClient(timeout=15) as c:
        # 1. Deposits
        try:
            qs = urlencode({"limit": LIMIT})
            r = await c.get(f"{base}/v5/asset/deposit/query-record?{qs}", headers=bybit_headers(qs))
            r.raise_for_status()
            for d in (r.json().get("result", {}).get("rows") or [])[:LIMIT]:
                status = "completed" if str(d.get("status")) == "3" else "pending"
                txs.append(_tx(d.get("txIndex", d.get("id", "")), "deposit",
                               d.get("coin", ""), d.get("amount", "0"),
                               d.get("successAt") or d.get("createTime"), status,
                               network=d.get("chain")))
        except Exception:
            pass

        # 2. Withdrawals
        try:
            qs = urlencode({"limit": LIMIT})
            r = await c.get(f"{base}/v5/asset/withdraw/query-record?{qs}", headers=bybit_headers(qs))
            r.raise_for_status()
            for w in (r.json().get("result", {}).get("rows") or [])[:LIMIT]:
                status = "completed" if w.get("status") == "success" else "pending"
                txs.append(_tx(w.get("withdrawId", w.get("id", "")), "withdraw",
                               w.get("coin", ""), w.get("amount", "0"),
                               w.get("updateTime") or w.get("createTime"), status,
                               network=w.get("chain")))
        except Exception:
            pass

        # 3. Transaction log as fallback (trading/transfer activity)
        if len(txs) < LIMIT:
            try:
                qs = urlencode({"limit": LIMIT})
                r = await c.get(f"{base}/v5/account/transaction-log?{qs}", headers=bybit_headers(qs))
                r.raise_for_status()
                for row in (r.json().get("result", {}).get("list") or [])[:LIMIT - len(txs)]:
                    ttype = _bybit_type(row.get("type", ""))
                    txs.append(_tx(row.get("id", ""), ttype,
                                   row.get("coin", ""), row.get("amount", "0"),
                                   row.get("transactionTime")))
            except Exception:
                pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


def _bybit_type(t: str) -> str:
    t = t.upper()
    if "DEPOSIT" in t: return "deposit"
    if "WITHDRAW" in t: return "withdraw"
    if "TRADE" in t or "FILL" in t: return "trade"
    return "transfer"


async def _gate_txs(creds: dict) -> list[Transaction]:
    from backend.providers.exchanges._signing import s, sha512_hex, hex_hmac_sha512

    base = "https://api.gateio.ws"

    def gate_headers(method: str, url_path: str, query: str = "") -> dict:
        ts = s()
        payload_hash = sha512_hex("")
        sign_str = "\n".join([method.upper(), url_path, query, payload_hash, ts])
        sign = hex_hmac_sha512(creds["api_secret"], sign_str)
        return {"KEY": creds["api_key"], "SIGN": sign, "Timestamp": ts}

    txs: list[Transaction] = []
    async with RetryClient(timeout=15) as c:
        # 1. Deposits
        try:
            path = "/api/v4/wallet/deposits"
            qs = f"limit={LIMIT}"
            r = await c.get(f"{base}{path}?{qs}", headers=gate_headers("GET", path, qs))
            r.raise_for_status()
            for d in (r.json() if isinstance(r.json(), list) else [])[:LIMIT]:
                status = "completed" if str(d.get("status", "")).lower() in ("done", "finish") else "pending"
                txs.append(_tx(d.get("txid", d.get("id", "")), "deposit",
                               d.get("currency", ""), d.get("amount", "0"),
                               d.get("timestamp"), status, network=d.get("chain")))
        except Exception:
            pass

        # 2. Withdrawals
        try:
            path = "/api/v4/wallet/withdrawals"
            qs = f"limit={LIMIT}"
            r = await c.get(f"{base}{path}?{qs}", headers=gate_headers("GET", path, qs))
            r.raise_for_status()
            for w in (r.json() if isinstance(r.json(), list) else [])[:LIMIT]:
                status = "completed" if str(w.get("status", "")).lower() in ("done", "finish") else "pending"
                txs.append(_tx(w.get("txid", w.get("id", "")), "withdraw",
                               w.get("currency", ""), w.get("amount", "0"),
                               w.get("timestamp"), status, network=w.get("chain")))
        except Exception:
            pass

        # 3. Spot trades (only if no deposit/withdrawal history at all)
        if not txs:
            try:
                path = "/api/v4/spot/my_trades"
                qs = f"limit={LIMIT}"
                r = await c.get(f"{base}{path}?{qs}", headers=gate_headers("GET", path, qs))
                r.raise_for_status()
                for t in (r.json() if isinstance(r.json(), list) else [])[:LIMIT]:
                    currency_pair = t.get("currency_pair", "/")
                    asset = currency_pair.split("_")[0]
                    txs.append(_tx(t.get("id", ""), "trade",
                                   asset, t.get("amount", "0"),
                                   t.get("create_time")))
            except Exception:
                pass

        # 4. Futures account book (USDT perp, only if still nothing)
        if not txs:
            try:
                path = "/api/v4/futures/usdt/account_book"
                qs = f"limit={LIMIT}"
                r = await c.get(f"{base}{path}?{qs}", headers=gate_headers("GET", path, qs))
                r.raise_for_status()
                for e in (r.json() if isinstance(r.json(), list) else [])[:LIMIT]:
                    ttype = "trade" if e.get("type") in ("pnl", "fee") else "transfer"
                    txs.append(_tx(e.get("id", ""), ttype,
                                   "USDT", e.get("change", "0"),
                                   e.get("time")))
            except Exception:
                pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


async def _kucoin_txs(creds: dict) -> list[Transaction]:
    from backend.providers.exchanges._signing import b64_hmac_sha256

    base = "https://api.kucoin.com"

    async with RetryClient(timeout=15) as c:
        r = await c.get(f"{base}/api/v1/timestamp")
        ts = str(int(r.json()["data"]))

    def kucoin_headers(method: str, path: str) -> dict:
        prehash = f"{ts}{method.upper()}{path}"
        sign = b64_hmac_sha256(creds["api_secret"], prehash)
        passphrase = b64_hmac_sha256(creds["api_secret"], creds["api_passphrase"])
        return {
            "KC-API-KEY": creds["api_key"],
            "KC-API-SIGN": sign,
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    txs: list[Transaction] = []
    async with RetryClient(timeout=15) as c:
        # KuCoin requires pageSize >= 10
        PAGE = max(LIMIT, 10)

        # 1. Deposits
        try:
            path = f"/api/v1/deposits?pageSize={PAGE}&currentPage=1"
            r = await c.get(f"{base}{path}", headers=kucoin_headers("GET", path))
            r.raise_for_status()
            for d in (r.json().get("data", {}).get("items") or [])[:LIMIT]:
                status = "completed" if d.get("status") == "SUCCESS" else "pending"
                txs.append(_tx(d.get("id", ""), "deposit",
                               d.get("currency", ""), d.get("amount", "0"),
                               d.get("updatedAt") or d.get("createdAt"), status,
                               network=d.get("chain")))
        except Exception as e:
            print(f"[kucoin deposits] {e}")

        # 2. Withdrawals
        try:
            path = f"/api/v1/withdrawals?pageSize={PAGE}&currentPage=1"
            r = await c.get(f"{base}{path}", headers=kucoin_headers("GET", path))
            r.raise_for_status()
            for w in (r.json().get("data", {}).get("items") or [])[:LIMIT]:
                status = "completed" if w.get("status") == "SUCCESS" else "pending"
                txs.append(_tx(w.get("id", ""), "withdraw",
                               w.get("currency", ""), w.get("amount", "0"),
                               w.get("updatedAt") or w.get("createdAt"), status,
                               network=w.get("chain")))
        except Exception as e:
            print(f"[kucoin withdrawals] {e}")

        # 3. Account ledger as fallback
        if not txs:
            try:
                path = f"/api/v1/accounts/ledgers?pageSize={PAGE}&currentPage=1"
                r = await c.get(f"{base}{path}", headers=kucoin_headers("GET", path))
                r.raise_for_status()
                for item in (r.json().get("data", {}).get("items") or [])[:LIMIT]:
                    direction = item.get("direction", "")
                    ttype = "deposit" if direction == "in" else "withdraw" if direction == "out" else "trade"
                    txs.append(_tx(item.get("id", ""), ttype,
                                   item.get("currency", ""), item.get("amount", "0"),
                                   item.get("createdAt")))
            except Exception as e:
                print(f"[kucoin ledgers] {e}")

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


async def _mexc_txs(creds: dict) -> list[Transaction]:
    from backend.providers.exchanges._signing import hex_hmac_sha256, ms

    base = "https://api.mexc.com"
    headers = {"X-MEXC-APIKEY": creds["api_key"]}

    txs: list[Transaction] = []

    async def signed_get(path: str, params: dict):
        p = dict(params)
        p["timestamp"] = ms()   # текущее локальное время, как в balance провайдере
        p["recvWindow"] = "5000"
        qs = urlencode(p, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)
        url = f"{base}{path}?{qs}&signature={sig}"
        async with RetryClient(timeout=15) as c2:
            r2 = await c2.get(url, headers=headers)
            r2.raise_for_status()
            return r2.json()

    # 1. Deposits
    try:
        data = await signed_get("/api/v3/capital/deposit/hisrec", {"limit": str(LIMIT)})
        deposits = data if isinstance(data, list) else (data.get("depositList") or [])
        for d in deposits[:LIMIT]:
            status = "completed" if str(d.get("status")) == "1" else "pending"
            txs.append(_tx(d.get("id", d.get("txId", "")), "deposit",
                           d.get("coin", ""), d.get("amount", "0"),
                           d.get("insertTime"), status, network=d.get("network")))
    except Exception:
        pass

    # 2. Withdrawals
    try:
        data = await signed_get("/api/v3/capital/withdraw/history", {"limit": str(LIMIT)})
        withdrawals = data if isinstance(data, list) else (data.get("withdrawList") or [])
        for w in withdrawals[:LIMIT]:
            status = "completed" if str(w.get("status")) == "7" else "pending"
            txs.append(_tx(w.get("id", ""), "withdraw",
                           w.get("coin", ""), w.get("amount", "0"),
                           w.get("applyTime"), status, network=w.get("network")))
    except Exception:
        pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


async def _bitget_txs(creds: dict) -> list[Transaction]:
    import base64 as _b64
    import hashlib
    import hmac as _hmac

    base = "https://api.bitget.com"

    def bitget_headers(method: str, path_with_qs: str) -> dict:
        ts = str(int(time.time() * 1000))
        message = f"{ts}{method.upper()}{path_with_qs}"
        digest = _hmac.new(creds["api_secret"].encode(), message.encode(), hashlib.sha256).digest()
        sign = _b64.b64encode(digest).decode()
        return {
            "ACCESS-KEY": creds["api_key"],
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": creds["api_passphrase"],
            "locale": "en-US",
        }

    txs: list[Transaction] = []
    async with RetryClient(timeout=15) as c:
        # 1. Spot account bills (deposits, withdrawals, trades)
        try:
            path = f"/api/v2/spot/account/bills?limit={LIMIT}"
            r = await c.get(f"{base}{path}", headers=bitget_headers("GET", path))
            r.raise_for_status()
            for b in (r.json().get("data") or [])[:LIMIT]:
                ttype = _bitget_type(b.get("businessType", ""))
                txs.append(_tx(b.get("billId", ""), ttype,
                               b.get("coin", ""), b.get("size", "0"),
                               b.get("cTime")))
        except Exception:
            pass

        # 2. USDT Futures bills
        if len(txs) < LIMIT:
            try:
                path = f"/api/v2/mix/account/bill?productType=USDT-FUTURES&pageSize={LIMIT}"
                r = await c.get(f"{base}{path}", headers=bitget_headers("GET", path))
                r.raise_for_status()
                resp = r.json().get("data") or {}
                bills = resp.get("bills") if isinstance(resp, dict) else (resp or [])
                for b in (bills or [])[:LIMIT - len(txs)]:
                    business = b.get("business", "")
                    ttype = "trade" if any(x in business for x in ("open", "close", "settle")) else "transfer"
                    txs.append(_tx(b.get("billId", ""), ttype,
                                   b.get("coin", "USDT"), b.get("amount", "0"),
                                   b.get("cTime")))
            except Exception:
                pass

        # 3. USDC Futures bills
        if len(txs) < LIMIT:
            try:
                path = f"/api/v2/mix/account/bill?productType=USDC-FUTURES&pageSize={LIMIT}"
                r = await c.get(f"{base}{path}", headers=bitget_headers("GET", path))
                r.raise_for_status()
                resp = r.json().get("data") or {}
                bills = resp.get("bills") if isinstance(resp, dict) else (resp or [])
                for b in (bills or [])[:LIMIT - len(txs)]:
                    business = b.get("business", "")
                    ttype = "trade" if any(x in business for x in ("open", "close", "settle")) else "transfer"
                    txs.append(_tx(b.get("billId", ""), ttype,
                                   b.get("coin", "USDC"), b.get("amount", "0"),
                                   b.get("cTime")))
            except Exception:
                pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


def _bitget_type(bt: str) -> str:
    bt = bt.lower()
    if "deposit" in bt: return "deposit"
    if "withdraw" in bt: return "withdraw"
    if "trade" in bt or "fill" in bt or "match" in bt: return "trade"
    return "transfer"


async def _backpack_txs(creds: dict) -> list[Transaction]:
    import base64 as _b64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    base = "https://api.backpack.exchange"
    recv_window = 60000

    def _make_headers(instruction: str, params: dict | None = None) -> dict:
        ts = int(time.time() * 1000)
        parts = [("instruction", instruction)]
        if params:
            parts += sorted((k, str(v)) for k, v in params.items())
        parts += [("timestamp", str(ts)), ("window", str(recv_window))]
        sign_str = urlencode(parts)
        seed = _b64.b64decode(creds["api_secret"])
        pk = Ed25519PrivateKey.from_private_bytes(seed)
        sig = _b64.b64encode(pk.sign(sign_str.encode())).decode()
        return {
            "X-API-KEY": creds["api_key"],
            "X-SIGNATURE": sig,
            "X-TIMESTAMP": str(ts),
            "X-WINDOW": str(recv_window),
        }

    txs: list[Transaction] = []
    async with RetryClient(timeout=15) as c:
        # 1. Deposits
        try:
            r = await c.get(f"{base}/wapi/v1/capital/deposits",
                            headers=_make_headers("depositQueryAll"))
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else (data.get("deposits") or [])
            for d in items[:LIMIT]:
                status = "completed" if str(d.get("status", "")).lower() in ("confirmed", "complete", "success") else "pending"
                txs.append(_tx(d.get("id", d.get("txId", "")), "deposit",
                               d.get("symbol", d.get("coin", "")),
                               d.get("quantity", d.get("amount", "0")),
                               d.get("createdAt") or d.get("timestamp"), status,
                               network=d.get("blockchain") or d.get("network")))
        except Exception:
            pass

        # 2. Withdrawals
        try:
            r = await c.get(f"{base}/wapi/v1/capital/withdrawals",
                            headers=_make_headers("withdrawalQueryAll"))
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else (data.get("withdrawals") or [])
            for w in items[:LIMIT]:
                status = "completed" if str(w.get("status", "")).lower() in ("confirmed", "complete", "success") else "pending"
                txs.append(_tx(w.get("id", w.get("txId", "")), "withdraw",
                               w.get("symbol", w.get("coin", "")),
                               w.get("quantity", w.get("amount", "0")),
                               w.get("createdAt") or w.get("timestamp"), status,
                               network=w.get("blockchain") or w.get("network")))
        except Exception:
            pass

        # 3. Fill history (trades) as fallback
        if len(txs) < LIMIT:
            try:
                r = await c.get(f"{base}/wapi/v1/history/fills",
                                headers=_make_headers("fillHistoryQueryAll"))
                r.raise_for_status()
                data = r.json()
                items = data if isinstance(data, list) else (data.get("fills") or [])
                for f in items[:LIMIT - len(txs)]:
                    sym = (f.get("symbol") or "")
                    asset = sym.replace("_USDC", "").replace("_USDT", "").replace("_SOL", "")
                    side = (f.get("side") or "").lower()
                    txs.append(_tx(f.get("tradeId", f.get("id", "")),
                                   f"trade ({side})" if side else "trade",
                                   asset, f.get("quantity", "0"),
                                   f.get("timestamp") or f.get("executedAt")))
            except Exception:
                pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


# ---------------------------------------------------------------------------
# Perp DEX fetchers
# ---------------------------------------------------------------------------

async def _hyperliquid_txs(address: str) -> list[Transaction]:
    async with RetryClient(timeout=15) as c:
        r = await c.post("https://api.hyperliquid.xyz/info",
                         json={"type": "userFills", "user": address},
                         headers={"accept": "application/json"})
        r.raise_for_status()
        data = r.json()

    txs = []
    fills = data if isinstance(data, list) else []
    for f in fills[:LIMIT]:
        coin = f.get("coin", "")
        side = "buy" if f.get("side", "") == "B" else "sell"
        txs.append(_tx(f.get("hash", f.get("oid", "")), f"trade ({side})",
                       coin, f.get("sz", "0"), f.get("time")))
    return txs


async def _lighter_txs(address: str) -> list[Transaction]:
    """Lighter public REST API — fills require auth (403), try order history instead."""
    base = "https://mainnet.zklighter.elliot.ai"
    txs: list[Transaction] = []

    async with RetryClient(timeout=15) as c:
        # Try order history endpoint (public)
        try:
            params = {"by": "l1_address", "value": address, "limit": LIMIT}
            r = await c.get(f"{base}/api/v1/account/order-history",
                            params=params, headers={"accept": "application/json"})
            if r.status_code == 200:
                data = r.json()
                orders = data if isinstance(data, list) else (data.get("order_history") or data.get("orders") or [])
                for o in orders[:LIMIT]:
                    market = o.get("market", {})
                    asset = market.get("base_asset_symbol", "") if isinstance(market, dict) else ""
                    side = "buy" if str(o.get("is_ask", "")).lower() in ("false", "0") else "sell"
                    status_raw = str(o.get("status", "")).lower()
                    status = "completed" if "fill" in status_raw or "complete" in status_raw else "pending"
                    txs.append(_tx(o.get("order_id", o.get("id", "")), f"trade ({side})",
                                   asset or "USDC",
                                   o.get("filled_quantity") or o.get("quantity", "0"),
                                   o.get("created_at") or o.get("timestamp"), status))
        except Exception:
            pass

        # Try fills via account info (some versions include recent fills in account response)
        if not txs:
            try:
                params = {"by": "l1_address", "value": address}
                r = await c.get(f"{base}/api/v1/account", params=params,
                                headers={"accept": "application/json"})
                if r.status_code == 200:
                    data = r.json()
                    acc = data if "orders" in data else (data.get("accounts") or [{}])[0]
                    for o in (acc.get("orders") or [])[:LIMIT]:
                        market = o.get("market", {})
                        asset = market.get("baseAsset", {}).get("symbol", "") if isinstance(market, dict) else ""
                        side = "buy" if str(o.get("isBuyer", "")).lower() in ("true", "1") else "sell"
                        txs.append(_tx(o.get("id", ""), f"trade ({side})",
                                       asset or "USDC", o.get("filledQty") or o.get("quantity", "0"),
                                       o.get("createdAt") or o.get("timestamp")))
            except Exception:
                pass

    return txs[:LIMIT]


async def _ethereal_txs(address: str) -> list[Transaction]:
    """Fetch fills from Ethereal — first resolve subaccount_id like the balance provider does."""
    base = "https://api.ethereal.trade"
    txs: list[Transaction] = []

    async with RetryClient(timeout=15) as c:
        # Step 1: resolve subaccount_id
        subaccount_id = None
        try:
            r = await c.get(f"{base}/v1/subaccount", params={"sender": address})
            if r.status_code == 200:
                subaccounts = r.json().get("data") or []
                if subaccounts:
                    subaccount_id = subaccounts[0].get("id")
        except Exception:
            pass

        # Step 2: fetch fills using subaccount_id
        if subaccount_id:
            try:
                r = await c.get(f"{base}/v1/fills", params={"subaccountId": subaccount_id, "limit": LIMIT})
                if r.status_code == 200:
                    items = r.json().get("data") or []
                    for f in items[:LIMIT]:
                        base_tok = f.get("baseToken") or {}
                        asset = base_tok.get("symbol", "") if isinstance(base_tok, dict) else str(base_tok)
                        side = (f.get("side") or "trade").lower()
                        amt = f.get("baseAmount") or f.get("amount") or f.get("qty") or "0"
                        txs.append(_tx(f.get("id", f.get("hash", "")), f"trade ({side})",
                                       asset or "USD", amt,
                                       f.get("createdAt") or f.get("timestamp")))
            except Exception:
                pass

        # Step 3: fallback — try order history with sender param
        if not txs:
            try:
                r = await c.get(f"{base}/v1/orders", params={"sender": address, "limit": LIMIT})
                if r.status_code == 200:
                    items = r.json().get("data") or []
                    for o in items[:LIMIT]:
                        base_tok = o.get("baseToken") or {}
                        asset = base_tok.get("symbol", "") if isinstance(base_tok, dict) else str(base_tok)
                        side = (o.get("side") or "trade").lower()
                        amt = o.get("filledBaseAmount") or o.get("baseAmount") or "0"
                        status_raw = str(o.get("status", "")).lower()
                        status = "completed" if "fill" in status_raw or "complet" in status_raw else "pending"
                        txs.append(_tx(o.get("id", ""), f"trade ({side})",
                                       asset or "USD", amt,
                                       o.get("updatedAt") or o.get("createdAt"), status))
            except Exception:
                pass

    return txs[:LIMIT]


# ---------------------------------------------------------------------------
# Chain fetchers
# ---------------------------------------------------------------------------

async def _evm_txs(address: str, chain: str) -> list[Transaction]:
    from settings import settings
    from backend.providers.chains.evm_chains import ANKR_CHAIN_MAP, NATIVE_TOKEN

    ankr_key = getattr(settings, "ANKR_KEY", None)
    if not ankr_key:
        return []

    ankr_chain = ANKR_CHAIN_MAP.get(chain)
    if not ankr_chain:
        return []

    url = f"https://rpc.ankr.com/multichain/{ankr_key}"
    addr_lower = address.lower()

    # Primary: token transfers (already decoded symbol + human amount)
    token_txs: list[Transaction] = []
    try:
        async with RetryClient(timeout=20) as c:
            r = await c.post(url, json={
                "jsonrpc": "2.0",
                "method": "ankr_getTokenTransfers",
                "params": {
                    "address": address,
                    "blockchain": ankr_chain,
                    "pageSize": LIMIT,
                    "descOrder": True,
                },
                "id": 1,
            })
            r.raise_for_status()
            data = r.json()
        for t in (data.get("result", {}).get("transfers") or [])[:LIMIT]:
            to_addr = (t.get("toAddress") or "").lower()
            from_addr = (t.get("fromAddress") or "").lower()
            direction = "deposit" if to_addr == addr_lower else "withdraw"
            counterparty = from_addr if direction == "deposit" else to_addr
            token_txs.append(_tx(
                t.get("transactionHash", ""), direction,
                t.get("tokenSymbol", ""), t.get("value", "0"),
                t.get("timestamp"), address=counterparty or None, network=chain,
            ))
    except Exception:
        pass

    if token_txs:
        return token_txs

    # Fallback: native transactions
    native_txs: list[Transaction] = []
    try:
        async with RetryClient(timeout=20) as c:
            r = await c.post(url, json={
                "jsonrpc": "2.0",
                "method": "ankr_getTransactionsByAddress",
                "params": {
                    "address": address,
                    "blockchain": ankr_chain,
                    "pageSize": LIMIT,
                    "descOrder": True,
                },
                "id": 1,
            })
            r.raise_for_status()
            data = r.json()
        symbol, decimals = NATIVE_TOKEN.get(chain, ("ETH", 18))
        for tx in (data.get("result", {}).get("transactions") or [])[:LIMIT]:
            value_hex = tx.get("value", "0x0") or "0x0"
            try:
                value_wei = int(value_hex, 16)
            except ValueError:
                value_wei = int(value_hex) if value_hex.isdigit() else 0
            amount = str(value_wei / (10 ** decimals))
            ts_hex = tx.get("timestamp", "0x0") or "0x0"
            try:
                ts = int(ts_hex, 16) if ts_hex.startswith("0x") else int(ts_hex)
            except Exception:
                ts = 0
            inp = tx.get("input", "0x") or "0x"
            ttype = "transfer" if inp in ("0x", "", None) else "contract"
            status = "completed" if tx.get("status") in ("0x1", 1, "1") else "failed"
            direction = "deposit" if (tx.get("to") or "").lower() == addr_lower else ttype
            native_txs.append(_tx(tx.get("hash", ""), direction, symbol, amount, ts, status, network=chain))
    except Exception:
        pass

    return native_txs


async def _tron_txs(address: str) -> list[Transaction]:
    from settings import settings
    tron_key = getattr(settings, "TRON_KEY", None)
    headers = {}
    if tron_key:
        headers["TRON-PRO-API-KEY"] = tron_key

    txs: list[Transaction] = []
    async with RetryClient(timeout=20, headers=headers) as c:
        try:
            r = await c.get(
                f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20",
                params={"limit": LIMIT, "order_by": "block_timestamp,desc"},
            )
            r.raise_for_status()
            data = r.json()
            for t in (data.get("data") or [])[:LIMIT]:
                token = t.get("token_info", {})
                symbol = token.get("symbol", "TRC20")
                decimals = int(token.get("decimals", 6))
                raw = int(t.get("value", "0") or "0")
                amount = str(raw / (10 ** decimals))
                is_deposit = t.get("to") == address
                direction = "deposit" if is_deposit else "withdraw"
                counterparty = t.get("from") if is_deposit else t.get("to")
                txs.append(_tx(t.get("transaction_id", ""), direction,
                               symbol, amount, t.get("block_timestamp"),
                               address=counterparty or None, network="TRON"))
        except Exception:
            pass
    return txs


async def _solana_txs(address: str) -> list[Transaction]:
    """Fetch recent Solana transactions via individual getTransaction calls."""
    import asyncio as _asyncio
    from settings import settings
    from backend.providers.chains.solana_provider import _symbol_for
    rpc = settings.SOLANA_RPC or "https://api.mainnet-beta.solana.com"

    def _parse_tx(sig: str, tx: dict) -> Transaction | None:
        """Parse a single transaction dict into a Transaction or None."""
        if not tx:
            return None
        block_time = tx.get("blockTime")
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return None  # skip failed

        account_keys_raw = (tx.get("transaction", {})
                              .get("message", {})
                              .get("accountKeys") or [])
        account_keys = [
            (k if isinstance(k, str) else k.get("pubkey", ""))
            for k in account_keys_raw
        ]

        # --- SPL token balance changes for our address ---
        pre_tok  = {b["accountIndex"]: b for b in (meta.get("preTokenBalances")  or [])}
        post_tok = {b["accountIndex"]: b for b in (meta.get("postTokenBalances") or [])}
        all_idx = set(pre_tok) | set(post_tok)

        for idx in all_idx:
            pre  = pre_tok.get(idx, {})
            post = post_tok.get(idx, {})
            owner = post.get("owner") or pre.get("owner") or ""
            if owner != address:
                continue
            mint = post.get("mint") or pre.get("mint") or ""
            try:
                pre_amt  = Decimal(str((pre.get("uiTokenAmount")  or {}).get("uiAmountString") or "0"))
                post_amt = Decimal(str((post.get("uiTokenAmount") or {}).get("uiAmountString") or "0"))
            except Exception:
                continue
            diff = post_amt - pre_amt
            if abs(diff) < Decimal("0.000001"):
                continue
            symbol    = _symbol_for(mint) if mint else "SPL"
            direction = "deposit" if diff > 0 else "withdraw"
            # counterparty: other token account owner for the same mint
            counterparty = None
            for oth in all_idx:
                if oth == idx:
                    continue
                op = pre_tok.get(oth, {}); oq = post_tok.get(oth, {})
                if (op.get("mint") or oq.get("mint")) != mint:
                    continue
                counterparty = oq.get("owner") or op.get("owner")
                break
            return _tx(sig, direction, symbol, str(abs(diff)),
                       block_time, address=counterparty or None, network="Solana")

        # --- Native SOL change (fallback) ---
        pre_sol_list  = meta.get("preBalances")  or []
        post_sol_list = meta.get("postBalances") or []
        if address in account_keys:
            idx = account_keys.index(address)
            if idx < len(pre_sol_list):
                pre_sol  = Decimal(pre_sol_list[idx])  / Decimal(10 ** 9)
                post_sol = Decimal(post_sol_list[idx]) / Decimal(10 ** 9)
                diff = post_sol - pre_sol
                fee  = Decimal(meta.get("fee", 0)) / Decimal(10 ** 9)
                if abs(diff) >= Decimal("0.000001") and abs(diff) > fee:
                    direction = "deposit" if diff > 0 else "withdraw"
                    counterparty = None
                    for i2, key in enumerate(account_keys):
                        if i2 == idx or i2 >= len(pre_sol_list):
                            continue
                        od = (Decimal(post_sol_list[i2]) - Decimal(pre_sol_list[i2])) / Decimal(10 ** 9)
                        if abs(od) >= Decimal("0.000001") and (od > 0) != (diff > 0):
                            counterparty = key
                            break
                    return _tx(sig, direction, "SOL", str(abs(diff)),
                               block_time, address=counterparty or None, network="Solana")
        return None

    async def _fetch_one(c: httpx.AsyncClient, sig: str) -> dict | None:
        try:
            r = await c.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [sig, {"encoding": "jsonParsed",
                                 "maxSupportedTransactionVersion": 0,
                                 "commitment": "confirmed"}],
            }, timeout=15)
            if r.status_code == 200:
                return r.json().get("result")
        except Exception:
            pass
        return None

    txs: list[Transaction] = []
    try:
        async with RetryClient(timeout=15) as c:
            # 1. recent signatures (fetch extra to account for failed/irrelevant txs)
            r = await c.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [address, {"limit": LIMIT * 4}],
            }, timeout=15)
            r.raise_for_status()
            sigs_data = r.json().get("result") or []
            if not sigs_data:
                return []

            signatures = [s["signature"] for s in sigs_data]

            # 2. Fetch all transactions in parallel
            results = await _asyncio.gather(
                *[_fetch_one(c, sig) for sig in signatures],
                return_exceptions=True,
            )

            for sig, tx_data in zip(signatures, results):
                if isinstance(tx_data, Exception) or tx_data is None:
                    continue
                parsed = _parse_tx(sig, tx_data)
                if parsed:
                    txs.append(parsed)
                if len(txs) >= LIMIT:
                    break

    except Exception:
        pass
    return txs[:LIMIT]


async def _aster_txs(address: str) -> list[Transaction]:
    """Aster DEX — fetch recent fills via JSON-RPC."""
    txs: list[Transaction] = []
    try:
        async with RetryClient(timeout=15) as c:
            r = await c.post(
                "https://tapi.asterdex.com/info",
                json={"id": 1, "jsonrpc": "2.0", "method": "aster_getUserFills",
                      "params": [address, LIMIT]},
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 200:
                result = r.json().get("result") or []
                fills = result if isinstance(result, list) else (result.get("fills") or [])
                for f in fills[:LIMIT]:
                    symbol = (f.get("symbol") or f.get("coin") or "")
                    asset = symbol.replace("USDT", "").replace("USD", "").rstrip("-_") or symbol
                    side = (f.get("side") or "").lower()
                    txs.append(_tx(
                        f.get("id") or f.get("tradeId") or f.get("hash", ""),
                        f"trade ({side})" if side else "trade",
                        asset or "USDT",
                        f.get("qty") or f.get("size") or f.get("sz") or "0",
                        f.get("time") or f.get("timestamp") or f.get("createdAt"),
                    ))
    except Exception:
        pass
    return txs[:LIMIT]


async def _kraken_txs(creds: dict) -> list[Transaction]:
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hmac

    base = "https://api.kraken.com"

    def _sign(path: str, body: str, nonce: str) -> str:
        sha256_hash = _hl.sha256((nonce + body).encode()).digest()
        mac = _hmac.new(_b64.b64decode(creds["api_secret"]), path.encode() + sha256_hash, _hl.sha512)
        return _b64.b64encode(mac.digest()).decode()

    async def _post(path: str, extra: dict | None = None) -> dict:
        from urllib.parse import urlencode
        nonce = str(int(time.time() * 1000))
        params = {"nonce": nonce, **(extra or {})}
        body = urlencode(params)
        sign = _sign(path, body, nonce)
        headers = {
            "API-Key": creds["api_key"],
            "API-Sign": sign,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with RetryClient(timeout=15) as c:
            r = await c.post(f"{base}{path}", content=body.encode(), headers=headers)
            r.raise_for_status()
            return r.json()

    _RENAME = {
        "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "ZUSD": "USD",
        "ZEUR": "EUR", "XXRP": "XRP", "XLTC": "LTC",
    }

    def _norm(a: str) -> str:
        return _RENAME.get(a, a)

    txs: list[Transaction] = []
    try:
        data = await _post("/0/private/Ledgers", {"type": "all", "ofs": 0})
        ledger = (data.get("result") or {}).get("ledger") or {}
        entries = sorted(ledger.values(), key=lambda x: float(x.get("time") or 0), reverse=True)
        for e in entries[:LIMIT]:
            ltype = (e.get("type") or "").lower()
            ttype = (
                "deposit" if ltype == "deposit"
                else "withdraw" if ltype == "withdrawal"
                else "trade" if ltype in ("trade", "settled")
                else "transfer"
            )
            txs.append(_tx(
                e.get("refid", ""),
                ttype,
                _norm(e.get("asset") or ""),
                e.get("amount", "0"),
                e.get("time"),
                address=e.get("wallet"),
            ))
    except Exception:
        pass
    return txs[:LIMIT]


async def _whitebit_txs(creds: dict) -> list[Transaction]:
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hmac
    import json as _json

    base = "https://whitebit.com"

    def _signed(path: str, extra: dict | None = None) -> tuple[str, dict, bytes]:
        nonce = str(int(time.time() * 1000))
        body_dict = {"request": path, "nonce": nonce, **(extra or {})}
        body_json = _json.dumps(body_dict, separators=(",", ":"))
        b64_body = _b64.b64encode(body_json.encode()).decode()
        sign = _hmac.new(creds["api_secret"].encode(), b64_body.encode(), _hl.sha512).hexdigest()
        headers = {
            "X-TXC-APIKEY": creds["api_key"],
            "X-TXC-SIGNATURE": sign,
            "X-TXC-NONCE": nonce,
            "Content-Type": "application/json",
        }
        return f"{base}{path}", headers, body_json.encode()

    txs: list[Transaction] = []
    async with RetryClient(timeout=15) as c:
        # Deposits
        try:
            path = "/api/v4/main-account/history"
            url, headers, body = _signed(path, {"transactionMethod": 1, "limit": LIMIT, "offset": 0})
            r = await c.post(url, content=body, headers=headers)
            r.raise_for_status()
            for item in (r.json().get("records") or [])[:LIMIT]:
                txs.append(_tx(
                    item.get("uniqueId") or item.get("transactionHash", ""),
                    "deposit",
                    item.get("ticker") or item.get("currency", ""),
                    item.get("amount", "0"),
                    item.get("createdAt"),
                    address=item.get("address"),
                ))
        except Exception:
            pass

        # Withdrawals
        if len(txs) < LIMIT:
            try:
                path = "/api/v4/main-account/history"
                url, headers, body = _signed(path, {"transactionMethod": 2, "limit": LIMIT, "offset": 0})
                r = await c.post(url, content=body, headers=headers)
                r.raise_for_status()
                for item in (r.json().get("records") or [])[:LIMIT - len(txs)]:
                    txs.append(_tx(
                        item.get("uniqueId") or item.get("transactionHash", ""),
                        "withdraw",
                        item.get("ticker") or item.get("currency", ""),
                        item.get("amount", "0"),
                        item.get("createdAt"),
                        address=item.get("address"),
                    ))
            except Exception:
                pass

        # Spot deals (trades)
        if len(txs) < LIMIT:
            try:
                path = "/api/v4/trade-account/executed-history"
                url, headers, body = _signed(path, {"limit": LIMIT, "offset": 0})
                r = await c.post(url, content=body, headers=headers)
                r.raise_for_status()
                deals = r.json()
                if isinstance(deals, dict):
                    deals = [item for sublist in deals.values() for item in (sublist or [])]
                for item in (deals or [])[:LIMIT - len(txs)]:
                    market = item.get("market", "")
                    base_asset = market.split("_")[0] if "_" in market else market
                    txs.append(_tx(
                        item.get("id", ""),
                        "trade",
                        base_asset,
                        item.get("amount") or item.get("qty", "0"),
                        item.get("time"),
                    ))
            except Exception:
                pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


async def _bingx_txs(creds: dict) -> list[Transaction]:
    import hashlib as _hl
    import hmac as _hmac
    from urllib.parse import urlencode

    base = "https://open-api.bingx.com"

    def _signed_url(path: str, params: dict | None = None) -> tuple[str, dict]:
        ts = str(int(time.time() * 1000))
        p = dict(params or {})
        qs = urlencode(sorted(p.items())) if p else ""
        payload = (qs + "&" if qs else "") + f"timestamp={ts}"
        sig = _hmac.new(creds["api_secret"].encode(), payload.encode(), _hl.sha256).hexdigest()
        return f"{base}{path}?{payload}&signature={sig}", {"X-BX-APIKEY": creds["api_key"]}

    txs: list[Transaction] = []
    async with RetryClient(timeout=15) as c:
        # Spot asset records
        try:
            url, headers = _signed_url("/openApi/spot/v1/account/depositOrders", {"limit": LIMIT})
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            for item in (r.json().get("data") or {}).get("list") or []:
                txs.append(_tx(
                    item.get("txId") or item.get("orderId", ""),
                    "deposit",
                    item.get("coin", ""),
                    item.get("amount", "0"),
                    item.get("insertTime"),
                    address=item.get("address"),
                ))
        except Exception:
            pass

        # Spot withdrawals
        if len(txs) < LIMIT:
            try:
                url, headers = _signed_url("/openApi/spot/v1/account/withdrawOrders", {"limit": LIMIT})
                r = await c.get(url, headers=headers)
                r.raise_for_status()
                for item in (r.json().get("data") or {}).get("list") or []:
                    txs.append(_tx(
                        item.get("id", ""),
                        "withdraw",
                        item.get("coin", ""),
                        item.get("amount", "0"),
                        item.get("applyTime"),
                        address=item.get("address"),
                    ))
            except Exception:
                pass

        # Perp futures income
        if len(txs) < LIMIT:
            try:
                url, headers = _signed_url("/openApi/swap/v2/user/income", {"incomeType": "TRANSFER", "limit": LIMIT})
                r = await c.get(url, headers=headers)
                r.raise_for_status()
                for item in (r.json().get("data") or [])[:LIMIT - len(txs)]:
                    inc_type = (item.get("incomeType") or "transfer").lower()
                    ttype = "deposit" if "transfer_in" in inc_type else "withdraw" if "transfer_out" in inc_type else "transfer"
                    txs.append(_tx(
                        item.get("tranId") or item.get("tradeId", ""),
                        ttype,
                        item.get("asset", "USDT"),
                        item.get("income", "0"),
                        item.get("time"),
                    ))
            except Exception:
                pass

    txs.sort(key=lambda t: t.timestamp, reverse=True)
    return txs[:LIMIT]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def fetch_transactions(db_wallet: Wallet) -> TransactionResponse:
    from backend.crypto import decrypt_credentials
    creds = decrypt_credentials(db_wallet.credentials or {})
    base = dict(
        wallet_id=db_wallet.id,
        wallet_name=db_wallet.name,
        wallet_type=db_wallet.wallet_type,
        type_value=db_wallet.type_value,
        transactions=[],
        error=None,
    )

    try:
        wt = db_wallet.wallet_type
        tv = db_wallet.type_value

        if wt == "exchange":
            c = {
                "api_key": creds.get("api_key", ""),
                "api_secret": creds.get("api_secret", ""),
                "api_passphrase": creds.get("api_passphrase", ""),
            }
            if tv == "binance":
                txs = await _binance_txs(c)
            elif tv == "okx":
                txs = await _okx_txs(c)
            elif tv == "bybit":
                txs = await _bybit_txs(c)
            elif tv == "gate":
                txs = await _gate_txs(c)
            elif tv == "kucoin":
                txs = await _kucoin_txs(c)
            elif tv == "mexc":
                txs = await _mexc_txs(c)
            elif tv == "bitget":
                txs = await _bitget_txs(c)
            elif tv == "backpack":
                txs = await _backpack_txs(c)
            elif tv == "kraken":
                txs = await _kraken_txs(c)
            elif tv == "whitebit":
                txs = await _whitebit_txs(c)
            elif tv == "bingx":
                txs = await _bingx_txs(c)
            else:
                txs = []

        elif wt == "chain":
            address = creds.get("address", "")
            if tv == "tron":
                txs = await _tron_txs(address)
            elif tv == "solana":
                txs = await _solana_txs(address)
            else:
                txs = await _evm_txs(address, tv)

        elif wt == "perpdex":
            address = creds.get("address", "")
            if tv == "hyperliquid":
                txs = await _hyperliquid_txs(address)
            elif tv == "aster":
                txs = await _aster_txs(address)
            elif tv == "lighter":
                txs = await _lighter_txs(address)
            elif tv == "ethereal":
                txs = await _ethereal_txs(address)
            else:
                txs = []

        else:
            txs = []

        base["transactions"] = txs[:LIMIT]

    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg or "Unauthorized" in msg or "Invalid" in msg:
            base["error"] = "Invalid API credentials"
        elif "timeout" in msg.lower() or "connect" in msg.lower() or "network" in msg.lower():
            base["error"] = "Provider unavailable — try again later"
        elif "429" in msg or "rate" in msg.lower():
            base["error"] = "Rate limit exceeded — try again later"
        else:
            base["error"] = "Failed to fetch — try again later"

    return TransactionResponse(**base)
