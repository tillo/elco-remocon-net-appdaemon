import hassapi as hass
from datetime import datetime
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


class Remocon(hass.Hass):
    def initialize(self):
        refresh_rate = self.args.get("refresh_rate")
        if refresh_rate is None:
            refresh_rate = 60
        self.log(f"Will fetch remocon.net data every {refresh_rate} min")
        self.run_every(self.get_remocon_data, datetime.now(), refresh_rate * 60)

        # cache of writable values from the latest GetData; used to build SetData payloads
        # since SetData requires both plantData and zoneData blocks.
        self.last_writable = None
        # full zoneData/plantData snapshots required by SetTemperature (CH temps endpoint).
        self.last_full_zoneData = None
        self.last_full_plantData = None

        # fetch data immediately on startup
        self.log("Fetching remocon.net data immediately on startup")
        self.get_remocon_data(None)

        # register write listeners if enabled
        if self.args.get("enable_writes", False):
            self.log("Write controls enabled — registering input_number/input_select listeners")
            for entity_id in WRITE_CONTROLS:
                self.listen_state(self.on_control_changed, entity_id)
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
            payload = {
                "useCache": False,
                "zone": zone,
                "filter": {"progIds": "null", "plant": True, "zone": True},
            }
            data_url = urljoin(base_url, posixpath.join("R2/PlantHomeBsb/GetData", gateway))
            self.log(f"Parameters gatewayId: {gateway} zone: {payload}")
            response = session.post(url=data_url, json=payload)
            if response.status_code == 200:
                result_json = json.loads(response.text)
                self.post_to_entities(result_json["data"])
                self.log("Done fetching remocon data.")
            else:
                self.error(f"Fetching error: {response.text}")
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
