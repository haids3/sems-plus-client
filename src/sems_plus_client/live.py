"""Live values pushed over MQTT (SEMS+ "second data").

SEMS+ pushes station and device values every few seconds to subscribers of
`/goodwe/second-data/station/<station id>` and
`/goodwe/second-data/device/<serial>`. The broker credentials come from the
API and are passed through unchanged. The feed only ever subscribes: the same
broker carries device commands.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
import logging
import ssl
from typing import Any, Literal

import aiomqtt

from .client import SemsPlusClient
from .errors import SemsPlusError

_LOGGER = logging.getLogger(__name__)

BROKER_PORT = 8885
BROKERS = {
    "au": "netty-wss-au.iot.goodwe-power.com",
    "eu": "netty-wss-eu.iot.goodwe-power.com",
    "hk": "netty-wss-hk.iot.goodwe-power.com",
    "us": "netty-wss-us.iot.goodwe-power.com",
    "cn": "netty-wss-hz.iot.goodwe-power.com",
}
_STATION_TOPIC = "/goodwe/second-data/station/"
_DEVICE_TOPIC = "/goodwe/second-data/device/"


@dataclass(frozen=True, slots=True)
class LiveMessage:
    """One pushed update. Every value in `data` arrives as a string."""

    kind: Literal["station", "device"]
    # The station id or device serial the topic names.
    key: str
    data: dict[str, Any]


def parse_live_message(topic: str, payload: bytes | str) -> LiveMessage | None:
    """Parse a second-data message, or return None for anything else.

    The payload is JSON, occasionally wrapped as `{"title", "message"}` with
    the JSON as a string inside `message`.
    """
    if topic.startswith(_STATION_TOPIC):
        kind: Literal["station", "device"] = "station"
        key = topic.removeprefix(_STATION_TOPIC)
    elif topic.startswith(_DEVICE_TOPIC):
        kind = "device"
        key = topic.removeprefix(_DEVICE_TOPIC)
    else:
        return None
    try:
        data = json.loads(payload)
        if isinstance(data, dict) and isinstance(inner := data.get("message"), str):
            data = json.loads(inner)
    except ValueError:
        return None
    if not key or not isinstance(data, dict):
        return None
    return LiveMessage(kind, key, data)


class SemsPlusLiveFeed:
    """Subscribes to second-data topics and hands each message to a callback.

    `async_run` reconnects on its own and fetches fresh broker credentials
    for every connection; cancel the task to stop it.
    """

    def __init__(
        self,
        client: SemsPlusClient,
        tls_context: ssl.SSLContext,
        *,
        min_retry: float = 30,
        max_retry: float = 300,
    ) -> None:
        self._client = client
        self._tls_context = tls_context
        self._min_retry = min_retry
        self._max_retry = max_retry

    async def async_run(
        self,
        stations: Iterable[str],
        devices: Iterable[str],
        on_message: Callable[[LiveMessage], None],
    ) -> None:
        topics = [_STATION_TOPIC + s for s in stations] + [
            _DEVICE_TOPIC + d for d in devices
        ]
        if not topics:
            return
        delay = self._min_retry
        while True:
            try:
                await self._async_listen(topics, on_message)
            except (aiomqtt.MqttError, SemsPlusError) as err:
                _LOGGER.debug("SEMS+ live feed lost (%s); retrying in %ss", err, delay)
            else:
                delay = self._min_retry
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_retry)

    async def _async_listen(
        self, topics: list[str], on_message: Callable[[LiveMessage], None]
    ) -> None:
        credentials = await self._client.async_get_live_credentials()
        host = BROKERS.get(self._client.region or "", BROKERS["eu"])
        async with aiomqtt.Client(
            host,
            BROKER_PORT,
            identifier=credentials.client_id,
            username=credentials.username,
            password=credentials.password,
            transport="websockets",
            # An Origin header passed through aiomqtt stops all deliveries.
            websocket_path="/mqtt",
            tls_context=self._tls_context,
            clean_session=True,
        ) as mqtt:
            for topic in topics:
                await mqtt.subscribe(topic, qos=0)
            _LOGGER.debug("SEMS+ live feed subscribed to %s topics", len(topics))
            async for message in mqtt.messages:
                payload = message.payload
                if not isinstance(payload, bytes | str):
                    continue
                if (
                    live := parse_live_message(str(message.topic), payload)
                ) is not None:
                    on_message(live)
