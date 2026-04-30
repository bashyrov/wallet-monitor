"""
RetryClient — httpx.AsyncClient with exponential backoff on transient errors.

Retries on:
  · HTTP 429 (rate limit) — honours Retry-After header
  · HTTP 5xx (server errors)
  · httpx.ReadTimeout / WriteTimeout / ConnectTimeout
  · httpx.ConnectError / RemoteProtocolError

All use the same backoff schedule. After `max_retries` attempts the last
response/exception is returned/raised as-is.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger("avalant.http")

_DEFAULT_INITIAL_WAIT = 0.5   # seconds
_DEFAULT_MAX_WAIT     = 8.0   # seconds
_DEFAULT_MAX_RETRIES  = 3

# Network-level exceptions worth retrying. NOT including HTTPError/StatusError
# because those wrap status codes we already handle by looking at response.
_RETRYABLE_EXC = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


class RetryClient(httpx.AsyncClient):
    def __init__(
        self,
        *args,
        initial_wait: float = _DEFAULT_INITIAL_WAIT,
        max_wait: float = _DEFAULT_MAX_WAIT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        **kwargs,
    ):
        # Default to a generous keepalive pool so repeat requests to the
        # same host (e.g. one user-stream listenKey + many position pulls
        # to fapi.binance.com) reuse the established TCP+TLS connection
        # instead of re-handshaking. Caller can override by passing their
        # own `limits=`.
        kwargs.setdefault(
            "limits",
            httpx.Limits(max_connections=100,
                         max_keepalive_connections=40,
                         keepalive_expiry=60.0),
        )
        super().__init__(*args, **kwargs)
        self._initial_wait = initial_wait
        self._max_wait = max_wait
        self._max_retries = max_retries

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        wait = self._initial_wait
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await super().send(request, **kwargs)
            except _RETRYABLE_EXC as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise
                logger.warning(
                    "%s on %s — retrying in %.1fs (%d/%d)",
                    type(exc).__name__, request.url.host, wait,
                    attempt + 1, self._max_retries,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2, self._max_wait)
                continue

            # 429 — honour Retry-After
            if response.status_code == 429 and attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After")
                rw = wait
                if retry_after:
                    try:
                        rw = min(float(retry_after), self._max_wait)
                    except ValueError:
                        pass
                logger.warning(
                    "429 from %s — waiting %.1fs before retry %d/%d",
                    request.url.host, rw, attempt + 1, self._max_retries,
                )
                await asyncio.sleep(rw)
                wait = min(wait * 2, self._max_wait)
                continue

            # 5xx — retry with backoff (servers get a moment to recover)
            if 500 <= response.status_code < 600 and attempt < self._max_retries:
                logger.warning(
                    "%d from %s — retrying in %.1fs (%d/%d)",
                    response.status_code, request.url.host, wait,
                    attempt + 1, self._max_retries,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2, self._max_wait)
                continue

            return response

        if last_exc is not None:
            raise last_exc
        return response  # unreachable, satisfies type checker
