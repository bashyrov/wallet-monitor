"""
Measure BOTH depth (levels) AND update rate for a pair.
Median of N runs, each T seconds.
Usage: python3 measure_depth_speed.py <exchange:SYM> [runs=3] [window=30]
"""
import asyncio, json, sys, time, uuid, statistics
from datetime import datetime, timedelta, timezone
import websockets
from jose import jwt

SECRET_KEY = "lezUBLzrNkRda0fLG/9VRxQEsZYGJR6B/Z9YWz0xPyD9JgYdzlFIQxe4XJtFWHAgvhNnxAenzhaS2gTehVxmiw=="
now = datetime.now(timezone.utc)
payload = {"sub": "1", "exp": now + timedelta(hours=2), "jti": str(uuid.uuid4())}
JWT = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

PAIR   = sys.argv[1] if len(sys.argv) > 1 else "binance:BTC"
RUNS   = int(sys.argv[2]) if len(sys.argv) > 2 else 3
WINDOW = int(sys.argv[3]) if len(sys.argv) > 3 else 30

async def one_run(run_n):
    url = "ws://go-fetcher:8090/api/screener/ws/book"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        await ws.send(json.dumps({"action": "subscribe", "pairs": [PAIR]}))
        # warm-up 2s
        t0 = time.time()
        while time.time() - t0 < 2:
            try: await asyncio.wait_for(ws.recv(), 1)
            except: pass
        # measure window
        frames = 0
        max_bids = max_asks = 0
        bid_ask_ok = True
        t0 = time.time()
        while time.time() - t0 < WINDOW:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
                for k, book in msg.get("books", {}).items():
                    b = book.get("bids", [])
                    a = book.get("asks", [])
                    if b and a:
                        frames += 1
                        if len(b) > max_bids: max_bids = len(b)
                        if len(a) > max_asks: max_asks = len(a)
                        bid = float(b[0][0]); ask = float(a[0][0])
                        if ask <= bid:
                            bid_ask_ok = False
            except asyncio.TimeoutError:
                pass
        rate = frames / WINDOW
        print(f"  run {run_n}: {rate:.2f}/s  bids={max_bids} asks={max_asks}  bid<ask={'✓' if bid_ask_ok else '✗ BID>ASK!'}")
        return rate, max_bids, max_asks, bid_ask_ok

async def main():
    print(f"Measuring {PAIR}: {RUNS} runs × {WINDOW}s each\n")
    rates, bids_l, asks_l, oks = [], [], [], []
    for i in range(1, RUNS+1):
        rate, b, a, ok = await one_run(i)
        rates.append(rate); bids_l.append(b); asks_l.append(a); oks.append(ok)
    med = statistics.median(rates)
    print(f"\nMedian rate : {med:.2f}/s")
    print(f"Max levels  : bids={max(bids_l)} asks={max(asks_l)}")
    print(f"bid<ask OK  : {'ALL ✓' if all(oks) else 'FAIL ✗'}")
    print(f"Depth target: {'✓ 20+' if max(bids_l) >= 20 else f'✗ only {max(bids_l)}'}")
    print(f"Speed target: {'✓ ≥20/s' if med >= 20 else f'✗ {med:.2f}/s (below 20/s)'}")

asyncio.run(main())
