"""Check OKX QNT price vs other exchanges."""
import asyncio, json, time, uuid
from datetime import datetime, timedelta, timezone
import websockets
from jose import jwt

SECRET_KEY = "lezUBLzrNkRda0fLG/9VRxQEsZYGJR6B/Z9YWz0xPyD9JgYdzlFIQxe4XJtFWHAgvhNnxAenzhaS2gTehVxmiw=="
now = datetime.now(timezone.utc)
payload = {"sub": "1", "exp": now + timedelta(hours=2), "jti": str(uuid.uuid4())}
JWT = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

async def check():
    url = "ws://go-fetcher:8090/api/screener/ws/book"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        # Check OKX and Binance together to compare
        await ws.send(json.dumps({"action": "subscribe", "pairs": ["okx:QNT","binance:QNT","bybit:QNT"]}))
        t0 = time.time()
        seen = {}
        while time.time() - t0 < 8:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
                for k, book in msg.get("books", {}).items():
                    b = book.get("bids", [])
                    a = book.get("asks", [])
                    if b and a:
                        if k not in seen:
                            print(f"\n{k}: bid={b[0][0]} ask={a[0][0]} levels={len(b)}/{len(a)}")
                            print(f"  Top 3 bids: {[x[0] for x in b[:3]]}")
                            print(f"  Top 3 asks: {[x[0] for x in a[:3]]}")
                        seen[k] = seen.get(k, 0) + 1
            except asyncio.TimeoutError:
                break
        print(f"\nFrames: {seen}")

asyncio.run(check())
