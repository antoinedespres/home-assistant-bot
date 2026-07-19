"""Minimal Home Assistant WebSocket API client: authenticates with a
long-lived access token and yields `state_changed` events. No HA-specific
entity IDs are hardcoded - callers filter by `device_class` instead, so this
works against any Home Assistant instance without configuration.

On every reconnect (not the first connect), it also replays any state
changes that happened while disconnected, fetched from HA's REST history
API, so a network gap doesn't silently swallow events - they're delivered
late but tagged as backfilled and carry their real historical timestamp."""
import datetime
import json
import logging
import threading
import time
from urllib.parse import quote

import requests
import websocket

logger = logging.getLogger("home-assistant-bot")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class HomeAssistantClient:
    def __init__(self, url, token, device_classes):
        self.url = url
        self.token = token
        # Device classes to backfill on reconnect (HA's history API requires
        # an explicit entity list, so we discover matching entities via
        # /api/states rather than hardcoding entity IDs).
        self.device_classes = set(device_classes)
        self._stop = threading.Event()
        # ISO timestamp up to which we have full event coverage (live or
        # backfilled). None until the first successful connection.
        self._last_seen = None
        self._connected_before = False

    def state_changes(self):
        """Yields (entity_id, old_state, new_state, backfilled) for every
        state_changed event - live ones as they happen, plus, right after a
        reconnect, any events that were missed while disconnected."""
        while not self._stop.is_set():
            try:
                yield from self._connect_and_listen()
            except Exception:
                logger.exception("HA WebSocket connection error, reconnecting in 5s")
            time.sleep(5)

    def _connect_and_listen(self):
        ws = websocket.create_connection(self.url, timeout=30)
        try:
            # Auth handshake: server sends auth_required, we send auth, it confirms.
            hello = json.loads(ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected first message from HA: {hello}")

            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth_result = json.loads(ws.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"HA authentication failed: {auth_result}")
            logger.info("Connected to Home Assistant")

            now = _now_iso()
            if self._connected_before and self._last_seen:
                yield from self._backfill_missed_events(self._last_seen, now)
            self._connected_before = True
            self._last_seen = now

            ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
            ack = json.loads(ws.recv())
            if not ack.get("success", False):
                raise RuntimeError(f"Failed to subscribe to state_changed: {ack}")

            ws.settimeout(60)
            while not self._stop.is_set():
                msg = json.loads(ws.recv())
                if msg.get("type") != "event":
                    continue
                data = msg["event"]["data"]
                new_state = data.get("new_state")
                if new_state and new_state.get("last_changed"):
                    self._last_seen = new_state["last_changed"]
                yield data["entity_id"], data.get("old_state"), new_state, False
        finally:
            ws.close()

    def _backfill_missed_events(self, start_iso, end_iso):
        logger.info("Reconnected after a gap - backfilling history from %s to %s", start_iso, end_iso)
        try:
            entries = self._fetch_history(start_iso, end_iso)
        except Exception:
            logger.exception("Failed to backfill missed history, continuing with live events only")
            return
        logger.info("Backfilling %d historical state(s)", len(entries))
        for entity_id, new_state in entries:
            if new_state.get("last_changed"):
                self._last_seen = new_state["last_changed"]
            yield entity_id, None, new_state, True

    def _http_base(self):
        base = self.url.replace("wss://", "https://").replace("ws://", "http://")
        return base.split("/api/websocket")[0]

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def fetch_states_by_device_class(self, device_classes=None):
        """Full current state objects for every entity matching one of the
        given device classes (defaults to this client's own set)."""
        device_classes = set(device_classes) if device_classes is not None else self.device_classes
        resp = requests.get(f"{self._http_base()}/api/states", headers=self._auth_headers(), timeout=30)
        resp.raise_for_status()
        return [
            s for s in resp.json()
            if s.get("attributes", {}).get("device_class") in device_classes
        ]

    def _fetch_relevant_entity_ids(self):
        """HA's history API requires an explicit entity list - discover which
        entities currently match our device classes via /api/states, so
        backfill still needs zero entity-ID configuration from the user."""
        return [s["entity_id"] for s in self.fetch_states_by_device_class()]

    def fetch_history(self, entity_ids, start_iso, end_iso):
        """Historical state entries for the given entity IDs, oldest first."""
        if not entity_ids:
            return []
        url = f"{self._http_base()}/api/history/period/{quote(start_iso)}"
        resp = requests.get(
            url,
            headers=self._auth_headers(),
            params={
                "end_time": end_iso,
                "filter_entity_id": ",".join(entity_ids),
            },
            timeout=30,
        )
        resp.raise_for_status()
        per_entity_lists = resp.json()
        flat = [state for entity_list in per_entity_lists for state in entity_list if state.get("entity_id")]
        flat.sort(key=lambda s: s.get("last_changed", ""))
        return [(state["entity_id"], state) for state in flat]

    def _fetch_history(self, start_iso, end_iso):
        return self.fetch_history(self._fetch_relevant_entity_ids(), start_iso, end_iso)
