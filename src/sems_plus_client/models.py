"""Typed views of SEMS+ API responses.

Every enum the API returns arrives as a string or a number depending on the
endpoint, so parsing is lenient: unknown or malformed values become None
rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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

# The `menuCode` the remote-control endpoints want for each device type.
MENU_CODES: dict[str, int] = {
    "INVERTER": 0,
    "ENERGY_STORAGE_INTEGRATED_CABINET": 0,
    "PCS": 0,
    "MICRO_INVERTER": 0,
    "BAT_SYS": 1,
    "SMART_METER": 2,
    "DONGLE": 3,
    "EV_CHARGER": 4,
    "SWITCH_CAB": 5,
    "DATA_LOGGER": 6,
    "BAT_BUSBAR": 8,
    "DIESEL_GEN": 9,
}


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
    # The `stations/flow` fields this station has (`pSystem`, `pThird`, ...).
    flow_items: frozenset[str] = frozenset()

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
            flow_items=_flow_items(data.get("chartMap")),
        )

    @property
    def can_control(self) -> bool:
        """SEMS+ lets this account change device settings (the web's own check)."""
        return "INVERTER_REMOTE" in self.permissions

    @property
    def can_read_controls(self) -> bool:
        return "INVERTER_REMOTE_READ" in self.permissions


def _flow_items(chart_map: Any) -> frozenset[str]:
    if not isinstance(chart_map, dict) or not isinstance(
        items := chart_map.get("energy_flow"), str
    ):
        return frozenset()
    return frozenset(item.strip() for item in items.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class PowerFlow:
    """Live station power flow, in kW.

    `battery` is positive while discharging and `grid` positive while
    importing; SEMS+ itself reports `pGrid` positive while exporting.
    `load` is always a magnitude. Fields a station lacks are None.
    """

    pv: float | None
    battery: float | None
    grid: float | None
    load: float | None
    soc: float | None
    updated_at: str | None
    third_party_pv: float | None = None
    ev_charger: float | None = None
    heat_pump: float | None = None
    generator: float | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PowerFlow:
        """Parse `stations/flow` or a second-data station message."""
        load = _float(data.get("pConsum"))
        grid = _float(data.get("pGrid"))
        return cls(
            pv=_float(data.get("pSystem", data.get("pAc"))),
            battery=_float(data.get("pBat")),
            grid=-grid if grid else grid,
            load=abs(load) if load is not None else None,
            soc=_float(data.get("soc")),
            updated_at=_str(data.get("refreshTime")) or _str(data.get("time")),
            third_party_pv=_float(data.get("pThird")),
            ev_charger=_float(data.get("pEvChar")),
            heat_pump=_float(data.get("pHeatPump")),
            generator=_float(data.get("pDiesel")),
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


@dataclass(frozen=True, slots=True)
class DeviceDetails:
    """Product details of a device, from the station's device page."""

    sn: str
    model: str | None
    brand: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DeviceDetails:
        return cls(
            sn=data["sn"],
            model=_str(data.get("model")),
            brand=_str(data.get("brand")),
        )


@dataclass(frozen=True, slots=True)
class DeviceInformation:
    """A device's `information` panel: model and firmware versions."""

    model: str | None
    # The inverter's firmware (SEMS+ calls it the safety version) or the
    # dongle's communication module version.
    firmware: str | None
    rated_power_kw: float | None
    # A dongle's link: "LAN" or "WiFi".
    connection: str | None

    @classmethod
    def from_api(cls, data: Any) -> DeviceInformation:
        fields = {
            row["code"]: row.get("data")
            for row in data or []
            if isinstance(row, dict) and isinstance(row.get("code"), str)
        }
        return cls(
            model=_str(fields.get("modelType")),
            firmware=_str(fields.get("safetyVersion"))
            or _str(fields.get("commModuleVer")),
            rated_power_kw=_float(fields.get("ratedPower")),
            connection=_str(fields.get("communicationMode")),
        )


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


class ControlType:
    """Known values of a function's `control` field (the web's widget)."""

    NUMBER = 3
    SELECT = 4
    SWITCH = 8
    COMMAND = 16
    TIME_RANGE = 24
    BITMASK = 25


@dataclass(frozen=True, slots=True)
class ControlFunction:
    """One remote-control function from a device's control menus."""

    address: str
    id: str
    key: str
    rw: str
    control: int | None
    unit: str | None
    gain: float | None
    value_range: str | None
    options: list[dict[str, Any]] = field(default_factory=list)
    # Stable English identifier, e.g. "PWLimitEnable"; most functions lack one.
    func_key: str | None = None
    # translateKeys of the menus above the function, outermost first.
    path: tuple[str, ...] = ()

    @property
    def writable(self) -> bool:
        return "W" in self.rw

    @property
    def bounds(self) -> tuple[float, float] | None:
        """The first `[min,max]` of `value_range`, as raw values."""
        try:
            numbers = json.loads(f"[{self.value_range}]")[0]
            low, high = float(numbers[0]), float(numbers[-1])
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        return low, high

    def option_value(self, trans_key: str) -> int | None:
        """The value of the `controlAttr` option with this transKey."""
        for option in self.options:
            if isinstance(option, dict) and option.get("transKey") == trans_key:
                return _int(option.get("value"))
        return None

    @classmethod
    def from_api(
        cls, data: dict[str, Any], path: tuple[str, ...] = ()
    ) -> ControlFunction:
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
            func_key=_str(data.get("funcKey")),
            path=path,
        )


def find_control_functions(
    tree: dict[str, Any], keys: frozenset[str] | set[str]
) -> dict[str, ControlFunction]:
    """Return the first function for each translateKey in `keys`.

    Menus share translateKeys with functions (`backup_mode` is both), but only
    functions carry an address and id.
    """
    found: dict[str, ControlFunction] = {}
    for function in list_control_functions(tree):
        if function.key in keys and function.key not in found:
            found[function.key] = function
    return found


def list_control_functions(tree: Any) -> list[ControlFunction]:
    """Every function in a control tree or menu response, in menu order.

    Accepts `getTopTreeByCode` and `getDeviceFunctionTabMenus` responses
    alike. Each function keeps the translateKeys of the menus above it, which
    tell apart functions sharing a key (two `limit_setting`s, one in W and one
    in %).
    """
    functions: list[ControlFunction] = []
    seen: set[str] = set()

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, path)
            return
        if not isinstance(node, dict):
            return
        if node.get("address") and node.get("id") and node.get("translateKey"):
            # Functions can share an address (immediate charge and stop
            # charging do), so only a repeated id is a duplicate.
            if (function_id := str(node["id"])) not in seen:
                seen.add(function_id)
                functions.append(ControlFunction.from_api(node, path))
            return
        menu_key = node.get("translateKey")
        inner = path + (menu_key,) if isinstance(menu_key, str) and menu_key else path
        for key, value in node.items():
            if key == "functionMenus":
                walk(value, path)
            elif isinstance(value, dict | list):
                walk(value, inner)

    walk(tree, ())
    return functions


@dataclass(frozen=True, slots=True)
class LiveCredentials:
    client_id: str
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkModeInfo:
    """`get-work-mode`: which work-mode scheme the device firmware uses."""

    # "1.0", "2.0" or "3.0"; see the API notes on work modes.
    version: str | None
    arm_version: str | None

    @classmethod
    def from_api(cls, data: Any) -> WorkModeInfo:
        data = data if isinstance(data, dict) else {}
        return cls(
            version=_str(data.get("workMode")), arm_version=_str(data.get("arm"))
        )


# `INVCurrentWorkMode`, the mode the inverter is running in (the web's table).
# Unknown values display as self use there.
WORK_MODES: dict[int, str] = {
    -1: "ai",
    1: "self_use",
    2: "pv_priority_charging",
    3: "pv_priority_export",
    4: "priority_import_power",
    5: "priority_export_power",
    6: "energy_saving",
    7: "off_grid",
    8: "battery_standby",
    9: "import",
    10: "export",
    11: "battery_charging",
    12: "battery_discharging",
    100: "backup",
    101: "tou",
    102: "tou",
    103: "delayed_charge",
    104: "delayed_charge",
    105: "peak_shaving",
    106: "peak_shaving",
    107: "pv_priority_export_power",
    255: "forced_shutdown_standby",
}

# Weekday-enable word of a TOU slot.
TOU_SLOT_ON = 249
TOU_SLOT_OFF = 6
# A discharge slot whose months include this one limits export power rather
# than battery discharge power. It is not a month.
TOU_EXPORT_LIMIT_MONTH = 12


@dataclass(frozen=True, slots=True)
class TouSlot:
    """One time-of-use slot from `remote/get` (`TOU1` ... `TOU12`).

    `power` is per-mille of rated power on work-mode versions 2 and 3:
    positive discharges, zero or negative charges. `weekdays` count from
    0 = Sunday, `months` from 0 = January; a discharge slot's months may also
    hold `TOU_EXPORT_LIMIT_MONTH`.
    """

    index: int
    start: str
    end: str
    week_enable: int
    weekdays: tuple[int, ...]
    power: int
    cutoff_soc: int
    months: tuple[int, ...]

    @property
    def enabled(self) -> bool:
        return self.week_enable == TOU_SLOT_ON

    @property
    def configured(self) -> bool:
        """The slot has a schedule; unused slots are all zeros."""
        return bool(self.weekdays) or self.start != self.end

    @property
    def charging(self) -> bool:
        """A charge slot; the web treats zero power as charging too."""
        return self.power <= 0

    @property
    def export_limited(self) -> bool:
        """A discharge slot whose power limits export, not battery discharge."""
        return not self.charging and TOU_EXPORT_LIMIT_MONTH in self.months

    @property
    def power_percent(self) -> float:
        """Charge power from the grid, or the discharge or export limit, in %."""
        return abs(self.power) / 10

    @property
    def calendar_months(self) -> tuple[int, ...]:
        return tuple(m for m in self.months if 0 <= m < TOU_EXPORT_LIMIT_MONTH)

    def with_mode(self, charging: bool) -> TouSlot:
        """The slot switched to charging or discharging at the same power."""
        if charging == self.charging:
            return self
        if not charging and self.power == 0:
            raise ValueError("Set a power above 0 before switching to discharge")
        return replace(self, power=-self.power, months=self.calendar_months)

    def with_power(self, percent: float) -> TouSlot:
        """The slot with a new power, keeping its mode."""
        magnitude = round(abs(percent) * 10)
        if not self.charging and magnitude == 0:
            raise ValueError("A discharge slot needs a power above 0")
        return replace(self, power=-magnitude if self.charging else magnitude)

    def with_export_limit(self, export: bool) -> TouSlot:
        """The discharge slot limiting export (True) or battery discharge."""
        if self.charging:
            raise ValueError("Only a discharge slot has a power limit method")
        months = self.calendar_months
        return replace(
            self, months=(*months, TOU_EXPORT_LIMIT_MONTH) if export else months
        )

    @classmethod
    def from_api(cls, index: int, value: dict[str, Any]) -> TouSlot:
        def ints(key: str) -> tuple[int, ...]:
            items = value.get(f"{key}{index}")
            if not isinstance(items, list):
                return ()
            return tuple(n for item in items if (n := _int(item)) is not None)

        return cls(
            index=index,
            start=_str(value.get(f"TOUStart{index}")) or "00:00",
            end=_str(value.get(f"TOUEnd{index}")) or "00:00",
            week_enable=_int(value.get(f"TOUWeekEnable{index}")) or 0,
            weekdays=ints("TOUWeek"),
            power=_int(value.get(f"ChargeDischargePW{index}")) or 0,
            cutoff_soc=_int(value.get(f"ChargeCutOffSet{index}")) or 0,
            months=ints("TOUMonth"),
        )

    def to_api(self) -> dict[str, Any]:
        """The `data` of a `remote/set` for this slot."""
        n = self.index
        return {
            f"TOUStart{n}": self.start,
            f"TOUEnd{n}": self.end,
            f"TOUWeekEnable{n}": self.week_enable,
            f"ChargeDischargePW{n}": self.power,
            f"ChargeCutOffSet{n}": self.cutoff_soc,
            f"TOUMonth{n}": list(self.months),
            f"TOUWeek{n}": list(self.weekdays),
        }

    def audit_log(self) -> dict[str, Any]:
        """The `controlItemLogs` the web sends with a slot change."""
        log: dict[str, Any] = {
            "start_t": self.start,
            "end_t": self.end,
            "switch": "on" if self.enabled else "off",
            "monthly_repetition": "、".join(
                _MONTH_KEYS[m] for m in self.calendar_months
            ),
            "wkly_rep": "、".join(
                _WEEKDAY_KEYS[d] for d in self.weekdays if 0 <= d < 7
            ),
            "cd_mod": "charge" if self.charging else "discharge",
            "import_power_soc": self.cutoff_soc,
        }
        percent = self.power_percent
        log["rated_power" if self.charging else "discharge_limit_pw"] = (
            int(percent) if percent.is_integer() else percent
        )
        return log


_MONTH_KEYS = (
    "jan_1",
    "feb_1",
    "march_1",
    "apr_1",
    "may_1",
    "june_1",
    "july_1",
    "aug_1",
    "sept_1",
    "oct_1",
    "nov_1",
    "dec_1",
)
_WEEKDAY_KEYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


@dataclass(frozen=True, slots=True)
class InverterFeatures:
    """Capability bits from `ARMFunction2` and `ARMFunction4`."""

    arm_function_2: int
    arm_function_4: int

    @property
    def auto_off_grid(self) -> bool:
        return bool(self.arm_function_4 & 1)

    @property
    def peak_shaving_v5(self) -> bool:
        return bool(self.arm_function_4 >> 5 & 1)

    @property
    def tou_power_limit_mode(self) -> bool:
        return bool(self.arm_function_4 >> 12 & 1)

    @property
    def tou_discharge_soc(self) -> bool:
        return bool(self.arm_function_2 >> 11 & 1)
