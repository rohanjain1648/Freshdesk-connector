"""Build Freshdesk Search API queries from structured, validated filters.

The agent never writes raw query strings. Instead it passes typed filters, which we
compile into Freshdesk's filter language, for example:

    (status:2 OR status:3) AND priority:4 AND tag:'refund' AND created_at:>'2026-09-01'

Why this matters:
* Freshdesk has no escape syntax for quotes. A value containing ' or " would break
  the query or widen it, so such values are rejected rather than escaped.
* Field names come from an allow-list (plus ``cf_*`` custom fields), so a model
  cannot probe arbitrary fields.
* Freshdesk caps the query at 512 characters, and we check that before calling the API.

Semantics: Freshdesk's ``:>`` and ``:<`` on dates are *inclusive* (>= and <=).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable

from .errors import ValidationError

MAX_QUERY_LENGTH = 512

STATUS_CODES = {"open": 2, "pending": 3, "resolved": 4, "closed": 5}
PRIORITY_CODES = {"low": 1, "medium": 2, "high": 3, "urgent": 4}

_CUSTOM_FIELD_RE = re.compile(r"^cf_[a-z0-9_]{1,60}$")
_FORBIDDEN = ("'", '"', "\\")


def _code(value: str | int, mapping: dict[str, int], kind: str) -> int:
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return int(value)
    key = str(value).strip().lower()
    if key not in mapping:
        raise ValidationError(f"Unknown {kind} {value!r}. Use one of {sorted(mapping)} or a numeric code.")
    return mapping[key]


def _quote(value: str, what: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValidationError(f"{what} must not be empty")
    if any(ch in text for ch in _FORBIDDEN):
        raise ValidationError(f"{what} must not contain quotes or backslashes: {value!r}")
    if len(text) > 100:
        raise ValidationError(f"{what} is too long (max 100 chars)")
    return f"'{text}'"


def _date(value: date | datetime | str, what: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValidationError(f"{what} must be a date like 2026-09-30, got {value!r}") from exc
    return f"'{value.isoformat()}'"


def _any_of(field: str, values: Iterable[str]) -> str:
    parts = [f"{field}:{v}" for v in values]
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def custom_field_name(name: str) -> str:
    key = name.strip().lower()
    if not key.startswith("cf_"):
        key = f"cf_{key}"
    if not _CUSTOM_FIELD_RE.match(key):
        raise ValidationError(f"Invalid custom field name {name!r} (letters, digits, underscore only)")
    return key


def build_ticket_query(
    *,
    status: list[str | int] | None = None,
    priority: list[str | int] | None = None,
    tags: list[str] | None = None,
    ticket_type: str | None = None,
    agent_id: int | None = None,
    group_id: int | None = None,
    created_after: date | str | None = None,
    created_before: date | str | None = None,
    updated_after: date | str | None = None,
    updated_before: date | str | None = None,
    custom_fields: dict[str, str | int | bool] | None = None,
) -> str:
    clauses: list[str] = []
    if status:
        clauses.append(_any_of("status", (str(_code(s, STATUS_CODES, "status")) for s in status)))
    if priority:
        clauses.append(_any_of("priority", (str(_code(p, PRIORITY_CODES, "priority")) for p in priority)))
    if tags:
        # Multiple tags are OR'ed: "any of these tags" is the useful default for triage.
        clauses.append(_any_of("tag", (_quote(t, "tag") for t in tags)))
    if ticket_type:
        clauses.append(f"type:{_quote(ticket_type, 'ticket_type')}")
    if agent_id is not None:
        clauses.append(f"agent_id:{int(agent_id)}")
    if group_id is not None:
        clauses.append(f"group_id:{int(group_id)}")
    if created_after:
        clauses.append(f"created_at:>{_date(created_after, 'created_after')}")
    if created_before:
        clauses.append(f"created_at:<{_date(created_before, 'created_before')}")
    if updated_after:
        clauses.append(f"updated_at:>{_date(updated_after, 'updated_after')}")
    if updated_before:
        clauses.append(f"updated_at:<{_date(updated_before, 'updated_before')}")
    for name, value in (custom_fields or {}).items():
        key = custom_field_name(name)
        if isinstance(value, bool):
            clauses.append(f"{key}:{'true' if value else 'false'}")
        elif isinstance(value, int):
            clauses.append(f"{key}:{value}")
        else:
            clauses.append(f"{key}:{_quote(value, key)}")

    if not clauses:
        raise ValidationError(
            "search_tickets needs at least one filter (status, priority, tags, dates, custom_fields, ...). "
            "To browse recent tickets without filters use list_tickets."
        )
    query = " AND ".join(clauses)
    if len(query) > MAX_QUERY_LENGTH:
        raise ValidationError(
            f"Search query is {len(query)} chars; Freshdesk allows {MAX_QUERY_LENGTH}. Use fewer values per filter."
        )
    return query
