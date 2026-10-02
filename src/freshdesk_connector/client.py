"""Async Freshdesk REST v2 client: auth, rate limiting, retries, error mapping, pagination."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import __version__
from .auth import basic_auth_header
from .config import Settings
from .errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
    ValidationError,
)
from .rate_limit import TokenBucket

log = logging.getLogger("freshdesk_connector")

_RETRYABLE_STATUS = {500, 502, 503, 504}
_FIELDS_TTL_SECONDS = 600.0


@dataclass
class CallStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    last_rate_total: int | None = None
    last_rate_remaining: int | None = None
    last_latency_ms: float | None = None
    by_endpoint: dict[str, int] = field(default_factory=dict)


@dataclass
class Page:
    items: list[dict[str, Any]]
    page: int
    per_page: int
    has_more: bool
    total: int | None = None


def _int_header(resp: httpx.Response, name: str) -> int | None:
    raw = resp.headers.get(name)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _retry_after(resp: httpx.Response, default: float = 5.0) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else default
    except ValueError:
        return default


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            parts = [
                f"{e.get('field', '?')}: {e.get('message', e.get('code', 'invalid'))}"
                for e in errors
                if isinstance(e, dict)
            ]
            return "; ".join(parts)
        return str(body.get("description") or body.get("message") or body)[:300]
    return str(body)[:300]


class FreshdeskClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self.limiter = TokenBucket(settings.rate_limit_per_minute, sleep=sleep, clock=clock)
        self.stats = CallStats()
        self._fields_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            transport=transport,
            timeout=settings.timeout_seconds,
            headers={
                "Authorization": basic_auth_header(settings.api_key),
                "Accept": "application/json",
                "User-Agent": f"razorpay-agent-studio-freshdesk-connector/{__version__}",
            },
            follow_redirects=False,  # never forward the auth header elsewhere
        )

    async def __aenter__(self) -> "FreshdeskClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    def _backoff(self, attempt: int) -> float:
        return min(8.0, 0.5 * (2**attempt)) * random.uniform(0.5, 1.0)

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        waited_on_429 = 0.0
        endpoint = path.split("?", 1)[0]
        while True:
            await self.limiter.acquire()
            started = time.perf_counter()
            try:
                resp = await self._http.request(method, path, params=clean)
            except httpx.TransportError as exc:
                if attempt >= self.settings.max_retries:
                    raise UpstreamError(
                        f"Could not reach Freshdesk after {attempt + 1} attempts ({type(exc).__name__}). "
                        "Try again shortly."
                    ) from exc
                self.stats.retries += 1
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            latency_ms = (time.perf_counter() - started) * 1000
            total = _int_header(resp, "X-RateLimit-Total")
            remaining = _int_header(resp, "X-RateLimit-Remaining")
            self.limiter.observe(total, remaining)
            self.stats.requests += 1
            self.stats.by_endpoint[endpoint] = self.stats.by_endpoint.get(endpoint, 0) + 1
            self.stats.last_latency_ms = round(latency_ms, 1)
            if total is not None:
                self.stats.last_rate_total = total
            if remaining is not None:
                self.stats.last_rate_remaining = remaining
            log.info(
                "freshdesk %s %s -> %s in %.0fms (remaining=%s)",
                method, endpoint, resp.status_code, latency_ms, remaining,
            )

            status = resp.status_code
            if status == 429:
                self.stats.rate_limited += 1
                wait = _retry_after(resp)
                self.limiter.block_for(wait)
                if attempt >= self.settings.max_retries or waited_on_429 + wait > self.settings.max_retry_wait_seconds:
                    raise RateLimitedError(
                        f"Freshdesk rate limit reached; retry after ~{int(wait)}s. "
                        "Reduce page sizes or the number of parallel calls.",
                        retry_after=wait,
                    )
                self.stats.retries += 1
                waited_on_429 += wait
                await self._sleep(wait)
                attempt += 1
                continue
            if status in _RETRYABLE_STATUS:
                if attempt >= self.settings.max_retries:
                    raise UpstreamError(f"Freshdesk returned HTTP {status} after {attempt + 1} attempts.")
                self.stats.retries += 1
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            if status == 401:
                raise AuthenticationError(
                    "Freshdesk rejected the API key (401). Re-run `freshdesk-connector auth login` "
                    "or update FRESHDESK_API_KEY."
                )
            if status == 403:
                raise AuthenticationError(
                    "This Freshdesk API key is not allowed to access that resource (403). "
                    f"Detail: {_error_detail(resp)}"
                )
            if status == 404:
                raise NotFoundError(f"Not found in Freshdesk: {endpoint}")
            if status in (400, 422):
                raise ValidationError(f"Freshdesk rejected the request: {_error_detail(resp)}")
            if status >= 400:
                raise UpstreamError(f"Unexpected Freshdesk response HTTP {status}: {_error_detail(resp)}")
            return resp

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request("GET", path, params)).json()

    async def _get_page(self, path: str, params: dict[str, Any], page: int, per_page: int) -> Page:
        resp = await self._request("GET", path, {**params, "page": page, "per_page": per_page})
        items = resp.json()
        return Page(items=items, page=page, per_page=per_page, has_more="next" in resp.links)

    # ------------------------------------------------------------------ endpoints

    async def get_me(self) -> dict[str, Any]:
        return await self._get_json("/agents/me")

    async def list_tickets(self, params: dict[str, Any], page: int, per_page: int) -> Page:
        return await self._get_page("/tickets", params, page, per_page)

    async def get_ticket(self, ticket_id: int, include: list[str]) -> dict[str, Any]:
        params = {"include": ",".join(include)} if include else None
        return await self._get_json(f"/tickets/{int(ticket_id)}", params)

    async def list_conversations(self, ticket_id: int, page: int, per_page: int) -> Page:
        return await self._get_page(f"/tickets/{int(ticket_id)}/conversations", {}, page, per_page)

    async def search_tickets(self, query: str, page: int) -> Page:
        # The search API wants the whole query wrapped in double quotes.
        resp = await self._request("GET", "/search/tickets", {"query": f'"{query}"', "page": page})
        body = resp.json()
        results = body.get("results", [])
        total = int(body.get("total", len(results)))
        # Fixed page size of 30; Freshdesk serves at most 10 pages (300 results).
        has_more = page * 30 < min(total, 300)
        return Page(items=results, page=page, per_page=30, has_more=has_more, total=total)

    async def get_contact(self, contact_id: int) -> dict[str, Any]:
        return await self._get_json(f"/contacts/{int(contact_id)}")

    async def list_contacts(self, params: dict[str, Any], page: int, per_page: int) -> Page:
        return await self._get_page("/contacts", params, page, per_page)

    async def get_ticket_fields(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._fields_cache and now - self._fields_cache[0] < _FIELDS_TTL_SECONDS:
            return self._fields_cache[1]
        fields = await self._get_json("/ticket_fields")
        self._fields_cache = (now, fields)
        return fields
