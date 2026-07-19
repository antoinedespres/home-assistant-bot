"""Discord slash commands: registration + interaction handling.

Read-only commands built on the same HA data paths the alerting side
already uses (device_class discovery, history API) - no new HA
permissions or entity configuration needed.
"""
import datetime
import logging

import requests

logger = logging.getLogger("home-assistant-bot")

API = "https://discord.com/api/v10"

# Discord option types
OPTION_STRING = 3
OPTION_INTEGER = 4

# Discord interaction/response types
TYPE_APPLICATION_COMMAND = 2
CALLBACK_DEFERRED_CHANNEL_MESSAGE = 5

COLOR_INFO = 0x3498DB
COLOR_WARN = 0xE67E22
COLOR_ERROR = 0xE74C3C

# Groups several raw device_classes under one /status section, in display order.
STATUS_CATEGORIES = [
    ("doors", "🚪 Doors & Windows", {"door", "opening", "window", "garage_door"}),
    ("tamper", "🚨 Tamper", {"tamper"}),
    ("power", "⚡ Power", {"power"}),
    ("temperature", "🌡️ Temperature", {"temperature"}),
    ("humidity", "💧 Humidity", {"humidity"}),
    ("battery", "🔋 Battery", {"battery"}),
]
DEVICE_CLASS_TO_CATEGORY = {dc: key for key, _, classes in STATUS_CATEGORIES for dc in classes}
CATEGORY_LABELS = {key: label for key, label, _ in STATUS_CATEGORIES}
CATEGORY_ORDER = {key: i for i, (key, _, _) in enumerate(STATUS_CATEGORIES)}

COMMANDS = [
    {
        "name": "status",
        "description": "Show the current state of every monitored entity (doors, tamper, power)",
    },
    {
        "name": "entities",
        "description": "List the entities currently auto-discovered for monitoring",
    },
    {
        "name": "battery",
        "description": "List battery levels for battery-powered sensors",
    },
    {
        "name": "history",
        "description": "Show recent history for one entity",
        "options": [
            {
                "name": "entity_id",
                "description": "Entity ID, e.g. binary_sensor.front_door (see /entities)",
                "type": OPTION_STRING,
                "required": True,
            },
            {
                "name": "hours",
                "description": "How many hours back to look (default 24)",
                "type": OPTION_INTEGER,
                "required": False,
            },
        ],
    },
]


