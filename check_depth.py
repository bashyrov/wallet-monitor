"""Check orderbook depth (number of levels) for a pair."""
import asyncio, json, sys
import websockets

JWT = sys.argv[1]
PAIR = sys.argv[2] if len(sys.argv) > 2 else "kucoin:BTC"

async def check():
    url = "wss://avalant.xyz/api/screener/ws/book"
    async with websockets.connect(url, ssl=True) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        await ws.send(json.dumps({"action": "subscribe", "pairs": [PAIR]}))
        found = 0
        for _ in range(15):
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                books = msg.get("books", {})
                for pair_key, book in books.items():
                    b = book.get("bids", [])
                    a = book.get("asks", [])
                    if b and a:
                        bid = float(b[0][0])
                        ask = float(a[0][0])
                        valid = ask > bid
                        print(f"{pair_key}: bids={len(b)} asks={len(a)} bid={bid:.4f} ask={ask:.4f} spread={ask-bid:.4f} bid<ask={valid}")
                        found += 1
                        if found >= 3:
                            return
            except asyncio.TimeoutError:
                print("timeout")
        if found == 0:
            print("NO DATA")

asyncio.run(check())
