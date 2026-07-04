#!/usr/bin/env python3
"""Pacific Shift MCP Proxy — add-on startup script.

This add-on does NOT run its own HTTP server (that was the bug in v1.0.0 —
requests hit port 8099 with nothing routing traffic to it from Nabu Casa).
Instead it installs a small custom HA integration
(custom_components/pacific_shift_mcp_proxy) that registers one native HA
webhook per configured route via hass.components.webhook — the same
mechanism ha-mcp's own webhook proxy uses, and the reason that one works
with Nabu Casa with zero extra tunneling. HA's HTTP layer (port 8123) is
what Nabu Casa already exposes; a registered webhook is reachable at
https://<nabu-casa-url>/api/webhook/<webhook_id> immediately.

This add-on's job is purely orchestration:
  1. Read `routes` from add-on options (name + upstream per route).
  2. Get-or-create a stable webhook_id per route name (persisted in /data
     so URLs survive add-on restarts).
  3. Write /config/.pacific_shift_mcp_proxy_config.json for the integration
     to read.
  4. Install/update the integration files into /config/custom_components/.
  5. Ensure a config entry exists (create via Supervisor->HA Core API if
     missing) and reload it so route changes take effect without a full
     HA restart. A full restart is only required on first install or when
     the integration's Python code itself changes version.
  6. Print each route's public webhook URL and run a lightweight keep-alive
     / health-check loop.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

DOMAIN = "pacific_shift_mcp_proxy"
INTEGRATION_SRC = Path("/opt/pacific_shift_mcp_proxy")
INTEGRATION_DST = Path("/config/custom_components/pacific_shift_mcp_proxy")
PROXY_CONFIG_FILE = Path("/config/.pacific_shift_mcp_proxy_config.json")
DATA_DIR = Path("/data")
WEBHOOK_IDS_FILE = DATA_DIR / "webhook_ids.json"


class IntegrationInstall(NamedTuple):
    first_install: bool
    version_changed: bool


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(level: str, message: str, stream=None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} [{level}] {message}", file=stream, flush=True)


def log_info(message: str) -> None:
    _log("INFO", message)


def log_error(message: str) -> None:
    _log("ERROR", message, sys.stderr)


# ---------------------------------------------------------------------------
# Supervisor / HA Core API helpers
# ---------------------------------------------------------------------------


def _ha_core_api(method: str, path: str, data: dict | None = None) -> dict | list | None:
    """Request to HA Core API via the Supervisor proxy. Returns parsed JSON."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    url = f"http://supervisor/core/api{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        log_error(f"HA Core API {method} {path}: {e}")
        return None


