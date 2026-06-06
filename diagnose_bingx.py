"""
Connects directly to BingX swap WS, subscribes bookTicker for BTC-USDT,
counts frames per 10s to see real source rate.
Run on prod server: python3 diagnose_bingx.py
"""
import asyncio, json, time, gzip
import websockets

async def main():
    url = "wss://open-api-swap.bingx.com/swap-market"
    print(f"Connecting to {url}...")
    async with websockets.connect(url, ssl=True) as ws:
        # bookTicker subscription
        sub = {"id": "1", "reqType": "sub", "dataType": "BTC-USDT@bookTicker"}
        await ws.send(json.dumps(sub))
        print(f"Sent: {json.dumps(sub)}")
        # Also try depth20 to compare
        sub2 = {"id": "2", "reqType": "sub", "dataType": "BTC-USDT@depth20"}
        await ws.send(json.dumps(sub2))
        print(f"Sent: {json.dumps(sub2)}\n")

        ticker_count = 0
        depth_count = 0
        t0 = time.time()
        deadline = t0 + 15

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                # bingx uses gzip
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
                text = raw.decode() if isinstance(raw, bytes) else raw
                if text.strip() == "Ping":
                    await ws.send("Pong")
                    continue
                msg = json.loads(text)
                dt = msg.get("dataType", "")
                if "bookTicker" in dt:
                    ticker_count += 1
                    if ticker_count <= 3:
                        print(f"[bookTicker] {json.dumps(msg)[:120]}")
                elif "depth" in dt:
                    depth_count += 1
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"err: {e}")

        elapsed = time.time() - t0
        print(f"\n--- 15s results ---")
        print(f"bookTicker frames: {ticker_count} ({ticker_count/elapsed:.2f}/s)")
        print(f"depth frames:      {depth_count} ({depth_count/elapsed:.2f}/s)")

asyncio.run(main())
