"""Async client for the GoodWe SEMS+ cloud API."""

from __future__ import annotations

import asyncio
import base64
from datetime import date
import hashlib
import json
import logging
import time
from typing import Any

import aiohttp

from .errors import (
    SemsPlusApiError,
    SemsPlusAuthError,
    SemsPlusConnectionError,
    SemsPlusPermissionError,
    SemsPlusRateLimitError,
)
from .models import (
    Alarm,
    AlarmCounts,
    BatterySystem,
    Device,
    FactorValue,
    PowerFlow,
    Station,
    StationInfo,
    StationStatistics,
    parse_devices,
    parse_factors,
)

_LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
# Used when a login response does not name the account's regional gateway.
FALLBACK_API_BASE = "https://eu-gateway.semsportal.com/web/sems"

_SUCCESS_CODES = {"0", "00000"}
_RATE_LIMIT_CODE = "GY0429"
_PERMISSION_CODE = "100025"
# A token the server no longer accepts (expired, or replaced by another login).
_SESSION_EXPIRED_CODES = {"100002", "C0602"}
# Login rejections that say nothing about the credentials.
_TRANSIENT_LOGIN_CODES = {"C0602"}

_RATE_LIMIT_PAUSE = 300
_MAX_RETRY_AFTER = 3_600
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_LOGIN_TOKEN = {
    "uid": "",
    "timestamp": 0,
    "token": "",
    "client": "semsPlusWeb",
    "version": "",
    "language": "en",
}

_STATISTICS_ITEMS = [
    "proConsumStats",
    "proGridStats",
    "proPurchaseStats",
    "proSelfConsumStats",
    "proDischarStats",
    "proCharStats",
    "proSystemTotalStats",
]


def _hash_password(password: str) -> str:
    """Encode a password the way the SEMS+ web login expects: base64(md5 hex)."""
    digest = hashlib.md5(password.encode(), usedforsecurity=False).hexdigest()
    return base64.b64encode(digest.encode()).decode()


def _signature(uid: str, token: str) -> str:
    """Build the X-Signature header the SEMS+ web gateway requires."""
    epoch_ms = round(time.time() * 1000)
    digest = hashlib.sha256(f"{epoch_ms}@{uid}@{token}".encode()).hexdigest()
    return base64.b64encode(f"{digest}@{epoch_ms}".encode()).decode()


