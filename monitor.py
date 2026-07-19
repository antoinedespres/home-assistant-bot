#!/usr/bin/env python3
"""Entry point: subscribes to Home Assistant's state_changed events and posts
color-coded Discord embed alerts for door/window sensors, tamper sensors,
and power consumption thresholds. Entities are discovered automatically by
`device_class` - no entity IDs need to be configured.

Events missed during a connection gap are backfilled from HA's history API
on reconnect (see ha_client.py) and reported with their real timestamp."""
import logging
import os

from alerting import CRITICAL, INFO, OK, WARNING, DiscordNotifier, Monitor
from commands import InteractionHandler, fetch_application_id, register_guild_commands
from ha_client import HomeAssistantClient
from presence import GatewayPresence

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("home-assistant-bot")

DOOR_DEVICE_CLASSES = {"door", "opening", "window", "garage_door"}
# Entities the alerting/backfill side actively watches and reacts to.
RELEVANT_DEVICE_CLASSES = DOOR_DEVICE_CLASSES | {"tamper", "power"}
# Entities shown by the read-only /status and /entities commands - a wider
# set than alerting, since a status dashboard is useful even for sensors
# that don't (yet) have alert logic of their own.
STATUS_DEVICE_CLASSES = RELEVANT_DEVICE_CLASSES | {"temperature", "humidity", "battery"}


def env(name, default=None, cast=str):
    value = os.environ.get(name, default)
    return cast(value) if value is not None else None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def handle_door(notifier, door_state, entity_id, new_state, timestamp, backfilled):
    friendly_name = new_state["attributes"].get("friendly_name", entity_id)
    is_open = new_state["state"] == "on"
    was_open = door_state.get(entity_id)
    if was_open is is_open:
        return
    door_state[entity_id] = is_open

    if is_open:
        notifier.send_embed(
            f"{friendly_name}: opened", "Door/window sensor opened.",
            {"Entity": entity_id}, WARNING, timestamp=timestamp, backfilled=backfilled,
        )
    else:
        notifier.send_embed(
            f"{friendly_name}: closed", "Door/window sensor closed.",
            {"Entity": entity_id}, OK, timestamp=timestamp, backfilled=backfilled,
        )


def handle_tamper(notifier, tamper_state, entity_id, new_state, timestamp, backfilled):
    friendly_name = new_state["attributes"].get("friendly_name", entity_id)
    is_tampered = new_state["state"] == "on"
    was_tampered = tamper_state.get(entity_id)
    if was_tampered is is_tampered:
        return
    tamper_state[entity_id] = is_tampered

    if is_tampered:
        notifier.send_embed(
            f"{friendly_name}: TAMPER DETECTED", "A tamper sensor was triggered.",
            {"Entity": entity_id}, CRITICAL, timestamp=timestamp, backfilled=backfilled,
        )
    else:
        notifier.send_embed(
            f"{friendly_name}: tamper cleared", "The tamper condition cleared.",
            {"Entity": entity_id}, OK, timestamp=timestamp, backfilled=backfilled,
        )


def handle_power(notifier, power_monitors, entity_id, new_state, warn_w, crit_w, timestamp, backfilled):
    value = as_float(new_state["state"])
    if value is None:
        return
    friendly_name = new_state["attributes"].get("friendly_name", entity_id)
    unit = new_state["attributes"].get("unit_of_measurement", "W")

    if entity_id not in power_monitors:
        power_monitors[entity_id] = Monitor(
            friendly_name, notifier, warn_threshold=warn_w, crit_threshold=crit_w,
            metric_label="Power draw", unit=unit,
        )
    power_monitors[entity_id].update(
        value, context={"Entity": entity_id}, timestamp=timestamp, backfilled=backfilled,
    )


def main():
    token = env("DISCORD_BOT_TOKEN")
    channel_id = env("DISCORD_CHANNEL_ID")
    guild_id = env("DISCORD_GUILD_ID")
    ha_url = env("HA_URL", "ws://localhost:8123/api/websocket")
    ha_token = env("HA_TOKEN")
    power_warn_w = env("POWER_WARN_THRESHOLD_W", "1500", float)
    power_crit_w = env("POWER_CRIT_THRESHOLD_W", "2000", float)

    if not token or not channel_id:
        raise SystemExit("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set (see .env.example)")
    if not ha_token:
        raise SystemExit("HA_TOKEN must be set to a Home Assistant long-lived access token (see .env.example)")

    notifier = DiscordNotifier(token, channel_id)
    version = env("APP_VERSION", "dev")
    logger.info("home-assistant-bot starting (version %s), connecting to %s", version, ha_url)
    notifier.send_embed(
        "home-assistant-bot started",
        "Deployment succeeded and the bot is now watching Home Assistant.",
        {"Version": version, "HA URL": ha_url},
        INFO,
    )

    client = HomeAssistantClient(ha_url, ha_token, RELEVANT_DEVICE_CLASSES)

    on_interaction = None
    if guild_id:
        application_id = fetch_application_id(token)
        register_guild_commands(token, application_id, guild_id)
        on_interaction = InteractionHandler(application_id, token, client, STATUS_DEVICE_CLASSES).handle
    else:
        logger.info("DISCORD_GUILD_ID not set - slash commands disabled")

    GatewayPresence(token, on_interaction=on_interaction).start()

    door_state = {}
    tamper_state = {}
    power_monitors = {}

    for entity_id, old_state, new_state, backfilled in client.state_changes():
        if new_state is None or new_state["state"] in ("unavailable", "unknown"):
            continue
        device_class = new_state["attributes"].get("device_class")
        timestamp = new_state.get("last_changed")

        if entity_id.startswith("binary_sensor.") and device_class in DOOR_DEVICE_CLASSES:
            handle_door(notifier, door_state, entity_id, new_state, timestamp, backfilled)
        elif entity_id.startswith("binary_sensor.") and device_class == "tamper":
            handle_tamper(notifier, tamper_state, entity_id, new_state, timestamp, backfilled)
        elif entity_id.startswith("sensor.") and device_class == "power":
            handle_power(
                notifier, power_monitors, entity_id, new_state,
                power_warn_w, power_crit_w, timestamp, backfilled,
            )


if __name__ == "__main__":
    main()
