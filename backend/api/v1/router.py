from fastapi import APIRouter

from backend.api.v1 import health, wallets, tags, portfolio, auth, admin, screener, alerts

router = APIRouter(prefix="/api")

router.include_router(auth.router)
router.include_router(health.router)
router.include_router(wallets.router)
router.include_router(tags.router)
router.include_router(portfolio.router)
router.include_router(admin.router)
router.include_router(screener.router)
router.include_router(alerts.router)
