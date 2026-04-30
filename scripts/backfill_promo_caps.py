"""One-shot backfill: cap per_user_max_uses=1 on all bonus_days promos.

Without a per-user cap, a single user can redeem the same bonus_days
code repeatedly and stack subscription days indefinitely. Tightens
existing promos to the same default new ones now get from
promo_service.create_code (default 1 when bonus_days > 0 and the
admin didn't explicitly opt out).

Idempotent — safe to re-run. Promos that already have an explicit
per_user_max_uses (any value) are left untouched. Run from the
repo root:

    python -m scripts.backfill_promo_caps          # dry-run
    python -m scripts.backfill_promo_caps --apply  # commit changes
"""
from __future__ import annotations

import argparse
import sys

from backend.db.base import SessionLocal
from backend.db.models import PromoCode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Commit changes (default: dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = (
            db.query(PromoCode)
            .filter(
                PromoCode.bonus_days > 0,
                PromoCode.per_user_max_uses.is_(None),
            )
            .all()
        )
        if not rows:
            print("No leaky promos found — nothing to do.")
            return 0

        print(f"Found {len(rows)} promos with bonus_days > 0 and per_user_max_uses=NULL:")
        print(f"{'code':<20}{'discount_pct':<14}{'bonus_days':<12}{'used_count':<12}{'is_active'}")
        print("-" * 68)
        for r in rows:
            print(f"{r.code:<20}{str(r.discount_pct):<14}"
                  f"{str(r.bonus_days):<12}{str(r.used_count):<12}{r.is_active}")

        if not args.apply:
            print()
            print("(dry-run) Re-run with --apply to set per_user_max_uses=1 on each.")
            return 0

        for r in rows:
            r.per_user_max_uses = 1
        db.commit()
        print()
        print(f"Updated {len(rows)} promo(s) — per_user_max_uses=1.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
