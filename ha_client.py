"""Minimal Home Assistant WebSocket API client: authenticates with a
long-lived access token and yields `state_changed` events. No HA-specific
entity IDs are hardcoded - callers filter by `device_class` instead, so this
works against any Home Assistant instance without configuration."""
import json
import logging
import threading
import time

import websocket

logger = logging.getLogger("home-assistant-bot")


class HomeAssistantClient:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self._stop = threading.Event()

    def state_changes(self):
        """Yields (entity_id, old_state, new_state) for every state_changed
        event, reconnecting automatically if the connection drops."""
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
                yield data["entity_id"], data.get("old_state"), data.get("new_state")
        finally:
            ws.close()
