"""Tests for the SEMS+ client's transport, session and endpoint handling."""

from __future__ import annotations

import asyncio
import json

import pytest

from sems_plus_client import (
    SemsPlusApiError,
    SemsPlusAuthError,
    SemsPlusClient,
    SemsPlusCommandError,
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

    assert flow.grid == -1.5
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
    # The device confirmation can take longer than an ordinary request.
    assert request.timeout.total > 30


async def test_rejected_write_is_a_command_error(
    session: FakeSession, client: SemsPlusClient
) -> None:
    url = API + "/sems-remote/api/v1/address/remote/setDeviceFunctionParameters"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add(
        "POST",
        url,
        {"code": "P0215", "translationCode": "op_fail", "description": "failed"},
    )

    with pytest.raises(SemsPlusCommandError) as err:
        await client.async_set_function_values(
            station_id=STATION,
            sn="SN1",
            device_name="Meter 1",
            values={"40343": 1},
            functions={"40343": "f1"},
            log={},
            virtual_sn="MTR1",
        )
    assert err.value.code == "P0215"
    assert session.calls_to("POST", url)[0].json["virtualSn"] == "MTR1"


async def test_write_does_not_hold_up_reads(
    session: FakeSession, client: SemsPlusClient
) -> None:
    write_url = API + "/sems-remote/api/v1/address/remote/setDeviceFunctionParameters"
    released = asyncio.Event()

    class SlowResponse(FakeResponse):
        async def json(self, content_type: str | None = None) -> object:
            await released.wait()
            return _ok(None)

    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", write_url, SlowResponse())
    session.add("GET", FLOW_URL, _ok({"pSystem": 1}))
    await client.async_login()

    write = asyncio.create_task(
        client.async_set_function_values(
            station_id=STATION,
            sn="SN1",
            device_name="All-in-One 1",
            values={"45218": 1},
            functions={"45218": "f1"},
            log={},
        )
    )
    await asyncio.sleep(0)
    flow = await asyncio.wait_for(client.async_get_power_flow(STATION), 1)

    assert flow.pv == 1
    assert not write.done()
    released.set()
    await write


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


async def test_general_functions_request(
    session: FakeSession, client: SemsPlusClient
) -> None:
    url = API + "/sems-remote/api/v2/address/remote/getDeviceFunctionTabMenus"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, _ok({"functionMenus": {"children": []}}))

    await client.async_get_general_functions("SN1", 0)

    (request,) = session.calls_to("POST", url)
    assert request.json == {
        "batIndex": "",
        "menuCode": 0,
        "module": "GENERAL_FUNCTIONS",
        "sn": "SN1",
    }


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param({"sn": "INV1"}, "INV1", id="meter-to-inverter"),
        pytest.param(None, "MTR1", id="unresolved"),
    ],
)
async def test_related_sn(
    session: FakeSession, client: SemsPlusClient, data: object, expected: str
) -> None:
    url = API + "/sems-remote/api/v2/address/remote/get-related-sn"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, _ok(data))

    assert await client.async_get_related_sn("MTR1", 2) == expected
    assert session.calls_to("POST", url)[0].json == {"sn": "MTR1", "menuCode": 2}


async def test_work_mode_request(session: FakeSession, client: SemsPlusClient) -> None:
    url = API + "/sems-remote/api/v2/address/remote/get-work-mode"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("GET", url, _ok({"workMode": "2.0", "arm": "425"}))

    info = await client.async_get_work_mode("SN1")

    assert info.version == "2.0"
    assert session.calls_to("GET", url)[0].params == {"sn": "SN1"}


async def test_device_details_are_paged(
    session: FakeSession, client: SemsPlusClient
) -> None:
    url = API + "/sems-plant/api/web/device/station/page"
    rows = [{"sn": f"SN{i}", "model": "GW8.3-BAT-D-G20"} for i in range(120)]
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, _ok({"dataList": rows[:100], "total": 120}))
    session.add("POST", url, _ok({"dataList": rows[100:], "total": 120}))

    details = await client.async_get_device_details(STATION)

    assert len(details) == 120
    assert details["SN7"].model == "GW8.3-BAT-D-G20"
    assert [c.json["current"] for c in session.calls_to("POST", url)] == [1, 2]


async def test_remote_get(session: FakeSession, client: SemsPlusClient) -> None:
    url = API + "/sems-remote/api/v1/remote/get"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add(
        "POST",
        url,
        _ok(
            {
                "sn": "SN1",
                "items": [
                    {
                        "functionName": "INVCurrentWorkMode",
                        "value": {"INVCurrentWorkMode": 1},
                    },
                    {"functionName": "GreenModeEnable", "value": {}},
                    {"functionName": "Broken", "value": None},
                ],
            }
        ),
    )

    values = await client.async_remote_get("SN1", ["INVCurrentWorkMode", "Broken"])

    assert values == {
        "INVCurrentWorkMode": {"INVCurrentWorkMode": 1},
        "GreenModeEnable": {},
    }
    assert session.calls_to("POST", url)[0].json == {
        "functionName": ["INVCurrentWorkMode", "Broken"],
        "sn": "SN1",
    }


async def test_remote_set(session: FakeSession, client: SemsPlusClient) -> None:
    url = API + "/sems-remote/api/v1/remote/set"
    session.add("POST", LOGIN_URL, LOGIN_OK)
    session.add("POST", url, {"code": "00000", "description": "ok"})

    await client.async_remote_set(
        station_id=STATION,
        sn="SN1",
        device_name="All-in-One 1",
        name="TOUModeEnable",
        data={"TOUModeEnable": 1},
        log={"TOU": "remote_Switch_on"},
    )

    (request,) = session.calls_to("POST", url)
    assert request.json == {
        "functionName": "TOUModeEnable",
        "sn": "SN1",
        "plantId": STATION,
        "deviceName": "All-in-One 1",
        "data": {"TOUModeEnable": 1},
        "waitingForDevice": True,
        "controlItemLogs": {"TOU": "remote_Switch_on"},
        "virtualSn": "SN1",
    }
    assert request.timeout.total > 30
