"""Connector error hierarchy.

Every error carries a message written for the *agent*: what went wrong and what
to do next. The MCP layer surfaces these verbatim as tool errors.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class for all connector errors."""


class ConfigurationError(ConnectorError):
    """Missing or invalid configuration (domain, API key, ...)."""


class AuthenticationError(ConnectorError):
    """Freshdesk rejected the credentials (401) or the key lacks permission (403)."""


class NotFoundError(ConnectorError):
    """The requested resource does not exist or is not visible to this API key."""


class ValidationError(ConnectorError):
    """The request was rejected by input validation (ours or Freshdesk's 400)."""


class RateLimitedError(ConnectorError):
    """Freshdesk rate limit exhausted and waiting would exceed the configured budget."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamError(ConnectorError):
    """Freshdesk returned 5xx or the network failed after all retries."""
