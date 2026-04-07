from collections import defaultdict
from decimal import Decimal


STABLE_COINS = (
    "USD", "USDT", "USDC", "USDC.E", "USDCE",
    "DAI", "USDE", "BUSD", "TUSD", "USDP", "USDD",
    "FDUSD", "PYUSD",
)


def pretty_print_balances(result):
    totals = result.totals or {}

    stable = {}
    custom = {}

    for asset, amount in totals.items():
        amt = Decimal(amount)

        if asset.upper() in STABLE_COINS:
            stable[asset] = amt
        else:
            custom[asset] = amt

    stable_sum = sum(stable.values(), Decimal("0"))

    print("\n==============================")
    print(f"[{result.provider}] {result.wallet.name}")

    print("\n💵 Stable Coins:")
    for k, v in stable.items():
        print(f"  {k}: {v}")

    print(f"  TOTAL: {stable_sum}")

    print("\n🪙 Custom Tokens:")
    for k, v in custom.items():
        print(f"  {k}: {v}")

