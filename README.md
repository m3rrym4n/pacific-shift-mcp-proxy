# Pacific Shift MCP Proxy

A multi-route MCP webhook proxy for Home Assistant. Routes claude.ai traffic through Nabu Casa to multiple MCP servers running on your LAN.

Based on the webhook proxy pattern from [homeassistant-ai/ha-mcp](https://github.com/homeassistant-ai/ha-mcp) (MIT License). Credit to the ha-mcp team for the core webhook proxy architecture.

## Why This Exists

The ha-mcp webhook proxy is hardwired to a single upstream target. This fork removes that constraint and replaces upstream auto-discovery with a simple config-based multi-route system.

This lets you expose multiple MCP servers through a single Home Assistant instance using the same Nabu Casa subscription — no extra tunnels, no OAuth broker bugs, no new infrastructure.

## How It Works

1. The add-on installs a lightweight custom integration into Home Assistant
2. For each configured route, it registers a webhook endpoint — the URL itself is the shared secret
3. When a request hits a webhook, it is proxied to the configured upstream MCP server
4. Nabu Casa provides the public HTTPS URL

## Installation

1. Add this repository to your Home Assistant add-on store:
   - Settings → Add-ons → Add-on Store → ⋮ → Repositories
   - Add: `https://github.com/m3rrym4n/pacific-shift-mcp-proxy`

2. Install **Pacific Shift MCP Proxy** from the add-on store

3. Configure your routes (see below)

4. Start the add-on and restart Home Assistant when prompted

5. Copy the webhook URLs from the add-on logs and add them as custom connectors in claude.ai

## Configuration

```yaml
routes:
  - name: bookstack
    upstream: "http://192.168.1.68:6001"
  - name: task_runner
    upstream: "http://192.168.1.68:6002"
remote_url: ""  # Leave blank for Nabu Casa auto-detection
debug_logging: false
```

## Webhook URLs

Each route gets its own webhook URL. After starting the add-on:

```
MCP Proxy URL (bookstack):    https://xxxxx.ui.nabu.casa/api/webhook/mcp_abc123
MCP Proxy URL (task_runner):  https://xxxxx.ui.nabu.casa/api/webhook/mcp_def456
```

Add each URL as a separate custom connector in claude.ai — no OAuth fields needed.

## Security

The webhook URL is the shared secret. Treat each URL like a password. Rotate by deleting `/data/webhook_id_{name}.txt` and restarting the add-on.

## License

MIT. Based on homeassistant-ai/ha-mcp (MIT).
