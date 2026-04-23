"""Import / syntax smoke tests.

Catches the class of failure that slipped through on 2026-04-22 — an
IndentationError inside a rarely-touched handler brought the whole web
role down, visible only after a `docker compose up -d` on prod.

Every module under backend/ and the app entry point is ast-parsed and
imported here. Any SyntaxError / IndentationError / ImportError fails CI
before it reaches deploy.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _py_files(root: Path):
    """Every .py under `root`, excluding venv / __pycache__ / build dirs."""
    for p in root.rglob("*.py"):
        parts = set(p.parts)
        if parts & {"venv", ".venv", "__pycache__", "node_modules", "frontend-next", "build", "dist"}:
            continue
        yield p


def _backend_modules():
    """Iterate every importable module under backend/ + top-level entry points."""
    yield "app"
    import backend
    for _finder, name, _ispkg in pkgutil.walk_packages(backend.__path__, prefix="backend."):
        yield name


@pytest.mark.parametrize("path", list(_py_files(ROOT)), ids=lambda p: str(p.relative_to(ROOT)))
def test_ast_parses(path: Path) -> None:
    """Every .py file in the repo must be syntactically valid Python.

    Regression guard: we had an IndentationError shipped to prod because the
    file was never parsed before deploy. `ast.parse` covers that class
    without actually importing the module (side-effect free)."""
    try:
        ast.parse(path.read_text())
    except SyntaxError as e:
        pytest.fail(f"{path} :: {e}")


# Modules whose import path touches a live WebSocket / external API at
# module level (e.g. `python-binance` opens a websocket listener). Importing
# them from a test would hang the whole CI job until the --timeout kicks in.
# We ast-parse them in test_ast_parses above — that's the real regression
# guard. A full import is only needed to catch non-syntax-level bugs, which
# these modules don't tend to have (adapters are thin wrappers).
_IMPORT_SKIP_PREFIXES = (
    "backend.providers.exchanges.binance_provider",  # python-binance socket manager
)


@pytest.mark.parametrize("modname", list(_backend_modules()))
def test_module_imports(modname: str) -> None:
    """Every backend module must import without error.

    This catches a broader class of issues than AST parsing: bad relative
    imports, module-level exceptions, circular imports, missing third-party
    deps that only fire when something actually loads the module."""
    if any(modname.startswith(p) for p in _IMPORT_SKIP_PREFIXES):
        pytest.skip(f"known module-level side-effect; covered by ast-parse")
    try:
        importlib.import_module(modname)
    except ImportError as e:
        # Skip modules whose optional third-party deps aren't installed in the
        # CI image — they're expected to fail on minimal installs.
        msg = str(e).lower()
        if any(pkg in msg for pkg in ("ethereal", "solders")):
            pytest.skip(f"optional dep: {e}")
        raise
