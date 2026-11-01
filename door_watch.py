"""Alerts when a door/window sensor stays open longer than the
`door_open_alert_minutes` setting (0 disables it)."""
import datetime
import logging
import threading
import time

from alerting import CRITICAL

logger = logging.getLogger("home-assistant-bot")

SETTING_KEY = "door_open_alert_minutes"


def parse_timestamp(value):
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.datetime.now(datetime.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


class OpenDoorWatcher:
    def __init__(self, notifier, settings, check_interval=5):
        self.notifier = notifier
        self.settings = settings
        self.check_interval = check_interval
        self._lock = threading.Lock()
        self._open = {}

    def opened(self, entity_id, friendly_name, since):
        with self._lock:
            self._open[entity_id] = {"name": friendly_name, "since": since, "alerted": False}

    def closed(self, entity_id):
        with self._lock:
            self._open.pop(entity_id, None)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            time.sleep(self.check_interval)
            try:
                self._check()
            except Exception:
                logger.exception("Open-door check failed")

    def _check(self):
        minutes = self.settings.get(SETTING_KEY)
        if minutes <= 0:
            return
        threshold = datetime.timedelta(minutes=minutes)
        now = datetime.datetime.now(datetime.timezone.utc)
        due = []
        with self._lock:
            for entity_id, door in self._open.items():
                if not door["alerted"] and now - door["since"] >= threshold:
                    door["alerted"] = True
                    due.append((entity_id, door))
        for entity_id, door in due:
            self.notifier.send_embed(
                f"{door['name']}: open for over {minutes} min",
                f"Door/window sensor has been open since <t:{int(door['since'].timestamp())}:R>.",
                {"Entity": entity_id, "Alert after": f"{minutes} min"},
                CRITICAL,
            )
