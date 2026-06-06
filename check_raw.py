import asyncio, json, sys
import websockets

JWT = sys.argv[1]
PAIR = sys.argv[2] if len(sys.argv) > 2 else "binance:BTC"

async def check():
    url = "wss://avalant.xyz/api/screener/ws/book"
    async with websockets.connect(url, ssl=True) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        await ws.send(json.dumps({"action": "subscribe", "pairs": [PAIR]}))
        for i in range(4):
            try:
                raw = await asyncio.wait_for(ws.recv(), 5)
                msg = json.loads(raw)
                keys = list(msg.keys())
                b = msg.get("bids") or msg.get("b") or []
                a = msg.get("asks") or msg.get("a") or []
                sym = msg.get("symbol") or msg.get("s") or "?"
                print(f"[{i}] keys={keys} sym={sym} bids={len(b)} asks={len(a)}")
                if b and a:
                    print(f"    bid[0]={b[0]} ask[0]={a[0]}")
            except asyncio.TimeoutError:
                print(f"[{i}] timeout")

asyncio.run(check())
