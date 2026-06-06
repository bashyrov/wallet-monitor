"""Check OKX QNT depth over longer window + check via direct REST."""
import asyncio, json, time, uuid, urllib.request
from datetime import datetime, timedelta, timezone
import websockets
from jose import jwt

SECRET_KEY = "lezUBLzrNkRda0fLG/9VRxQEsZYGJR6B/Z9YWz0xPyD9JgYdzlFIQxe4XJtFWHAgvhNnxAenzhaS2gTehVxmiw=="
now = datetime.now(timezone.utc)
payload = {"sub": "1", "exp": now + timedelta(hours=2), "jti": str(uuid.uuid4())}
JWT = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

# 1. Check OKX REST API directly for QNT-USDT-SWAP orderbook
print("=== OKX REST orderbook (QNT-USDT-SWAP) ===")
try:
    url = "https://www.okx.com/api/v5/market/books?instId=QNT-USDT-SWAP&sz=5"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read())
    if data.get("data"):
        bk = data["data"][0]
        bids = bk.get("bids", [])
        asks = bk.get("asks", [])
        print(f"  bids ({len(bids)}): {[[b[0],b[1]] for b in bids[:3]]}")
        print(f"  asks ({len(asks)}): {[[a[0],a[1]] for a in asks[:3]]}")
    else:
        print(f"  Response: {data}")
except Exception as e:
    print(f"  ERROR: {e}")

# 2. Check WS over 30s to see if books channel eventually arrives
print("\n=== WS book for okx:QNT (30s window) ===")
async def check():
    url = "ws://go-fetcher:8090/api/screener/ws/book"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        await ws.send(json.dumps({"action": "subscribe", "pairs": ["okx:QNT"]}))
        t0 = time.time()
        max_levels = (0, 0)
        frames = 0
        while time.time() - t0 < 30:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                for k, book in msg.get("books", {}).items():
                    b = book.get("bids", [])
                    a = book.get("asks", [])
                    if b and a:
                        frames += 1
                        if len(b) > max_levels[0]:
                            max_levels = (len(b), len(a))
                            elapsed = time.time() - t0
                            print(f"  t+{elapsed:.1f}s new max: {len(b)} bids / {len(a)} asks  bid={b[0][0]} ask={a[0][0]}")
            except asyncio.TimeoutError:
                print(f"  t+{time.time()-t0:.0f}s timeout, max so far: {max_levels[0]} bids")
        print(f"\nTotal frames: {frames}, max levels: {max_levels[0]}/{max_levels[1]}")

asyncio.run(check())
