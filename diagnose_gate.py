"""
Connects directly to Gate futures WS, subscribes futures.book_ticker for BTC,
dumps first 5 complete frames to understand event/channel field format.
Run on prod server: python3 diagnose_gate.py
"""
import asyncio, json, time
import websockets

async def main():
    url = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    print(f"Connecting to {url}...")
    async with websockets.connect(url, ssl=True) as ws:
        sub = {
            "time": int(time.time()),
            "channel": "futures.book_ticker",
            "event": "subscribe",
            "payload": ["BTC_USDT"]
        }
        await ws.send(json.dumps(sub))
        print(f"Sent subscribe: {json.dumps(sub)}\n")
        count = 0
        while count < 8:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msg = json.loads(raw)
                ch = msg.get("channel", "?")
                ev = msg.get("event", "?")
                result = msg.get("result", "?")
                print(f"[{count}] channel={ch!r:30} event={ev!r:15} result={json.dumps(result)[:80]}")
                count += 1
            except asyncio.TimeoutError:
                print("timeout")
                break

asyncio.run(main())
