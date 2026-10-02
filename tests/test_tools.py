"""End-to-end: MCP client -> MCP server -> FreshdeskClient -> mock Freshdesk."""

from contextlib import asynccontextmanager
from datetime import date, timedelta

import pytest
from mcp.client.client import Client

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.mock import INJECTION_PAYMENT_ID, INJECTION_TICKET_ID, MockFreshdesk
from freshdesk_connector.server import build_server

EXPECTED_TOOLS = {
    "list_tickets", "get_ticket", "search_tickets", "list_ticket_conversations",
    "get_contact", "find_contacts", "list_ticket_fields", "connector_status",
}


@pytest.fixture
def mock():
    return MockFreshdesk(seed=7)


@asynccontextmanager
async def connect(settings, mock, fake_sleep):
    # A context manager rather than a yield-fixture: the MCP client's anyio task group
    # must be entered and exited in the same task.
    fd = FreshdeskClient(settings, transport=mock.transport(), sleep=fake_sleep, clock=fake_sleep.clock)
    try:
        async with Client(build_server(settings, client=fd)) as c:
            yield c
    finally:
        await fd.aclose()


@pytest.fixture
def client_cm(settings, mock, fake_sleep):
    return lambda: connect(settings, mock, fake_sleep)


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def test_all_tools_are_registered_read_only_with_schemas(client_cm):
    async with client_cm() as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == EXPECTED_TOOLS
        for t in tools.values():
            assert t.annotations.read_only_hint is True
            assert t.annotations.destructive_hint is False
            assert t.output_schema, f"{t.name} has no output schema"
            assert t.description


async def test_search_by_payment_id_and_get_ticket(client_cm):
    async with client_cm() as client:
        page = await call(client, "search_tickets", {"payment_id": INJECTION_PAYMENT_ID})
        assert page["total"] == 1 and page["tickets"][0]["id"] == INJECTION_TICKET_ID
        assert page["query"] == f"cf_payment_id:'{INJECTION_PAYMENT_ID}'"

        detail = await call(client, "get_ticket", {"ticket_id": INJECTION_TICKET_ID})
        assert detail["status"] in {"Open", "Pending", "Resolved", "Closed", "Waiting on Bank"}
        assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in detail["description_text"]  # passed through as data
        assert detail["url"].endswith(f"/a/tickets/{INJECTION_TICKET_ID}")


async def test_search_filters_are_applied(client_cm, mock):
    async with client_cm() as client:
        since = (date.today() - timedelta(days=60)).isoformat()
        page = await call(client, "search_tickets", {"status": ["open"], "tags": ["refund"], "created_after": since})
        expected = [t for t in mock.tickets.values() if t["status"] == 2 and "refund" in t["tags"]]
        assert page["total"] == len(expected) > 0
        assert all(t["status"] == "Open" and "refund" in t["tags"] for t in page["tickets"])


async def test_custom_status_label_comes_from_ticket_fields(client_cm, mock):
    async with client_cm() as client:
        page = await call(client, "search_tickets", {"status": ["6"]})
        assert page["total"] == sum(1 for t in mock.tickets.values() if t["status"] == 6) > 0
        assert {t["status"] for t in page["tickets"]} == {"Waiting on Bank"}


async def test_list_tickets_pagination_and_default_window(client_cm):
    async with client_cm() as client:
        page = await call(client, "list_tickets", {"per_page": 10})
        assert len(page["tickets"]) == 10 and page["has_more"] and page["next_page"] == 2
        assert "30 days" in page["note"]


async def test_list_tickets_by_requester_email(client_cm, mock):
    async with client_cm() as client:
        page = await call(client, "list_tickets", {
            "requester_email": "customer02@example.com", "updated_since": "2000-01-01T00:00:00Z",
            "include_requester": True,
        })
        assert page["tickets"] and all(t["requester_id"] == 5002 for t in page["tickets"])
        assert page["tickets"][0]["requester"]["email"] == "c***@example.com"  # redacted


async def test_long_thread_is_truncated_and_pageable(client_cm):
    async with client_cm() as client:
        detail = await call(client, "get_ticket", {"ticket_id": 1005, "max_body_chars": 200})
        assert len(detail["conversations"]) == 10 and detail["conversations_truncated"]
        rest = await call(client, "list_ticket_conversations", {"ticket_id": 1005, "page": 2, "per_page": 10})
        assert len(rest["conversations"]) == 4 and not rest["has_more"]
        body = (await call(client, "list_ticket_conversations", {"ticket_id": 1005, "max_body_chars": 200}))["conversations"][0]["body_text"]
        assert "[truncated" in body


async def test_contacts_are_redacted(client_cm):
    async with client_cm() as client:
        contact = await call(client, "get_contact", {"contact_id": 5001})
        assert contact["email"] == "c***@example.com" and contact["phone"] == "***0001"
        found = await call(client, "find_contacts", {"email": "customer01@example.com"})
        assert [c["id"] for c in found["contacts"]] == [5001]


async def test_pii_redaction_can_be_disabled(mock, fake_sleep):
    from freshdesk_connector.config import Settings

    s = Settings(domain="mock-merchant", api_key="mock-api-key", mock=True, redact_pii=False, rate_limit_per_minute=6000)
    fd = FreshdeskClient(s, transport=mock.transport(), sleep=fake_sleep, clock=fake_sleep.clock)
    async with Client(build_server(s, client=fd)) as c:
        contact = (await c.call_tool("get_contact", {"contact_id": 5001})).structured_content
    assert contact["email"] == "customer01@example.com"


async def test_errors_are_returned_as_tool_errors(client_cm):
    async with client_cm() as client:
        for name, args, needle in [
            ("get_ticket", {"ticket_id": 999999}, "Not found"),
            ("search_tickets", {}, "at least one filter"),
            ("search_tickets", {"tags": ["x' OR status:5"]}, "quotes"),
            ("find_contacts", {}, "at least one"),
            ("list_tickets", {"updated_since": "yesterday"}, "ISO-8601"),
        ]:
            result = await client.call_tool(name, args)
            assert result.is_error, name
            assert needle in result.content[0].text


async def test_schema_validation_rejects_out_of_range(client_cm):
    async with client_cm() as client:
        result = await client.call_tool("list_tickets", {"per_page": 500})
        assert result.is_error


async def test_rate_limited_upstream_is_transparent_to_agent(settings, fake_sleep):
    mock = MockFreshdesk(rate_limit_every=2)
    fd = FreshdeskClient(settings, transport=mock.transport(), sleep=fake_sleep, clock=fake_sleep.clock)
    async with Client(build_server(settings, client=fd)) as c:
        for _ in range(3):
            assert not (await c.call_tool("get_ticket", {"ticket_id": 1001})).is_error
        status = (await c.call_tool("connector_status", {})).structured_content
    assert status["ok"] and status["rate_limited_responses"] >= 2 and status["retries"] >= 2


async def test_connector_status_reports_auth_failure(fake_sleep):
    from freshdesk_connector.config import Settings

    bad = Settings(domain="mock-merchant", api_key="wrong", mock=True)
    fd = FreshdeskClient(bad, transport=MockFreshdesk().transport(), sleep=fake_sleep, clock=fake_sleep.clock)
    async with Client(build_server(bad, client=fd)) as c:
        status = (await c.call_tool("connector_status", {})).structured_content
    assert status["ok"] is False and "401" in status["error"]
