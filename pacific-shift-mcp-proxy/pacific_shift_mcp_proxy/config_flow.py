"""Config flow for Pacific Shift MCP Proxy.

This integration carries no user-facing setup UI. The add-on is the only
thing that creates its config entry (via the HA Core API, see start.py),
and the entry itself stores no data — the real configuration (routes,
webhook IDs, target URLs) lives in /config/.pacific_shift_mcp_proxy_config.json,
written by the add-on and read by __init__.py on setup/reload. This mirrors
the ha-mcp webhook proxy's mcp_proxy/config_flow.py pattern exactly.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

DOMAIN = "pacific_shift_mcp_proxy"


class PacificShiftMcpProxyConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Pacific Shift MCP Proxy."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle setup via the UI (not the normal path — see async_step_import)."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Pacific Shift MCP Proxy", data={})

        return self.async_show_form(step_id="user")

    async def async_step_import(
        self, import_data: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle auto-creation from the add-on's Supervisor API call."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Pacific Shift MCP Proxy", data={})
