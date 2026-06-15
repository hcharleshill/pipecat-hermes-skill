"""
Minimal Python 3 compatible ARI client shim for the Pipecat Hermes Skill bridge.

Replaces the ancient/broken 'ari' package from PyPI (Python 2 era, urlparse/import client issues).

Provides just enough of the interface used by asterisk_ari_bridge.py:
- ari.connect(base_url, username, password)
- client.on_channel_event("StasisStart", cb)
- client.on_channel_event("StasisEnd", cb)
- client.run(apps="hermes")
- client.channels.externalMedia(...)
- client.bridges.create(...)
- channel.answer(), channel.hangup(), channel.json
- bridge.addChannel(channel=...)
- returned objects expose .json dict

Uses only stdlib + requests + websocket-client (which are already dependencies).
"""

import json
import logging
import threading
import time
import urllib.parse as urlparse
from typing import Any, Callable, Dict, Optional

import requests
import websocket

logger = logging.getLogger(__name__)


class _ARIObject:
    """Base proxy for ARI resources (Channel, Bridge, etc.)."""
    def __init__(self, client: "_ARIClient", resource_type: str, json_data: Dict[str, Any]):
        self._client = client
        self._resource_type = resource_type
        self.json = json_data or {}

    def __getattr__(self, name: str):
        # Allow access to json fields directly if needed
        if name in self.json:
            return self.json[name]
        raise AttributeError(name)


class _Channel(_ARIObject):
    def __init__(self, client, json_data):
        super().__init__(client, "channel", json_data)

    def answer(self):
        channel_id = self.json.get("id")
        if not channel_id:
            return
        url = f"{self._client.base_url}/ari/channels/{channel_id}/answer"
        self._client._request("POST", url)

    def hangup(self):
        channel_id = self.json.get("id")
        if not channel_id:
            return
        url = f"{self._client.base_url}/ari/channels/{channel_id}"
        self._client._request("DELETE", url)


class _Bridge(_ARIObject):
    def __init__(self, client, json_data):
        super().__init__(client, "bridge", json_data)

    def addChannel(self, channel: str = None, **kwargs):
        bridge_id = self.json.get("id")
        channel_id = channel or kwargs.get("channel")
        if not bridge_id or not channel_id:
            logger.warning("addChannel missing bridge or channel id")
            return
        url = f"{self._client.base_url}/ari/bridges/{bridge_id}/addChannel"
        self._client._request("POST", url, params={"channel": channel_id})


class _Channels:
    def __init__(self, client):
        self._client = client

    def getChannelVar(self, channelId: str = None, variable: str = None, **kwargs):
        channel_id = channelId or kwargs.get("channel")
        var = variable or kwargs.get("variable")
        if not channel_id or not var:
            return None
        url = f"{self._client.base_url}/ari/channels/{channel_id}/variable"
        data = self._client._request("GET", url, params={"variable": var})
        if isinstance(data, dict):
            return data.get("value")
        return None

    def hangup(self, channelId: str = None, **kwargs):
        channel_id = channelId or kwargs.get("channel")
        if not channel_id:
            return
        url = f"{self._client.base_url}/ari/channels/{channel_id}"
        self._client._request("DELETE", url)

    def externalMedia(self, channelId: str = None, app: str = None, externalMediaOptions: Dict[str, str] = None, **kwargs):
        # This creates an External Media channel
        url = f"{self._client.base_url}/ari/channels/externalMedia"
        params = {
            "channelId": channelId or kwargs.get("channelId"),
            "app": app or kwargs.get("app"),
        }
        if externalMediaOptions:
            # Map common options. The old 'ari' lib used "data" in externalMediaOptions,
            # but Asterisk ARI externalMedia requires "external_host" for the RTP target
            # (and "format" for the codec).
            for k, v in externalMediaOptions.items():
                if k == "data":
                    params["external_host"] = v
                else:
                    params[k] = v

        data = self._client._request("POST", url, params=params)
        return _Channel(self._client, data or {})


