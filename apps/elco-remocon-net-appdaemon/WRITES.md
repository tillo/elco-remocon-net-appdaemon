# Setpoint write support (mdapi fork)

This fork adds optional write-back to the BSB cloud (`POST /R2/PlantHomeBsb/SetData/<gw>`)
on top of the upstream read-only feature set. Disabled by default.

## Enabling

In `apps.yaml`:

```yaml
remocon:
  module: elco-remocon-net-appdaemon
  class: Remocon
  plugin: HASS
  base_url: https://www.remocon-net.remotethermo.com
  username: !secret remocon_username
  password: !secret remocon_password
  gateway_id: !secret remocon_gateway_id
  bearer_token: !secret remocon_bearer_token
  refresh_rate: 10
  enable_writes: true   # opt in to the SetData listeners
```

Without `enable_writes`, behaviour is identical to upstream (read-only).

## Required HA helpers

The app listens for state changes on six HA helper entities. They must exist
before the app starts (or before the first change you want to push). Add to
your HA `configuration.yaml`:

```yaml
input_number:
  elco_dhw_comfort_temp_set:
    name: ELCO DHW comfort setpoint
    min: 45
    max: 65
    step: 1
    unit_of_measurement: "°C"
  elco_dhw_reduced_temp_set:
    name: ELCO DHW reduced setpoint
    min: 8
    max: 55
    step: 1
    unit_of_measurement: "°C"
  elco_ch_comfort_temp_set:
    name: ELCO CH comfort setpoint
    min: 16.5
    max: 35
    step: 0.5
    unit_of_measurement: "°C"
  elco_ch_reduced_temp_set:
    name: ELCO CH reduced setpoint
    min: 10
    max: 19.5
    step: 0.5
    unit_of_measurement: "°C"

input_select:
  elco_zone_mode_set:
    name: ELCO zone mode
    options:
      - Protection
      - Automatic
      - Reduced
      - Comfort
  elco_dhw_mode_set:
    name: ELCO DHW mode
    options:
      - "Off"
      - "On"
```

The min/max ranges above match what the cloud reports for an LMS14 / heat-only
plant. Adjust if your hardware reports different limits.

## Mode encodings

| HA `input_select` value | BSB int |
|---|---|
| Protection | 0 |
| Automatic | 1 |
| Reduced | 2 |
| Comfort | 3 |

| HA `input_select` value | BSB int |
|---|---|
| Off | 0 |
| On | 1 |

## How it works

1. Each successful GetData poll caches the current writable values in
   `self.last_writable` (both `plantData` and `zoneData` blocks).
2. When a watched HA helper changes, the corresponding field is updated in the
   cache and a `POST /R2/PlantHomeBsb/SetData/<gw>` is issued. SetData rejects
   payloads that are missing either block, so we always send both.
3. On a successful write, a follow-up GetData poll runs ~5 s later to refresh
   the sensors.

## Caveats

- Writes go through the Ariston Group cloud, not directly to the boiler. There
  is a few-seconds round-trip and rate-limiting on the cloud side.
- Sending the full GetData echo back is rejected with
  `:-( Bsb parameters read/write error from GW <gw>: <ids>` (HTTP 599) because
  read-only datapoints can't be written. Only the six writable fields above
  may appear in the payload.
- **CH schedule-slot temperatures (chComfortTemp, chReducedTemp) are NOT
  writable via `PlantHomeBsb/SetData`.** The cloud accepts the request with
  `ok: true` but the boiler does not honor the new values, and the cloud's
  cached register can silently desync (a no-op echo of 19.5/16.5 has been
  observed to leave the cloud reporting 4.0/4.0 with inverted min/max
  sentinels). The module routes CH temp helper changes to the dedicated
  schedule-slot endpoint instead:

      POST /R2/PlantTimeProgBsb/SetTemperature/<gw>
      {
        "zoneNum":   <zone>,
        "comfort":   <new comfort °C>,
        "reduced":   <new reduced °C>,
        "plantData": null,
        "zoneData":  <full current zoneData object>
      }

  Both `comfort` and `reduced` must be present every call (pass the current
  value for whichever side is not changing); `zoneData` must be the full
  freshly-fetched object — not the slim writable subset used by SetData.
  Payload-shape source: dashboard JS, `je` constructor in
  `Scripts/R2/app.bundle.min.js` — `this.zoneNum=e, this.comfort=t,
  this.reduced=n, this.plantData=r, this.zoneData=i`.
