# Plan — Freshdesk private connector for Agent Studio

## 1. Ideation: why Freshdesk, and what problem we are really solving

**Merchant scenario.** A D2C merchant on Razorpay runs customer support on Freshdesk.
Payment-related tickets ("amount debited but order not confirmed", "refund not received",
"charged twice", "UPI autopay cancelled") make up a large share of the support queue.
At first the merchant asks: *"Can an agent read our Freshdesk?"*

**The real problem behind that request.** Support leads cannot see which payment issues are piling up,
how old they are, or which customers are affected, so they triage by hand.
An Agent Studio agent can only help if it can do these things:

1. **Find** the right tickets by structured criteria (status, priority, tag, date, *order id / payment id*).
2. **Read** a ticket's full thread, so it can understand the customer's actual issue.
3. **Resolve** the requester (contact) to join with Razorpay data (payments, refunds) through other connectors.
4. Do all of this **safely**: read-only, within the merchant's API rate budget, without leaking secrets,
   and without trusting ticket text as instructions.

**Why Freshdesk over the other options.**

| Option | Auth | Sandbox for reviewers | Fit with Razorpay merchants |
|---|---|---|---|
| **Freshdesk** | API key (Basic) | Free 14-day trial, instant | Support tickets about payments and refunds, a high-value agent use case |
| Zoho Inventory | OAuth 2.0 | Needs a Zoho org | Good, but inventory reads are less agent-centric |
| WooCommerce | Consumer key/secret | Needs a self-hosted WP | Orders; setup is heavy for a reviewer |
| Unicommerce | Token | Enterprise-only | Hard to test |

## 2. Scope

**In scope (v1, read-only)**
- API-key auth flow: `auth login` prompts for the key, validates it against `/agents/me`, and stores it locally with `0600` permissions. Environment variables override the stored file.
- Primitives: list, get, and search for **tickets**; list **conversations**; get and find **contacts**; discover **ticket fields** (schema).
- Rate-limit handling: a client-side token bucket that adapts to `X-RateLimit-*` headers, honours `Retry-After` on 429, uses exponential backoff with jitter on 5xx and network errors, and fails fast to the agent when the wait would be too long.
- An MCP server (stdio and streamable HTTP with a bearer token) with typed input and output schemas and read-only annotations.
- A generated MCP tool spec (`docs/tool_spec.json`).
- A **mock mode** (synthetic Freshdesk with simulated 429s), so it can be evaluated without credentials.
- Docs: README (setup and run), CAPABILITIES (what the agent can and cannot do), DESIGN (decisions and limitations).

**Out of scope (deliberately)**
- Writes (reply, update status, add a note). Least privilege for v1; see CAPABILITIES for the roadmap.
- Full-text search. Freshdesk's Search API only filters on fields, and we surface that honestly.
- Multi-tenant credential vaulting. Agent Studio owns that; one connector instance serves one merchant.

## 3. Architecture

```
Agent Studio agent ──MCP (stdio | streamable-http + bearer)──▶ server.py (tools, schemas)
                                                                 │
                                         search.py (safe query builder)   models.py (normalise, truncate, redact)
                                                                 │
                                                       client.py (FreshdeskClient)
                                              auth.py ── rate_limit.py ── retries/backoff
                                                                 │
                                    httpx ──▶ https://<domain>.freshdesk.com/api/v2   (or mock.py transport)
```

## 4. Tool surface

| Tool | Freshdesk endpoint | Purpose |
|---|---|---|
| `list_tickets` | `GET /tickets` | Recent tickets with predefined filters, requester, `updated_since`, sort, pagination |
| `get_ticket` | `GET /tickets/{id}?include=conversations,requester` | One ticket plus its thread (truncated) |
| `search_tickets` | `GET /search/tickets?query=…` | Structured filters → a validated query (status, priority, tags, dates, custom fields such as `order_id`) |
| `list_ticket_conversations` | `GET /tickets/{id}/conversations` | Full thread, paginated |
| `get_contact` | `GET /contacts/{id}` | Requester details (PII can be redacted) |
| `find_contacts` | `GET /contacts?email=/phone=/mobile=` | Look up a customer by identifier |
| `list_ticket_fields` | `GET /ticket_fields` | Schema discovery: statuses, custom fields, allowed values |
| `connector_status` | `GET /agents/me` | Auth health and remaining rate budget |

## 5. Implementation steps

1. Project skeleton: `pyproject.toml`, `src/freshdesk_connector`, `tests/`, `.env.example`, `.gitignore`.
2. `config.py` and `auth.py`: settings, domain validation (SSRF guard), credential store, login, status and logout.
3. `rate_limit.py` and `client.py`: token bucket, header sync, retry policy, error mapping, `Link` pagination.
4. `search.py`: query builder with validation (field allow-list, quote rejection, 512-character limit, date formats).
5. `models.py`: normalisation (enum labels, truncation, PII redaction, portal URLs).
6. `mock.py`: deterministic synthetic dataset, a mini query evaluator, simulated 429s and auth.
7. `server.py`: MCP tools with annotations, plus a triage prompt.
8. `cli.py`: the `auth`, `serve`, `spec` and `demo` commands.
9. Tests: query builder, client (auth header, retry on 429/5xx, pagination), end-to-end tools through the MCP client.
10. Docs and the generated tool spec.

## 6. How to measure impact (FDE lens)

- **Time-to-triage**: minutes from a ticket being created to its first categorisation, before and after the agent.
- **Coverage**: the percentage of payment tickets that the agent links to an order or payment id.
- **Safety**: zero write calls (read-only by construction); 429 rate per 1,000 tool calls; p95 tool latency.
- The connector logs each upstream call's latency and the remaining rate budget, which gives a baseline for these metrics.
