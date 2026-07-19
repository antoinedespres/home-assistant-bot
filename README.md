# Home Assistant Bot

Discord bot that watches a Home Assistant instance and posts color-coded alert embeds (🟢 recovered / 🟠 warning / 🔴 critical) for:

- **Door/window sensors** — 🟠 when opened, 🟢 when closed.
- **Tamper sensors** — 🔴 when triggered, 🟢 when cleared.
- **Power consumption** — 🟠/🔴 when a plug crosses a configurable wattage threshold, 🟢 when it drops back below.

Entities are discovered **automatically by `device_class`** (`door`/`opening`/`window`/`garage_door`, `tamper`, `power`) via Home Assistant's `state_changed` events — no entity IDs need to be configured. Works with any Home Assistant instance, any integration (not specific to Zigbee/ZHA).

## Setup

1. **Discord bot**: create a Discord Application + Bot at [discord.com/developers/applications](https://discord.com/developers/applications), copy its token, and invite it to your server with the `Send Messages` + `Embed Links` permissions (OAuth2 → URL Generator → scope `bot`).
2. **Home Assistant long-lived access token**: in Home Assistant, click your profile (bottom left) → **Security** tab → **Create long-lived access token** → copy it (shown only once).
3. Copy `.env.example` to `.env` and fill in `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `HA_URL` (`ws://` on a local network, `wss://` through a public HTTPS reverse proxy), and `HA_TOKEN`.
4. Run it:
   ```
   docker compose up -d
   ```

## Configuration

All settings live in `.env` (see `.env.example`): Discord credentials, the Home Assistant WebSocket URL/token, and the power sensor warn/critical thresholds (in whatever unit your sensors report, usually W).

## CI/CD

`.github/workflows/release-and-deploy.yml` follows a simple release flow: on every push to `main`, it bumps the patch version in `VERSION`, tags the commit, builds a Docker image, pushes it to `ghcr.io/<owner>/home-assistant-bot`, then SSHes into a target host and runs `docker compose pull && docker compose up -d`.

Required repo secrets: `VPS_HOST`, `VPS_SSH_PORT`, `VPS_SSH_USER`, `VPS_SSH_KEY` (a dedicated deploy keypair, not your personal key). The deploy target must have already run `docker login ghcr.io` once with a token that has `read:packages` access, and have this repo's `docker-compose.yml` + `.env` in `~/apps/home-assistant-bot/`.

## Notes

- Home Assistant's WebSocket API handles reconnection gracefully on this bot's side (auto-reconnects with backoff on any connection error).
- **Missed-event backfill**: if the connection drops (network blip, HA restart, VPS-to-HA link down, ...), any state changes that happened during the gap are not lost. On reconnect, the bot queries Home Assistant's REST history API (`/api/history/period`) for the door/tamper/power entities, and replays whatever it missed — each alert is tagged "(backfilled)" and carries the event's real historical timestamp, not the reconnect time. Entities to backfill are discovered the same way as live ones (`device_class` via `/api/states`), so this also needs no configuration. This only recovers events Home Assistant itself recorded — if the underlying integration (e.g. a Zigbee coordinator) also lost the event, there's nothing to backfill from.
- The bot also opens a minimal Discord Gateway connection just to show as online with a status — no events are received over it, alerts are still sent over plain REST.