def fetch_application_id(bot_token):
    resp = requests.get(f"{API}/oauth2/applications/@me", headers={"Authorization": f"Bot {bot_token}"}, timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


def register_guild_commands(bot_token, application_id, guild_id):
    """Guild-scoped registration: updates instantly (global commands can take
    up to an hour to propagate), which matters while iterating/testing."""
    url = f"{API}/applications/{application_id}/guilds/{guild_id}/commands"
    resp = requests.put(url, headers={"Authorization": f"Bot {bot_token}"}, json=COMMANDS, timeout=15)
    if resp.status_code >= 300:
        logger.error("Failed to register slash commands: %s %s", resp.status_code, resp.text)
    else:
        logger.info("Registered %d slash command(s) for guild %s", len(COMMANDS), guild_id)


def _friendly_name(state):
    return state.get("attributes", {}).get("friendly_name", state.get("entity_id"))


class InteractionHandler:
    def __init__(self, application_id, bot_token, ha_client, device_classes):
        self.application_id = application_id
        self.bot_token = bot_token
        self.ha_client = ha_client
        self.device_classes = device_classes

    def handle(self, interaction):
        """Entry point for a Gateway INTERACTION_CREATE dispatch. Meant to be
        run in its own thread - deferring/responding does blocking HTTP."""
        if interaction.get("type") != TYPE_APPLICATION_COMMAND:
            return
        interaction_id = interaction["id"]
        token = interaction["token"]
        name = interaction["data"]["name"]
        options = {opt["name"]: opt["value"] for opt in interaction["data"].get("options", [])}
        logger.info("Slash command received: /%s %s", name, options)

        self._defer(interaction_id, token)
        try:
            embed = self._dispatch(name, options)
        except Exception:
            logger.exception("Error handling /%s", name)
            embed = {"title": "⚠️ Error", "description": "Something went wrong handling that command.", "color": COLOR_ERROR}
        self._respond(token, embed)

    def _defer(self, interaction_id, token):
        url = f"{API}/interactions/{interaction_id}/{token}/callback"
        resp = requests.post(url, json={"type": CALLBACK_DEFERRED_CHANNEL_MESSAGE}, timeout=10)
        if resp.status_code >= 300:
            logger.error("Failed to defer interaction: %s %s", resp.status_code, resp.text)

    def _respond(self, token, embed):
        url = f"{API}/webhooks/{self.application_id}/{token}/messages/@original"
        resp = requests.patch(url, json={"embeds": [embed]}, timeout=10)
        if resp.status_code >= 300:
            logger.error("Failed to send interaction response: %s %s", resp.status_code, resp.text)

    def _dispatch(self, name, options):
        if name == "status":
            return self._cmd_status()
        if name == "entities":
            return self._cmd_entities()
        if name == "battery":
            return self._cmd_battery()
        if name == "history":
            return self._cmd_history(options.get("entity_id"), options.get("hours", 24))
        return {"title": "Unknown command", "color": COLOR_WARN}

    def _cmd_status(self):
        states = self.ha_client.fetch_states_by_device_class(self.device_classes)
        if not states:
            return {"title": "Status", "description": "No monitored entities found.", "color": COLOR_WARN}

        buckets = {}
        uncategorized = []
        for s in states:
            category = DEVICE_CLASS_TO_CATEGORY.get(s["attributes"].get("device_class"))
            if category:
                buckets.setdefault(category, []).append(s)
            else:
                uncategorized.append(s)

        fields = []
        for category in sorted(buckets, key=lambda c: CATEGORY_ORDER.get(c, 99)):
            lines = [
                f"**{_friendly_name(s)}**: {_format_status_value(s, category)}"
                for s in sorted(buckets[category], key=_friendly_name)
            ]
            fields.append({"name": CATEGORY_LABELS[category], "value": "\n".join(lines)[:1024], "inline": False})
        if uncategorized:
            lines = [f"**{_friendly_name(s)}**: {_format_status_value(s, None)}" for s in sorted(uncategorized, key=_friendly_name)]
            fields.append({"name": "Other", "value": "\n".join(lines)[:1024], "inline": False})

        return {
            "title": "Current status",
            "color": COLOR_INFO,
            "fields": fields,
            "footer": {"text": f"{len(states)} entities"},
        }

    def _cmd_entities(self):
        states = self.ha_client.fetch_states_by_device_class(self.device_classes)
        if not states:
            return {"title": "Entities", "description": "No entities discovered yet.", "color": COLOR_WARN}
        by_class = {}
        for s in states:
            dc = s["attributes"].get("device_class", "?")
            by_class.setdefault(dc, []).append(f"`{s['entity_id']}`")
        fields = [{"name": dc, "value": "\n".join(ids), "inline": False} for dc, ids in sorted(by_class.items())]
        return {"title": "Monitored entities", "color": COLOR_INFO, "fields": fields}

    def _cmd_battery(self):
        states = self.ha_client.fetch_states_by_device_class({"battery"})
        if not states:
            return {"title": "Battery levels", "description": "No battery sensors found.", "color": COLOR_WARN}
        fields = []
        for s in sorted(states, key=lambda s: _safe_float(s["state"])):
            try:
                pct = float(s["state"])
                label = f"{pct:.0f}%"
            except (TypeError, ValueError):
                label = s["state"]
            fields.append({"name": _friendly_name(s), "value": label, "inline": True})
        low = [f for f in fields if f["value"].endswith("%") and float(f["value"][:-1]) < 20]
        description = f"⚠️ {len(low)} sensor(s) below 20%" if low else "All sensors above 20%."
        return {"title": "Battery levels", "description": description, "color": COLOR_WARN if low else COLOR_INFO, "fields": fields[:25]}

    def _cmd_history(self, entity_id, hours):
        if not entity_id:
            return {"title": "History", "description": "Missing entity_id.", "color": COLOR_ERROR}
        hours = hours or 24
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(hours=hours)
        entries = self.ha_client.fetch_history([entity_id], start.isoformat(), end.isoformat())
        if not entries:
            return {
                "title": f"History: {entity_id}",
                "description": f"No history found in the last {hours}h (check the entity ID with /entities).",
                "color": COLOR_WARN,
            }
        lines = [f"`{s['last_changed'][:19]}` → **{s['state']}**" for _, s in entries[-20:]]
        return {
            "title": f"History: {_friendly_name(entries[-1][1])}",
            "description": "\n".join(lines),
            "color": COLOR_INFO,
            "footer": {"text": f"Last {hours}h, most recent {min(len(entries), 20)} of {len(entries)} change(s)"},
        }


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _format_status_value(state, category):
    if category == "doors":
        return "🔓 Open" if state["state"] == "on" else "🔒 Closed"
    if category == "tamper":
        return "⚠️ Triggered" if state["state"] == "on" else "✅ Clear"
    unit = state.get("attributes", {}).get("unit_of_measurement", "")
    return f"{state['state']} {unit}".strip()
