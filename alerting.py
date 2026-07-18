"""Alert state machine + Discord embed delivery:
OK -> WARN -> CRITICAL -> (recovered to) OK monitor lifecycle."""
import datetime
import logging

import requests

logger = logging.getLogger("home-assistant-bot")

OK, WARNING, CRITICAL, INFO = "OK", "WARNING", "CRITICAL", "INFO"

COLOR = {
    OK: 0x2ECC71,       # green
    WARNING: 0xE67E22,  # orange
    CRITICAL: 0xE74C3C,  # red
    INFO: 0x3498DB,     # blue - standalone notice, not part of a monitor's state
}
EMOJI = {OK: "✅", WARNING: "⚠️", CRITICAL: "\U0001F6A8", INFO: "\U0001F535"}


class DiscordNotifier:
    def __init__(self, bot_token, channel_id):
        self.channel_id = channel_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        })

    def send_embed(self, title, description, fields, state):
        payload = {
            "embeds": [{
                "title": f"{EMOJI[state]} {title}",
                "description": description,
                "color": COLOR[state],
                "fields": [{"name": k, "value": str(v), "inline": True} for k, v in fields.items()],
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "footer": {"text": "home-assistant-bot"},
            }]
        }
        url = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
        resp = self.session.post(url, json=payload, timeout=10)
        if resp.status_code >= 300:
            logger.error("Discord API error %s: %s", resp.status_code, resp.text)
        else:
            logger.info("Alert sent: %s [%s]", title, state)


class Monitor:
    """Tracks OK/WARNING/CRITICAL state for one entity, only notifying on transitions."""

    def __init__(self, name, notifier, warn_threshold, crit_threshold, metric_label, unit=""):
        self.name = name
        self.notifier = notifier
        self.warn_threshold = warn_threshold
        self.crit_threshold = crit_threshold
        self.metric_label = metric_label
        self.unit = unit
        self.state = OK

    def update(self, value, context=None):
        context = context or {}
        new_state = OK
        if value >= self.crit_threshold:
            new_state = CRITICAL
        elif value >= self.warn_threshold:
            new_state = WARNING

        if new_state == self.state:
            return
        previous = self.state
        self.state = new_state

        if new_state == OK:
            title = f"{self.name}: back to normal"
            description = f"Back to normal (was {previous})."
        elif new_state == WARNING:
            title = f"{self.name}: elevated"
            description = f"{self.metric_label} crossed the warning threshold."
        else:
            title = f"{self.name}: critical"
            description = f"{self.metric_label} crossed the critical threshold."

        fields = {
            self.metric_label: f"{value}{self.unit}",
            "Warn at": f"{self.warn_threshold}{self.unit}",
            "Critical at": f"{self.crit_threshold}{self.unit}",
        }
        fields.update(context)
        self.notifier.send_embed(title, description, fields, new_state)
