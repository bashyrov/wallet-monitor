import sys, json

gate = []
paradex = []
bitget = []

for line in sys.stdin:
    try:
        o = json.loads(line)
        ex = o.get("exchange", "")
        p = o.get("preview", "")
        if ex == "gate" and "book_ticker" in p and len(gate) < 2:
            gate.append(p)
        if ex == "paradex" and "bbo." in p and len(paradex) < 2:
            paradex.append(p)
        if ex == "bitget" and len(bitget) < 2:
            bitget.append(p)
    except:
        pass

print("=== GATE book_ticker ===")
for f in gate:
    print(f)
print("\n=== PARADEX bbo ===")
for f in paradex:
    print(f)
print("\n=== BITGET ===")
for f in bitget:
    print(f)
