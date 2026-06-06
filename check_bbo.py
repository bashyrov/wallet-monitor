import asyncio, json, sys
import websockets

JWT = sys.argv[1] if len(sys.argv) > 1 else ""
PAIR = sys.argv[2] if len(sys.argv) > 2 else "binance:BTC"

async def check():
    url = "wss://avalant.xyz/api/screener/ws/book"
    async with websockets.connect(url, ssl=True) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        await ws.send(json.dumps({"action": "subscribe", "pairs": [PAIR]}))
        found = 0
        ok = True
        for _ in range(10):
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                # books wrapper: {"books": {"binance:BTC": {bids, asks}}}
                books = msg.get("books", {})
                for pair_key, book in books.items():
                    b = book.get("bids", [])
                    a = book.get("asks", [])
                    if b and a:
                        bid = float(b[0][0])
                        ask = float(a[0][0])
                        valid = ask > bid
                        if not valid:
                            ok = False
                        print(f"{pair_key}: bid={bid:.2f} ask={ask:.2f} spread={ask-bid:.4f} valid={valid}")
                        found += 1
            except asyncio.TimeoutError:
                print("timeout")
        if found == 0:
            print("NO DATA received")
        elif ok:
            print(f"bid<ask OK across {found} frames")
        else:
            print("BID >= ASK PROBLEM!")

asyncio.run(check())
