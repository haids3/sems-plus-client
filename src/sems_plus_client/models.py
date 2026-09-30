"""Typed views of SEMS+ API responses.

Every enum the API returns arrives as a string or a number depending on the
endpoint, so parsing is lenient: unknown or malformed values become None
rather than raising.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
import json
import math
from typing import Any


def _float(value: Any) -> float | None:
    """Return a finite float, or None for anything else."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None and number.is_integer() else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _date(value: Any) -> date | None:
    """Parse the date part of an ISO timestamp such as "2026-03-05T09:17:41.69"."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _epoch_ms(value: Any) -> datetime | None:
    millis = _int(value)
    return datetime.fromtimestamp(millis / 1000, UTC) if millis else None


class DeviceType(StrEnum):
    """Device types reported by the station device list."""

    INVERTER = "INVERTER"
    ALL_IN_ONE = "ENERGY_STORAGE_INTEGRATED_CABINET"
    BATTERY_RACK = "BATTERY_RACK"
    SMART_METER = "SMART_METER"
    DONGLE = "DONGLE"


# Device types whose telemetry carries inverter data and that can be controlled.
INVERTER_TYPES = frozenset({DeviceType.INVERTER, DeviceType.ALL_IN_ONE})


@dataclass(frozen=True, slots=True)
class Station:
    """A station as listed for the account."""

    id: str
    name: str
    status: int | None
    capacity_kw: float | None
    time_zone: str | None
    is_shared: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Station:
        return cls(
            id=data["id"],
            name=_str(data.get("name")) or data["id"],
            status=_int(data.get("status")),
            capacity_kw=_float(data.get("installedPower")),
            time_zone=_str(data.get("timeZone")),
            is_shared=bool(data.get("isShared")),
        )


@dataclass(frozen=True, slots=True)
class StationInfo:
    """Station-wide status from `stations/basic/info`."""

    id: str
    name: str
    status: int | None
    # None for PV-only stations, which do not report a grid connection.
    on_grid: bool | None
    is_all_in_one: bool
    battery_capacity_kwh: float | None
    pv_capacity_kw: float | None
    time_zone: str | None
    permissions: frozenset[str]
    # Statistics only exist from here on; see `async_get_statistics`.
    created: date | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> StationInfo:
        grid_status = _int(data.get("gridStatus"))
        return cls(
            id=data["stationId"],
            name=_str(data.get("name")) or data["stationId"],
            status=_int(data.get("status")),
            on_grid=None if grid_status is None else grid_status == 1,
            is_all_in_one=bool(data.get("isAllInOne")),
            battery_capacity_kwh=_float(data.get("batteryCapacity")),
            pv_capacity_kw=_float(data.get("pvCapacity")),
            time_zone=_str(data.get("timeZone")),
            permissions=frozenset(
                p for p in data.get("permissions") or [] if isinstance(p, str)
            ),
            created=_date(data.get("createTime")),
        )

    @property
    def can_control(self) -> bool:
        return "INVERTER_REMOTE" in self.permissions


@dataclass(frozen=True, slots=True)
class PowerFlow:
    """Live station power flow, in kW.

    `battery` is positive while discharging and `grid` positive while
    importing. `load` is always a magnitude.
    """

    pv: float | None
    battery: float | None
    grid: float | None
    load: float | None
    soc: float | None
    updated_at: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PowerFlow:
        load = _float(data.get("pConsum"))
        return cls(
            pv=_float(data.get("pSystem", data.get("pAc"))),
            battery=_float(data.get("pBat")),
            grid=_float(data.get("pGrid")),
            load=abs(load) if load is not None else None,
            soc=_float(data.get("soc")),
            updated_at=_str(data.get("refreshTime")),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """A device attached to a station."""

    sn: str
    name: str
    device_type: str
    status: int | None
    # The inverter a smart meter hangs off.
    parent_sn: str | None = None
    subtype: str | None = None

    @property
    def is_inverter(self) -> bool:
        return self.device_type in INVERTER_TYPES


def parse_devices(data: dict[str, Any]) -> list[Device]:
    """Flatten the `device/all-status` grouping into one device per serial."""
    devices: list[Device] = []
    for type_group in data.get("deviceDetailList") or []:
        device_type = type_group.get("deviceType")
        if not isinstance(device_type, str):
            continue
        for status_group in type_group.get("statusDetailList") or []:
            details = status_group.get("detailMap") or {}
            for sn in status_group.get("snList") or []:
                if not isinstance(sn, str):
                    continue
                detail = details.get(sn) or {}
                devices.append(
                    Device(
                        sn=sn,
                        name=_str(detail.get("name")) or sn,
                        device_type=device_type,
                        status=_int(status_group.get("status")),
                        parent_sn=_str(detail.get("preSn")),
                        subtype=_str(detail.get("subtype")),
                    )
                )
    return devices


type FactorValue = float | str | None


def parse_factors(groups: Any) -> dict[str, FactorValue]:
    """Flatten telemetry or counter groups into `{factor code: value}`.

    Numeric factors become floats. A factor the device does not report is kept
    with a None value, so callers can tell "unsupported" from "not returned".
    """
    factors: dict[str, FactorValue] = {}
    if not isinstance(groups, list):
        return factors
    for group in groups:
        for factor in group.get("factors") or [] if isinstance(group, dict) else []:
            code = factor.get("code")
            if not isinstance(code, str):
                continue
            value = factor.get("data")
            if factor.get("dataType") == "STRING":
                factors[code] = _str(value)
            else:
                factors[code] = _float(value)
    return factors


@dataclass(frozen=True, slots=True)
class BatterySystem:
    """A battery system related to an All-in-One (`relatedDevices`)."""

    sn: str
    name: str
    index: str
    key: str
    soc: float | None
    power: float | None
    connected: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> BatterySystem:
        return cls(
            sn=data["sn"],
            name=_str(data.get("name")) or data["sn"],
            index=str(data.get("no") or ""),
            key=_str(data.get("translateCode")) or data["sn"],
            soc=_float(data.get("soc")),
            power=_float(data.get("pbat")),
            connected=bool(data.get("isConnected")),
        )


@dataclass(frozen=True, slots=True)
class StationStatistics:
    """Energy totals for one statistics request, in kWh."""

    totals: dict[str, float]
    series: dict[str, list[tuple[str, float]]]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> StationStatistics:
        series: dict[str, list[tuple[str, float]]] = {}
        for item in data.get("dataList") or []:
            name = item.get("item")
            if not isinstance(name, str):
                continue
            points = [
                (point["date"], value)
                for point in item.get("statisticsList") or []
                if isinstance(point.get("date"), str)
                and (value := _float(point.get("val"))) is not None
            ]
            if points:
                series[name] = points
        totals = {
            key: number
            for key, value in data.items()
            if key != "dataList" and (number := _float(value)) is not None
        }
        return cls(totals=totals, series=series)

    def total(self, item: str) -> float | None:
        """Sum of one item's series (e.g. `proGridStats`) across the range."""
        points = self.series.get(item)
        return sum(value for _, value in points) if points else None


