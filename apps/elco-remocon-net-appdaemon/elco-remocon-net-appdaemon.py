import hassapi as hass
from datetime import datetime, timedelta
import requests
import json
from urllib.parse import quote, urljoin
import posixpath


# zoneData.mode encoding (BSB)
ZONE_MODE_TO_INT = {"Protection": 0, "Automatic": 1, "Reduced": 2, "Comfort": 3}
ZONE_MODE_TO_TEXT = {v: k for k, v in ZONE_MODE_TO_INT.items()}

# plantData.dhwMode encoding (BSB)
DHW_MODE_TO_INT = {"Off": 0, "On": 1}
DHW_MODE_TO_TEXT = {v: k for k, v in DHW_MODE_TO_INT.items()}

# HA writable-control entities → (block, field, kind)
# block: "plantData" | "zoneData"
# kind:  "temp" (number)  |  "zone_mode" (4-way text)  |  "dhw_mode" (2-way text)
WRITE_CONTROLS = {
    "input_number.elco_dhw_comfort_temp_set":  ("plantData", "dhwComfortTemp",  "temp"),
    "input_number.elco_dhw_reduced_temp_set":  ("plantData", "dhwReducedTemp",  "temp"),
    "input_select.elco_dhw_mode_set":          ("plantData", "dhwMode",         "dhw_mode"),
    "input_number.elco_ch_comfort_temp_set":   ("zoneData",  "chComfortTemp",   "temp"),
    "input_number.elco_ch_reduced_temp_set":   ("zoneData",  "chReducedTemp",   "temp"),
    "input_select.elco_zone_mode_set":         ("zoneData",  "mode",            "zone_mode"),
}

# Holiday write controls — handled separately from WRITE_CONTROLS because they
# mutate zoneData.holidays (an array) rather than a scalar field, and the BSB
# state semantics (added/changed/deleted flags) are non-trivial.
HOLIDAY_DATE_ENTITY = "input_datetime.elco_holiday_until"
HOLIDAY_ACTIVE_ENTITY = "input_boolean.elco_holiday_active"
HOLIDAY_USE_REDUCED_ENTITY = "input_boolean.elco_holiday_use_reduced"

# Schedule write — triggered by an HA event with the full weeklyPlan in data.
# Using an event (vs. an input_text) avoids HA's 255-char limit on text helpers
# and lets large/complex plans round-trip without truncation.
#
# Example HA service call:
#   service: hassio.send_command            # any event-firing transport works
# or via REST API:
#   POST /api/events/elco_set_weekly_plan {"plans": [...], "ext": false, ...}
SCHEDULE_EVENT = "elco_set_weekly_plan"

# Weekday index → entity-id suffix. Remocon JS uses Date.getDay() (0=Sunday) for
# the day field on schedule slices, so we mirror that.
WEEKDAY_SUFFIX = {
    0: "sunday",
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
}


