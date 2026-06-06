"""Check QNT orderbook depth across all exchanges."""
import asyncio, json, time, uuid
from datetime import datetime, timedelta, timezone
import websockets
from jose import jwt

SECRET_KEY = "lezUBLzrNkRda0fLG/9VRxQEsZYGJR6B/Z9YWz0xPyD9JgYdzlFIQxe4XJtFWHAgvhNnxAenzhaS2gTehVxmiw=="
now = datetime.now(timezone.utc)
payload = {"sub": "1", "exp": now + timedelta(hours=2), "jti": str(uuid.uuid4())}
JWT = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

EXCHANGES = [
    "binance","bybit","okx","gate","kucoin","bitget","bingx","htx","kraken",
    "whitebit","backpack","aster","hyperliquid","paradex","extended","mexc"
]

async def check_all():
    url = "ws://go-fetcher:8090/api/screener/ws/book"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        pairs = [f"{ex}:QNT" for ex in EXCHANGES]
        await ws.send(json.dumps({"action": "subscribe", "pairs": pairs}))

        results = {}
        counts = {}
        t0 = time.time()

        while time.time() - t0 < 15:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
                for k, book in msg.get("books", {}).items():
                    b = book.get("bids", [])
                    a = book.get("asks", [])
                    if b and a:
                        results[k] = (len(b), len(a), float(b[0][0]), float(a[0][0]))
                        counts[k] = counts.get(k, 0) + 1
            except asyncio.TimeoutError:
                break

        print(f"\n{'PAIR':<25} {'BIDS':>5} {'ASKS':>5} {'BID':>12} {'ASK':>12} {'UPD/15s':>8} {'OK':>4}")
        print("-" * 75)
        for ex in EXCHANGES:
            k = f"{ex}:QNT"
            if k in results:
                nb, na, bid, ask = results[k]
                ok = "✓" if ask > bid else "BID>ASK!"
                upd = counts.get(k, 0)
                print(f"{k:<25} {nb:>5} {na:>5} {bid:>12.4f} {ask:>12.4f} {upd:>8} {ok:>4}")
            else:
                print(f"{k:<25}   NO DATA (not listed or 0 upd)")

asyncio.run(check_all())
