"""Pacific Shift MCP Proxy - multi-route MCP webhook proxy for Home Assistant.

This integration is auto-installed and auto-configured by the Pacific Shift
MCP Proxy add-on. Unlike a single-target webhook proxy, it can register
MANY webhooks in one config entry — one per configured route — each
proxying to a different LAN MCP server (bookstack-mcp, task-runner, future
services). This is the fix for the original design flaw: the previous
version of this add-on ran its own aiohttp server on port 8099 and never
registered anything with HA's webhook component, so Nabu Casa had no route
to it and the generated URLs were dead.

The pattern here mirrors homeassistant-ai/ha-mcp's mcp_proxy integration:
HA's own HTTP layer (port 8123) is what Nabu Casa already tunnels, so a
webhook registered via homeassistant.components.webhook.async_register is
reachable at https://<nabu-casa-url>/api/webhook/<webhook_id> with zero
extra tunneling. This integration does that registration; the add-on
(start.py) only handles discovery, config-file writing, and config-entry
lifecycle via the Supervisor/HA Core API.

Configuration is read from /config/.pacific_shift_mcp_proxy_config.json,
written by the add-on's startup script. No manual configuration is needed.

Each webhook URL is itself the shared secret (same model as ha-mcp's
default unauthenticated mode) — there is no OAuth layer in this version.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from homeassistant.components.webhook import async_register, async_unregister
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "pacific_shift_mcp_proxy"
CONFIG_FILE = Path("/config/.pacific_shift_mcp_proxy_config.json")

# Content-Types we'll pass straight through from upstream (everything else
# gets forced to application/json — prevents HTML/script injection via a
# misbehaving or compromised upstream).
_ALLOWED_CONTENT_TYPES = ("application/json", "text/event-stream")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Nothing to do at the configuration.yaml level — config entry only."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up all configured routes' webhooks from the proxy config file."""
    try:
        proxy_config = await hass.async_add_executor_job(_read_config)
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.error("Pacific Shift MCP Proxy: failed to read %s: %s", CONFIG_FILE, err)
        raise ConfigEntryError(
            f"Failed to read {CONFIG_FILE}: {err}. Restart the Pacific Shift "
            "MCP Proxy add-on to regenerate the config file."
        ) from err

    if proxy_config is None:
        _LOGGER.info(
            "Pacific Shift MCP Proxy: no config found at %s yet. "
            "Start/restart the add-on to activate routes.",
            CONFIG_FILE,
        )
        hass.data[DOMAIN] = {"routes": {}}
        return True

    routes = proxy_config.get("routes", [])
    if not routes:
        _LOGGER.warning(
            "Pacific Shift MCP Proxy: config file has no routes configured."
        )

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, sock_connect=10, sock_read=300),
    )

    registered: dict[str, dict[str, Any]] = {}
    try:
        for route in routes:
            name = route.get("name")
            webhook_id = route.get("webhook_id")
            target_url = route.get("target_url")
            if not name or not webhook_id or not target_url:
                _LOGGER.error(
                    "Pacific Shift MCP Proxy: skipping malformed route entry %r "
                    "(need name, webhook_id, target_url)",
                    route,
                )
                continue

            masked_wh = webhook_id[:6] + "..." if len(webhook_id) > 6 else "***"

            try:
                async_register(
                    hass,
                    DOMAIN,
                    f"Pacific Shift MCP Proxy: {name}",
                    webhook_id,
                    _make_webhook_handler(target_url, name, session),
                    allowed_methods=["POST", "GET"],
                )
            except Exception as err:
                _LOGGER.exception(
                    "Pacific Shift MCP Proxy: failed to register webhook for "
                    "route '%s' (/api/webhook/%s)",
                    name,
                    masked_wh,
                )
                raise ConfigEntryError(
                    f"Failed to register webhook for route '{name}': {err}"
                ) from err

            registered[webhook_id] = {"name": name, "target_url": target_url}
            _LOGGER.info(
                "Pacific Shift MCP Proxy: route '%s' -> %s registered at "
                "/api/webhook/%s",
                name,
                target_url,
                masked_wh,
            )
    except ConfigEntryError:
        # Roll back anything we already registered this call, then close the
        # session — don't leave half the routes live on a failed setup.
        for webhook_id in registered:
            async_unregister(hass, webhook_id)
        await session.close()
        raise

    hass.data[DOMAIN] = {"routes": registered, "session": session}
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload: unregister every route's webhook and close the shared session."""
    data = hass.data.pop(DOMAIN, {})
    for webhook_id in data.get("routes", {}):
        async_unregister(hass, webhook_id)
    session = data.get("session")
    if session:
        await session.close()
    return True


def _read_config() -> dict | None:
    """Read proxy config from JSON file (blocking I/O).

    Returns None only when the file does not exist (fresh install, add-on
    not started yet). Read/parse errors propagate so the caller can tell
    "no config yet" apart from "config is corrupted".
    """
    if not CONFIG_FILE.exists():
        return None
    data: dict = json.loads(CONFIG_FILE.read_text())
    return data


def _make_webhook_handler(target_url: str, route_name: str, session: aiohttp.ClientSession):
    """Build a webhook handler bound to one route's target_url.

    Each route gets its own closure so a single config entry can proxy N
    upstream MCP servers through N independent webhook IDs, all reachable
    over the same Nabu Casa / reverse-proxy tunnel that already exposes HA
    port 8123 — no per-route tunnel or port-forward needed.
    """

    async def _handle_webhook(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.StreamResponse:
        body = await request.read()

        forward_headers = {}
        for key, value in request.headers.items():
            if key.lower() in (
                "host",
                "content-length",
                "transfer-encoding",
                "connection",
                "cookie",
            ):
                continue
            forward_headers[key] = value

        try:
            async with session.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                data=body if body else None,
            ) as upstream_resp:
                content_type = upstream_resp.headers.get("Content-Type", "")

                resp_headers = {
                    "Cache-Control": "no-cache, no-transform",
                    "Content-Encoding": "identity",
                }
                mcp_session = upstream_resp.headers.get("Mcp-Session-Id")
                if mcp_session:
                    resp_headers["Mcp-Session-Id"] = mcp_session

                if "text/event-stream" in content_type:
                    resp_headers["Content-Type"] = "text/event-stream"
                    resp_headers["X-Accel-Buffering"] = "no"
                    response = web.StreamResponse(
                        status=upstream_resp.status, headers=resp_headers
                    )
                    await response.prepare(request)
                    async for chunk in upstream_resp.content.iter_any():
                        await response.write(chunk)
                    await response.write_eof()
                    return response

                if not any(ct in content_type for ct in _ALLOWED_CONTENT_TYPES):
                    content_type = "application/json"
                resp_headers["Content-Type"] = content_type
                resp_body = await upstream_resp.read()
                return web.Response(
                    status=upstream_resp.status,
                    body=resp_body,
                    headers=resp_headers,
                )
        except aiohttp.ClientError as err:
            _LOGGER.error(
                "Pacific Shift MCP Proxy: route '%s' upstream request failed: %s",
                route_name,
                err,
            )
            return web.Response(
                status=502, text=f"Pacific Shift MCP Proxy: '{route_name}' unavailable"
            )
        except Exception:
            _LOGGER.exception(
                "Pacific Shift MCP Proxy: route '%s' unexpected error", route_name
            )
            return web.Response(status=500, text="Pacific Shift MCP Proxy: internal error")

    return _handle_webhook
