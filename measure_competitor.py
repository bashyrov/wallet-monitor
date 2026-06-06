"""
Measure competitor orderbook update rate: connects to their WS stream
for BTC and ETH, counts frames for 30s each, 3 runs, reports median.

Competitors to check (adjust URL/subscribe per competitor):
- arbion.xyz or similar
- coinalyze, velo, etc.

Since we cannot directly WS to arbion from the prod server (it's a browser
app), we measure our OWN system as the baseline AND note competitor claims.

Instead: measure our full venue lineup med-of-3 x 30s for BTC to get
the final comparison table.
"""
import asyncio, json, statistics, time, uuid
from datetime import datetime, timedelta, timezone
import websockets
from jose import jwt

SECRET_KEY = "lezUBLzrNkRda0fLG/9VRxQEsZYGJR6B/Z9YWz0xPyD9JgYdzlFIQxe4XJtFWHAgvhNnxAenzhaS2gTehVxmiw=="
now = datetime.now(timezone.utc)
payload = {"sub": "1", "exp": now + timedelta(hours=2), "jti": str(uuid.uuid4())}
JWT = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

VENUES = [
    "binance", "bybit", "okx", "kraken", "backpack", "paradex",
    "bitget", "htx", "kucoin", "gate", "whitebit", "hyperliquid",
    "aster", "extended", "mexc", "bingx",
]
SYMBOL = "BTC"
RUNS = 3
WINDOW = 30

async def one_run(venue, run_n):
    url = "ws://go-fetcher:8090/api/screener/ws/book"
    pair = f"{venue}:{SYMBOL}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"auth": JWT}))
        await ws.send(json.dumps({"action": "subscribe", "pairs": [pair]}))
        # 2s warmup
        t0 = time.time()
        while time.time() - t0 < 2:
            try: await asyncio.wait_for(ws.recv(), 1)
            except: pass
        # measure
        frames = 0; max_bids = 0; bid_ask_ok = True
        t0 = time.time()
        while time.time() - t0 < WINDOW:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
                for k, book in msg.get("books", {}).items():
                    b = book.get("bids", []); a = book.get("asks", [])
                    if b and a:
                        frames += 1
                        if len(b) > max_bids: max_bids = len(b)
                        if float(a[0][0]) <= float(b[0][0]): bid_ask_ok = False
            except asyncio.TimeoutError: break
        return frames / WINDOW, max_bids, bid_ask_ok

async def main():
    print(f"\n{'VENUE':<14} {'R1':>7} {'R2':>7} {'R3':>7} {'MED':>7} {'LVLS':>5} {'OK':>4}")
    print("-" * 58)
    results = {}
    for venue in VENUES:
        rates = []; bids_l = []; oks = []
        for i in range(1, RUNS+1):
            rate, b, ok = await one_run(venue, i)
            rates.append(rate); bids_l.append(b); oks.append(ok)
        med = statistics.median(rates)
        results[venue] = med
        ok_str = "✓" if all(oks) else "✗"
        print(f"{venue:<14} {rates[0]:>7.1f} {rates[1]:>7.1f} {rates[2]:>7.1f} {med:>7.1f} {max(bids_l):>5} {ok_str:>4}")
    print("\nTarget: ≥20/s. Source-limited venues will not reach target regardless of our code.")

asyncio.run(main())
