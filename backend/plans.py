"""Plan definitions shared across the backend."""

PLAN_LIMITS: dict[str, int | None] = {
    "basic":      4,
    "pro":        30,
    "platinum":   70,
    "enterprise": None,   # custom / unlimited
    "unlim":      None,   # admin-only unlimited
}

VALID_PLANS = set(PLAN_LIMITS.keys())
# Plans that can only be assigned to admin users
ADMIN_ONLY_PLANS = {"unlim"}


def wallet_limit(plan: str) -> int | None:
    """Return wallet limit for a plan, or None if unlimited."""
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["basic"])
