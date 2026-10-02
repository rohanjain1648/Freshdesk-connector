# Freshdesk connector for Razorpay Agent Studio

A **read-only MCP server** that lets an Agent Studio agent list, get and search a merchant's
Freshdesk tickets, conversations and contacts, with API-key auth, rate-limit handling, PII
redaction and guardrails against prompt injection from ticket text.

- What the agent can and cannot do: [docs/CAPABILITIES.md](docs/CAPABILITIES.md)
- Design decisions, threat model, limitations: [docs/DESIGN.md](docs/DESIGN.md)
- MCP tool specification (generated): [docs/tool_spec.json](docs/tool_spec.json)
- Ideation and plan: [PLAN.md](PLAN.md)

## Why this exists

Payment-related tickets ("debited but order not confirmed", "refund not received") are a large share of a
D2C merchant's support queue. The request is "let an agent read our Freshdesk", but the real need is **finding
the right tickets by order id or payment id, reading the full issue, and joining it with Razorpay data**
without a human copying ids between tools. The tool surface is built around that.

## Quick start (no credentials needed)

Requires Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

freshdesk-connector demo          # scripted agent session against a synthetic Freshdesk
pytest                            # 43 tests
```

`demo` drives the real MCP server through an MCP client against a built-in fake Freshdesk that injects
429 responses, so you can see retries working. All data is synthetic.

## Connect to a real Freshdesk

1. In Freshdesk: **Profile settings → View API key**. Use a dedicated agent with the least-privileged role
   that can see the needed tickets (the connector only issues GET requests).
2. Authenticate. The key is verified live against `/api/v2/agents/me` before it is stored:
   ```bash
   freshdesk-connector auth login --domain acme      # prompts for the key (hidden input)
   freshdesk-connector auth status
   ```
   Alternatively set `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY` (they take precedence over the stored file).
   See [.env.example](.env.example) for every setting.
3. Run the server:
   ```bash
   freshdesk-connector serve                                  # stdio (local agents, Claude Desktop, etc.)
   CONNECTOR_AUTH_TOKEN=<secret> freshdesk-connector serve --transport http --port 8765
   # endpoint: http://127.0.0.1:8765/mcp   (Authorization: Bearer <secret>)   health: /healthz
   ```
4. Point Agent Studio at it as an MCP server (stdio command `freshdesk-connector serve`, or the HTTP URL plus
   the bearer token). Example stdio config:
   ```json
   { "mcpServers": { "freshdesk": { "command": "freshdesk-connector", "args": ["serve"] } } }
   ```
5. Map your account's custom fields. Each merchant names the order and payment id fields differently.
   Run the `list_ticket_fields` tool once, then set `FRESHDESK_ORDER_ID_FIELD` and `FRESHDESK_PAYMENT_ID_FIELD`.

The HTTP transport refuses to start on a non-loopback address without `CONNECTOR_AUTH_TOKEN`.

## Tools

| Tool | What it does |
|---|---|
| `search_tickets` | Filter by status, priority, tags, type, agent, group, created/updated dates, order id, payment id, custom fields |
| `list_tickets` | Recent tickets; filter by requester email/id, company, `updated_since`; sort and paginate |
| `get_ticket` | One ticket with description, requester and latest conversations |
| `list_ticket_conversations` | Full thread, paginated |
| `get_contact` / `find_contacts` | Customer by id, or by exact email, phone, mobile, company |
| `list_ticket_fields` | Statuses (including custom ones), types and custom fields for this account |
| `connector_status` | Auth health and remaining API budget |

All tools carry `readOnlyHint`, typed input schemas and typed output schemas. A `triage_payment_tickets`
prompt is included as a ready-made workflow.

## Rate limiting

Freshdesk allows 50 to 700 calls per minute per account depending on plan, shared with every other
integration. The connector:

1. smooths bursts with a client-side token bucket sized by `FRESHDESK_RATE_LIMIT_PER_MINUTE`;
2. reads `X-RateLimit-Total` / `X-RateLimit-Remaining` on every response and adapts the bucket to the
   account's real remaining budget;
3. on `429`, honours `Retry-After` and pauses **all** concurrent callers, not only the failing one;
4. retries 5xx and network errors with exponential backoff and jitter (max 3 retries);
5. if the required wait exceeds `FRESHDESK_MAX_RETRY_WAIT_SECONDS` (20s), returns a clear
   rate-limited error to the agent rather than blocking it for minutes.

## Assumptions and limitations

- **Read-only by design.** No reply, update, assign or close. See CAPABILITIES for why and for the roadmap.
- **No keyword search.** Freshdesk's Search API filters on fields only. The connector says so rather than faking it.
- **Search returns at most 300 results** (10 pages of 30). `list_tickets` defaults to the last 30 days unless `updated_since` is set.
- **API key, not OAuth.** Freshdesk's REST API only supports per-agent API keys. Tickets are visible according
  to that agent's permissions.
- **Embedded conversations cap at 10.** Use `list_ticket_conversations` for longer threads.
- Verified against a faithful mock and the documented API behaviour. **I have not run it against a live
  Freshdesk account**, because the evaluation environment had no credentials; the first live run should be
  `auth login` followed by `connector_status`.
- One connector instance serves one merchant account. Multi-tenant credential storage belongs to the
  Agent Studio platform.
- PII masking covers structured contact fields. Free text in ticket bodies is not scrubbed.

## Layout

```
src/freshdesk_connector/
  server.py      MCP tools, annotations, instructions, bearer-auth middleware
  client.py      HTTP client: auth, retries, error mapping, pagination
  rate_limit.py  token bucket synced to X-RateLimit headers
  search.py      validated query builder for the Search API
  models.py      output schemas, normalisation, truncation, PII masking
  auth.py        API-key login / verify / store
  mock.py        synthetic Freshdesk with 429 injection
  cli.py demo.py config.py errors.py
tests/           43 tests: query builder, client, rate limiter, end-to-end MCP
docs/            CAPABILITIES.md, DESIGN.md, tool_spec.json
```
