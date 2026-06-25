"""Legacy plan-string fallbacks. Source of truth is the `plans` DB table
(see backend/services/plan_service.py + backend/db/models.Plan); this
module survives only for code paths that still touch the legacy
`users.plan` string column. New callers should use
plan_service.effective_limits(db, user) which reads users.plan_id.

Active plan slugs (per /pricing page):
    free        — $0/mo, 5 wallets, +500ms trade delay
    screener    — $45/mo, screener only (no portfolio)
    full        — $55/mo, screener + 30 portfolio wallets
    unlim       — admin-only, unlimited (not for sale)

Legacy slugs (pro/platinum/enterprise/basic) were removed when
inactive rows got cleaned up — see the corresponding alembic
migration. `basic` had been the registration default before plan_id
became authoritative; remaining `users.plan='basic'` rows were
migrated to 'free' in the same migration.
"""

PLAN_LIMITS: dict[str, int | None] = {
    "free":     5,
    "screener": 0,        # screener-only, no portfolio
    "full":     30,
    "unlim":    None,     # admin-only unlimited
}

VALID_PLANS = set(PLAN_LIMITS.keys())
# Plans that can only be assigned to admin users.
ADMIN_ONLY_PLANS = {"unlim"}


def wallet_limit(plan: str) -> int | None:
    """Return wallet limit for a plan slug, or None if unlimited.
    Falls back to free-tier limit for unknown slugs so a typo never
    accidentally grants a higher cap."""
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
