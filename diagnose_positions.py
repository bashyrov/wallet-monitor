"""
Diagnose position/balance fetch latency.
Connects to the positions API endpoint (no real trades needed — just times the call).
Usage: python3 diagnose_positions.py <jwt>
"""
import asyncio, json, sys, time, urllib.request, urllib.error

JWT = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "https://avalant.xyz"

def api_get(path, jwt=""):
    req = urllib.request.Request(f"{BASE}{path}")
    if jwt:
        req.add_header("Authorization", f"Bearer {jwt}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
            elapsed = time.time() - t0
            return data, elapsed
    except urllib.error.HTTPError as e:
        return {"error": e.code, "reason": str(e)}, time.time() - t0
    except Exception as e:
        return {"error": str(e)}, time.time() - t0

print("=== Position/Balance API Latency Diagnosis ===\n")

# 1. Check /api/trade/status (shows which wallets are configured)
print("1. /api/trade/status")
data, t = api_get("/api/trade/status", JWT)
if "error" in data:
    print(f"   ERROR: {data}")
else:
    wallets = data.get("wallets", [])
    print(f"   {t*1000:.0f}ms — {len(wallets)} trading wallets")
    for w in wallets[:5]:
        print(f"   {w.get('exchange','?')}: {w.get('name','?')}")

print()

# 2. Check /api/trade/positions latency (3 runs)
print("2. /api/trade/positions (3 runs)")
times = []
for i in range(3):
    data, t = api_get("/api/trade/positions", JWT)
    times.append(t)
    status = "OK" if "error" not in data else f"ERR:{data.get('error')}"
    print(f"   Run {i+1}: {t*1000:.0f}ms — {status}")

if times:
    import statistics
    print(f"   Median: {statistics.median(times)*1000:.0f}ms, Min: {min(times)*1000:.0f}ms, Max: {max(times)*1000:.0f}ms")

print()

# 3. Check /api/trade/balances latency
print("3. /api/trade/balances (3 runs)")
times2 = []
for i in range(3):
    data, t = api_get("/api/trade/balances", JWT)
    times2.append(t)
    status = "OK" if "error" not in data else f"ERR:{data.get('error')}"
    print(f"   Run {i+1}: {t*1000:.0f}ms — {status}")

if times2:
    print(f"   Median: {statistics.median(times2)*1000:.0f}ms, Min: {min(times2)*1000:.0f}ms, Max: {max(times2)*1000:.0f}ms")

print()
print("=== Summary ===")
if times:
    p_med = statistics.median(times)*1000
    b_med = statistics.median(times2)*1000 if times2 else 0
    print(f"Positions p50: {p_med:.0f}ms")
    print(f"Balances  p50: {b_med:.0f}ms")
    if p_med < 300:
        print("Status: FAST (WS user-stream short-circuit likely active)")
    elif p_med < 1000:
        print("Status: MODERATE (REST with parallel wallets)")
    else:
        print("Status: SLOW (REST serial or timeouts)")