@dataclass(frozen=True, slots=True)
class AlarmCounts:
    total: int
    active: int
    recovered: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AlarmCounts:
        return cls(
            total=_int(data.get("total")) or 0,
            active=_int(data.get("happened")) or 0,
            recovered=_int(data.get("recovery")) or 0,
        )


@dataclass(frozen=True, slots=True)
class Alarm:
    id: str
    code: str | None
    name: str
    level: str | None
    active: bool
    device_sn: str | None
    device_name: str | None
    happened_at: datetime | None
    recovered_at: datetime | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Alarm:
        return cls(
            id=data["warningid"],
            code=_str(data.get("warning_code")),
            name=_str(data.get("warningNameEn"))
            or _str(data.get("warning_code"))
            or "Unknown alarm",
            level=_str(data.get("alarmLevel")),
            # status: 0 = occurring, 1 = recovered.
            active=_int(data.get("status")) == 0,
            device_sn=_str(data.get("devicesn")),
            device_name=_str(data.get("deviceName")),
            happened_at=_epoch_ms(data.get("happentime")),
            recovered_at=_epoch_ms(data.get("recoverytime")),
        )


@dataclass(frozen=True, slots=True)
class ControlFunction:
    """One remote-control function from a device's control tree."""

    address: str
    id: str
    key: str
    rw: str
    control: int | None
    unit: str | None
    gain: float | None
    value_range: str | None
    options: list[dict[str, Any]] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return "W" in self.rw

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ControlFunction:
        try:
            options = json.loads(data.get("controlAttr") or "[]")
        except ValueError:
            options = []
        return cls(
            address=str(data["address"]),
            id=str(data["id"]),
            key=data["translateKey"],
            rw=str(data.get("rwType") or ""),
            control=_int(data.get("control")),
            unit=_str(data.get("unit")),
            gain=_float(data.get("gain")),
            value_range=_str(data.get("range")),
            options=options if isinstance(options, list) else [],
        )


def find_control_functions(
    tree: dict[str, Any], keys: frozenset[str] | set[str]
) -> dict[str, ControlFunction]:
    """Return the first function for each translateKey in `keys`.

    Menus share translateKeys with functions (`backup_mode` is both), but only
    functions carry an address and id.
    """
    found: dict[str, ControlFunction] = {}
    pending: deque[Any] = deque([tree])
    while pending:
        node = pending.popleft()
        if isinstance(node, list):
            pending.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        key = node.get("translateKey")
        if key in keys and key not in found and node.get("address") and node.get("id"):
            found[key] = ControlFunction.from_api(node)
        pending.extend(v for v in node.values() if isinstance(v, dict | list))
    return found
