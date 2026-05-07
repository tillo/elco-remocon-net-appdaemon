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
- The two-helper pattern (`*_set` for control, `sensor.elco_*` for state) is
  intentional. The state sensors continue to reflect what the boiler reports;
  the `*_set` helpers reflect the most recent user-requested value.
- If the gateway is offline, writes will succeed at the cloud layer but the
  boiler won't change until reconnect. The sensors will lag accordingly.