def _ha_core_api_quiet(method: str, path: str) -> list | dict | None:
    """Like _ha_core_api but suppresses error logging (for polling loops)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    url = f"http://supervisor/core/api{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def get_nabu_casa_url() -> str | None:
    """Read Nabu Casa remote URL from HA cloud storage."""
    cloud_storage = Path("/config/.storage/cloud")
    try:
        if cloud_storage.exists():
            cloud_data = json.loads(cloud_storage.read_text())
            data = cloud_data.get("data", {})
            if data.get("remote_enabled"):
                domain = data.get("remote_domain")
                if domain:
                    return f"https://{domain}"
    except (OSError, json.JSONDecodeError) as e:
        log_info(f"Nabu Casa cloud config not available: {e}")
    return None


def _resolve_remote_url(remote_url: str) -> str | None:
    if remote_url and remote_url.strip():
        url = remote_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url
    return get_nabu_casa_url()


# ---------------------------------------------------------------------------
# Per-route webhook ID persistence
# ---------------------------------------------------------------------------


def _load_webhook_ids() -> dict[str, str]:
    if WEBHOOK_IDS_FILE.exists():
        try:
            data = json.loads(WEBHOOK_IDS_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not read {WEBHOOK_IDS_FILE}: {e}")
    return {}


def _save_webhook_ids(ids: dict[str, str]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WEBHOOK_IDS_FILE.write_text(json.dumps(ids))
    except OSError as e:
        log_error(f"Failed to persist webhook IDs: {e}")


def _get_or_create_webhook_id(ids: dict[str, str], route_name: str) -> str:
    """Stable per-route webhook ID — generated once, reused across restarts
    so a route's public URL doesn't change every time the add-on restarts."""
    if route_name in ids and ids[route_name]:
        return ids[route_name]
    wid = f"psmp_{secrets.token_hex(16)}"
    ids[route_name] = wid
    return wid


# ---------------------------------------------------------------------------
# Integration install
# ---------------------------------------------------------------------------


def _install_integration() -> IntegrationInstall:
    """Copy/update the pacific_shift_mcp_proxy custom component into HA config."""
    if not INTEGRATION_SRC.exists():
        log_error(f"Integration source not found at {INTEGRATION_SRC}")
        return IntegrationInstall(False, False)

    Path("/config/custom_components").mkdir(parents=True, exist_ok=True)

    first_install = not INTEGRATION_DST.exists()
    src_manifest = INTEGRATION_SRC / "manifest.json"
    dst_manifest = INTEGRATION_DST / "manifest.json"

    sv = dv = None
    if src_manifest.exists():
        try:
            sv = json.loads(src_manifest.read_text()).get("version")
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not parse source manifest: {e}")
    if dst_manifest.exists():
        try:
            dv = json.loads(dst_manifest.read_text()).get("version")
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not parse destination manifest: {e}")

    versions_differ = sv is not None and dv is not None and sv != dv
    needs_update = first_install or versions_differ or dv is None
    version_changed = versions_differ and not first_install

    if needs_update:
        if INTEGRATION_DST.exists():
            shutil.rmtree(INTEGRATION_DST)
        shutil.copytree(INTEGRATION_SRC, INTEGRATION_DST)
        log_info("Installed pacific_shift_mcp_proxy integration")
    else:
        log_info("pacific_shift_mcp_proxy integration up to date")

    return IntegrationInstall(first_install, version_changed)


def _ensure_config_entry(retries: int = 5, delay: int = 10) -> bool:
    for attempt in range(1, retries + 1):
        entries = _ha_core_api("GET", "/config/config_entries/entry")
        if entries is not None:
            for entry in entries:
                if isinstance(entry, dict) and entry.get("domain") == DOMAIN:
                    log_info("Config entry exists")
                    return True

            log_info(f"Creating config entry (attempt {attempt}/{retries})...")
            flow = _ha_core_api("POST", "/config/config_entries/flow", {"handler": DOMAIN})
            if flow is None:
                if attempt < retries:
                    time.sleep(delay)
                continue
            if not isinstance(flow, dict):
                continue
            rtype = flow.get("type")
            if rtype in ("abort", "create_entry"):
                log_info("Config entry ready")
                return True
            if rtype == "form" and flow.get("flow_id"):
                complete = _ha_core_api(
                    "POST", f"/config/config_entries/flow/{flow['flow_id']}", {}
                )
                if isinstance(complete, dict) and complete.get("type") == "create_entry":
                    log_info("Config entry created")
                    return True

        if attempt < retries:
            log_info(f"HA not ready, retrying in {delay}s...")
            time.sleep(delay)
    return False


def _remove_config_entry() -> None:
    entries = _ha_core_api("GET", "/config/config_entries/entry")
    if entries is None:
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain") == DOMAIN:
            eid = entry.get("entry_id")
            if eid:
                _ha_core_api("DELETE", f"/config/config_entries/entry/{eid}")
                log_info("Removed config entry")


def _reload_config_entry() -> None:
    entries = _ha_core_api("GET", "/config/config_entries/entry")
    if entries is None:
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain") == DOMAIN:
            eid = entry.get("entry_id")
            if eid:
                result = _ha_core_api("POST", f"/config/config_entries/entry/{eid}/reload")
                if result is not None:
                    log_info("Reloaded config entry")
                return


def _wait_for_ha_restart(poll_interval: int = 10, timeout: int = 600) -> None:
    log_info("Waiting for Home Assistant to restart...")
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        result = _ha_core_api_quiet("GET", "/config/config_entries/entry")
        if result is None:
            log_info("HA Core is restarting...")
            break
        if isinstance(result, list):
            for entry in result:
                if isinstance(entry, dict) and entry.get("domain") == DOMAIN:
                    log_info("Integration already loaded — HA must have restarted")
                    return
        time.sleep(poll_interval)

    while time.monotonic() - start < timeout:
        time.sleep(poll_interval)
        result = _ha_core_api_quiet("GET", "/config/config_entries/entry")
        if result is not None:
            log_info("HA Core is back up")
            return

    log_info("Timed out waiting for HA restart — continuing anyway")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def _health_check(target_url: str) -> bool:
    try:
        parsed = urlparse(target_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        with socket.create_connection((host, port), timeout=5):
            return True
    except (OSError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def _install_shutdown_handlers() -> dict[str, str | None]:
    shutdown_reason: dict[str, str | None] = {"reason": None}

    def _on_signal(signum, _frame) -> None:
        shutdown_reason["reason"] = signal.Signals(signum).name
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)
    return shutdown_reason


def _shutdown_cleanup(reason: str | None) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, signal.SIG_DFL)
    log_info(f"Shutting down (reason: {reason or 'unknown'})...")
    # Keep the config entry + integration files + persisted webhook IDs in
    # place on a normal stop/restart — only a full uninstall should remove
    # them, and that's a manual step (matches ha-mcp's webhook proxy).
    log_info("Pacific Shift MCP Proxy stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    log_info("Starting Pacific Shift MCP Proxy...")

    config_file = Path("/data/options.json")
    remote_url = ""
    debug_logging = False
    raw_routes: list[dict] = []

    if not config_file.exists():
        log_error("No add-on configuration found at /data/options.json")
        return 1

    try:
        config = json.load(config_file.open())
        remote_url = config.get("remote_url", "")
        debug_logging = bool(config.get("debug_logging", False))
        raw_routes = config.get("routes", [])
    except (OSError, json.JSONDecodeError) as e:
        log_error(f"Could not read add-on configuration ({type(e).__name__}): {e}")
        return 1

    if not raw_routes:
        log_error(
            "No routes configured. Add at least one route (name + upstream) "
            "in the add-on's Configuration tab."
        )
        return 1

    # Resolve stable webhook IDs per route and build the config file the
    # integration reads.
    webhook_ids = _load_webhook_ids()
    resolved_routes = []
    for route in raw_routes:
        name = route.get("name", "").strip()
        upstream = route.get("upstream", "").strip()
        if not name or not upstream:
            log_error(f"Skipping malformed route entry: {route!r}")
            continue
        webhook_id = _get_or_create_webhook_id(webhook_ids, name)
        resolved_routes.append({"name": name, "webhook_id": webhook_id, "target_url": upstream})
    _save_webhook_ids(webhook_ids)

    if not resolved_routes:
        log_error("No valid routes after validation. Check the add-on configuration.")
        return 1

    proxy_config = {"routes": resolved_routes, "debug_logging": debug_logging}
    try:
        PROXY_CONFIG_FILE.write_text(json.dumps(proxy_config))
    except OSError as e:
        log_error(f"Failed to write proxy config: {e}")
        return 1

    first_install, version_changed = _install_integration()

    if version_changed:
        log_info("")
        log_info("*" * 60)
        log_info("  INTEGRATION UPDATED — restart Home Assistant to load")
        log_info("  the new pacific_shift_mcp_proxy code.")
        log_info("*" * 60)
        _ha_core_api(
            "POST",
            "/services/persistent_notification/create",
            {
                "title": "Pacific Shift MCP Proxy: Restart Required",
                "message": (
                    "The integration was updated to a new version. Restart "
                    "Home Assistant (Settings -> System -> Restart) to load "
                    "the new code. Existing routes keep working with the "
                    "previous version's code in the meantime."
                ),
                "notification_id": "pacific_shift_mcp_proxy_update",
            },
        )

    if first_install:
        log_info("First install detected — HA restart required to load integration")
        _ha_core_api(
            "POST",
            "/services/persistent_notification/create",
            {
                "title": "Pacific Shift MCP Proxy: Restart Required",
                "message": (
                    "The Pacific Shift MCP Proxy integration was installed. "
                    "Restart Home Assistant (Settings -> System -> Restart) "
                    "to complete setup. Routes will activate automatically "
                    "after restart."
                ),
                "notification_id": "pacific_shift_mcp_proxy_restart",
            },
        )
        _wait_for_ha_restart()
        if not _ensure_config_entry():
            log_error(
                "Could not create config entry after HA restart. Routes are "
                "NOT active. Restart Home Assistant again, or add the "
                "integration manually from Settings -> Devices & Services."
            )
        else:
            _reload_config_entry()
            _ha_core_api(
                "POST",
                "/services/persistent_notification/dismiss",
                {"notification_id": "pacific_shift_mcp_proxy_restart"},
            )
            log_info("Setup completed after HA restart")
    else:
        if not _ensure_config_entry():
            log_error(
                "Could not create config entry. Routes are NOT active. "
                "Restart Home Assistant; if the problem persists, add the "
                "integration manually from Settings -> Devices & Services."
            )
        else:
            # Reload so the integration re-reads the config file we just
            # wrote — picks up route changes without a full HA restart.
            _reload_config_entry()
            _ha_core_api(
                "POST",
                "/services/persistent_notification/dismiss",
                {"notification_id": "pacific_shift_mcp_proxy_restart"},
            )

    resolved_remote = _resolve_remote_url(remote_url)
    log_info("")
    log_info("=" * 70)
    for route in resolved_routes:
        log_info(f"  Route '{route['name']}' -> {route['target_url']}")
        if resolved_remote:
            log_info(f"    Public URL: {resolved_remote}/api/webhook/{route['webhook_id']}")
        else:
            log_info(
                f"    Public URL: https://<your-external-url>/api/webhook/{route['webhook_id']}"
            )
            log_info("    Set 'remote_url' in add-on config, or enable Nabu Casa")
        log_info("")
    log_info("  Each webhook URL above IS the shared secret — copy the full")
    log_info("  URL into your MCP client's connector configuration.")
    log_info("=" * 70)
    log_info("")

    shutdown_reason = _install_shutdown_handlers()

    log_info("Entering keep-alive loop (health check every 60s)...")
    consecutive_failures = {r["name"]: 0 for r in resolved_routes}
    last_health = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_health >= 60:
                last_health = now
                for route in resolved_routes:
                    if _health_check(route["target_url"]):
                        if consecutive_failures[route["name"]] > 0:
                            log_info(f"Route '{route['name']}' is reachable again")
                        consecutive_failures[route["name"]] = 0
                    else:
                        consecutive_failures[route["name"]] += 1
                        n = consecutive_failures[route["name"]]
                        if n == 1:
                            log_error(f"Route '{route['name']}' unreachable: {route['target_url']}")
                        elif n % 5 == 0:
                            log_error(f"Route '{route['name']}' still unreachable after {n} checks")
            time.sleep(10)
    except KeyboardInterrupt:
        if shutdown_reason["reason"] is None:
            shutdown_reason["reason"] = "KeyboardInterrupt"

    _shutdown_cleanup(shutdown_reason["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
