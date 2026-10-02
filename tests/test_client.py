import base64

import httpx
import pytest

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.config import ConfigurationError, Settings, normalize_domain
from freshdesk_connector.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
    ValidationError,
)
from freshdesk_connector.mock import MockFreshdesk
from freshdesk_connector.rate_limit import TokenBucket


def scripted(responses):
    """Transport that replays a list of (status, headers, json) and records requests."""
    seen = []

    def handler(request):
        seen.append(request)
        status, headers, body = responses[min(len(seen), len(responses)) - 1]
        return httpx.Response(status, json=body, headers=headers)

    return httpx.MockTransport(handler), seen


async def test_sends_basic_auth_with_api_key(settings, fake_sleep):
    transport, seen = scripted([(200, {}, {"id": 1})])
    async with FreshdeskClient(settings, transport=transport, sleep=fake_sleep, clock=fake_sleep.clock) as c:
        await c.get_me()
    expected = base64.b64encode(f"{settings.api_key}:X".encode()).decode()
    assert seen[0].headers["Authorization"] == f"Basic {expected}"
    assert str(seen[0].url) == "https://mock-merchant.freshdesk.com/api/v2/agents/me"


async def test_retries_429_honouring_retry_after(settings, fake_sleep):
    transport, seen = scripted([
        (429, {"Retry-After": "3", "X-RateLimit-Remaining": "0"}, {}),
        (200, {"X-RateLimit-Total": "200", "X-RateLimit-Remaining": "150"}, {"id": 1}),
    ])
    async with FreshdeskClient(settings, transport=transport, sleep=fake_sleep, clock=fake_sleep.clock) as c:
        assert (await c.get_me())["id"] == 1
        assert c.stats.rate_limited == 1 and c.stats.retries == 1
        assert c.stats.last_rate_remaining == 150
    assert 3.0 in fake_sleep.calls
    assert len(seen) == 2


async def test_gives_up_fast_when_retry_after_exceeds_budget(settings, fake_sleep):
    transport, seen = scripted([(429, {"Retry-After": "120"}, {})])
    async with FreshdeskClient(settings, transport=transport, sleep=fake_sleep, clock=fake_sleep.clock) as c:
        with pytest.raises(RateLimitedError) as exc:
            await c.get_me()
    assert exc.value.retry_after == 120
    assert len(seen) == 1  # did not block the agent for two minutes


async def test_retries_5xx_with_backoff_then_fails(settings, fake_sleep):
    transport, seen = scripted([(503, {}, {})])
    async with FreshdeskClient(settings, transport=transport, sleep=fake_sleep, clock=fake_sleep.clock) as c:
        with pytest.raises(UpstreamError):
            await c.get_me()
    assert len(seen) == settings.max_retries + 1
    assert all(0 < d <= 8 for d in fake_sleep.calls)


async def test_retries_network_errors(settings, fake_sleep):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"id": 7})

    async with FreshdeskClient(settings, transport=httpx.MockTransport(handler), sleep=fake_sleep, clock=fake_sleep.clock) as c:
        assert (await c.get_me())["id"] == 7


@pytest.mark.parametrize("status,error", [
    (401, AuthenticationError), (403, AuthenticationError), (404, NotFoundError), (400, ValidationError),
])
async def test_maps_http_errors(settings, fake_sleep, status, error):
    body = {"description": "Validation failed", "errors": [{"field": "status", "message": "bad", "code": "invalid_value"}]}
    transport, _ = scripted([(status, {}, body)])
    async with FreshdeskClient(settings, transport=transport, sleep=fake_sleep, clock=fake_sleep.clock) as c:
        with pytest.raises(error):
            await c.get_ticket(1, [])


async def test_wrong_key_against_mock_is_401(fake_sleep):
    bad = Settings(domain="mock-merchant", api_key="wrong", mock=True)
    async with FreshdeskClient(bad, transport=MockFreshdesk().transport(), sleep=fake_sleep, clock=fake_sleep.clock) as c:
        with pytest.raises(AuthenticationError):
            await c.get_me()


async def test_link_header_pagination(settings, fake_sleep):
    mock = MockFreshdesk()
    async with FreshdeskClient(settings, transport=mock.transport(), sleep=fake_sleep, clock=fake_sleep.clock) as c:
        p1 = await c.list_tickets({"updated_since": "2000-01-01T00:00:00Z"}, page=1, per_page=25)
        p3 = await c.list_tickets({"updated_since": "2000-01-01T00:00:00Z"}, page=3, per_page=25)
    assert len(p1.items) == 25 and p1.has_more
    assert len(p3.items) == 10 and not p3.has_more


async def test_search_pagination_respects_300_cap(settings, fake_sleep):
    transport, _ = scripted([(200, {}, {"results": [{"id": 1}] * 30, "total": 900})])
    async with FreshdeskClient(settings, transport=transport, sleep=fake_sleep, clock=fake_sleep.clock) as c:
        assert (await c.search_tickets("status:2", 9)).has_more
        assert not (await c.search_tickets("status:2", 10)).has_more


async def test_does_not_follow_redirects(settings, fake_sleep):
    transport, _ = scripted([(302, {"Location": "https://evil.example.com/"}, {})])
    async with FreshdeskClient(settings, transport=transport, sleep=fake_sleep, clock=fake_sleep.clock) as c:
        resp = await c._request("GET", "/agents/me")
    assert resp.status_code == 302  # returned, never re-sent with the auth header


@pytest.mark.parametrize("raw,expected", [
    ("acme", "acme"), ("ACME.freshdesk.com", "acme"), ("https://acme.freshdesk.com/a/tickets", "acme"),
])
def test_domain_normalization(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", ["evil.com/", "acme.evil.com", "a b", "acme.freshdesk.com.evil.com"])
def test_domain_rejects_foreign_hosts(raw):
    with pytest.raises(ConfigurationError):
        normalize_domain(raw)


async def test_token_bucket_throttles_and_syncs_with_headers(fake_sleep):
    clock = {"t": 0.0}

    async def sleep(s):
        fake_sleep.calls.append(s)
        clock["t"] += s

    bucket = TokenBucket(60, burst=2, clock=lambda: clock["t"], sleep=sleep)
    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()  # bucket empty -> waits ~1s at 60/min
    assert fake_sleep.calls and abs(sum(fake_sleep.calls) - 1.0) < 1e-6

    bucket.observe(total=30, remaining=1)  # server says the account is nearly out of budget
    assert bucket.rate_per_minute == 30 and bucket.tokens == 0
    bucket.block_for(5)
    fake_sleep.calls.clear()
    await bucket.acquire()
    assert sum(fake_sleep.calls) >= 5