class _Bridges:
    def __init__(self, client):
        self._client = client

    def create(self, type: str = "mixing", **kwargs):
        url = f"{self._client.base_url}/ari/bridges"
        params = {"type": type}
        data = self._client._request("POST", url, params=params)
        return _Bridge(self._client, data or {})

    def destroy(self, bridgeId: str = None, **kwargs):
        bridge_id = bridgeId or kwargs.get("bridge")
        if not bridge_id:
            return
        url = f"{self._client.base_url}/ari/bridges/{bridge_id}"
        self._client._request("DELETE", url)

    def addChannel(self, bridgeId: str = None, channel: str = None, **kwargs):
        bridge_id = bridgeId or kwargs.get("bridge")
        channel_id = channel or kwargs.get("channel")
        if not bridge_id or not channel_id:
            logger.warning("bridges.addChannel missing bridge or channel id")
            return
        url = f"{self._client.base_url}/ari/bridges/{bridge_id}/addChannel"
        self._client._request("POST", url, params={"channel": channel_id})


class _ARIClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.auth = (username, password)

        self._handlers: Dict[str, list[Callable]] = {}
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False

    def _request(self, method: str, url: str, params: Optional[Dict] = None, json_body: Any = None):
        try:
            resp = requests.request(
                method,
                url,
                auth=self.auth,
                params=params,
                json=json_body,
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.warning(f"ARI {method} {url} -> {resp.status_code}: {resp.text[:200]}")
            if resp.content:
                try:
                    return resp.json()
                except Exception:
                    return resp.text
            return None
        except Exception as e:
            logger.error(f"ARI request failed: {method} {url} : {e}")
            raise

    def on_channel_event(self, event_name: str, callback: Callable):
        """Register handler for a Stasis event type (e.g. 'StasisStart')."""
        if event_name not in self._handlers:
            self._handlers[event_name] = []
        self._handlers[event_name].append(callback)

    def _dispatch(self, event: Dict[str, Any]):
        etype = event.get("type")
        if not etype:
            return

        # The bridge registers specifically for channel events via on_channel_event,
        # but Asterisk sends them as top level "type": "StasisStart" etc.
        handlers = self._handlers.get(etype, [])
        for cb in handlers:
            try:
                # Reconstruct a "channel" object if present in the event
                channel_data = event.get("channel") or {}
                if channel_data:
                    ch = _Channel(self, channel_data)
                    cb(event, ch)
                else:
                    # Some events may not have a primary channel
                    cb(event, _ARIObject(self, "event", event))
            except Exception as e:
                logger.exception(f"Handler for {etype} failed: {e}")

    def run(self, apps: str = "hermes"):
        """
        Connect to the ARI WebSocket and dispatch events.
        This is a blocking call (like the original ari library).
        """
        apps_list = apps if isinstance(apps, (list, tuple)) else [apps]
        app_str = "&".join(f"app={a}" for a in apps_list)

        # Build WS URL
        # ARI WS: ws://host:port/ari/events?api_key=user:pass&app=...
        # We use the http base and convert to ws.
        parsed = urlparse.urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{scheme}://{parsed.netloc}/ari/events?api_key={self.username}:{self.password}&{app_str}"

        logger.info(f"Connecting to ARI events WS: {ws_url}")

        def on_message(ws, message):
            try:
                event = json.loads(message)
                self._dispatch(event)
            except Exception as e:
                logger.debug(f"Bad ARI event: {e}")

        def on_error(ws, error):
            logger.warning(f"ARI WS error: {error}")

        def on_close(ws, close_status_code, close_msg):
            logger.info("ARI WS closed")
            self._running = False

        def on_open(ws):
            logger.info("ARI WS connected")
            self._running = True

        self._ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        # Run the websocket in a thread so we can keep the main thread if needed,
        # but to match the original .run() blocking behavior we run it here.
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    # Resource accessors used by the bridge
    @property
    def channels(self):
        return _Channels(self)

    @property
    def bridges(self):
        return _Bridges(self)


def connect(base_url: str, username: str, password: str) -> _ARIClient:
    """Public API matching the original ari library usage."""
    return _ARIClient(base_url, username, password)


# For any code that does "from ari import Client" style (not used here but for completeness)
Client = _ARIObject  # not really used
