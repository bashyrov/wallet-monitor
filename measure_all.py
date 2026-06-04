"""
Measure updates/sec for ALL venue pairs via /ws/book.
Usage: python measure_all.py <jwt> [duration_sec]
Prints a table sorted by upd/sec desc.
"""
import asyncio, json, sys, time
import websockets

PAIRS = [
    ("binance",     "binance:BTC"),
    ("bybit",       "bybit:BTC"),
    ("okx",         "okx:BTC"),
    ("gate",        "gate:BTC"),
    ("mexc",        "mexc:BTC"),
    ("kucoin",      "kucoin:BTC"),
    ("bitget",      "bitget:BTC"),
    ("bingx",       "bingx:BTC"),
    ("htx",         "htx:BTC"),
    ("kraken",      "kraken:BTC"),
    ("whitebit",    "whitebit:BTC"),
    ("aster",       "aster:BTC"),
    ("hyperliquid", "hyperliquid:BTC"),
    ("paradex",     "paradex:BTC"),
    ("lighter",     "lighter:BTC"),
    ("backpack",    "backpack:BTC"),
    ("extended",    "extended:BTC"),
]

async def measure_one(jwt, pair, duration):
    url = "wss://avalant.xyz/api/screener/ws/book"
    try:
        async with websockets.connect(url, ssl=True, open_timeout=6) as ws:
            await ws.send(json.dumps({"auth": jwt}))
            await ws.send(json.dumps({"action": "subscribe", "pairs": [pair]}))
            frames = 0
            t0 = time.time()
            deadline = t0 + duration
            while time.time() < deadline:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=1.0)
                    frames += 1
                except asyncio.TimeoutError:
                    pass
            return frames / (time.time() - t0)
    except Exception as e:
        return f"ERR:{type(e).__name__}"

async def main(jwt, duration=10):
    print(f"Measuring {len(PAIRS)} venues × {duration}s each ...\n")
    results = []
    for name, pair in PAIRS:
        rate = await measure_one(jwt, pair, duration)
        status = f"{rate:.2f}" if isinstance(rate, float) else rate
        flag = ""
        if isinstance(rate, float):
            if rate < 0.1:
                flag = " ← БАГ (нет данных)"
            elif rate < 2.0:
                flag = " ← ИСТОЧНИК медленнее flush"
        print(f"  {name:<14} {pair:<22} {status:>8} upd/sec{flag}")
        results.append((name, pair, rate))

    print("\n--- Итог ---")
    ok    = [(n,r) for n,_,r in results if isinstance(r, float) and r >= 2.0]
    slow  = [(n,r) for n,_,r in results if isinstance(r, float) and 0 < r < 2.0]
    dead  = [(n,r) for n,_,r in results if not isinstance(r, float) or r < 0.1]
    if ok:    print(f"  OK ({len(ok)}): " + ", ".join(f"{n} {r:.1f}/с" for n,r in sorted(ok, key=lambda x:-x[1])))
    if slow:  print(f"  SLOW ({len(slow)}): " + ", ".join(f"{n} {r:.1f}/с" for n,r in slow))
    if dead:  print(f"  БАГ ({len(dead)}): " + ", ".join(n for n,_ in dead))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python measure_all.py <jwt> [duration]"); sys.exit(1)
    jwt = sys.argv[1]
    dur = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    asyncio.run(main(jwt, dur))