- DHW writes (`dhwComfortTemp`, `dhwReducedTemp`, `dhwMode`) and zone-mode
  writes (`mode.value`) go through `PlantHomeBsb/SetData`, which works for
  those fields. The SetData payload deliberately omits CH temps so DHW
  writes can never accidentally clobber CH state.
- The BSB controller enforces `reduced <= comfort` for both the DHW pair
  and the CH pair. Local guard rejects violating writes (cloud accepts
  them with a misleading `ok: true` and silently reverts the boiler) —
  adjust the counterpart helper first.
- All polls use `useCache: false`. A `useCache: true` read after a SetData
  returns stale cloud-cached values for several seconds, and the cached
  `last_writable` would diverge from boiler state, breaking constraint checks
  on the next write.
- The two-helper pattern (`*_set` for control, `sensor.elco_*` for state) is
  intentional. The state sensors continue to reflect what the boiler reports;
  the `*_set` helpers reflect the most recent user-requested value.
- If the gateway is offline, writes will succeed at the cloud layer but the
  boiler won't change until reconnect. The sensors will lag accordingly.

## Holiday set/clear

The "Set holiday" feature in the Remocon-NET app maps to mutations of
`zoneData.holidays` (an array on `BsbZoneData`) that are echoed back via the
existing `PlantHomeBsb/SetData/<gw>` endpoint. There is no separate holiday
endpoint — the cloud merges by `zone` + `index` on each save.

Per-entry shape (source: `holidayModel` in `Scripts/R2/app.bundle.min.js`):

```
{ index: <int>, fromAsIso: "YYYY-MM-DDTHH:MM:SS", toAsIso: "YYYY-MM-DDTHH:MM:SS",
  added: bool?, changed: bool?, deleted: bool? }
```

Mutation rules (mirrored from `holidayModel.set` / `deleteCurrentHoliday`):

- Setting a return date when no active holiday exists → push
  `{index: <len>, added: true, fromAsIso: <now>, toAsIso: <return date>}`.
- Setting a return date when a holiday already exists → update its `toAsIso`
  and mark `changed: true`.
- Clearing the holiday → mark the active entry `deleted: true`. The cloud
  removes it on the next SetData.

Two HA helpers drive this:

```yaml
input_boolean:
  elco_holiday_active:
    name: ELCO Holiday active
    icon: mdi:airplane
input_datetime:
  elco_holiday_until:
    name: ELCO Holiday return date
    icon: mdi:calendar-end
    has_date: true
    has_time: false
```

A single listener watches both. When either changes, the app reconciles:
toggling `elco_holiday_active=on` with a future date in `elco_holiday_until`
issues an add/change; toggling `off` issues a delete. The zone mode is forced
to `Automatic` for an active holiday — the boiler firmware refuses the save
otherwise.

Read path: `sensor.elco_holiday_until` carries the active return date
(`toAsIso`) as a `device_class: timestamp` state, `binary_sensor.elco_holiday_active`
mirrors whether any non-deleted holiday exists.

## Weekly schedule (read-only for now)

The Remocon "Chauffage" schedule view comes back inside the existing
`PlantHomeBsb/GetData` response when the filter explicitly requests it.
Default `progIds: null` (or `[]`) excludes schedules from the response;
`progIds: [<zone>]` includes the heating-zone schedule:

```
POST /R2/PlantHomeBsb/GetData/<gw>
body: {useCache: false, zone: 1, filter: {progIds: [1], plant: true, zone: true}}
response: {ok: true, data: {plantData, zoneData, timeProgs: [{weeklyPlan, ...}]}}
```

`progIds` enum (from `Scripts/R2/app.bundle.min.js`):

| Value | Programme        |
|-------|------------------|
| 1..6  | ChZn1..ChZn6 (heating zone 1..6) |
| 7     | Dhw              |
| 8     | Extra            |
| 9..14 | CoolZn1..CoolZn6 |
| 15,16 | Extra1, Extra2   |

