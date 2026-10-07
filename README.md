# sems-plus-client

Async Python client for the GoodWe **SEMS+** cloud API: the `*-gateway.semsportal.com`
service behind the SEMS+ app and the semsplus.goodwe.com web portal.

It only speaks SEMS+. Classic SEMS (`semsportal.com/api/v3/...`) routes return
404 for SEMS+ accounts, so there is no legacy fallback here.

Written for the [`sems_plus`](https://github.com/haids3/ha-sems-plus) Home
Assistant integration, but has no Home Assistant dependency.

## Usage

```python
import aiohttp
from sems_plus_client import SemsPlusClient

async with aiohttp.ClientSession() as session:
    client = SemsPlusClient(session, "user@example.com", "password")
    for station in await client.async_get_stations():
        flow = await client.async_get_power_flow(station.id)
        print(station.name, flow.pv, flow.battery, flow.grid, flow.load)
```

One client represents one account. It serialises and spaces its requests, so
every station of the account can share it without tripping the API's rate
limit. A rate-limit response raises `SemsPlusRateLimitError` and pauses the
client for the time the API asked for. Writes wait for the device to confirm,
which can take over 30 seconds, so they get a longer timeout and do not hold
up the queue; a device that rejects a write raises `SemsPlusCommandError`.

`PowerFlow.grid` is positive while importing. SEMS+ itself reports `pGrid`
positive while exporting; the client flips it.

## What it covers

| area | methods |
|---|---|
| stations | `async_get_stations`, `async_get_station_info`, `async_get_power_flow` |
| devices | `async_get_devices`, `async_get_telemetry`, `async_get_counters`, `async_get_battery_systems` |
| energy | `async_get_statistics`, `async_get_currency` |
| alarms | `async_get_alarm_counts`, `async_get_alarms` |
| devices | `async_get_device_details` (models), `async_get_device_information` (firmware) |
| remote control | `async_get_general_functions`, `async_get_control_tree`, `async_get_battery_functions`, `async_get_related_sn`, `async_get_function_values`, `async_set_function_values`, `list_control_functions`, `find_control_functions` |
| work modes, TOU | `async_get_work_mode`, `async_remote_get`, `async_remote_set`, `TouSlot`, `WORK_MODES`, `InverterFeatures` |
| live values | `SemsPlusLiveFeed`, `async_get_live_credentials`, `async_live_enabled` |

Telemetry and counters come back keyed by SEMS+'s own factor codes (`pAc`,
`MPPT-1:Vpv`, `soc`, `proPvStatsToday`, ...) rather than renamed. A factor the
device lists without a value is kept as `None`, so "unsupported" and "not
returned" can be told apart.

Remote control works from the device's own control menus: list its functions
(`list_control_functions` keeps each one's `funcKey` and menu path), match the
one you want, and read or write it by the address and id the menu gives.
Nothing is hard-coded per model. `MENU_CODES` gives the `menuCode` each device
type needs.

`SemsPlusLiveFeed` subscribes to the MQTT "second data" SEMS+ pushes every few
seconds for each station and device. It only ever subscribes.

## Attribution

The SEMS+ web login (password encoding, the `semsPlusWeb` token and the
`X-Signature` scheme) was worked out in
[TimSoethout/goodwe-sems-home-assistant](https://github.com/TimSoethout/goodwe-sems-home-assistant)
(MIT) by its contributors. This is an independent async implementation.
Everything else, including alarms, station status, the control tree and TOU, was
established by probing live accounts and from a web-portal capture. See
[the fork's API notes](https://github.com/haids3/goodwe-sems-home-assistant/blob/feat/alarms-and-grid-status/docs/sems-plus-api-notes.md).

Not affiliated with or endorsed by GoodWe.

## License

MIT
