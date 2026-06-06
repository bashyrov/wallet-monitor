"""Check Extended (x10) orderbook WS source rate."""
import asyncio, json, time
import websockets

async def main():
    url = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks"
    print(f"Connecting to {url}...")
    try:
        async with websockets.connect(url, ssl=True, ping_interval=9, ping_timeout=5) as ws:
            snap_count = 0
            delta_count = 0
            other_count = 0
            t0 = time.time()
            deadline = t0 + 15
            print("Counting frames for 15s...")
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    try:
                        msg = json.loads(raw)
                        tp = msg.get("type", "")
                        if tp == "SNAPSHOT":
                            snap_count += 1
                        elif tp == "DELTA":
                            delta_count += 1
                            if delta_count <= 2:
                                print(f"DELTA: {json.dumps(msg)[:100]}")
                        else:
                            other_count += 1
                    except Exception:
                        other_count += 1
                except asyncio.TimeoutError:
                    print("3s timeout — no data, server may close connection")
            elapsed = time.time() - t0
            print(f"\n--- {elapsed:.1f}s ---")
            print(f"SNAPSHOT: {snap_count} ({snap_count/elapsed:.2f}/s)")
            print(f"DELTA:    {delta_count} ({delta_count/elapsed:.2f}/s)")
            print(f"other:    {other_count}")
    except Exception as e:
        print(f"Connection error: {e}")

asyncio.run(main())
