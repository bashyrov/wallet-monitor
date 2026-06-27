"""Public metadata endpoints — used by landing/index/checkout pages to
render counts ('14 chains, 13 CEX, 5 perp DEX') and by /screener and
/arb to drive their exchange filter dropdowns from one source instead
of hard-coded JS arrays."""

import os

from fastapi import APIRouter

from backend.services.venues import get_venues_meta

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/server-ip")
def server_ip():
    """Outbound IP the platform uses for venue API calls. Surfaced on the
    Add-key form so users know which IP to whitelist on the exchange.
    Set via AVALANT_SERVER_IP env var; empty string when unset (frontend
    hides the row in that case)."""
    return {"ip": (os.environ.get("AVALANT_SERVER_IP") or "").strip()}


@router.get("/venues")
def venues():
    """Live composition of supported venues. Stable shape:

        {
          screener: {
            cex: [{id, label}, …],
            perp_dex: [{id, label}, …],
            spot: [{id, label}, …],
          },
          portfolio: {
            cex: [{id, label, enabled}, …],
            perp_dex: [{id, label, enabled}, …],
            chains: [{id, label, enabled}, …],
          },
          counts: { …all the above lengths… }
        }

    Cached client-side via Cache-Control. Served by either replica."""
    return get_venues_meta()
