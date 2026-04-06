"""
RetryClient — httpx.AsyncClient with exponential backoff on 429.

On HTTP 429 the client waits `initial_wait` seconds, then retries.
Each subsequent 429 doubles the wait (capped at `max_wait`).
After `max_retries` attempts it returns the last 429 response as-is.

Usage: drop-in replacement for httpx.AsyncClient.
  client = RetryClient(timeout=20)
  async with RetryClient(timeout=20) as client:
      ...
"""
import asyncio
import logging

import httpx

logger = logging.getLogger("avalant.http")

_DEFAULT_INITIAL_WAIT = 1.0   # seconds
_DEFAULT_MAX_WAIT     = 16.0  # seconds
_DEFAULT_MAX_RETRIES  = 4


class RetryClient(httpx.AsyncClient):
    def __init__(
        self,
        *args,
        initial_wait: float = _DEFAULT_INITIAL_WAIT,
        max_wait: float = _DEFAULT_MAX_WAIT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._initial_wait = initial_wait
        self._max_wait = max_wait
        self._max_retries = max_retries

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        wait = self._initial_wait
        for attempt in range(self._max_retries + 1):
            response = await super().send(request, **kwargs)
            if response.status_code != 429 or attempt == self._max_retries:
                return response

            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(float(retry_after), self._max_wait)
                except ValueError:
                    pass

            logger.warning(
                "429 from %s — waiting %.1fs before retry %d/%d",
                request.url.host, wait, attempt + 1, self._max_retries,
            )
            await asyncio.sleep(wait)
            wait = min(wait * 2, self._max_wait)

        return response  # unreachable, satisfies type checker