For a single-zone CH plant, `progIds: [zone]` is what you want.

`timeProgs[0].weeklyPlan` shape:

```
{ plans: [
    { days: [<int day-of-week>...], slices: [{from: <min from midnight>, temp: <0|1>}, ...] },
    ...
  ],
  allowedTemp, defaultTemp, baseTemp, tick, maxSwitches, ext, pilot
}
```

**`slice.temp` is a 0/1 state flag, NOT a °C value.** 0 = reduced setpoint,
1 = comfort setpoint. The actual temperatures come from `zoneData.chComfortTemp`
and `zoneData.chReducedTemp` (BSB stores those as separate scalars; the
schedule only encodes which setpoint applies to each slice).

Day numbering is `Date.getDay()` (0=Sunday, 1=Monday, ..., 6=Saturday).
The app flattens this into 7 sensors `sensor.elco_schedule_{monday..sunday}`,
each carrying two attributes:
- `slices`: raw slice list with `{from_min, from_hhmm, flag, mode, temp}`
- `intervals`: derived `[{start_min, end_min, start_hhmm, end_hhmm, mode, temp}]`,
  each interval ending where the next slice begins (or at 24:00 for the last).

Write-back for the schedule (`PlantTimeProgBsb/SetTimeProg/<gw>`) is **not yet
wired**. Existing CH temp writes still go through `PlantTimeProgBsb/SetTemperature`
(see above).

## Cloud-side fields that are NOT exposed for BSB plants

The `BsbPlantData` class (Remocon JS bundle) declares these fields:
`outsideTemp, hasOutsideTempProbe, dhwComfortTemp, dhwReducedTemp, dhwMode,
dhwEnabled, flameSensor, heatPumpOn, dhwStorageTemp, dhwStorageTempError,
hasDhwStorageProbe, outsideTempError, isDhwProgReadOnly`.

There is **no boiler flow/return water temperature** in the BSB data model.
The "current temperature" shown in the Remocon hot-water screen is the DHW
tank probe (`dhwStorageTemp` → `sensor.elco_domestic_hot_water_storage_temperature`).
For room temperature, use `sensor.elco_room_temperature`. There is no third
"boiler water" temp available from this API surface.

## Holiday-stays-in-Reduced flag

`zoneData.useReducedOperationModeOnHoliday` is a single bool that switches
between two BSB behaviours during an active holiday:

- `false` (controller default): the heating zone is fully off for the duration.
- `true`: the zone drops to the **Reduced** setpoint instead. Useful when the
  property has frost-sensitive plumbing or pets staying behind.

Exposed read-side as `binary_sensor.elco_holiday_use_reduced`; toggled via
`input_boolean.elco_holiday_use_reduced`. The listener mutates `zoneData.useReducedOperationModeOnHoliday` and POSTs `PlantHomeBsb/SetData`.

## Weekly schedule write-back

POST `/R2/PlantTimeProgBsb/SetTimeProg/<gw>` accepts:

```
{ progId: <int>, weeklyPlan: <new plan>, prevWeeklyPlan: <last known plan> }
```

`prevWeeklyPlan` is used by the cloud for conflict detection; the app caches
the most recently fetched plan in `self.last_weekly_plan` for this purpose.

The user-facing surface is a single staging entity, `input_text.elco_weekly_plan_json`:
write a JSON document of the form

```
{
  "plans": [
    {"days": [<0..6>...], "slices": [{"from": <minutes>, "temp": 0 or 1}, ...]},
    ...
  ]
}
```

into it. The app parses, merges the user's `plans` array on top of the cached
`weeklyPlan` (preserving `ext`, `maxSwitches`, `tick`, `baseTemp`, etc.), then
POSTs SetTimeProg. Validation is minimal — a rejected payload comes back as
`ok=false` from the cloud and is logged.

Slice temperature is the BSB **flag** (0 = reduced, 1 = comfort), not the
actual °C. To change the °C, write through the existing `input_number.elco_ch_comfort_temp_set`
/ `elco_ch_reduced_temp_set` helpers (routed to `SetTemperature`).
