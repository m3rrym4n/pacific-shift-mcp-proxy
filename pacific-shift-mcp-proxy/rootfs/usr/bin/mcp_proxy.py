#!/usr/bin/env python3
"""
Pacific Shift MCP Proxy
Multi-route MCP webhook proxy for Home Assistant.

Based on the webhook proxy pattern from homeassistant-ai/ha-mcp (MIT License).
Credit: https://github.com/homeassistant-ai/ha-mcp

This version removes the single-upstream ha-mcp auto-discovery and replaces it
with a config-driven multi-route system. Each route gets its own webhook ID,
its own secret URL, and proxies to a configured upstream MCP server.
"""

import asyncio
import json
import logging
import os
import secrets
import string
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG_LOGGING") == "true" else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mcp_proxy")

DATA_DIR = Path("/data")
OPTIONS_PATH = Path("/data/options.json")


def load_config():
    """Load add-on options from HA Supervisor."""
    if OPTIONS_PATH.exists():
        with open(OPTIONS_PATH) as f:
            return json.load(f)
    log.warning("No options.json found, using defaults")
    return {
        "routes": [{"name": "example", "upstream": "http://localhost:6001"}],
        "remote_url": "",
        "debug_logging": False,
    }


def get_or_create_webhook_id(name: str) -> str:
    """Load or generate a persistent webhook ID for a named route."""
    path = DATA_DIR / f"webhook_id_{name}.txt"
    if path.exists():
        webhook_id = path.read_text().strip()
        if webhook_id:
            return webhook_id
    alphabet = string.ascii_letters + string.digits
    webhook_id = "mcp_" + "".join(secrets.choice(alphabet) for _ in range(32))
    path.write_text(webhook_id)
    log.info("Generated new webhook ID for route: %s", name)
    return webhook_id


def get_nabu_casa_url() -> str | None:
    """Try to get Nabu Casa remote URL from HA Supervisor API."""
    try:
        import urllib.request
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        req = urllib.request.Request(
            "http://supervisor/core/api/cloud",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            remote_domain = data.get("remote_domain")
            if remote_domain:
                return f"https://{remote_domain}"
    except Exception as e:
        log.debug("Nabu Casa auto-detect failed: %s", e)
    return None


async def proxy_request(
    request: web.Request,
    upstream: str,
    name: str,
    debug: bool,
) -> web.StreamResponse | web.Response:
    """Proxy an inbound webhook request to the configured upstream MCP server."""
    if debug:
        log.info(
            "MCP Proxy [%s] inbound: %s from %s (Authorization: %s)",
            name,
            request.method,
            request.remote,
            "present" if "Authorization" in request.headers else "absent",
        )

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in (
            "host", "connection", "transfer-encoding",
            "upgrade", "proxy-authorization", "proxy-authenticate",
        )
    }

    try:
        body = await request.read()
        timeout = aiohttp.ClientTimeout(total=300)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method=request.method,
                url=upstream,
                headers=forward_headers,
                data=body or None,
                allow_redirects=False,
            ) as upstream_resp:
                if debug:
                    log.info(
                        "MCP Proxy [%s] -> upstream %d (%s)",
                        name,
                        upstream_resp.status,
                        upstream_resp.content_type,
                    )

                response = web.StreamResponse(
                    status=upstream_resp.status,
                    reason=upstream_resp.reason,
                )
                for key, value in upstream_resp.headers.items():
                    if key.lower() not in (
                        "connection", "transfer-encoding", "content-encoding"
                    ):
                        response.headers[key] = value

                await response.prepare(request)
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response

    except aiohttp.ClientConnectorError as e:
        log.error("MCP Proxy [%s] upstream unreachable: %s", name, e)
        return web.Response(status=502, text=f"Upstream MCP server unreachable: {e}")
    except Exception as e:
        log.error("MCP Proxy [%s] proxy error: %s", name, e)
        return web.Response(status=500, text=f"Proxy error: {e}")


async def health_check_loop(routes: list[dict]) -> None:
    """Periodically check all upstream MCP servers."""
    while True:
        await asyncio.sleep(60)
        for route in routes:
            name = route["name"]
            upstream = route["upstream"]
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as session:
                    async with session.get(upstream) as resp:
                        log.debug("Health [%s]: %d", name, resp.status)
            except Exception as e:
                log.warning("Health [%s]: unreachable: %s", name, e)


async def main() -> None:
    config = load_config()
    routes = config.get("routes", [])
    remote_url = config.get("remote_url", "").strip().rstrip("/")
    debug = config.get("debug_logging", False)

    if debug:
        log.setLevel(logging.DEBUG)

    if not routes:
        log.error("No routes configured. Add routes in the add-on configuration.")
        sys.exit(1)

    if not remote_url:
        remote_url = get_nabu_casa_url()
        if remote_url:
            log.info("Auto-detected Nabu Casa URL: %s", remote_url)
        else:
            remote_url = "http://homeassistant.local:8123"
            log.warning("Could not auto-detect Nabu Casa URL. Defaulting to: %s", remote_url)
    remote_url = remote_url.rstrip("/")

    app = web.Application()

    for route in routes:
        name = route["name"]
        upstream = route["upstream"].rstrip("/")
        webhook_id = get_or_create_webhook_id(name)
        webhook_path = f"/api/webhook/{webhook_id}"
        webhook_url = f"{remote_url}{webhook_path}"

        def make_handler(route_name: str, route_upstream: str):
            async def handler(request: web.Request) -> web.StreamResponse | web.Response:
                return await proxy_request(request, route_upstream, route_name, debug)
            return handler

        app.router.add_route("*", webhook_path, make_handler(name, upstream))

        log.info("Route registered: %s -> %s", name, upstream)
        log.info("MCP Proxy URL (%s): %s", name, webhook_url)
        # Print clearly for log scraping
        print(f"MCP Proxy URL ({name}): {webhook_url}", flush=True)

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", health)
    asyncio.create_task(health_check_loop(routes))

    log.info(
        "Pacific Shift MCP Proxy started — %d route(s) active",
        len(routes),
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    log.info("Listening on :8099")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
