"""API-key authentication flow.

Freshdesk REST v2 authenticates with a per-agent API key sent as HTTP Basic auth
(username = key, password = "X"). It has no OAuth flow for the REST API, so the
connector implements the key flow end to end:

1. ``auth login`` prompts for the domain and key (the key is never echoed).
2. The key is **validated live** against ``GET /agents/me`` before it is saved.
3. Credentials are stored in ``~/.freshdesk-connector/credentials.json`` with
   owner-only permissions. Environment variables always take precedence, which
   is how a hosted Agent Studio deployment injects per-merchant secrets.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx


def credentials_path() -> Path:
    base = os.environ.get("FRESHDESK_CONNECTOR_HOME")
    root = Path(base) if base else Path.home() / ".freshdesk-connector"
    return root / "credentials.json"


def basic_auth_header(api_key: str) -> str:
    token = base64.b64encode(f"{api_key}:X".encode()).decode()
    return f"Basic {token}"


def mask_key(api_key: str) -> str:
    return f"{api_key[:4]}…{api_key[-2:]}" if len(api_key) > 8 else "****"


def load_stored_credentials() -> dict[str, str] | None:
    path = credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("domain") and data.get("api_key"):
        return {"domain": str(data["domain"]), "api_key": str(data["api_key"])}
    return None


def save_credentials(domain: str, api_key: str) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the file with 0600 *before* writing the secret into it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"domain": domain, "api_key": api_key}, fh)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # Windows ignores POSIX modes; the file lives in the user profile.
        pass
    return path


def delete_credentials() -> bool:
    path = credentials_path()
    if path.exists():
        path.unlink()
        return True
    return False


async def verify_credentials(
    domain: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Validate the key against ``/agents/me`` and return the agent profile.

    Uses the full client so validation gets the same retry and rate-limit
    behaviour as normal calls.
    """
    from .client import FreshdeskClient
    from .config import Settings

    settings = Settings(domain=domain, api_key=api_key)
    async with FreshdeskClient(settings, transport=transport) as client:
        return await client.get_me()
