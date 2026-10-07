"""Tests for parsing SEMS+ responses."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sems_plus_client import (
    Alarm,
    AlarmCounts,
    DeviceInformation,
    DeviceType,
    PowerFlow,
    StationInfo,
    StationStatistics,
    WorkModeInfo,
    find_control_functions,
    list_control_functions,
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
        "chartMap": {"energy_flow": "pSystem,soc, pBat,pGrid", "income": "x"},
    }
    if grid_status is not None:
        data["gridStatus"] = grid_status

    info = StationInfo.from_api(data)

    assert info.on_grid is on_grid
    assert info.status == 1
    assert info.created == date(2026, 3, 5)
    assert info.flow_items == {"pSystem", "soc", "pBat", "pGrid"}
    assert info.can_control
    assert not info.can_read_controls


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(
            {"pSystem": 0.03, "pBat": -4.97, "pGrid": -8.99, "pConsum": 4.05},
            (0.03, -4.97, 8.99, 4.05, None),
            id="importing",
        ),
        pytest.param(
            {"pSystem": 4.4, "pBat": 0, "pGrid": 3.6, "pConsum": -0.8, "soc": 100},
            (4.4, 0, -3.6, 0.8, 100),
            id="exporting",
        ),
        pytest.param(
            {"pSystem": 1, "pBat": 0.5, "pGrid": 0, "pConsum": 1.5, "soc": 50},
            (1, 0.5, 0, 1.5, 50),
            id="idle-grid",
        ),
    ],
)
def test_power_flow_signs(data: dict[str, float], expected: tuple[float, ...]) -> None:
    flow = PowerFlow.from_api(data)

    assert (flow.pv, flow.battery, flow.grid, flow.load, flow.soc) == expected
    assert str(flow.grid) != "-0.0"


def test_power_flow_from_second_data_message() -> None:
    flow = PowerFlow.from_api(
        {
            "stationId": "S1",
            "time": "2026-10-08 09:52:45",
            "pSystem": "5.931",
            "pConsum": "1.007",
            "pBat": "0.0",
            "pGrid": "4.924",
            "soc": "100.0",
            "pThird": "0.5",
        }
    )

    assert flow.pv == 5.931
    assert flow.grid == -4.924
    assert flow.third_party_pv == 0.5
    assert flow.ev_charger is None
    assert flow.updated_at == "2026-10-08 09:52:45"


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


GENERAL_FUNCTIONS = {
    "sn": "INV1",
    "functionMenus": {
        "children": [
            {
                "translateKey": "device_start_stop",
                "children": [],
                "functions": [
                    {
                        "address": "45218",
                        "id": "f-run",
                        "translateKey": "run_stop",
                        "control": 8,
                        "rwType": "RW",
                        "range": "[0,1]",
                        "gain": 1,
                        "controlAttr": '[{"transKey":"remote_Switch_on","value":"1"},'
                        '{"transKey":"remote_Switch_off","value":"0"}]',
                    },
                    {
                        "address": "45221",
                        "id": "f-restart",
                        "translateKey": "restart",
                        "funcKey": "Restart",
                        "control": 16,
                        "rwType": "WO",
                        "range": "[361]",
                        "gain": 1,
                        "controlAttr": '[{"transKey":"restart","value":"361"}]',
                    },
                ],
            },
            {
                "translateKey": "grid-tie_power_limit",
                "children": [],
                "functions": [
                    {
                        "address": "47510",
                        "id": "f-limit",
                        "translateKey": "limit_setting",
                        "funcKey": "PWLimitThr",
                        "control": 3,
                        "rwType": "RW",
                        "range": "[0,30000]",
                        "gain": 1,
                        "unit": "W",
                    },
                    {
                        "address": "42004",
                        "id": "f-parallel",
                        "translateKey": "restric_set_parallel",
                        "funcKey": "PWLimitThr",
                        "control": 3,
                        "rwType": "RW",
                        "range": "[0,1000000]",
                        "gain": 1,
                        "unit": "W",
                    },
                ],
            },
            {
                "translateKey": "work_mode",
                "children": [
                    {
                        "translateKey": "tou_mode",
                        "children": [
                            {
                                "translateKey": "work_group_1",
                                "functions": [
                                    {
                                        "address": "47559",
                                        "id": "f-start",
                                        "translateKey": "start_t",
                                        "control": 24,
                                        "rwType": "RW",
                                        "range": "[0,23],[0,59]",
                                        "relationFuncs": [
                                            {
                                                "address": "47560",
                                                "id": "f-end",
                                                "translateKey": "end_t",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        ]
    },
}


def test_list_control_functions_keeps_menu_paths() -> None:
    functions = list_control_functions(GENERAL_FUNCTIONS)

    assert [f.address for f in functions] == [
        "45218",
        "45221",
        "47510",
        "42004",
        "47559",
    ]
    by_address = {f.address: f for f in functions}
    assert by_address["47510"].path == ("grid-tie_power_limit",)
    assert by_address["47559"].path == ("work_mode", "tou_mode", "work_group_1")
    # Two functions share a funcKey; only the key and menu tell them apart.
    assert by_address["42004"].func_key == by_address["47510"].func_key
    assert by_address["47510"].bounds == (0, 30000)
    assert by_address["47559"].bounds == (0, 23)
    assert by_address["45221"].option_value("restart") == 361
    assert by_address["45218"].option_value("remote_Switch_off") == 0
    assert by_address["45218"].option_value("missing") is None
    assert find_control_functions(GENERAL_FUNCTIONS, {"run_stop"})["run_stop"].id == (
        "f-run"
    )


def test_work_mode_info() -> None:
    assert WorkModeInfo.from_api({"workMode": "3.0", "arm": "745"}) == WorkModeInfo(
        "3.0", "745"
    )
    assert WorkModeInfo.from_api(None) == WorkModeInfo(None, None)


def test_list_control_functions_keeps_functions_sharing_an_address() -> None:
    menus = {
        "functions": [
            {"address": "47545", "id": "f-on", "translateKey": "immediate_charge"},
            {"address": "47545", "id": "f-off", "translateKey": "stop_charging"},
            {"address": "47545", "id": "f-on", "translateKey": "immediate_charge"},
        ]
    }

    assert [f.key for f in list_control_functions(menus)] == [
        "immediate_charge",
        "stop_charging",
    ]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param(
            [
                {"code": "modelType", "data": "GW10K-EHA-G20"},
                {"code": "safetyVersion", "data": "010101"},
                {"code": "ratedPower", "data": "9.999"},
                {"code": "remark", "dataType": "STRING"},
            ],
            DeviceInformation("GW10K-EHA-G20", "010101", 9.999, None),
            id="inverter",
        ),
        pytest.param(
            [
                {"code": "communicationMode", "data": "LAN"},
                {"code": "commModuleVer", "data": "V2.7.64"},
            ],
            DeviceInformation(None, "V2.7.64", None, "LAN"),
            id="dongle",
        ),
        pytest.param(None, DeviceInformation(None, None, None, None), id="empty"),
    ],
)
def test_device_information(rows: object, expected: DeviceInformation) -> None:
    assert DeviceInformation.from_api(rows) == expected
