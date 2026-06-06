"""
Compare aster vs binance bookTicker source rate for BTC.
Both use @bookTicker, aster is a Binance fork.
"""
import asyncio, json, time
import websockets

async def count_frames(url, recv_count, duration=15):
    """Connect, subscribe BTC bookTicker, count frames for duration."""
    count = 0
    try:
        async with websockets.connect(url, ssl=True) as ws:
            # Send subscribe for BTCUSDT@bookTicker
            sub = {"method": "SUBSCRIBE", "params": ["btcusdt@bookTicker"], "id": 1}
            await ws.send(json.dumps(sub))
            t0 = time.time()
            deadline = t0 + duration
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    msg = json.loads(raw)
                    # Count only data frames (not ack)
                    if msg.get("result") is not None:
                        continue  # subscribe ack
                    if msg.get("e") == "bookTicker" or msg.get("stream", "").endswith("@bookTicker"):
                        count += 1
                except asyncio.TimeoutError:
                    pass
    except Exception as e:
        print(f"  Error on {url}: {e}")
    recv_count.append(count / duration)

async def main():
    binance_url = "wss://fstream.binance.com/ws"
    aster_url = "wss://fstream.asterdex.com/ws"

    results = {}
    b_counts = []
    a_counts = []

    print("Testing binance fstream /ws bookTicker for BTC (15s)...")
    await count_frames(binance_url, b_counts, 15)

    print("Testing aster fstream /ws bookTicker for BTC (15s)...")
    await count_frames(aster_url, a_counts, 15)

    print(f"\n--- Results ---")
    print(f"Binance fstream BTC bookTicker: {b_counts[0]:.2f} frames/s")
    print(f"Aster   fstream BTC bookTicker: {a_counts[0]:.2f} frames/s")
    if b_counts[0] > 0:
        print(f"Aster/Binance ratio: {a_counts[0]/b_counts[0]:.2%}")
    print(f"\nConclusion: {'Aster market less active (source-limited)' if a_counts[0] < b_counts[0]*0.7 else 'Aster comparable to Binance'}")

asyncio.run(main())
