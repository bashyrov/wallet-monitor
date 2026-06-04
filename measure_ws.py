"""
Baseline WS frame counter.
Usage: python measure_ws.py <jwt> <pair> [seconds]
pair format: binance:BTC  (exchange:SYMBOL)
"""
import asyncio, json, sys, time
import websockets

async def measure(jwt, pair, duration=10):
    url = "wss://avalant.xyz/api/screener/ws/book"
    print(f"Connecting to {url} ...")
    async with websockets.connect(url, ssl=True) as ws:
        # auth
        await ws.send(json.dumps({"auth": jwt}))
        # subscribe
        await ws.send(json.dumps({"action": "subscribe", "pairs": [pair]}))
        print(f"Subscribed to {pair}, measuring {duration}s ...")
        frames = 0
        t0 = time.time()
        deadline = t0 + duration
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                frames += 1
            except asyncio.TimeoutError:
                pass
        elapsed = time.time() - t0
        rate = frames / elapsed
        print(f"Pair: {pair}")
        print(f"Frames: {frames} in {elapsed:.1f}s")
        print(f"Rate: {rate:.2f} updates/sec")
        return rate

if __name__ == "__main__":
    jwt = sys.argv[1]
    pair = sys.argv[2] if len(sys.argv) > 2 else "binance:BTC"
    dur = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    asyncio.run(measure(jwt, pair, dur))
