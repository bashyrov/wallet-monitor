"""Per-exchange token contract registry.

Problem it solves: a row like 'ASTEROID' can appear in the arb feed with a
55% spread between Binance and Aster because they list DIFFERENT tokens
under the same ticker. Price never converges and the opp flickers every
cycle. Net profit looks great but trading it is impossible — you'd buy
Token A on one venue and try to short Token B on the other.

Approach:
  · For every CEX that exposes token chain + contract-address info on a
    public endpoint (KuCoin, Gate, Bitget — Binance/Bybit require auth and
    are deliberately skipped), cache a map:
        {exchange: {SYMBOL: {chain: contract_address}}}
  · Refresh once every REGISTRY_TTL_S (24h is plenty — tokens don't
    move chains / re-deploy often).
  · Expose `validate_pair_identity(symbol, ex_a, ex_b)` → True / False /
    None. True when the two venues have at least one overlapping
    (chain, contract) tuple. None when we just don't have data for one
    or both — caller decides whether to be conservative.
  · Result is memoised in `_pair_verdict_cache` so a 55%-spread opp with
    a matching contract doesn't re-check every cycle.

Performance: the registry refresh runs once a day in a thread; each
fetch is a single GET against a public endpoint. Validation during arb
compute is one dict lookup per high-spread pair — effectively free.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

import httpx

logger = logging.getLogger("avalant.token_registry")

_CACHE_DIR = "/tmp/avalant_cache"
_REGISTRY_FILE = os.path.join(_CACHE_DIR, "token_registry.json")
REGISTRY_TTL_S = 86_400.0       # 24h — full refresh cadence
_PAIR_VERDICT_TTL_S = 604_800.0 # 1 week for positive verdicts
_NEGATIVE_TTL_S = 3600.0        # 1h for rejects (tokens might get relisted)

# Dedicated sync client — token-registry fetches are bursty and we don't
# want them sharing a pool with the arb hot path.
_http = httpx.Client(
    timeout=httpx.Timeout(connect=4.0, read=8.0, write=4.0, pool=2.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=30),
    http2=False,
)

# {exchange: {SYMBOL_UPPER: {chain_lower: contract_address_lower}}}
_registry: dict[str, dict[str, dict[str, str]]] = {}
_registry_ts: float = 0.0

# Validation memo: {(symbol, ex_a, ex_b): (verdict, ts)}
# ex_a / ex_b stored alphabetically so (a,b) and (b,a) share the entry.
_pair_verdict: dict[tuple[str, str, str], tuple[bool, float]] = {}
_lock = threading.RLock()

# Chain-name aliases — different exchanges call the same chain different
# things. We compare canonical lowercase names so "ERC20" and "ETH" both
# collapse to "ethereum". Unknown aliases pass through as-is.
_CHAIN_ALIASES = {
    "erc20": "ethereum", "eth": "ethereum", "ether": "ethereum", "ethereum": "ethereum",
    "bep20": "bsc", "bsc": "bsc", "bnb smart chain": "bsc", "binance smart chain": "bsc",
    "trc20": "tron", "tron": "tron", "trx": "tron",
    "arbitrum": "arbitrum", "arb": "arbitrum", "arbitrum one": "arbitrum",
    "optimism": "optimism", "op": "optimism",
    "polygon": "polygon", "matic": "polygon",
    "base": "base",
    "avax-c": "avalanche", "avalanche c-chain": "avalanche", "avalanche": "avalanche", "avax": "avalanche",
    "solana": "solana", "sol": "solana",
    "zksync": "zksync", "zksync era": "zksync",
    "linea": "linea",
    "scroll": "scroll",
    "mantle": "mantle",
    "blast": "blast",
    "ton": "ton",
    "sui": "sui",
    "aptos": "aptos",
}


def _canon_chain(name: str | None) -> str:
    if not name:
        return ""
    k = name.strip().lower()
    return _CHAIN_ALIASES.get(k, k)


# ── Per-exchange fetchers ─────────────────────────────────────────────────────
# Each returns {SYMBOL: {chain: contract}} or {} on failure. Keep them
# defensive; a single bad response must not poison the whole registry.

def _fetch_kucoin() -> dict[str, dict[str, str]]:
    """GET /api/v3/currencies — public, returns chains[].contractAddress."""
    try:
        r = _http.get("https://api.kucoin.com/api/v3/currencies", timeout=10.0)
        items = (r.json() or {}).get("data") or []
    except Exception as exc:
        logger.warning("kucoin registry fetch: %s", exc)
        return {}
    out: dict[str, dict[str, str]] = {}
    for c in items:
        sym = (c.get("currency") or "").upper()
        if not sym:
            continue
        chains = {}
        for ch in (c.get("chains") or []):
            addr = (ch.get("contractAddress") or "").strip().lower()
            chain = _canon_chain(ch.get("chainName") or ch.get("chain") or "")
            if addr and chain:
                chains[chain] = addr
        if chains:
            out[sym] = chains
    logger.info("kucoin registry: %d symbols with on-chain info", len(out))
    return out


def _fetch_gate() -> dict[str, dict[str, str]]:
    """GET /api/v4/spot/currencies — public, rows include chains[] with
    `name` and `addr`. Single call, no per-symbol iteration needed."""
    try:
        r = _http.get("https://api.gateio.ws/api/v4/spot/currencies", timeout=12.0)
        rows = r.json() or []
    except Exception as exc:
        logger.warning("gate registry fetch: %s", exc)
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        sym = (row.get("currency") or "").upper()
        if not sym:
            continue
        chains = {}
        for ch in (row.get("chains") or []):
            addr = (ch.get("addr") or "").strip().lower()
            chain = _canon_chain(ch.get("name") or "")
            if addr and chain:
                chains[chain] = addr
        if chains:
            out[sym] = chains
    logger.info("gate registry: %d symbols with on-chain info", len(out))
    return out


def _fetch_bitget() -> dict[str, dict[str, str]]:
    """GET /api/v2/spot/public/coins — public, returns chains[]."""
    try:
        r = _http.get("https://api.bitget.com/api/v2/spot/public/coins", timeout=10.0)
        items = (r.json() or {}).get("data") or []
    except Exception as exc:
        logger.warning("bitget registry fetch: %s", exc)
        return {}
    out: dict[str, dict[str, str]] = {}
    for c in items:
        sym = (c.get("coin") or "").upper()
        if not sym:
            continue
        chains = {}
        for ch in (c.get("chains") or []):
            addr = (ch.get("contractAddress") or "").strip().lower()
            chain = _canon_chain(ch.get("chain") or "")
            if addr and chain:
                chains[chain] = addr
        if chains:
            out[sym] = chains
    logger.info("bitget registry: %d symbols", len(out))
    return out


def _fetch_binance() -> dict[str, dict[str, str]]:
    """Binance has no public `/sapi/v1/capital/config/getall` (auth gated)
    but their website hits a separate public endpoint that returns the
    same data for every coin — `bapi/capital/v1/public/capital/getNetworkCoinAll`.
    Each row has networkList[] with `network` + `contractAddress`."""
    try:
        r = _http.get(
            "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll",
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        items = (r.json() or {}).get("data") or []
    except Exception as exc:
        logger.warning("binance registry fetch: %s", exc)
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in items:
        sym = (row.get("coin") or "").upper()
        if not sym:
            continue
        chains = {}
        for n in (row.get("networkList") or []):
            addr = (n.get("contractAddress") or "").strip().lower()
            chain = _canon_chain(n.get("network") or "")
            if addr and chain:
                chains[chain] = addr
        if chains:
            out[sym] = chains
    logger.info("binance registry: %d symbols with on-chain info", len(out))
    return out


def _fetch_mexc() -> dict[str, dict[str, str]]:
    """MEXC doesn't expose contract info on public v3 coin endpoints; their
    `coinRatings` service is internal. Leaving as a stub so the fetcher
    list is uniform — the `validate_pair_identity` logic treats absence
    of data as 'unknown', not as a mismatch."""
    return {}


_FETCHERS = {
    "binance": _fetch_binance,
    "kucoin":  _fetch_kucoin,
    "gate":    _fetch_gate,
    "bitget":  _fetch_bitget,
    # Unavailable (auth required / no public data):
    # 'bybit': …     # /v5/asset/coin/query-info — auth required
    # 'okx': …       # no public contract info
    # 'mexc': …      # internal endpoint only
    # 'bingx': …     # auth required
    # 'whitebit': …  # has chain names only, no contract addresses
}


# ── Public API ────────────────────────────────────────────────────────────────
def _refresh_once() -> None:
    """Repopulate _registry from all configured fetchers. Called by the
    background thread + on startup. Runs fetchers serially — 3 calls
    total, each < 10s, well within the daily budget."""
    global _registry, _registry_ts
    collected: dict[str, dict[str, dict[str, str]]] = {}
    for ex, fn in _FETCHERS.items():
        try:
            collected[ex] = fn()
        except Exception as exc:
            logger.warning("token registry fetch (%s) failed: %s", ex, exc)
    with _lock:
        _registry = collected
        _registry_ts = time.time()
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _REGISTRY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": _registry_ts, "registry": collected}, f, separators=(",", ":"))
        os.replace(tmp, _REGISTRY_FILE)
    except Exception as exc:
        logger.debug("token registry persist failed: %s", exc)


def _load_from_disk() -> None:
    """Warm start — read the last persisted registry so new processes
    don't block on the first arb cycle waiting for the network fetch."""
    global _registry, _registry_ts
    try:
        with open(_REGISTRY_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    with _lock:
        _registry = data.get("registry") or {}
        _registry_ts = float(data.get("ts") or 0.0)


def validate_pair_identity(symbol: str, ex_a: str, ex_b: str) -> Optional[bool]:
    """Are these two venues actually listing the same token under this symbol?

    Returns:
      True  — at least one (chain, contract) pair matches across both.
      False — both venues have the symbol but no chain/contract in common
              → different tokens (ticker collision), drop the opp.
      None  — we don't have data for at least one venue. Caller picks
              the policy (the arb compute path treats None as 'allow'
              for spreads within normal range, 'reject' for huge spreads).
    """
    sym = symbol.upper()
    a, b = sorted([ex_a.lower(), ex_b.lower()])
    key = (sym, a, b)
    now = time.time()
    with _lock:
        cached = _pair_verdict.get(key)
        if cached is not None:
            verdict, ts = cached
            ttl = _PAIR_VERDICT_TTL_S if verdict else _NEGATIVE_TTL_S
            if now - ts < ttl:
                return verdict

    with _lock:
        reg_a = _registry.get(a, {}).get(sym)
        reg_b = _registry.get(b, {}).get(sym)
    if not reg_a or not reg_b:
        # Unknown — could be either (a) we don't cover this exchange (Binance,
        # Bybit, …) or (b) one side genuinely doesn't list the token.
        return None

    match = False
    for chain in reg_a:
        if chain in reg_b and reg_a[chain] == reg_b[chain]:
            match = True
            break
    with _lock:
        _pair_verdict[key] = (match, now)
    return match


def registry_snapshot() -> dict:
    """Diag view for /api/metrics or ops debugging."""
    with _lock:
        return {
            "ts": _registry_ts,
            "age_s": round(time.time() - _registry_ts, 1) if _registry_ts else None,
            "exchanges": {ex: len(syms) for ex, syms in _registry.items()},
            "pair_verdicts": len(_pair_verdict),
        }


# ── Background refresh loop ───────────────────────────────────────────────────
_thread: threading.Thread | None = None
_stop_evt: threading.Event | None = None


def _loop(stop_evt: threading.Event) -> None:
    logger.info("token registry loop started (TTL=%.0fh)", REGISTRY_TTL_S / 3600)
    _load_from_disk()
    while not stop_evt.is_set():
        try:
            if time.time() - _registry_ts >= REGISTRY_TTL_S:
                _refresh_once()
        except Exception as exc:
            logger.warning("token registry pass failed: %s", exc)
        stop_evt.wait(3600.0)   # poll every hour; fetch gated by TTL


def start_token_registry() -> None:
    global _thread, _stop_evt
    if _thread and _thread.is_alive():
        return
    _stop_evt = threading.Event()
    _thread = threading.Thread(target=_loop, args=(_stop_evt,),
                               name="token-registry", daemon=True)
    _thread.start()


def stop_token_registry() -> None:
    global _thread, _stop_evt
    if _stop_evt:
        _stop_evt.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3.0)
    _thread = None
    _stop_evt = None
