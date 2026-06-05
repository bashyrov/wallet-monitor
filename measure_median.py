"""
СТАНДАРТ ЗАМЕРА (STATUS.md §7.x):
- Фикс-окно: 30с на прогон
- Пара: BTC (ликвидная)
- Медиана из 3 прогонов
Usage: python3 measure_median.py <jwt> [pair] [runs]
pair default: binance:BTC
"""
import asyncio, json, sys, time, statistics
import websockets

async def measure_one(jwt, pair, duration=30):
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
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
                    frames += 1
                except asyncio.TimeoutError:
                    pass
            return frames / (time.time() - t0)
    except Exception as e:
        return f"ERR:{type(e).__name__}"

async def main(jwt, pair="binance:BTC", runs=3, duration=30):
    print(f"Measuring {pair} — {runs} runs × {duration}s (median protocol)")
    results = []
    for i in range(runs):
        print(f"  Run {i+1}/{runs}...", end=" ", flush=True)
        rate = await measure_one(jwt, pair, duration)
        if isinstance(rate, float):
            print(f"{rate:.2f}/s")
            results.append(rate)
        else:
            print(f"FAILED: {rate}")

    if results:
        med = statistics.median(results)
        print(f"\nMedian: {med:.2f}/s  (runs: {[f'{r:.2f}' for r in results]})")
        return med
    return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 measure_median.py <jwt> [pair] [runs]")
        sys.exit(1)
    jwt = sys.argv[1]
    pair = sys.argv[2] if len(sys.argv) > 2 else "binance:BTC"
    runs = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    asyncio.run(main(jwt, pair, runs))
