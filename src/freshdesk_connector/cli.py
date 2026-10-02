"""Command-line entry point: ``freshdesk-connector {auth,serve,spec,demo}``.

Everything except tool output goes to stderr, because stdout carries the MCP stdio protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .auth import (
    credentials_path,
    delete_credentials,
    load_stored_credentials,
    mask_key,
    save_credentials,
    verify_credentials,
)
from .config import Settings, normalize_domain
from .errors import ConnectorError


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (no extra dependency). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------- auth

def cmd_auth_login(args: argparse.Namespace) -> int:
    domain_raw = args.domain or input("Freshdesk domain (e.g. acme or acme.freshdesk.com): ")
    domain = normalize_domain(domain_raw)
    api_key = os.environ.get("FRESHDESK_API_KEY") if args.from_env else None
    api_key = api_key or getpass.getpass("Freshdesk API key (Profile settings → View API key): ").strip()
    if not api_key:
        _eprint("No API key entered.")
        return 2
    _eprint(f"Verifying key {mask_key(api_key)} against https://{domain}.freshdesk.com/api/v2/agents/me …")
    try:
        me = asyncio.run(verify_credentials(domain, api_key))
    except ConnectorError as exc:
        _eprint(f"✗ Verification failed: {exc}")
        return 1
    path = save_credentials(domain, api_key)
    name = (me.get("contact") or {}).get("name", "unknown agent")
    _eprint(f"✓ Authenticated as {name}. Credentials saved to {path} (owner-only).")
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    try:
        settings = Settings.load(mock=args.mock or None)
    except ConnectorError as exc:
        _eprint(f"✗ {exc}")
        return 1
    source = "mock" if settings.mock else ("env" if os.environ.get("FRESHDESK_API_KEY") else str(credentials_path()))
    _eprint(f"Domain: {settings.domain}.freshdesk.com   key: {mask_key(settings.api_key)}   source: {source}")

    async def check() -> dict[str, Any]:
        from .server import build_server

        mcp = build_server(settings)
        try:
            return await mcp.freshdesk_client.get_me()  # type: ignore[attr-defined]
        finally:
            await mcp.freshdesk_client.aclose()  # type: ignore[attr-defined]

    try:
        me = asyncio.run(check())
    except ConnectorError as exc:
        _eprint(f"✗ {exc}")
        return 1
    _eprint(f"✓ Authenticated as {(me.get('contact') or {}).get('name')}")
    return 0


def cmd_auth_logout(_: argparse.Namespace) -> int:
    _eprint("Removed stored credentials." if delete_credentials() else "No stored credentials.")
    return 0


# ---------------------------------------------------------------------- serve

def cmd_serve(args: argparse.Namespace) -> int:
    from .server import BearerAuthMiddleware, build_server

    try:
        settings = Settings.load(mock=args.mock or None)
    except ConnectorError as exc:
        _eprint(f"✗ {exc}")
        return 1
    mcp = build_server(settings)
    _eprint(f"freshdesk-connector {__version__} | mode={'mock' if settings.mock else 'live'} "
            f"| domain={settings.domain} | transport={args.transport}")

    if args.transport == "stdio":
        mcp.run("stdio")
        return 0

    import uvicorn

    token = os.environ.get("CONNECTOR_AUTH_TOKEN", "")
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if not token and not (local or args.insecure_no_auth):
        _eprint("Refusing to expose the HTTP transport without CONNECTOR_AUTH_TOKEN. "
                "Set it, bind to 127.0.0.1, or pass --insecure-no-auth.")
        return 2
    app: Any = mcp.streamable_http_app(host=args.host)
    if token:
        app = BearerAuthMiddleware(app, token)
    _eprint(f"MCP endpoint: http://{args.host}:{args.port}/mcp   (auth: {'bearer' if token else 'none'})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------------- spec

async def export_spec(settings: Settings) -> dict[str, Any]:
    from .server import build_server

    mcp = build_server(settings)
    tools = await mcp.list_tools()
    prompts = await mcp.list_prompts()
    await mcp.freshdesk_client.aclose()  # type: ignore[attr-defined]
    return {
        "server": {"name": "freshdesk", "version": __version__, "instructions": mcp.instructions},
        "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
        "prompts": [p.model_dump(mode="json", by_alias=True, exclude_none=True) for p in prompts],
    }


def cmd_spec(args: argparse.Namespace) -> int:
    spec = asyncio.run(export_spec(Settings.load(mock=True)))
    text = json.dumps(spec, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        _eprint(f"Wrote {len(spec['tools'])} tool definitions to {args.out}")
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------- demo

def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    asyncio.run(run_demo(rate_limit_every=args.rate_limit_every))
    return 0


# ---------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="freshdesk-connector", description="Read-only Freshdesk MCP connector")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="Log each upstream call to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="Manage Freshdesk API-key credentials")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser("login", help="Prompt for, verify and store an API key")
    login.add_argument("--domain")
    login.add_argument("--from-env", action="store_true", help="Read the key from FRESHDESK_API_KEY instead of prompting")
    login.set_defaults(func=cmd_auth_login)
    status = auth_sub.add_parser("status", help="Show which credentials are in use and verify them")
    status.add_argument("--mock", action="store_true")
    status.set_defaults(func=cmd_auth_status)
    auth_sub.add_parser("logout", help="Delete stored credentials").set_defaults(func=cmd_auth_logout)

    serve = sub.add_parser("serve", help="Run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--mock", action="store_true", help="Use synthetic Freshdesk data")
    serve.add_argument("--insecure-no-auth", action="store_true")
    serve.set_defaults(func=cmd_serve)

    spec = sub.add_parser("spec", help="Print or write the MCP tool specification (JSON)")
    spec.add_argument("--out")
    spec.set_defaults(func=cmd_spec)

    demo = sub.add_parser("demo", help="Run a scripted triage session against the mock")
    demo.add_argument("--rate-limit-every", type=int, default=5, help="Mock returns 429 every N requests (0 = off)")
    demo.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
