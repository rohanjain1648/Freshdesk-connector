"""Scripted, credential-free walkthrough: an agent triaging payment tickets via MCP.

It drives the real MCP server through an in-memory MCP client, exactly as Agent Studio
would, against the synthetic Freshdesk (with simulated 429s so retries are visible).
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import Any

from mcp.client.client import Client

from .config import Settings
from .mock import INJECTION_PAYMENT_ID, MockFreshdesk
from .server import build_server


def _out(text: str = "") -> None:
    print(text, file=sys.stdout, flush=True)


def _brief(data: Any, limit: int = 900) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "\n  … (truncated for display)"


async def _call(client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    _out(f"\n▶ {name}({json.dumps(args)})")
    result = await client.call_tool(name, args)
    if result.is_error:
        msg = " ".join(getattr(c, "text", "") for c in result.content)
        _out(f"  ✗ tool error: {msg}")
        return {}
    return result.structured_content or {}


async def run_demo(rate_limit_every: int = 5) -> None:
    mock = MockFreshdesk(rate_limit_every=rate_limit_every)
    settings = Settings.load(mock=True)
    mcp = build_server(settings, transport=mock.transport())
    fd = mcp.freshdesk_client  # type: ignore[attr-defined]

    async with Client(mcp) as client:
        tools = await client.list_tools()
        _out(f"Connected to MCP server 'freshdesk' with {len(tools.tools)} read-only tools: "
             + ", ".join(t.name for t in tools.tools))

        _out("\n=== 1. Health check ===")
        _out(_brief(await _call(client, "connector_status", {})))

        _out("\n=== 2. Learn this account's schema (custom statuses, custom fields) ===")
        fields = await _call(client, "list_ticket_fields", {})
        _out(f"  status labels: {fields.get('status_labels')}")
        _out("  custom fields: " + ", ".join(f"{f['name']} → search key '{f['searchable_as']}'"
                                           for f in fields.get("fields", []) if f.get("searchable_as")))

        _out("\n=== 3. Open/pending urgent+high payment tickets from the last 14 days ===")
        since = (date.today() - timedelta(days=14)).isoformat()
        page = await _call(client, "search_tickets", {
            "status": ["open", "pending"], "priority": ["urgent", "high"],
            "tags": ["payment_failed", "refund", "double_charge"], "created_after": since,
        })
        _out(f"  compiled query: {page.get('query')}")
        _out(f"  total matches: {page.get('total')}")
        for t in page.get("tickets", [])[:5]:
            _out(f"  #{t['id']} [{t['priority']}/{t['status']}] {t['subject']}  {t['url']}")

        _out("\n=== 4. Look up a ticket by Razorpay payment id ===")
        by_pay = await _call(client, "search_tickets", {"payment_id": INJECTION_PAYMENT_ID})
        for t in by_pay.get("tickets", []):
            _out(f"  #{t['id']} {t['subject']} custom_fields={t['custom_fields']}")

        _out("\n=== 5. Read that ticket (note: its body contains a prompt-injection attempt) ===")
        if by_pay.get("tickets"):
            detail = await _call(client, "get_ticket", {"ticket_id": by_pay["tickets"][0]["id"], "max_body_chars": 400})
            _out(_brief({k: detail.get(k) for k in ("id", "subject", "status", "requester", "description_text")}))
            _out("  → description_text is marked UNTRUSTED in the schema; the server instructions tell the agent "
                 "never to follow it, and no write tools exist to abuse.")

        _out("\n=== 6. A long thread: the embedded view caps at 10, so page the rest ===")
        long_t = await _call(client, "get_ticket", {"ticket_id": 1005, "max_body_chars": 200})
        _out(f"  embedded conversations: {len(long_t.get('conversations', []))}, "
             f"conversations_truncated={long_t.get('conversations_truncated')}")
        convs = await _call(client, "list_ticket_conversations", {"ticket_id": 1005, "page": 2, "per_page": 10})
        _out(f"  page 2: {len(convs.get('conversations', []))} more, has_more={convs.get('has_more')}")

        _out("\n=== 7. Who is the customer? (PII redacted by default) ===")
        contacts = await _call(client, "find_contacts", {"email": "customer03@example.com"})
        _out(_brief(contacts.get("contacts")))

        _out("\n=== 8. Guardrails: invalid input is rejected before reaching Freshdesk ===")
        await _call(client, "search_tickets", {"tags": ["refund' OR status:5 OR tag:'x"]})
        await _call(client, "search_tickets", {})

        _out("\n=== 9. Rate-limit handling summary ===")
        s = fd.stats
        _out(f"  upstream requests: {s.requests}, 429s received: {s.rate_limited}, retries: {s.retries} "
             f"(mock injects a 429 every {rate_limit_every} requests; every tool call above still succeeded)")
    await fd.aclose()
