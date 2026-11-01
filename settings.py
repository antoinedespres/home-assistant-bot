"""Settings changed at runtime through slash commands, persisted to a JSON
file so they survive restarts and redeploys. Only overridden values are
written, so a changed default in .env still applies to untouched keys."""
import json
import logging
import os
import threading

logger = logging.getLogger("home-assistant-bot")


class Settings:
    def __init__(self, path, defaults):
        self._path = path
        self._defaults = dict(defaults)
        self._overrides = {}
        self._lock = threading.Lock()
        try:
            with open(path) as f:
                self._overrides = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.exception("Could not read settings from %s, using defaults", path)

    def get(self, key):
        with self._lock:
            return self._overrides.get(key, self._defaults[key])

    def set(self, key, value):
        with self._lock:
            overrides = {**self._overrides, key: value}
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w") as f:
                json.dump(overrides, f, indent=2)
            os.replace(tmp, self._path)
            self._overrides = overrides
