"""Formal interface for trade adapters.

Registration in `ADAPTERS` happens in __init__.py. This ABC exists so
missing methods are caught at import time, not the first time a user
triggers an unused code path. All current adapters are classmethod-based
(stateless, creds passed per-call) so we don't subclass — we check at
ADAPTERS wiring via `_verify_adapter()`.

The verification-at-wire pattern (rather than inheritance) is cheap and
keeps the existing classmethod style intact. It catches typos, missing
methods, and signature drift without forcing every adapter to inherit.
"""
from __future__ import annotations

import inspect
from typing import Protocol


class TradeAdapter(Protocol):
    """Documented interface — classmethod-based.
    See _verify_adapter() for runtime checks."""

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict: ...

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str,
                           leverage: int, margin_mode: str) -> None: ...

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict: ...

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict: ...

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]: ...


REQUIRED_METHODS = (
    ("fetch_balance",   ["cls", "creds"]),
    ("set_leverage",    ["cls", "creds", "symbol", "leverage", "margin_mode"]),
    ("place_order",     ["cls", "creds", "symbol", "side", "quantity"]),
    ("close_position",  ["cls", "creds", "symbol", "side"]),
    ("list_positions",  ["cls", "creds"]),
)


def verify_adapter(name: str, adapter: type) -> None:
    """Raise ImportError with a clear message if the adapter class is
    missing required methods or has wrong signatures. Called at module
    load from backend/services/trade_adapters/__init__.py."""
    errors: list[str] = []
    for method_name, required_params in REQUIRED_METHODS:
        fn = getattr(adapter, method_name, None)
        if fn is None:
            errors.append(f"missing method {method_name}()")
            continue
        if not inspect.iscoroutinefunction(fn) and not inspect.iscoroutinefunction(
            getattr(fn, "__func__", fn)
        ):
            errors.append(f"{method_name}() must be an async def")
            continue
        try:
            sig = inspect.signature(getattr(fn, "__func__", fn))
        except (TypeError, ValueError):
            continue
        params = list(sig.parameters.keys())
        # The first required param is always "cls" — classmethod.
        for req in required_params:
            if req not in params:
                errors.append(f"{method_name}() missing parameter `{req}`")
    if errors:
        raise ImportError(
            f"TradeAdapter {name} ({adapter.__name__}) failed interface check:\n  - "
            + "\n  - ".join(errors)
        )