class SemsPlusClient:
    """One SEMS+ account.

    A client serialises its requests and spaces them apart, so every station of
    an account can share one client without bursting the API.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account: str,
        password: str,
        *,
        request_spacing: float = 0.1,
    ) -> None:
        self._session = session
        self._account = account
        self._password = password
        self._request_spacing = request_spacing
        self._token: dict[str, Any] | None = None
        self._login_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        self._paused_until = 0.0

    @property
    def account(self) -> str:
        return self._account

    async def async_login(self) -> None:
        """Log in, replacing any current session."""
        async with self._login_lock:
            await self._async_login()

    async def _async_login(self) -> None:
        self._raise_if_paused()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://semsplus.goodwe.com",
            "Referer": "https://semsplus.goodwe.com/",
            "User-Agent": _USER_AGENT,
            "Token": json.dumps(_LOGIN_TOKEN, separators=(",", ":")),
            "X-Signature": _signature("", ""),
        }
        body = {
            "account": self._account,
            "pwd": _hash_password(self._password),
            "agreement": 1,
            "isChinese": False,
            "isLocal": False,
        }
        response = await self._async_send("POST", LOGIN_URL, headers, body=body)
        code = str(response.get("code"))
        if code == _RATE_LIMIT_CODE:
            raise self._pause(_RATE_LIMIT_PAUSE)
        data = response.get("data")
        if code not in _SUCCESS_CODES or not isinstance(data, dict):
            if code in _TRANSIENT_LOGIN_CODES:
                raise SemsPlusApiError(code, "SEMS+ login failed")
            raise SemsPlusAuthError(f"SEMS+ rejected the credentials (code {code})")
        if not data.get("token"):
            raise SemsPlusConnectionError("SEMS+ login returned no token")
        api = response.get("api") or data.get("api") or FALLBACK_API_BASE
        self._token = {**data, "api": api}
        _LOGGER.debug("Logged in to SEMS+ via %s", api)

    async def _async_ensure_token(self, rejected: dict[str, Any] | None) -> dict:
        """Return a token, logging in if there is none or it was just rejected."""
        async with self._login_lock:
            # Another request may already have replaced the rejected token.
            if self._token is None or self._token is rejected:
                await self._async_login()
            assert self._token is not None
            return self._token

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
    ) -> Any:
        """Call an authenticated endpoint and return its `data`."""
        rejected: dict[str, Any] | None = None
        for attempt in range(2):
            token = await self._async_ensure_token(rejected)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
                "token": json.dumps(token),
                "X-Signature": _signature(
                    str(token.get("uid", "")), str(token.get("token", ""))
                ),
            }
            response = await self._async_send(
                method, token["api"] + path, headers, params=params, body=body
            )
            code = str(response.get("code"))
            if code in _SUCCESS_CODES:
                return response.get("data")
            if code == _RATE_LIMIT_CODE:
                raise self._pause(_RATE_LIMIT_PAUSE)
            if code == _PERMISSION_CODE:
                raise SemsPlusPermissionError(_message(response))
            if code in _SESSION_EXPIRED_CODES and attempt == 0:
                _LOGGER.debug("SEMS+ session expired (code %s); logging in", code)
                rejected = token
                continue
            raise SemsPlusApiError(code, _message(response))
        raise AssertionError("unreachable")

    async def _async_send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
    ) -> dict[str, Any]:
        """Send one request, spaced after the previous one."""
        async with self._request_lock:
            self._raise_if_paused()
            wait = self._last_request + self._request_spacing - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=body,
                    timeout=_REQUEST_TIMEOUT,
                ) as response:
                    if response.status == 429:
                        raise self._pause(
                            _retry_after(response.headers.get("Retry-After"))
                        )
                    if response.status >= 400:
                        raise SemsPlusConnectionError(
                            f"SEMS+ returned HTTP {response.status} for {url}"
                        )
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise SemsPlusConnectionError(f"SEMS+ request failed: {err!r}") from err
            finally:
                self._last_request = time.monotonic()
        if not isinstance(payload, dict):
            raise SemsPlusConnectionError("SEMS+ returned an unexpected payload")
        return payload

    def _pause(self, seconds: int) -> SemsPlusRateLimitError:
        """Stop sending requests for a while and return the error to raise."""
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)
        return SemsPlusRateLimitError(seconds)

    def _raise_if_paused(self) -> None:
        remaining = self._paused_until - time.monotonic()
        if remaining > 0:
            raise SemsPlusRateLimitError(int(remaining) + 1)

    async def async_get_stations(self) -> list[Station]:
        """Return every station the account can see."""
        stations: list[Station] = []
        page = 1
        while True:
            data = await self._async_request(
                "POST",
                "/sems-plant/api/portal/stations/page",
                body={"current": page, "size": 100},
            )
            rows = (data or {}).get("dataList") or []
            stations.extend(
                Station.from_api(row) for row in rows if isinstance(row.get("id"), str)
            )
            total = (data or {}).get("total") or 0
            if not rows or len(stations) >= total:
                return stations
            page += 1

    async def async_get_station_info(self, station_id: str) -> StationInfo:
        data = await self._async_request(
            "POST",
            "/sems-plant/api/app/v2/stations/basic/info",
            params={"stationId": station_id},
            body={},
        )
        return StationInfo.from_api(data)

    async def async_get_power_flow(self, station_id: str) -> PowerFlow:
        data = await self._async_request(
            "GET", "/sems-plant/api/stations/flow", params={"stationId": station_id}
        )
        return PowerFlow.from_api(data or {})

    async def async_get_devices(self, station_id: str) -> list[Device]:
        data = await self._async_request(
            "GET",
            "/sems-plant/api/stations/device/all-status",
            params={"stationId": station_id},
        )
        return parse_devices(data or {})

    async def async_get_telemetry(
        self, station_id: str, device: Device
    ) -> dict[str, FactorValue]:
        """Live values, keyed by factor code (`pAc`, `MPPT-1:Vpv`, `soc`, ...)."""
        return await self._async_get_factors("telemetry", station_id, device)

    async def async_get_counters(
        self, station_id: str, device: Device
    ) -> dict[str, FactorValue]:
        """Energy counters, keyed by factor code (`proPvStatsToday`, ...)."""
        return await self._async_get_factors("telecounting", station_id, device)

    async def _async_get_factors(
        self, kind: str, station_id: str, device: Device
    ) -> dict[str, FactorValue]:
        data = await self._async_request(
            "GET",
            f"/sems-plant/api/equipments/{device.sn}/{kind}",
            params={"deviceType": device.device_type, "pwId": station_id},
        )
        return parse_factors(data)

    async def async_get_battery_systems(
        self, station_id: str, inverter_sn: str
    ) -> list[BatterySystem]:
        """Battery systems behind an All-in-One.

        Each one's `index` is the `batIndex` its battery controls need.
        """
        data = await self._async_request(
            "GET",
            f"/sems-plant/api/equipments/{inverter_sn}/relatedDevices",
            params={
                "sn": inverter_sn,
                "deviceType": "ENERGY_STORAGE_INTEGRATED_CABINET",
                "pwId": station_id,
            },
        )
        return [
            BatterySystem.from_api(row)
            for row in data or []
            if isinstance(row, dict)
            and row.get("type") == "BAT_SYS"
            and isinstance(row.get("sn"), str)
        ]

    async def async_get_statistics(
        self, station_id: str, dimension: str, start: date, end: date
    ) -> StationStatistics:
        """Energy statistics per `dimension` ("day", "month", "year") over a range."""
        data = await self._async_request(
            "POST",
            "/sems-plant/api/stations/statistics",
            body={
                "stationId": station_id,
                "isReport": False,
                "items": _STATISTICS_ITEMS,
                "dimension": dimension,
                "startTime": f"{start.isoformat()} 00:00:00",
                "endTime": f"{end.isoformat()} 23:59:59",
            },
        )
        return StationStatistics.from_api(data or {})

    async def async_get_currency(self, station_id: str, day: date) -> str | None:
        """The currency the station's income figures are reported in."""
        data = await self._async_request(
            "POST",
            "/sems-plant/api/stations/production",
            body={
                "stationId": station_id,
                "items": ["profitGridStats"],
                "dimension": "day",
                "isReport": False,
                "startTime": f"{day.isoformat()} 00:00:00",
                "endTime": f"{day.isoformat()} 23:59:59",
            },
        )
        currency = (data or {}).get("currency")
        return currency if isinstance(currency, str) and currency else None

    async def async_get_alarm_counts(self, station_id: str) -> AlarmCounts:
        data = await self._async_request(
            "POST",
            "/sems-alarm/api/alarm/statistics",
            body={"stationIds": [station_id]},
        )
        return AlarmCounts.from_api(data or {})

    async def async_get_alarms(
        self, station_id: str, page_size: int = 20
    ) -> list[Alarm]:
        """The most recent alarms, active and recovered."""
        data = await self._async_request(
            "POST",
            "/sems-alarm/api/v2/alarm/page",
            body={"pageIndex": 1, "pageSize": page_size, "stationIds": [station_id]},
        )
        return [
            Alarm.from_api(row)
            for row in (data or {}).get("dataList") or []
            if isinstance(row, dict) and isinstance(row.get("warningid"), str)
        ]

    async def async_get_control_tree(self, sn: str) -> dict[str, Any]:
        """Every remote-control function the device exposes, as one tree."""
        data = await self._async_request(
            "POST",
            "/sems-remote/api/v2/address/remote/getTopTreeByCode",
            body={"sn": sn, "menuCode": 0, "batIndex": ""},
        )
        return data if isinstance(data, dict) else {}

    async def async_get_battery_functions(
        self, sn: str, battery_index: str
    ) -> dict[str, Any]:
        """The battery system's general functions (immediate charging, ...).

        These are not part of the control tree; `battery_index` is a battery
        system's `index` from `async_get_battery_systems`.
        """
        data = await self._async_request(
            "POST",
            "/sems-remote/api/v2/address/remote/getDeviceFunctionTabMenus",
            body={
                "batIndex": battery_index,
                "menuCode": 1,
                "module": "GENERAL_FUNCTIONS",
                "sn": sn,
            },
        )
        return data if isinstance(data, dict) else {}

    async def async_get_function_values(
        self, sn: str, functions: dict[str, str]
    ) -> dict[str, float | None]:
        """Current values keyed by address; `functions` maps address to function id.

        Values come back divided by the function's gain.
        """
        data = await self._async_request(
            "POST",
            "/sems-remote/api/v1/address/remote/get-cache-device-function-parameters",
            body={"sn": sn, "addresses": list(functions), "addrFuncMap": functions},
        )
        values = (data or {}).get("data") or {}
        return {address: _to_float(values.get(address)) for address in functions}

    async def async_set_function_values(
        self,
        *,
        station_id: str,
        sn: str,
        device_name: str,
        values: dict[str, int],
        functions: dict[str, str],
        log: dict[str, Any],
    ) -> None:
        """Write function values.

        `values` and `functions` are keyed by address; `log` is the audit entry
        SEMS+ records alongside the change.
        """
        await self._async_request(
            "POST",
            "/sems-remote/api/v1/address/remote/setDeviceFunctionParameters",
            body={
                "sn": sn,
                "addressMap": values,
                "addrFuncMap": functions,
                "controlItemLogs": log,
                "waitingForDevice": True,
                "plantId": station_id,
                "deviceName": device_name,
                "virtualSn": sn,
            },
        )


def _message(response: dict[str, Any]) -> str:
    for key in ("msg", "description", "errorMsg"):
        if isinstance(value := response.get(key), str) and value:
            return value
    return "SEMS+ request failed"


def _retry_after(value: str | None) -> int:
    try:
        seconds = int(value) if value is not None else _RATE_LIMIT_PAUSE
    except ValueError:
        seconds = _RATE_LIMIT_PAUSE
    return min(max(seconds, 1), _MAX_RETRY_AFTER)


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
