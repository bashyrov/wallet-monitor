import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from backend.api.deps import get_current_user
from backend.services.arbitrage_service import get_arbitrage_opportunities, get_funding_data
from backend.services.auth_service import decode_token

router = APIRouter(prefix="/screener", tags=["screener"])
logger = logging.getLogger("avalant.screener")

# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/funding")
async def funding_rates(_=Depends(get_current_user)):
    """Funding rates across perpetual futures exchanges. Cached 30s per exchange."""
    return await get_funding_data()


@router.get("/arbitrage")
async def arbitrage_opportunities(_=Depends(get_current_user)):
    """Cross-exchange funding arbitrage opportunities with price spread and fees."""
    return await get_arbitrage_opportunities()


# ── WebSocket: live funding rates ──────────────────────────────────────────────

_funding_clients: set[WebSocket] = set()
_arb_clients: set[WebSocket] = set()
_broadcaster_task: asyncio.Task | None = None
BROADCAST_INTERVAL = 30  # seconds


async def _push(clients: set[WebSocket], msg: str) -> None:
    dead: set[WebSocket] = set()
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    clients -= dead
    if dead:
        logger.debug("Screener WS: removed %d dead connections", len(dead))


async def _broadcast_loop() -> None:
    """Every 30s refresh both data sets and push to subscribed clients."""
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        if _funding_clients:
            try:
                data = await get_funding_data()
                await _push(_funding_clients, json.dumps(data))
                logger.debug("Screener funding WS: pushed to %d clients", len(_funding_clients))
            except Exception as exc:
                logger.warning("Screener funding broadcast error: %s", exc)
        if _arb_clients:
            try:
                data = await get_arbitrage_opportunities()
                await _push(_arb_clients, json.dumps(data))
                logger.debug("Screener arb WS: pushed to %d clients", len(_arb_clients))
            except Exception as exc:
                logger.warning("Screener arb broadcast error: %s", exc)


def start_screener_broadcaster() -> None:
    global _broadcaster_task
    _broadcaster_task = asyncio.create_task(_broadcast_loop())
    logger.info("Screener broadcaster started")


def stop_screener_broadcaster() -> None:
    global _broadcaster_task
    if _broadcaster_task:
        _broadcaster_task.cancel()
        _broadcaster_task = None


async def _ws_handler(websocket: WebSocket, clients: set[WebSocket], token: str,
                      fetch_fn, label: str) -> None:
    user_id = decode_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    clients.add(websocket)
    logger.debug("Screener %s WS connect uid=%s (total=%d)", label, user_id, len(clients))

    try:
        data = await fetch_fn()
        await websocket.send_json(data)
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("Screener %s WS error uid=%s: %s", label, user_id, exc)
    finally:
        clients.discard(websocket)
        logger.debug("Screener %s WS disconnect uid=%s (total=%d)", label, user_id, len(clients))


@router.websocket("/ws/funding")
async def funding_ws(websocket: WebSocket, token: str = Query(...)) -> None:
    await _ws_handler(websocket, _funding_clients, token, get_funding_data, "funding")


@router.websocket("/ws/arb")
async def arb_ws(websocket: WebSocket, token: str = Query(...)) -> None:
    await _ws_handler(websocket, _arb_clients, token, get_arbitrage_opportunities, "arb")
