"""Tests for the second-data MQTT feed."""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt
import pytest

from sems_plus_client import (
    LiveCredentials,
    LiveMessage,
    SemsPlusLiveFeed,
    parse_live_message,
)

STATION_TOPIC = "/goodwe/second-data/station/S1"


@pytest.mark.parametrize(
    ("topic", "payload", "expected"),
    [
        pytest.param(
            STATION_TOPIC,
            b'{"pGrid": "4.924", "stationId": "S1"}',
            LiveMessage("station", "S1", {"pGrid": "4.924", "stationId": "S1"}),
            id="station",
        ),
        pytest.param(
            "/goodwe/second-data/device/INV1",
            json.dumps({"title": "t", "message": json.dumps({"pAc": "1.2"})}),
            LiveMessage("device", "INV1", {"pAc": "1.2"}),
            id="wrapped-device",
        ),
        pytest.param("/goodwe/ccm/server/frpset/INV1", b"{}", None, id="other"),
        pytest.param(STATION_TOPIC, b"not json", None, id="garbage"),
        pytest.param(STATION_TOPIC, b"[1, 2]", None, id="not-an-object"),
    ],
)
def test_parse_live_message(
    topic: str, payload: bytes | str, expected: LiveMessage | None
) -> None:
    assert parse_live_message(topic, payload) == expected


class FakeMqtt:
    """Plays one connection: optionally fails, else yields its messages."""

    connections: list[FakeMqtt] = []
    script: list[list[tuple[str, bytes]] | Exception] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host = host
        self.kwargs = kwargs
        self.subscribed: list[str] = []
        self.plan = FakeMqtt.script.pop(0) if FakeMqtt.script else []
        FakeMqtt.connections.append(self)

    async def __aenter__(self) -> FakeMqtt:
        if isinstance(self.plan, Exception):
            raise self.plan
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        async def generate() -> Any:
            for topic, payload in self.plan:
                yield MagicMock(topic=topic, payload=payload)
            # Then stay connected until cancelled.
            await asyncio.Event().wait()
            yield  # pragma: no cover

        return generate()


async def test_feed_subscribes_reconnects_and_delivers() -> None:
    client = MagicMock()
    client.region = "au"
    client.async_get_live_credentials = AsyncMock(
        side_effect=[
            LiveCredentials("id-1", "user-1", "pw-1"),
            LiveCredentials("id-2", "user-2", "pw-2"),
        ]
    )
    FakeMqtt.connections = []
    FakeMqtt.script = [
        aiomqtt.MqttError("refused"),
        [(STATION_TOPIC, b'{"pSystem": "5.9"}')],
    ]
    received: list[LiveMessage] = []
    delivered = asyncio.Event()

    def on_message(message: LiveMessage) -> None:
        received.append(message)
        delivered.set()

    feed = SemsPlusLiveFeed(client, ssl.create_default_context(), min_retry=0)
    with patch("sems_plus_client.live.aiomqtt.Client", FakeMqtt):
        task = asyncio.create_task(feed.async_run(["S1"], ["INV1"], on_message))
        await asyncio.wait_for(delivered.wait(), 1)
        task.cancel()

    assert received == [LiveMessage("station", "S1", {"pSystem": "5.9"})]
    first, second = FakeMqtt.connections
    assert first.kwargs["identifier"] == "id-1"
    # Fresh credentials for the second connection.
    assert second.kwargs["username"] == "user-2"
    assert second.host == "netty-wss-au.iot.goodwe-power.com"
    assert second.kwargs["transport"] == "websockets"
    assert second.subscribed == [STATION_TOPIC, "/goodwe/second-data/device/INV1"]
