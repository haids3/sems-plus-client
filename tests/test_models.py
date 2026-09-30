"""Tests for parsing SEMS+ responses."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sems_plus_client import (
    Alarm,
    AlarmCounts,
    DeviceType,
    PowerFlow,
    StationInfo,
    StationStatistics,
    find_control_functions,
    parse_devices,
    parse_factors,
)


def test_parse_devices_flattens_status_groups() -> None:
    data = {
        "deviceDetailList": [
            {
                "deviceType": "ENERGY_STORAGE_INTEGRATED_CABINET",
                "statusDetailList": [
                    {
                        "status": 5,
                        "snList": ["INV1"],
                        "detailMap": {
                            "INV1": {"name": "All-in-One 1", "subtype": "RESIDENTIAL"}
                        },
                    }
                ],
            },
            {
                "deviceType": "SMART_METER",
                "statusDetailList": [
                    {
                        "status": 1,
                        "snList": ["MTR1"],
                        "detailMap": {"MTR1": {"name": "Meter 1", "preSn": "INV1"}},
                    }
                ],
            },
        ]
    }

    inverter, meter = parse_devices(data)

    assert inverter.device_type == DeviceType.ALL_IN_ONE
    assert inverter.is_inverter
    assert inverter.status == 5
    assert meter.parent_sn == "INV1"
    assert not meter.is_inverter


def test_parse_factors_types_values_and_keeps_unreported() -> None:
    groups = [
        {
            "code": "system",
            "factors": [
                {"code": "sn", "data": "INV1", "dataType": "STRING"},
                {"code": "pAc", "data": "1.25", "dataType": "NUMERIC"},
                {"code": "soc", "data": None, "dataType": "NUMERIC"},
                {"code": "bad", "data": "n/a", "dataType": "NUMERIC"},
            ],
        }
    ]

    assert parse_factors(groups) == {
        "sn": "INV1",
        "pAc": 1.25,
        "soc": None,
        "bad": None,
    }
    # Dongles answer with an empty object instead of a list.
    assert parse_factors({}) == {}


@pytest.mark.parametrize(
    ("grid_status", "on_grid"),
    [
        pytest.param("1", True, id="on-grid"),
        pytest.param("0", False, id="off-grid"),
        pytest.param(None, None, id="pv-only"),
    ],
)
def test_station_info_grid_status(
    grid_status: str | None, on_grid: bool | None
) -> None:
    data = {
        "stationId": "S1",
        "name": "Test",
        "status": "1",
        "permissions": ["INVERTER_REMOTE"],
        "createTime": "2026-03-05T09:17:41.69",
    }
    if grid_status is not None:
        data["gridStatus"] = grid_status

    info = StationInfo.from_api(data)

    assert info.on_grid is on_grid
    assert info.status == 1
    assert info.created == date(2026, 3, 5)
    assert info.can_control


def test_power_flow_load_is_a_magnitude() -> None:
    flow = PowerFlow.from_api(
        {"pSystem": 2, "pBat": -0.5, "pGrid": -1, "pConsum": -0.5, "soc": 80}
    )

    assert (flow.pv, flow.battery, flow.grid, flow.load, flow.soc) == (
        2,
        -0.5,
        -1,
        0.5,
        80,
    )


def test_statistics_series_and_totals() -> None:
    stats = StationStatistics.from_api(
        {
            "production": 26.9,
            "currencyNote": "ignored",
            "dataList": [
                {
                    "item": "proGridStats",
                    "statisticsList": [
                        {"date": "2026-01-01", "val": 1.5},
                        {"date": "2026-01-02", "val": 2},
                    ],
                }
            ],
        }
    )

    assert stats.totals == {"production": 26.9}
    assert stats.total("proGridStats") == 3.5
    assert stats.total("proCharStats") is None


def test_alarm_counts_are_strings() -> None:
    assert AlarmCounts.from_api({"total": "3", "happened": "1", "recovery": "2"}) == (
        AlarmCounts(total=3, active=1, recovered=2)
    )


def test_alarm_parsing() -> None:
    alarm = Alarm.from_api(
        {
            "warningid": "abc",
            "warning_code": "E-1",
            "warningNameEn": "Grid Undervoltage",
            "alarmLevel": "Total_FaultLevel_alarm",
            "status": 0,
            "devicesn": "INV1",
            "happentime": "1767225600000",
            "recoverytime": None,
        }
    )

    assert alarm.active
    assert alarm.name == "Grid Undervoltage"
    assert alarm.happened_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert alarm.recovered_at is None


def test_find_control_functions_skips_menus() -> None:
    tree = {
        "functionMenus": {
            "translateKey": "inverter",
            "children": [
                # A menu sharing a function's translateKey has no address.
                {"translateKey": "run_stop", "menuId": "m1", "children": []},
                {
                    "translateKey": "device_start_stop",
                    "menuId": "m2",
                    "functions": [
                        {
                            "address": 45218,
                            "id": "f1",
                            "translateKey": "run_stop",
                            "rwType": "RW",
                            "control": 8,
                            "controlAttr": (
                                '[{"transKey":"remote_Switch_on","value":"1"}]'
                            ),
                        }
                    ],
                },
            ],
        }
    }

    functions = find_control_functions(tree, {"run_stop", "restart"})

    assert list(functions) == ["run_stop"]
    run_stop = functions["run_stop"]
    assert (run_stop.address, run_stop.id, run_stop.writable) == ("45218", "f1", True)
    assert run_stop.options == [{"transKey": "remote_Switch_on", "value": "1"}]
