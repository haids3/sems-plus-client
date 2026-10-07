"""A fake aiohttp session, so client tests control exactly what the API returns."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import pytest

from sems_plus_client import SemsPlusClient


@dataclass
class FakeResponse:
    payload: Any = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    async def json(self, content_type: str | None = None) -> Any:
        return self.payload

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, str] | None
    json: Any
    timeout: Any = None


class FakeSession:
    """Answers each (method, url) from a queue; the last answer repeats."""

    def __init__(self) -> None:
        self._responses: dict[tuple[str, str], deque[FakeResponse]] = defaultdict(deque)
        self.calls: list[Call] = []

    def add(self, method: str, url: str, response: FakeResponse | dict) -> None:
        if isinstance(response, dict):
            response = FakeResponse(response)
        self._responses[(method, url)].append(response)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        json: Any = None,
        timeout: Any = None,
    ) -> FakeResponse:
        self.calls.append(Call(method, url, headers, params, json, timeout))
        queue = self._responses.get((method, url))
        if not queue:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return queue.popleft() if len(queue) > 1 else queue[0]

    def calls_to(self, method: str, url: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.url == url]


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(session: FakeSession) -> SemsPlusClient:
    return SemsPlusClient(
        session,  # type: ignore[arg-type]
        "user@example.com",
        "secret",
        request_spacing=0,
    )
