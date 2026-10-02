# Design notes

## Decisions and why

| Decision | Reasoning |
|---|---|
| **Freshdesk** over Zoho Inventory, WooCommerce, Unicommerce | Tickets are where payment problems surface for a Razorpay merchant, a free trial exists for reviewers, and the API has real constraints worth engineering around (rate limits, search quirks, pagination) |
| **MCP**, stdio and streamable HTTP | The assignment accepts an MCP spec, and it is the standard way agents discover typed tools. stdio for local use, HTTP with bearer auth for hosted use |
| **Structured filters instead of a raw query string** | Freshdesk has no quote escaping, so a free-form string from a model could break or widen a query. Compiling typed filters removes that class of problem |
| **Redaction on by default** | The agent rarely needs a raw email or phone; a masked value still lets it distinguish customers. Turning it off is an operator decision |
| **Fail fast on long rate-limit waits** | An agent blocked for minutes is worse than one told "rate limited, retry after N seconds" and able to adapt |
| **`follow_redirects=False` and a domain allow-list** | The API key is sent as Basic auth. A mistyped domain or a redirect must not carry it to a third party |
| **Mock mode as a first-class feature** | Lets a reviewer exercise every path, including 429s and prompt-injection content, without credentials or a Freshdesk account |

## Freshdesk behaviours the connector handles

- Search wants the whole query wrapped in double quotes, allows 512 characters, a fixed page size of 30 and at most
  10 pages.
- `/tickets` returns only the last 30 days unless `updated_since` is given, and embeds at most 10 conversations.
- Pagination is signalled by a `Link: rel="next"` header, not a total count.
- Status codes are numeric; custom statuses exist only in `/ticket_fields`, which the connector caches and uses to label them.
- Rate limits are per account and shared with other apps, hence adapting to `X-RateLimit-Remaining`.

## Bug found by the tests

`TokenBucket.acquire` could spin forever: after sleeping for a very short computed delay, rounding left the token
count just under 1, so the next iteration computed an even shorter delay that no longer advanced the clock. It surfaced
as a `MemoryError` in a test with a virtual clock. The fix grants the token explicitly once the required wait has been served.

## Testing approach

- Query builder: unit tests including injection attempts.
- Client: scripted transports for 429 with `Retry-After`, 5xx backoff, network errors, redirects, status mapping and
  pagination. Sleep and clock are injected, so the suite runs in under 4 seconds.
- End to end: a real MCP client talks to the real MCP server over an in-memory transport, which talks to a mock Freshdesk
  that evaluates the search language. This checks tool schemas, annotations, redaction, truncation and error surfacing.
- Manual: stdio through a real subprocess, and HTTP with and without the bearer token.

## Known limitations and risks

- **Not run against live Freshdesk.** The mock follows the documented API, but real accounts may differ (extra
  fields, plan-specific limits). The first live check is `auth login` then `connector_status`.
- Rate-limit state is per process. Two connector instances sharing one account will each think they own the budget;
  the header sync reduces, but does not remove, that risk.
- Ticket field metadata is cached for 10 minutes, so a newly added custom status may show as `Custom (n)` until then.
- Redaction does not scrub PII inside free-text ticket bodies.
- The stored API key is plaintext in a user-only file. This is acceptable for a developer machine, but a
  hosted deployment should inject it from a secret manager through the environment.

## How I would measure impact with a merchant

| Metric | Baseline | Target |
|---|---|---|
| Time from ticket creation to first categorisation | Measure manually for a week | Down by more than half |
| Payment tickets linked to an order or payment id | Sample 50 tickets | Above 90% |
| Agent tool calls that hit a 429 | n/a | Below 1% |
| Write calls made by the connector | n/a | Exactly 0 |

`connector_status` already exposes request, retry and 429 counts to support the last two rows.
