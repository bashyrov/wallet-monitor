"""Check funding data freshness on the live prod server."""
import json, sys, time, urllib.request

url = "https://avalant.xyz/api/screener/funding"
with urllib.request.urlopen(url, timeout=10) as r:
    d = json.load(r)

rows = d.get("rows", []) or []
ts = d.get("ts", 0)
now = time.time()

print(f"API ts: {ts} (age: {now-ts:.1f}s)")
print(f"Total rows: {len(rows)}")

# Check Binance BTC specifically
binance_btc = [r for r in rows if r.get("symbol") == "BTC" and r.get("exchange") == "binance"]
print(f"\nBinance BTC rows: {len(binance_btc)}")
for r in binance_btc[:3]:
    age = now - r.get("ts", now)
    print(f"  {json.dumps(r)} age={age:.1f}s")

# Check all binance rows - are they fresh?
binance_rows = [r for r in rows if r.get("exchange") == "binance"]
if binance_rows:
    ages = [now - r.get("ts", now) for r in binance_rows]
    print(f"\nBinance rows: {len(binance_rows)}, age min={min(ages):.1f}s max={max(ages):.1f}s mean={sum(ages)/len(ages):.1f}s")

# Check top exchanges freshness
exchanges = {}
for r in rows:
    ex = r.get("exchange", "?")
    row_ts = r.get("ts", 0)
    if ex not in exchanges or row_ts > exchanges[ex]:
        exchanges[ex] = row_ts

print("\nExchange freshness (newest row per exchange):")
for ex, ex_ts in sorted(exchanges.items()):
    age = now - ex_ts
    status = "OK" if age < 10 else "STALE" if age < 60 else "DEAD"
    print(f"  {ex:<15} age={age:.1f}s [{status}]")
