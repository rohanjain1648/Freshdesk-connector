"""Runtime settings, resolved from environment variables and the local credential store."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .errors import ConfigurationError

# Freshdesk's REST API is only served from <subdomain>.freshdesk.com. Restricting the
# host to that pattern means a mis-typed or malicious domain cannot redirect the API
# key (sent as Basic auth) to an arbitrary server.
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

MOCK_DOMAIN = "mock-merchant"
MOCK_API_KEY = "mock-api-key"


def normalize_domain(raw: str) -> str:
    """Accept 'acme', 'acme.freshdesk.com' or 'https://acme.freshdesk.com/' and return 'acme'."""
    value = raw.strip().lower()
    value = re.sub(r"^https?://", "", value).split("/", 1)[0]
    if value.endswith(".freshdesk.com"):
        value = value[: -len(".freshdesk.com")]
    if not _SUBDOMAIN_RE.match(value):
        raise ConfigurationError(
            f"Invalid Freshdesk domain {raw!r}. Expected your subdomain, e.g. 'acme' for "
            "https://acme.freshdesk.com."
        )
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    domain: str
    api_key: str
    mock: bool = False
    redact_pii: bool = True
    rate_limit_per_minute: float = 50.0
    max_retry_wait_seconds: float = 20.0
    max_retries: int = 3
    timeout_seconds: float = 20.0
    # Merchant-specific custom-field names that hold commerce identifiers. Every
    # Freshdesk account names these differently; FDEs set them during onboarding.
    order_id_field: str = "cf_order_id"
    payment_id_field: str = "cf_payment_id"

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}.freshdesk.com/api/v2"

    @property
    def portal_url(self) -> str:
        return f"https://{self.domain}.freshdesk.com"

    @classmethod
    def load(cls, *, mock: bool | None = None) -> "Settings":
        """Resolve settings: explicit flag > env vars > stored credentials."""
        from .auth import load_stored_credentials  # local import avoids a cycle

        use_mock = _env_bool("FRESHDESK_MOCK", False) if mock is None else mock
        common = dict(
            redact_pii=_env_bool("FRESHDESK_REDACT_PII", True),
            rate_limit_per_minute=_env_float("FRESHDESK_RATE_LIMIT_PER_MINUTE", 50.0),
            max_retry_wait_seconds=_env_float("FRESHDESK_MAX_RETRY_WAIT_SECONDS", 20.0),
            order_id_field=os.environ.get("FRESHDESK_ORDER_ID_FIELD", "cf_order_id"),
            payment_id_field=os.environ.get("FRESHDESK_PAYMENT_ID_FIELD", "cf_payment_id"),
        )
        if use_mock:
            return cls(domain=MOCK_DOMAIN, api_key=MOCK_API_KEY, mock=True, **common)

        domain = os.environ.get("FRESHDESK_DOMAIN", "").strip()
        api_key = os.environ.get("FRESHDESK_API_KEY", "").strip()
        if not (domain and api_key):
            stored = load_stored_credentials()
            if stored:
                domain = domain or stored["domain"]
                api_key = api_key or stored["api_key"]
        if not (domain and api_key):
            raise ConfigurationError(
                "Freshdesk credentials not configured. Run `freshdesk-connector auth login`, "
                "set FRESHDESK_DOMAIN and FRESHDESK_API_KEY, or use --mock."
            )
        return cls(domain=normalize_domain(domain), api_key=api_key, **common)
