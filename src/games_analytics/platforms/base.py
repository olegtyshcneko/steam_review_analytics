from __future__ import annotations

import asyncio
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


USER_AGENT = "games-analytics/0.2 (public review research; contact via GitHub)"


class SourceError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class HttpSource:
    def __init__(self, requests_per_second: float, max_retries: int, timeout: float = 30):
        self.limiter = RateLimiter(requests_per_second)
        self.max_retries = max_retries
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | bytes | None = None,
    ) -> str:
        for attempt in range(self.max_retries + 1):
            await self.limiter.wait()
            try:
                response = await self.client.request(method, url, params=params, data=data)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self.max_retries:
                        raise SourceError(f"HTTP {response.status_code} from {url}", response.status_code)
                    retry_after = _retry_after(response.headers.get("Retry-After"))
                    await asyncio.sleep(
                        retry_after if retry_after is not None else min(30, 2**attempt + random.random())
                    )
                    continue
                response.raise_for_status()
                return response.text
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_retries:
                    raise SourceError(f"Request failed for {url}: {exc}") from exc
                await asyncio.sleep(min(30, 2**attempt + random.random()))
            except httpx.HTTPStatusError as exc:
                raise SourceError(f"Invalid response from {url}: {exc}", exc.response.status_code) from exc
        raise AssertionError("unreachable")

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict:
        text = await self.request_text("GET", url, params=params)
        try:
            payload = httpx.Response(200, text=text).json()
        except ValueError as exc:
            raise SourceError(f"Invalid JSON response from {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"Expected JSON object from {url}")
        return payload


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from datetime import datetime, timezone

            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None
