"""Tests for the SEMS+ client's transport, session and endpoint handling."""

from __future__ import annotations

import json

import pytest

from sems_plus_client import (
    SemsPlusApiError,
    SemsPlusAuthError,
    SemsPlusClient,
    SemsPlusConnectionError,
    SemsPlusPermissionError,
    SemsPlusRateLimitError,
)
from sems_plus_client.client import LOGIN_URL

from .conftest import FakeResponse, FakeSession

API = "https://xx-gateway.example.com/web/sems"
STATION = "00000000-0000-4000-8000-000000000001"
FLOW_URL = API + "/sems-plant/api/stations/flow"
LOGIN_OK = {
    "code": "00000",
    "api": API,
    "data": {"uid": "user-1", "token": "tok-1", "timestamp": 1},
}


def _ok(data: object) -> dict:
    return {"code": "00000", "data": data}


async def test_login_encodes_password_and_uses_regional_gateway(
    session: FakeSession, client: SemsPlusClient
) -> None:
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", FLOW_URL, _ok({"pGrid": 1.5}))

    flow = await client.async_get_power_flow(STATION)

    assert flow.grid == 1.5
    (login,) = session.calls_to("POST", LOGIN_URL)
    # base64(md5("secret"))
    assert login.json["pwd"] == "NWViZTIyOTRlY2QwZTBmMDhlYWI3NjkwZDJhNmVlNjk="
    assert login.json["account"] == "user@example.com"
    (request,) = session.calls_to("GET", FLOW_URL)
    assert request.params == {"stationId": STATION}


async def test_requests_carry_token_and_signature(
    session: FakeSession, client: SemsPlusClient
) -> None:
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", FLOW_URL, _ok({}))

    await client.async_get_power_flow(STATION)

    (request,) = session.calls_to("GET", FLOW_URL)
    assert json.loads(request.headers["token"])["token"] == "tok-1"
    assert request.headers["X-Signature"]


async def test_expired_session_logs_in_again_once(
    session: FakeSession, client: SemsPlusClient
) -> None:
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", FLOW_URL, {"code": "100002", "msg": "expired"})
    session.add("GET", FLOW_URL, _ok({"soc": 50}))

    assert (await client.async_get_power_flow(STATION)).soc == 50
    assert len(session.calls_to("POST", LOGIN_URL)) == 2


async def test_session_rejected_twice_raises(
    session: FakeSession, client: SemsPlusClient
) -> None:
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", FLOW_URL, {"code": "100002", "msg": "expired"})

    with pytest.raises(SemsPlusApiError) as err:
        await client.async_get_power_flow(STATION)
    assert err.value.code == "100002"


@pytest.mark.parametrize(
    ("login_code", "error"),
    [
        pytest.param("C0501", SemsPlusAuthError, id="bad-credentials"),
        pytest.param("C0602", SemsPlusApiError, id="transient"),
        pytest.param("GY0429", SemsPlusRateLimitError, id="rate-limited"),
    ],
)
async def test_login_rejections(
    session: FakeSession,
    client: SemsPlusClient,
    login_code: str,
    error: type[Exception],
) -> None:
    session.add("POST", LOGIN_URL, {"code": login_code, "msg": "no"})

    with pytest.raises(error):
        await client.async_login()


@pytest.mark.parametrize(
    ("response", "retry_after"),
    [
        pytest.param(
            FakeResponse(status=429, headers={"Retry-After": "120"}), 120, id="http"
        ),
        pytest.param(FakeResponse({"code": "GY0429"}), 300, id="api-code"),
    ],
)
async def test_rate_limit_pauses_the_client(
    session: FakeSession,
    client: SemsPlusClient,
    response: FakeResponse,
    retry_after: int,
) -> None:
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", FLOW_URL, response)

    with pytest.raises(SemsPlusRateLimitError) as err:
        await client.async_get_power_flow(STATION)
    assert err.value.retry_after == retry_after

    with pytest.raises(SemsPlusRateLimitError):
        await client.async_get_power_flow(STATION)
    # The paused client sent nothing the second time.
    assert len(session.calls_to("GET", FLOW_URL)) == 1


async def test_permission_error(session: FakeSession, client: SemsPlusClient) -> None:
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", FLOW_URL, {"code": "100025", "msg": "denied"})

    with pytest.raises(SemsPlusPermissionError):
        await client.async_get_power_flow(STATION)


async def test_http_error_is_a_connection_error(
    session: FakeSession, client: SemsPlusClient
) -> None:
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", FLOW_URL, FakeResponse(status=404))

    with pytest.raises(SemsPlusConnectionError):
        await client.async_get_power_flow(STATION)


async def test_stations_are_paged(session: FakeSession, client: SemsPlusClient) -> None:
    url = API + "/sems-plant/api/portal/stations/page"
    rows = [{"id": f"station-{i}", "name": f"Station {i}"} for i in range(150)]
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, _ok({"dataList": rows[:100], "total": 150}))
    session.add("POST", url, _ok({"dataList": rows[100:], "total": 150}))

    stations = await client.async_get_stations()

    assert [s.id for s in stations] == [row["id"] for row in rows]
    assert [c.json["current"] for c in session.calls_to("POST", url)] == [1, 2]


async def test_function_values_are_keyed_by_address(
    session: FakeSession, client: SemsPlusClient
) -> None:
    url = (
        API + "/sems-remote/api/v1/address/remote/get-cache-device-function-parameters"
    )
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, _ok({"sn": "SN1", "data": {"45218": 1}}))

    values = await client.async_get_function_values(
        "SN1", {"45218": "f1", "45221": "f2"}
    )

    assert values == {"45218": 1.0, "45221": None}
    (request,) = session.calls_to("POST", url)
    assert request.json == {
        "sn": "SN1",
        "addresses": ["45218", "45221"],
        "addrFuncMap": {"45218": "f1", "45221": "f2"},
    }


async def test_set_function_values_payload(
    session: FakeSession, client: SemsPlusClient
) -> None:
    url = API + "/sems-remote/api/v1/address/remote/setDeviceFunctionParameters"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, _ok(None))

    await client.async_set_function_values(
        station_id=STATION,
        sn="SN1",
        device_name="All-in-One 1",
        values={"45218": 0},
        functions={"45218": "f1"},
        log={"run_stop": "remote_Switch_off"},
    )

    (request,) = session.calls_to("POST", url)
    assert request.json == {
        "sn": "SN1",
        "addressMap": {"45218": 0},
        "addrFuncMap": {"45218": "f1"},
        "controlItemLogs": {"run_stop": "remote_Switch_off"},
        "waitingForDevice": True,
        "plantId": STATION,
        "deviceName": "All-in-One 1",
        "virtualSn": "SN1",
    }


async def test_battery_functions_request(
    session: FakeSession, client: SemsPlusClient
) -> None:
    url = API + "/sems-remote/api/v2/address/remote/getDeviceFunctionTabMenus"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, _ok({"functionMenus": {"children": []}}))

    assert await client.async_get_battery_functions("SN1", "1") == {
        "functionMenus": {"children": []}
    }
    (request,) = session.calls_to("POST", url)
    assert request.json == {
        "batIndex": "1",
        "menuCode": 1,
        "module": "GENERAL_FUNCTIONS",
        "sn": "SN1",
    }