class Remocon(hass.Hass):
    def initialize(self):
        refresh_rate = self.args.get("refresh_rate")
        if refresh_rate is None:
            refresh_rate = 60
        self.log(f"Will fetch remocon.net data every {refresh_rate} min")
        self.run_every(self.get_remocon_data, datetime.now(), refresh_rate * 60)
        # Monthly history of heating-energy lifetime via HA's WS-only
        # recorder/statistics_during_period. Runs every 6h. Publishes the
        # result to sensor.elco_heating_monthly_history for the dashboard
        # comparison card.
        self.run_every(self.refresh_monthly_stats,
                       datetime.now() + timedelta(seconds=30), 6 * 3600)

        # cache of writable values from the latest GetData; used to build SetData payloads
        # since SetData requires both plantData and zoneData blocks.
        self.last_writable = None
        # full zoneData/plantData snapshots required by SetTemperature (CH temps endpoint).
        self.last_full_zoneData = None
        self.last_full_plantData = None
        # Last fetched weeklyPlan — needed as `prevWeeklyPlan` in SetTimeProg.
        self.last_weekly_plan = None

        # fetch data immediately on startup
        self.log("Fetching remocon.net data immediately on startup")
        self.get_remocon_data(None)

        # register write listeners if enabled
        if self.args.get("enable_writes", False):
            self.log("Write controls enabled — registering input_number/input_select listeners")
            for entity_id in WRITE_CONTROLS:
                self.listen_state(self.on_control_changed, entity_id)
            # Holiday helpers — single listener; we read both entities each time
            # one of them changes so an active=on toggle + a date pick land as one write.
            self.listen_state(self.on_holiday_changed, HOLIDAY_DATE_ENTITY)
            self.listen_state(self.on_holiday_changed, HOLIDAY_ACTIVE_ENTITY)
            # Whether holiday drops to Reduced (off=false) or fully shuts CH (true).
            # Default in BSB controllers is false (boiler fully off during holiday).
            self.listen_state(self.on_holiday_use_reduced_changed, HOLIDAY_USE_REDUCED_ENTITY)
            # Schedule write — listens for an HA event; payload is the full weeklyPlan.
            self.listen_event(self.on_schedule_event, SCHEDULE_EVENT)
        else:
            self.log("Write controls disabled (set enable_writes: true in apps.yaml to enable)")

        self.log("Fetching remocon.net data completed")

    # ----- READ PATH (unchanged behavior; refactored to share session helpers) -----

    def post_to_entities(self, data):
        self.log("Posting to entities...")

        def _post_data(sensor, payload):
            try:
                entity_url = f"{ha_url}/api/states/{sensor}"
                token = "Bearer {}".format(self.args["bearer_token"])
                headers = {"Authorization": token, "Content-Type": "application/json"}
                requests.post(entity_url, json=payload, headers=headers)
                self.log(f"Set state on entity {sensor}")
            except Exception as e:
                self.log(e)

        def _post_plantData(plantData):
            data = {
                "state": plantData["outsideTemp"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "friendly_name": "Elco Outside Temperature",
                },
            }
            _post_data("sensor.elco_outside_temperature", data)

            data = {
                "state": plantData["dhwStorageTemp"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "friendly_name": "Elco Domestic Hot Water Storage Temperature",
                },
            }
            _post_data(
                "sensor.elco_domestic_hot_water_storage_temperature",
                data,
            )

            elco_domestic_hot_water_temperature = {
                "state": plantData["dhwComfortTemp"]["value"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "min": plantData["dhwComfortTemp"]["min"],
                    "max": plantData["dhwComfortTemp"]["max"],
                    "step": plantData["dhwComfortTemp"]["step"],
                    "friendly_name": "Elco Domestic Hot Water Storage Temperature",
                },
            }
            _post_data(
                "sensor.elco_domestic_hot_water_temperature",
                elco_domestic_hot_water_temperature,
            )

            data = {
                "state": plantData["dhwReducedTemp"]["value"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "min": plantData["dhwReducedTemp"]["min"],
                    "max": plantData["dhwReducedTemp"]["max"],
                    "step": plantData["dhwReducedTemp"]["step"],
                    "friendly_name": "Elco Domestic Hot Water Storage Reduced Temperature",
                },
            }
            _post_data(
                "sensor.elco_domestic_hot_water_reduced_temperature",
                data,
            )

            data = {
                "state": "on" if plantData["dhwMode"]["value"] == 1 else "off",
                "attributes": {
                    "device_class": "running",
                    "friendly_name": "Elco Domestic Hot Water Storage Mode",
                    "icon": "mdi:hvac",
                },
            }
            _post_data(
                "binary_sensor.elco_domestic_hot_water_storage_mode",
                data,
            )

            data = {
                "state": "on" if plantData["dhwStorageTempError"] == 1 else "off",
                "attributes": {
                    "device_class": "problem",
                    "icon": "mdi:radiator",
                    "friendly_name": "Elco Domestic Hot Water Storage Temperature Error",
                },
            }
            _post_data(
                "binary_sensor.elco_domestic_hot_water_storage_temperature_error",
                data,
            )

            data = {
                "state": "on" if plantData["outsideTempError"] == 1 else "off",
                "attributes": {
                    "device_class": "problem",
                    "icon": "mdi:radiator",
                    "friendly_name": "Elco Outside Temperature Error",
                },
            }
            _post_data(
                "binary_sensor.elco_outside_temperature_error",
                data,
            )

            data = {
                "state": "on" if plantData["hasOutsideTempProbe"] == 1 else "off",
                "attributes": {
                    "device_class": "connectivity",
                    "icon": "mdi:thermometer-probe",
                    "friendly_name": "Elco has Outside Temperature Probe",
                },
            }
            _post_data(
                "binary_sensor.elco_has_outside_temperature_probe",
                data,
            )

            data = {
                "state": "on" if plantData["flameSensor"] == 1 else "off",
                "attributes": {
                    "device_class": "connectivity",
                    "icon": "mdi:fire",
                    "friendly_name": "Elco Flame sensor",
                },
            }
            _post_data(
                "binary_sensor.elco_flame_sensor",
                data,
            )

            data = {
                "state": "on" if plantData["hasDhwStorageProbe"] == 1 else "off",
                "attributes": {
                    "device_class": "connectivity",
                    "friendly_name": "Elco has Domestic Hot Water Storage Probe",
                    "icon": "mdi:thermometer-probe",
                },
            }
            _post_data(
                "binary_sensor.elco_has_domestic_hot_water_storage_probe",
                data,
            )

            data = {
                "state": "on" if plantData["heatPumpOn"] == 1 else "off",
                "attributes": {
                    "device_class": "running",
                    "icon": "mdi:radiator",
                    "friendly_name": "Elco HeatPump On",
                },
            }
            _post_data("binary_sensor.elco_heatpump_on", data)

            data = {
                "state": "on" if plantData["dhwEnabled"] == 1 else "off",
                "attributes": {
                    "device_class": "running",
                    "icon": "mdi:radiator",
                    "friendly_name": "Elco Domestic Hot Water Enabled",
                },
            }
            _post_data(
                "binary_sensor.elco_domestic_hot_water_enabled",
                data,
            )

        def _post_zoneData(zoneData):
            data = {
                "state": zoneData["chComfortTemp"]["value"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "min": zoneData["chComfortTemp"]["min"],
                    "max": zoneData["chComfortTemp"]["max"],
                    "step": zoneData["chComfortTemp"]["step"],
                    "friendly_name": "Elco Comfort room Temperature setpoint",
                },
            }
            _post_data(
                "sensor.elco_comfort_room_temperature_setpoint",
                data,
            )

            data = {
                "state": zoneData["coolComfortTemp"]["value"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "min": zoneData["coolComfortTemp"]["min"],
                    "max": zoneData["coolComfortTemp"]["max"],
                    "step": zoneData["coolComfortTemp"]["step"],
                    "friendly_name": "Elco Cool Comfort room Temperature setpoint",
                },
            }
            _post_data(
                "sensor.elco_cool_comfort_room_temperature_setpoint",
                data,
            )

            data = {
                "state": zoneData["coolReducedTemp"]["value"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "min": zoneData["coolReducedTemp"]["min"],
                    "max": zoneData["coolReducedTemp"]["max"],
                    "step": zoneData["coolReducedTemp"]["step"],
                    "friendly_name": "Elco Cool Reduced room Temperature setpoint",
                },
            }
            _post_data(
                "sensor.elco_cool_reduced_room_temperature_setpoint",
                data,
            )

            data = {
                "state": zoneData["chReducedTemp"]["value"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "min": zoneData["chReducedTemp"]["min"],
                    "max": zoneData["chReducedTemp"]["max"],
                    "step": zoneData["chReducedTemp"]["step"],
                    "friendly_name": "Elco Reduced room Temperature setpoint",
                },
            }
            _post_data(
                "sensor.elco_reduced_room_temperature_setpoint",
                data,
            )

            data = {
                "state": zoneData["roomTemp"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "friendly_name": "Elco Room Temperature",
                },
            }
            _post_data("sensor.elco_room_temperature", data)

            data = {
                "state": zoneData["desiredRoomTemp"],
                "attributes": {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "friendly_name": "Elco Desired Room Temperature",
                },
            }
            _post_data("sensor.elco_desired_room_temperature", data)

            mode_dict = {0: "Protection", 1: "Automatic", 2: "Reduced", 3: "Comfort"}
            data = {
                "state": mode_dict.get(zoneData["mode"]["value"]),
                "attributes": {
                    "icon": "mdi:radiator",
                    "friendly_name": "Elco Room Operation mode heating",
                },
            }
            _post_data(
                "sensor.elco_room_operation_mode_heating",
                data,
            )

            data = {
                "state": "on" if zoneData["isHeatingActive"] == 1 else "off",
                "attributes": {
                    "device_class": "running",
                    "friendly_name": "Elco Room Heating is Active",
                },
            }
            _post_data("binary_sensor.elco_room_heating_is_active", data)

            data = {
                "state": "on" if zoneData["isCoolingActive"] == 1 else "off",
                "attributes": {
                    "device_class": "running",
                    "friendly_name": "Elco Room Cooling is Active",
                },
            }
            _post_data("binary_sensor.elco_room_cooling_is_active", data)

            data = {
                "state": "on" if zoneData["heatOrCoolRequest"] == 1 else "off",
                "attributes": {
                    "device_class": "problem",
                    "friendly_name": "Elco Room Heating or Cool is Request",
                },
            }
            _post_data(
                "binary_sensor.elco_room_heating_is_request",
                data,
            )

            data = {
                "state": "on" if zoneData["roomTempError"] == 1 else "off",
                "attributes": {
                    "device_class": "problem",
                    "friendly_name": "Elco Room Temperature Error",
                },
            }
            _post_data(
                "binary_sensor.elco_room_temperature_error",
                data,
            )

            # Additional BsbZoneData fields not previously exposed.
            if "chProtectionTemp" in zoneData:
                _post_data("sensor.elco_ch_protection_temp", {
                    "state": zoneData["chProtectionTemp"],
                    "attributes": {
                        "unit_of_measurement": "°C",
                        "device_class": "temperature",
                        "friendly_name": "Elco CH Frost Protection Temperature",
                        "icon": "mdi:snowflake-thermometer",
                    },
                })
            if "chHolidayTemp" in zoneData:
                _post_data("sensor.elco_ch_holiday_temp", {
                    "state": zoneData["chHolidayTemp"],
                    "attributes": {
                        "unit_of_measurement": "°C",
                        "device_class": "temperature",
                        "friendly_name": "Elco CH Holiday Setpoint",
                        "icon": "mdi:airplane-clock",
                    },
                })
            if "useReducedOperationModeOnHoliday" in zoneData:
                _post_data("binary_sensor.elco_holiday_use_reduced", {
                    "state": "on" if zoneData["useReducedOperationModeOnHoliday"] else "off",
                    "attributes": {
                        "friendly_name": "Elco Holiday stays in Reduced",
                        "icon": "mdi:weather-night",
                    },
                })
            if "hasRoomSensor" in zoneData:
                _post_data("binary_sensor.elco_has_room_sensor", {
                    "state": "on" if zoneData["hasRoomSensor"] else "off",
                    "attributes": {
                        "device_class": "connectivity",
                        "friendly_name": "Elco has Room Sensor",
                        "icon": "mdi:thermometer-probe",
                    },
                })

            # holidays: array of {index, fromAsIso, toAsIso, added, changed, deleted}
            # from BsbZoneData. The "current" holiday is the first non-deleted one.
            holidays = zoneData.get("holidays") or []
            current = next((h for h in holidays if not (h or {}).get("deleted")), None)
            if current:
                until = current.get("toAsIso")
                _post_data("binary_sensor.elco_holiday_active", {
                    "state": "on",
                    "attributes": {
                        "device_class": "running",
                        "friendly_name": "Elco Holiday Active",
                        "icon": "mdi:airplane",
                        "from": current.get("fromAsIso"),
                        "index": current.get("index"),
                    },
                })
                _post_data("sensor.elco_holiday_until", {
                    "state": until or "unknown",
                    "attributes": {
                        "device_class": "timestamp" if until else None,
                        "friendly_name": "Elco Holiday Return Date",
                        "icon": "mdi:calendar-end",
                    },
                })
            else:
                _post_data("binary_sensor.elco_holiday_active", {
                    "state": "off",
                    "attributes": {
                        "device_class": "running",
                        "friendly_name": "Elco Holiday Active",
                        "icon": "mdi:airplane",
                    },
                })
                _post_data("sensor.elco_holiday_until", {
                    "state": "none",
                    "attributes": {
                        "friendly_name": "Elco Holiday Return Date",
                        "icon": "mdi:calendar-end",
                    },
                })

        def _post_schedule(weeklyPlan, comfort_temp, reduced_temp):
            """Publish 7 sensors (one per weekday) carrying the slice list as an
            attribute. State = number of slices for that day so HA history is useful.

            weeklyPlan shape (from BsbTimeProgBsb GetData response):
              {
                plans: [
                  {days: [<int day-of-week>...], slices: [{from: <min from midnight>, temp: <0|1>}, ...]},
                  ...
                ],
                allowedTemp, defaultTemp, baseTemp, tick, maxSwitches, ext, pilot
              }

            `slice.temp` is a state flag (0=reduced, 1=comfort), NOT a °C value.
            The actual °C come from zoneData.chComfortTemp / chReducedTemp.
            Day-of-week indices use Date.getDay() — 0=Sunday, 1=Monday, ...
            """
            plans = (weeklyPlan or {}).get("plans") or []
            # Flatten: per-weekday slice list
            by_day = {d: [] for d in WEEKDAY_SUFFIX}
            for plan in plans:
                if not plan:
                    continue
                slices = sorted(
                    [s for s in (plan.get("slices") or []) if s is not None],
                    key=lambda s: s.get("from", 0),
                )
                for d in plan.get("days") or []:
                    if d in by_day:
                        by_day[d] = slices

            def mode_for(flag):
                return "comfort" if flag == 1 else "reduced"

            def temp_for(flag):
                return comfort_temp if flag == 1 else reduced_temp

            for day_idx, suffix in WEEKDAY_SUFFIX.items():
                slices = by_day[day_idx]
                # Render two attributes for the dashboard:
                # - `slices`: raw slice list (flag + from-minutes + HH:MM convenience)
                # - `intervals`: derived [{start_hhmm, end_hhmm, mode, temp}, ...]
                #   ending each at the next slice (or 24:00 for the last one)
                rendered = []
                intervals = []
                for i, s in enumerate(slices):
                    m = int(s.get("from", 0))
                    flag = s.get("temp")
                    rendered.append({
                        "from_min": m,
                        "from_hhmm": f"{m // 60:02d}:{m % 60:02d}",
                        "flag": flag,
                        "mode": mode_for(flag),
                        "temp": temp_for(flag),
                    })
                    end_m = int(slices[i + 1].get("from", 1440)) if (i + 1) < len(slices) else 1440
                    intervals.append({
                        "start_min": m,
                        "end_min": end_m,
                        "start_hhmm": f"{m // 60:02d}:{m % 60:02d}",
                        "end_hhmm": f"{end_m // 60:02d}:{end_m % 60:02d}",
                        "mode": mode_for(flag),
                        "temp": temp_for(flag),
                    })
                _post_data(f"sensor.elco_schedule_{suffix}", {
                    "state": len(rendered),
                    "attributes": {
                        "friendly_name": f"Elco Schedule {suffix.capitalize()}",
                        "icon": "mdi:calendar-clock",
                        "unit_of_measurement": "slices",
                        "slices": rendered,
                        "intervals": intervals,
                        "ext": (weeklyPlan or {}).get("ext"),
                        "max_switches": (weeklyPlan or {}).get("maxSwitches"),
                    },
                })

        try:
            if self.args.get("ha_url"):
                # get HA's url from app's first, if configured/overridden by user
                self.log("Using ha_url from app's configuration")
                ha_url = self.args["ha_url"]
            else:
                self.log("Using ha_url from plugins's HASS configuration")
                ha_url = self.config["plugins"]["HASS"]["ha_url"]
        except Exception as e:
            self.error(
                "No Home Assistant URL could be found. Please configure ha_url in the app's configuration. Aborting."
            )
            self.error(e)
            return

        _post_plantData(data["plantData"])
        _post_zoneData(data["zoneData"])
        # timeProgs is populated when get_remocon_data's payload includes
        # filter.progIds=[<zone>]. Each timeProg's slice.temp is a 0/1 mode
        # flag — map it to the actual °C from zoneData (BSB stores those
        # separately as chComfortTemp / chReducedTemp scalars).
        time_progs = data.get("timeProgs") or []
        if time_progs:
            comfort_c = float(data["zoneData"]["chComfortTemp"]["value"])
            reduced_c = float(data["zoneData"]["chReducedTemp"]["value"])
            weekly_plan = (time_progs[0] or {}).get("weeklyPlan") or {}
            _post_schedule(weekly_plan, comfort_c, reduced_c)
            # Cache for SetTimeProg's prevWeeklyPlan slot (cloud uses it for
            # conflict detection against concurrent edits).
            self.last_weekly_plan = weekly_plan

        # cache writable values for SetData payloads
        self.last_writable = {
            "plantData": {
                "dhwComfortTemp": {"value": data["plantData"]["dhwComfortTemp"]["value"]},
                "dhwReducedTemp": {"value": data["plantData"]["dhwReducedTemp"]["value"]},
                "dhwMode":        {"value": data["plantData"]["dhwMode"]["value"]},
            },
            "zoneData": {
                "zone": self.args.get("zone") or 1,
                "chComfortTemp": {"value": data["zoneData"]["chComfortTemp"]["value"]},
                "chReducedTemp": {"value": data["zoneData"]["chReducedTemp"]["value"]},
                "mode":          {"value": data["zoneData"]["mode"]["value"]},
                "useReducedOperationModeOnHoliday": bool(data["zoneData"].get("useReducedOperationModeOnHoliday", False)),
            },
        }
        # Full plant/zone snapshots are needed by the SetTemperature endpoint, which
        # writes CH schedule-slot temps via /R2/PlantTimeProgBsb/SetTemperature/<gw>
        # with payload {zoneNum, comfort, reduced, plantData:null, zoneData:<full>}.
        self.last_full_zoneData = data["zoneData"]
        self.last_full_plantData = data["plantData"]

        # prime HA helpers (input_number / input_select) so they mirror reality
        if self.args.get("enable_writes", False):
            self._prime_helpers()

    # ----- SHARED HELPERS -----

    def _base_url(self):
        return self.args.get("base_url") or "https://www.remocon-net.remotethermo.com"

    def _login(self):
        """Open a session, log in, return the authenticated requests.Session."""
        base_url = self._base_url()
        username = quote(self.args.get("username"), safe="")
        password = quote(self.args.get("password"), safe="")
        login_url = urljoin(base_url, "R2/Account/Login?returnUrl=HTTP/2")
        payload = f"Email={username}&Password={password}&RememberMe=false"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "browserUtcOffset=-120",
        }
        session = requests.session()
        response = session.post(url=login_url, headers=headers, data=payload)
        if response.status_code != 200:
            raise RuntimeError(f"Authentication HTTP {response.status_code}: {response.text[:200]}")
        result = json.loads(response.text)
        if not result.get("ok"):
            raise RuntimeError(f"Authentication rejected: {result.get('message')}")
        return session

    # ----- READ PATH (preserved external interface) -----

    def get_remocon_data(self, kwargs):
        self.log("Fetching remocon data...")
        try:
            base_url = self._base_url()
            zone = self.args.get("zone") or 1
            gateway = self.args.get("gateway_id")
            if not gateway:
                self.error("There was a problem getting configuration values, gateway_id is not defined. Aborting.")
                return
        except Exception:
            self.error("There was a problem getting configuration values. Aborting.")
            return
        try:
            session = self._login()
            # useCache must be False: a True read after a SetData returns stale values
            # and breaks any constraint check that depends on the freshly-written counterpart
            # (e.g. chReducedTemp must be <= chComfortTemp; ditto dhwReduced <= dhwComfort).
            # progIds: per BSB enum, 1=ChZn1 (heating zone 1), 2..6=ChZn2..ChZn6,
            # 7=Dhw, 9..14=CoolZn1..CoolZn6. The cloud returns the requested
            # programmes inside data.timeProgs; passing [zone] gives us the CH
            # schedule for the current heating zone. useCache must be False —
            # see the comment block on writes.
            payload = {
                "useCache": False,
                "zone": zone,
                "filter": {"progIds": [zone], "plant": True, "zone": True},
            }
            data_url = urljoin(base_url, posixpath.join("R2/PlantHomeBsb/GetData", gateway))
            self.log(f"Parameters gatewayId: {gateway} zone: {payload}")
            response = session.post(url=data_url, json=payload)
            if response.status_code != 200:
                self.error(f"Fetching error: {response.text}")
                return
            result_json = json.loads(response.text)
            data = result_json["data"]

            self.post_to_entities(data)
            self.log("Done fetching remocon data.")
        except Exception as e:
            self.error(f"Unhandled exception: {e}")

    # ----- WRITE PATH -----

    def _prime_helpers(self):
        """Push current writable values into the HA helper entities so they reflect reality.
        Listener treats an incoming change that already matches cache as a no-op, so this
        does not feed back into a write."""
        plant = self.last_writable["plantData"]
        zone = self.last_writable["zoneData"]
        targets = [
            ("input_number", "input_number.elco_dhw_comfort_temp_set", float(plant["dhwComfortTemp"]["value"])),
            ("input_number", "input_number.elco_dhw_reduced_temp_set", float(plant["dhwReducedTemp"]["value"])),
            ("input_number", "input_number.elco_ch_comfort_temp_set",  float(zone["chComfortTemp"]["value"])),
            ("input_number", "input_number.elco_ch_reduced_temp_set",  float(zone["chReducedTemp"]["value"])),
            ("input_select", "input_select.elco_zone_mode_set", ZONE_MODE_TO_TEXT.get(zone["mode"]["value"])),
            ("input_select", "input_select.elco_dhw_mode_set",  DHW_MODE_TO_TEXT.get(plant["dhwMode"]["value"])),
        ]
        for domain, entity_id, value in targets:
            if value is None:
                continue
            try:
                if domain == "input_number":
                    self.call_service("input_number/set_value", entity_id=entity_id, value=value)
                else:
                    self.call_service("input_select/select_option", entity_id=entity_id, option=value)
            except Exception as e:
                self.log(f"prime {entity_id} failed: {e}")

        # Holiday helpers: mirror zoneData.holidays into the input_boolean +
        # input_datetime so the dashboard reflects boiler state on startup.
        try:
            holidays = (self.last_full_zoneData or {}).get("holidays") or []
            current = next((h for h in holidays if not (h or {}).get("deleted")), None)
            if current and current.get("toAsIso"):
                self.call_service("input_boolean/turn_on", entity_id=HOLIDAY_ACTIVE_ENTITY)
                # toAsIso is "YYYY-MM-DDTHH:MM:SS"; input_datetime with has_date:true
                # accepts "YYYY-MM-DD".
                date_only = current["toAsIso"].split("T")[0]
                self.call_service("input_datetime/set_datetime",
                                  entity_id=HOLIDAY_DATE_ENTITY, date=date_only)
            else:
                self.call_service("input_boolean/turn_off", entity_id=HOLIDAY_ACTIVE_ENTITY)
        except Exception as e:
            self.log(f"prime holiday helpers failed: {e}")

        # Holiday Reduced mode mirror.
        try:
            if self.last_writable["zoneData"]["useReducedOperationModeOnHoliday"]:
                self.call_service("input_boolean/turn_on", entity_id=HOLIDAY_USE_REDUCED_ENTITY)
            else:
                self.call_service("input_boolean/turn_off", entity_id=HOLIDAY_USE_REDUCED_ENTITY)
        except Exception as e:
            self.log(f"prime {HOLIDAY_USE_REDUCED_ENTITY} failed: {e}")

    def on_control_changed(self, entity, attribute, old, new, kwargs):
        """Listener for input_number / input_select changes. Pushes the new value to remocon-net.
        No-op when the change already matches the cached current value (covers our own primes
        and user toggles that land back on the live state)."""
        if new in ("unavailable", "unknown", None):
            return
        if entity not in WRITE_CONTROLS:
            return
        if not self.last_writable:
            self.error(f"{entity}: cannot write — no GetData snapshot cached yet")
            return

        block, field, kind = WRITE_CONTROLS[entity]
        try:
            if kind == "temp":
                converted = float(new)
            elif kind == "zone_mode":
                converted = ZONE_MODE_TO_INT.get(new)
                if converted is None:
                    self.error(f"{entity}: unknown zone mode '{new}' (expected {list(ZONE_MODE_TO_INT)})")
                    return
            elif kind == "dhw_mode":
                converted = DHW_MODE_TO_INT.get(new)
                if converted is None:
                    self.error(f"{entity}: unknown DHW mode '{new}' (expected {list(DHW_MODE_TO_INT)})")
                    return
        except (ValueError, TypeError) as e:
            self.error(f"{entity}: bad value '{new}': {e}")
            return

        current = self.last_writable[block][field]["value"]
        if kind == "temp":
            if abs(converted - float(current)) < 1e-3:
                self.log(f"{entity}: no-op ({new} matches current); skipping SetData")
                return
        else:
            if int(converted) == int(current):
                self.log(f"{entity}: no-op ({new} matches current); skipping SetData")
                return

        # BSB enforces reduced <= comfort for both DHW and CH. The cloud will accept the
        # write (return ok:true) but silently revert the boiler — and on the next poll the
        # cached counterpart can come back stale, corrupting future writes. Guard locally.
        plant = self.last_writable["plantData"]
        zone = self.last_writable["zoneData"]
        constraint_violation = None
        if field == "chComfortTemp" and converted < float(zone["chReducedTemp"]["value"]):
            constraint_violation = f"chComfort {converted} < chReduced {zone['chReducedTemp']['value']}"
        elif field == "chReducedTemp" and converted > float(zone["chComfortTemp"]["value"]):
            constraint_violation = f"chReduced {converted} > chComfort {zone['chComfortTemp']['value']}"
        elif field == "dhwComfortTemp" and converted < float(plant["dhwReducedTemp"]["value"]):
            constraint_violation = f"dhwComfort {converted} < dhwReduced {plant['dhwReducedTemp']['value']}"
        elif field == "dhwReducedTemp" and converted > float(plant["dhwComfortTemp"]["value"]):
            constraint_violation = f"dhwReduced {converted} > dhwComfort {plant['dhwComfortTemp']['value']}"

        if constraint_violation:
            self.error(f"{entity}: refusing write — would violate boiler constraint ({constraint_violation}). "
                       f"Adjust the counterpart helper first.")
            return

        self.last_writable[block][field] = {"value": converted}

        # Route CH temps to SetTemperature (the schedule-slot temp endpoint), not SetData.
        # SetData accepts those fields with ok:true but does not propagate to the boiler
        # and can corrupt the cloud-side register.
        if field in ("chComfortTemp", "chReducedTemp"):
            new_comfort = float(zone["chComfortTemp"]["value"])
            new_reduced = float(zone["chReducedTemp"]["value"])
            if field == "chComfortTemp":
                new_comfort = converted
            else:
                new_reduced = converted
            self._post_settemperature(new_comfort, new_reduced, reason=f"{entity} → {new}")
        else:
            self._post_setdata(reason=f"{entity} → {new}")

    def _post_settemperature(self, new_comfort, new_reduced, reason):
        """Write CH schedule-slot temps via /R2/PlantTimeProgBsb/SetTemperature/<gw>.

        Payload shape (decoded from the dashboard's `je` constructor):
            {zoneNum, comfort, reduced, plantData: null, zoneData: <full current zoneData>}

        Both `comfort` and `reduced` must be present in every call — the cloud uses them
        together to update the schedule's slot setpoints. Pass current values for whichever
        side is not changing.
        """
        gateway = self.args.get("gateway_id")
        if not gateway:
            self.error("Cannot write: gateway_id not configured")
            return
        if not self.last_full_zoneData:
            self.error("Cannot write CH temps: no zoneData snapshot cached yet")
            return

        payload = {
            "zoneNum": self.last_writable["zoneData"]["zone"],
            "comfort": new_comfort,
            "reduced": new_reduced,
            "plantData": None,
            "zoneData": self.last_full_zoneData,
        }
        url = urljoin(self._base_url(), posixpath.join("R2/PlantTimeProgBsb/SetTemperature", gateway))
        self.log(f"SetTemperature ({reason}): comfort={new_comfort}, reduced={new_reduced}")
        try:
            session = self._login()
            response = session.post(url=url, json=payload)
            if response.status_code != 200:
                self.error(f"SetTemperature HTTP {response.status_code}: {response.text[:300]}")
                return
            result = json.loads(response.text)
            if not result.get("ok"):
                self.error(f"SetTemperature rejected: {result.get('message')} (debug: {result.get('debugMessage')})")
                return
            self.log(f"SetTemperature ok ({reason})")
            self.run_in(self.get_remocon_data, 5)
        except Exception as e:
            self.error(f"SetTemperature exception: {e}")

    def _post_setdata(self, reason):
        gateway = self.args.get("gateway_id")
        if not gateway:
            self.error("Cannot write: gateway_id not configured")
            return

        plant = self.last_writable["plantData"]
        zone = self.last_writable["zoneData"]
        # Include only fields we actually want to write. PlantHomeBsb/SetData requires
        # both plantData and zoneData blocks, but extra fields like chComfortTemp /
        # chReducedTemp must be omitted: the cloud accepts them with ok:true but does
        # not propagate to the boiler and can corrupt the cloud-side cache.
        payload = {
            "plantData": {
                "dhwComfortTemp": {"value": plant["dhwComfortTemp"]["value"]},
                "dhwReducedTemp": {"value": plant["dhwReducedTemp"]["value"]},
                "dhwMode":        {"value": plant["dhwMode"]["value"]},
            },
            "zoneData": {
                "zone": zone["zone"],
                "mode": {"value": zone["mode"]["value"]},
            },
            "viewModel": {"zoneNumber": zone["zone"]},
        }
        url = urljoin(self._base_url(), posixpath.join("R2/PlantHomeBsb/SetData", gateway))
        self.log(f"SetData ({reason}): payload={json.dumps(payload)}")
        try:
            session = self._login()
            response = session.post(url=url, json=payload)
            if response.status_code != 200:
                self.error(f"SetData HTTP {response.status_code}: {response.text[:300]}")
                return
            result = json.loads(response.text)
            if not result.get("ok"):
                self.error(f"SetData rejected: {result.get('message')} (debug: {result.get('debugMessage')})")
                return
            self.log(f"SetData ok ({reason})")
            # refresh sensors a few seconds later so HA reflects the new state
            self.run_in(self.get_remocon_data, 5)
        except Exception as e:
            self.error(f"SetData exception: {e}")

    # ----- HOLIDAY WRITE PATH -----

    def on_holiday_changed(self, entity, attribute, old, new, kwargs):
        """Single listener for both input_datetime.elco_holiday_until and
        input_boolean.elco_holiday_active. We look at both helpers each call
        and reconcile against the cloud:

        - active=on  + a future date → ensure a holiday exists with toAsIso=date
        - active=off                  → mark current holiday deleted (cloud removes)

        Mutation semantics come from the Remocon JS holidayModel (Scripts/R2/app.bundle.min.js):
        * `set(date)`: if a current (non-deleted) holiday exists, set its
          toAsIso=date and mark changed=true; else push a new {added:true,
          fromAsIso:<now>, toAsIso:date, index:<len-of-array>}.
        * `deleteCurrentHoliday()`: set the first non-deleted holiday's
          deleted=true. The cloud removes it on the next SetData.
        """
        if new in ("unavailable", "unknown", None):
            return
        if not self.last_writable:
            self.error("Holiday change ignored — no GetData snapshot cached yet")
            return

        active_state = self.get_state(HOLIDAY_ACTIVE_ENTITY)
        date_state = self.get_state(HOLIDAY_DATE_ENTITY)
        if active_state in ("unavailable", "unknown", None):
            self.log(f"Holiday active state unavailable ({active_state}); skipping")
            return

        want_active = (active_state == "on")
        if want_active:
            if date_state in ("unavailable", "unknown", None, ""):
                self.log(f"Holiday set to active but {HOLIDAY_DATE_ENTITY}={date_state}; "
                         f"pick a return date to push the write")
                return
            try:
                d = datetime.strptime(date_state, "%Y-%m-%d")
            except ValueError:
                try:
                    d = datetime.fromisoformat(date_state)
                except ValueError:
                    self.error(f"Cannot parse {HOLIDAY_DATE_ENTITY}={date_state}")
                    return
            if d.date() < datetime.now().date():
                self.error(f"Holiday return date {d.date()} is in the past; ignoring")
                return
            until_iso = d.strftime("%Y-%m-%dT00:00")
        else:
            until_iso = None

        # The /api/v2/remote/bsbHolidays endpoint always writes to a numbered
        # slot. The mobile app uses slot 0; we mirror that. Slots 1..7 stay
        # untouched.
        if want_active:
            today_iso = datetime.now().strftime("%Y-%m-%dT00:00")
            new_holiday = {"index": 0, "from": today_iso, "to": until_iso}
            reason = f"holiday set today → {until_iso[:10]}"
        else:
            # "Clear" = write the empty marker (zero-length on 1 Jan). Boiler
            # ignores any holiday whose range is fully in the past.
            new_holiday = {"index": 0, "from": "2026-01-01T00:00", "to": "2026-01-01T00:00"}
            reason = "holiday cleared"

        self._post_holiday_v2(new_holiday, reason=reason)

    def _post_holiday_v2(self, new_holiday, reason):
        """POST /api/v2/remote/bsbHolidays/<gw>/<zone> — the modern mobile-app
        endpoint discovered by intercepting Remocon NET Android v6.0.7773.40339.

        Body shape:
            { "currentZoneMode": <int>,
              "new":  {"index": 0..7, "from": "YYYY-MM-DDT00:00", "to": "YYYY-MM-DDT00:00"},
              "old":  {"index": 0..7, "from": ..., "to": ..., "osv": true} }

        The boiler has a fixed array of 8 holiday slots (0..7). Slot 0 is the
        one the mobile app uses; we mirror that. Slots not in use return
        from=to=2026-01-01T00:00 (zero-length range that the boiler ignores).
        ADD/MODIFY/DELETE are all the same operation — write the desired
        (from, to) to the slot. To "clear" a slot, write the empty marker.

        Auth: standard /R2/Account/Login session cookie works. No need for the
        ar.authToken header the mobile app uses — the cookie-based auth on
        legacy /R2 endpoints carries over to /api/v2.
        """
        gateway = self.args.get("gateway_id")
        if not gateway:
            self.error("Cannot write holiday: gateway_id not configured")
            return
        zone = self.args.get("zone") or 1

        url_base = self._base_url()
        list_url = urljoin(url_base, f"api/v2/remote/bsbHolidays/{gateway}/{zone}")
        post_url = list_url
        headers = {"User-Agent": "ktor-client", "Accept": "application/json",
                   "Content-Type": "application/json"}

        try:
            session = self._login()
            # Read the current slot state so we can include a faithful `old`
            # for the cloud's optimistic-concurrency check.
            r = session.get(list_url, headers=headers)
            if r.status_code != 200:
                self.error(f"GET bsbHolidays HTTP {r.status_code}: {r.text[:200]}")
                return
            slots = r.json()
            if not isinstance(slots, list) or new_holiday["index"] >= len(slots):
                self.error(f"Unexpected bsbHolidays response: {r.text[:200]}")
                return
            old = dict(slots[new_holiday["index"]])
            old.setdefault("osv", True)

            zone_mode = int(self.last_writable["zoneData"]["mode"]["value"]) \
                        if self.last_writable else 1
            body = {
                "currentZoneMode": zone_mode,
                "new": new_holiday,
                "old": old,
            }
            self.log(f"Holiday v2 ({reason}): {json.dumps(body)}")
            r = session.post(post_url, headers=headers, json=body)
            if r.status_code != 200:
                self.error(f"Holiday v2 HTTP {r.status_code}: {r.text[:300]}")
                return
            result = r.json()
            if not result.get("success"):
                self.error(f"Holiday v2 rejected: {r.text[:300]}")
                return
            self.log(f"Holiday v2 ok ({reason})")
            self.run_in(self.get_remocon_data, 5)
        except Exception as e:
            self.error(f"Holiday v2 exception: {e}")

    # ----- HOLIDAY-MODE-REDUCED FLAG -----

    def on_holiday_use_reduced_changed(self, entity, attribute, old, new, kwargs):
        """When the user toggles input_boolean.elco_holiday_use_reduced, push the
        bool to zoneData.useReducedOperationModeOnHoliday via SetData. False (the
        BSB default) shuts CH fully off during holiday; True drops it to the
        Reduced setpoint instead."""
        if new in ("unavailable", "unknown", None):
            return
        if not self.last_writable:
            self.error(f"{entity}: cannot write — no GetData snapshot cached yet")
            return
        want = (new == "on")
        current = bool(self.last_writable["zoneData"].get("useReducedOperationModeOnHoliday", False))
        if want == current:
            self.log(f"{entity}: no-op ({new} matches current)")
            return
        self.last_writable["zoneData"]["useReducedOperationModeOnHoliday"] = want
        self._post_setdata_with_holiday_flag(want, reason=f"{entity} → {new}")

    def _post_setdata_with_holiday_flag(self, use_reduced, reason):
        gateway = self.args.get("gateway_id")
        if not gateway:
            self.error("Cannot write: gateway_id not configured")
            return
        plant = self.last_writable["plantData"]
        zone = self.last_writable["zoneData"]
        payload = {
            "plantData": {
                "dhwComfortTemp": {"value": plant["dhwComfortTemp"]["value"]},
                "dhwReducedTemp": {"value": plant["dhwReducedTemp"]["value"]},
                "dhwMode":        {"value": plant["dhwMode"]["value"]},
            },
            "zoneData": {
                "zone": zone["zone"],
                "mode": {"value": zone["mode"]["value"]},
                "useReducedOperationModeOnHoliday": bool(use_reduced),
            },
            "viewModel": {"zoneNumber": zone["zone"]},
        }
        url = urljoin(self._base_url(), posixpath.join("R2/PlantHomeBsb/SetData", gateway))
        self.log(f"SetData (holiday-reduced: {reason}): payload={json.dumps(payload)}")
        try:
            session = self._login()
            response = session.post(url=url, json=payload)
            if response.status_code != 200:
                self.error(f"SetData HTTP {response.status_code}: {response.text[:300]}")
                return
            result = json.loads(response.text)
            if not result.get("ok"):
                self.error(f"SetData rejected: {result.get('message')} (debug: {result.get('debugMessage')})")
                return
            self.log(f"SetData ok ({reason})")
            self.run_in(self.get_remocon_data, 5)
        except Exception as e:
            self.error(f"SetData exception: {e}")

    # ----- SCHEDULE WRITE PATH -----

    def on_schedule_event(self, event_name, data, kwargs):
        """Pushes a new weekly plan to /R2/PlantTimeProgBsb/SetTimeProg/<gw>.

        Trigger: HA event `elco_set_weekly_plan` (POST /api/events/elco_set_weekly_plan
        or service call with event payload). Event data shape:
            {"plans": [{"days": [<0..6>...], "slices": [{"from": <min>, "temp": <0|1>}, ...]}, ...]}
        ext, maxSwitches, tick, baseTemp etc. are merged from the cached
        weeklyPlan to preserve boiler-required fields.

        Cloud payload (mirrors the dashboard JS `Re` constructor):
            {progId, weeklyPlan, prevWeeklyPlan}
        """
        if not isinstance(data, dict) or not isinstance(data.get("plans"), list):
            self.error(f"{event_name}: event data must be an object with a 'plans' list")
            return
        user_plan = data

        gateway = self.args.get("gateway_id")
        if not gateway:
            self.error("Cannot write schedule: gateway_id not configured")
            return
        zone = self.args.get("zone") or 1

        # Fold the user's plans into the previously-cached weeklyPlan so we
        # preserve ext/maxSwitches/tick/baseTemp etc. that the boiler expects.
        prev = getattr(self, "last_weekly_plan", None)

        merged = dict(prev) if isinstance(prev, dict) else {}
        merged["plans"] = user_plan["plans"]
        for k in ("ext", "maxSwitches", "tick", "baseTemp", "defaultTemp", "allowedTemp", "pilot"):
            if k in user_plan:
                merged[k] = user_plan[k]

        payload = {
            "progId": zone,
            "weeklyPlan": merged,
            "prevWeeklyPlan": prev,
        }
        url = urljoin(self._base_url(), posixpath.join("R2/PlantTimeProgBsb/SetTimeProg", gateway))
        self.log(f"SetTimeProg ({event_name}): plans={len(merged.get('plans') or [])}")
        try:
            session = self._login()
            response = session.post(url=url, json=payload)
            if response.status_code != 200:
                self.error(f"SetTimeProg HTTP {response.status_code}: {response.text[:300]}")
                return
            result = json.loads(response.text)
            if not result.get("ok"):
                self.error(f"SetTimeProg rejected: {result.get('message')} (debug: {result.get('debugMessage')})")
                return
            self.log("SetTimeProg ok; scheduling refresh")
            self.run_in(self.get_remocon_data, 5)
        except Exception as e:
            self.error(f"SetTimeProg exception: {e}")

    # ----- MONTHLY ENERGY HISTORY (recorder.statistics_during_period via WS) -----

    def refresh_monthly_stats(self, kwargs):
        """Query HA's recorder/statistics_during_period WebSocket command for
        the last ~14 months of sensor.elco_heating_energy_total, then publish
        the result to sensor.elco_heating_monthly_history (state = number of
        months returned, attribute history = the list).

        Why WS: recorder.statistics_during_period is exposed only as a WS
        command, not a service — trigger-template sensors couldn't use it.
        AppDaemon already has the HA bearer token in its config, so opening a
        secondary WS connection from here is straightforward.
        """
        try:
            import asyncio
            from datetime import timedelta as _td
            start = (datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                     - _td(days=400)).replace(day=1)
            result = asyncio.run(self._ws_statistics_during_period(
                statistic_id="sensor.elco_heating_energy_total",
                start_iso=start.isoformat(),
                period="month",
            ))
            if result is None:
                self.log("refresh_monthly_stats: WS query failed; skipping publish")
                return

            try:
                ha_url = self.args.get("ha_url") or self.config["plugins"]["HASS"]["ha_url"]
            except Exception:
                self.error("refresh_monthly_stats: no ha_url configured")
                return
            data = {
                "state": len(result),
                "attributes": {
                    "friendly_name": "Elco heating monthly history",
                    "icon": "mdi:calendar-month",
                    "history": result,
                },
            }
            url = f"{ha_url}/api/states/sensor.elco_heating_monthly_history"
            headers = {"Authorization": f"Bearer {self.args['bearer_token']}",
                       "Content-Type": "application/json"}
            requests.post(url, json=data, headers=headers, timeout=10)
            self.log(f"refresh_monthly_stats: published {len(result)} monthly buckets")
        except Exception as e:
            self.error(f"refresh_monthly_stats exception: {e}")

    async def _ws_statistics_during_period(self, statistic_id, start_iso, period):
        """Open a short-lived WS connection to HA, run statistics_during_period,
        return the result list or None on error."""
        import aiohttp
        try:
            ha_url = self.args.get("ha_url") or self.config["plugins"]["HASS"]["ha_url"]
        except Exception:
            return None
        # http -> ws, https -> wss
        ws_url = ha_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        token = self.args["bearer_token"]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, timeout=10) as ws:
                    # auth handshake
                    hello = await ws.receive_json()
                    if hello.get("type") != "auth_required":
                        self.error(f"WS expected auth_required, got {hello}")
                        return None
                    await ws.send_json({"type": "auth", "access_token": token})
                    ok = await ws.receive_json()
                    if ok.get("type") != "auth_ok":
                        self.error(f"WS auth failed: {ok}")
                        return None
                    # the actual query
                    await ws.send_json({
                        "id": 1,
                        "type": "recorder/statistics_during_period",
                        "start_time": start_iso,
                        "period": period,
                        "statistic_ids": [statistic_id],
                        "types": ["change"],
                    })
                    resp = await ws.receive_json()
                    if not resp.get("success"):
                        self.error(f"WS stats query rejected: {resp}")
                        return None
                    return (resp.get("result") or {}).get(statistic_id) or []
        except Exception as e:
            self.error(f"WS stats query exception: {e}")
            return None
