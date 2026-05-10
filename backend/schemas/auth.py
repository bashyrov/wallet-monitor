from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator, model_validator


class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str
    referral_code: Optional[str] = None

    @field_validator("username")
    @classmethod
    def username_length(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        return v

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v

    @field_validator("referral_code")
    @classmethod
    def referral_code_clean(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if not v:
            return None
        if len(v) > 16:
            raise ValueError("Referral code too long")
        return v


class UserLogin(BaseModel):
    login: str   # email or username
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    recovery_used: bool | None = None


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    is_admin: bool
    plan: str = "basic"
    plan_id: Optional[int] = None
    plan_expires_at: Optional[datetime] = None
    wallet_limit: Optional[int] = None   # alias for portfolio_limit (legacy)
    portfolio_limit: Optional[int] = None
    exchange_keys_per_venue: Optional[int] = None
    is_plan_expired: bool = False
    tg_username: Optional[str] = None
    tg_linked: bool = False          # True if tg_chat_id is set (user ran /start)
    totp_enabled: bool = False       # True when the user has armed TOTP 2FA
    auto_renew: bool = True          # False = user clicked Cancel — we stop expiry pings
    created_at: datetime
    email_verified_at: Optional[datetime] = None  # None = unverified; frontend shows a banner

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _fill_derived(self) -> "UserOut":
        # Legacy fallback: wallet_limit was hard-coded by plan slug. With
        # the DB-driven plan system the source of truth is /api/plans;
        # downstream callers that pre-date plan_id keep working via the
        # `plan` slug → static lookup, but if /api/auth/me is enriched
        # with plan_service.effective_limits in the route handler the
        # wallet_limit / portfolio_limit fields end up identical.
        if self.wallet_limit is None and self.portfolio_limit is not None:
            self.wallet_limit = self.portfolio_limit
        return self
