# AGENTS.md — Pacific Shift MCP Proxy

## Purpose

HA add-on that proxies MCP traffic from claude.ai (via Nabu Casa webhook) to multiple MCP servers running on the LAN.

Multi-route extension of the ha-mcp webhook proxy pattern. Each configured route gets its own webhook ID and proxies to a different upstream MCP server on ZimaOS.

## Architecture

```
claude.ai
    ↓ HTTPS (Nabu Casa — existing subscription)
Home Assistant Pi (192.168.1.79) port 8123
    /api/webhook/mcp_{id_bookstack}   → bookstack-mcp  (192.168.1.68:6001)
    /api/webhook/mcp_{id_task_runner} → task-runner    (192.168.1.68:6002)
```

## Stack

- Python 3 + aiohttp
- HA add-on (runs as container on HA Pi, NOT on ZimaOS)
- No database dependency
- Webhook IDs persisted to /data/webhook_id_{name}.txt per route

## Add-on Structure

```
pacific-shift-mcp-proxy/
    config.yaml          — HA add-on manifest
    Dockerfile           — Container build
    rootfs/
        etc/services.d/mcp-proxy/run    — s6-overlay entrypoint
        etc/services.d/mcp-proxy/finish — s6-overlay shutdown
        usr/bin/mcp_proxy.py            — Main proxy application
```

## Configuration (via HA add-on UI)

```yaml
routes:
  - name: bookstack
    upstream: "http://192.168.1.68:6001"
  - name: task_runner
    upstream: "http://192.168.1.68:6002"
remote_url: ""  # blank = Nabu Casa auto-detect
debug_logging: false
```

## Deployment — IMPORTANT

This add-on deploys via HA's add-on store, NOT via Codex/docker build.

To deploy changes:
1. Push code changes to GitHub (via Claude GitHub MCP or manual git push from ZimaOS)
2. In HA: Settings → Add-ons → Pacific Shift MCP Proxy → Update (or reinstall)
3. Restart the add-on
4. Check logs for the webhook URLs

There is NO manual docker build step — HA builds the image from the Dockerfile automatically.

## Adding a New Route

1. Deploy the new MCP server on ZimaOS at a port in the 6000 range
2. Add the route in HA add-on config
3. Restart the add-on
4. Copy new webhook URL from logs
5. Add as new custom connector in claude.ai

## Rotating a Webhook URL

1. Stop the add-on
2. Delete /data/webhook_id_{name}.txt via HA terminal
3. Start the add-on — new ID generated
4. Update claude.ai connector with new URL

## Port

Proxy listens on port 8099 internally. HA routes webhook traffic via its own HTTP layer.

## Upstream MCP Ports (ZimaOS 192.168.1.68)

- 6001 — bookstack-mcp
- 6002 — task-runner  
- 6003 — future: portainer-mcp
- 6004 — future: unifi-mcp
