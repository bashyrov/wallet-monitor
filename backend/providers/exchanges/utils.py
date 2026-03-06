from decimal import Decimal


def merge_sum(*parts):
    totals = {}
    for part in parts:
        for k, v in part.items():
            if v > 0:
                totals[k] = totals.get(k, Decimal("0")) + v

    return {k: str(v) for k, v in totals.items() if v > 0}