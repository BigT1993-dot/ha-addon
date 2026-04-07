import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, request

import paho.mqtt.client as mqtt


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("evcc_auto_mode")
OPTIONS_PATH = "/data/options.json"
RUNTIME_CONFIG_PATH = "/data/runtime_config.json"
RUNTIME_STATE_PATH = "/data/runtime_state.json"
MAX_HISTORY_ENTRIES = 100
HOME_ASSISTANT_API_URL = "http://supervisor/core/api"
POWER_SENSOR_CACHE_TTL_SECONDS = 30
POWER_SENSOR_POLL_INTERVAL_SECONDS = 5


class ConfigError(RuntimeError):
    pass


@dataclass
class AddonConfig:
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_topic_prefix: str
    loadpoint_id: int
    homeassistant_power_sensor_entity_id: str
    homeassistant_battery_power_sensor_entity_id: str
    export_power_threshold_w: float
    import_power_threshold_w: float
    export_delay_seconds: int
    import_delay_seconds: int
    battery_discharge_power_threshold_w: float
    battery_discharge_delay_seconds: int
    evcc_active_current_threshold: float
    auto_reset_on_restart: bool

    @property
    def loadpoint_prefix(self) -> str:
        return f"{self.mqtt_topic_prefix}/loadpoints/{self.loadpoint_id}"

    @property
    def topics(self) -> dict[str, str]:
        return {
            "grid_power": f"{self.mqtt_topic_prefix}/site/grid/power",
            "battery_power": f"{self.mqtt_topic_prefix}/site/batteryPower",
            "buffer_soc": f"{self.mqtt_topic_prefix}/site/bufferSoc",
            "battery_soc": f"{self.mqtt_topic_prefix}/site/batterySoc",
            "connected": f"{self.loadpoint_prefix}/connected",
            "mode": f"{self.loadpoint_prefix}/mode",
            "mode_set": f"{self.loadpoint_prefix}/mode/set",
            "offered_current": f"{self.loadpoint_prefix}/offeredCurrent",
            "plan_active": f"{self.loadpoint_prefix}/planActive",
        }

    @property
    def ha_discovery_prefix(self) -> str:
        return "homeassistant"

    @property
    def ha_sensor_object_id(self) -> str:
        return f"evcc_auto_mode_loadpoint_{self.loadpoint_id}_last_action"

    @property
    def ha_state_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/addon/evcc-auto-mode/loadpoints/{self.loadpoint_id}/last_action/state"

    @property
    def ha_attributes_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/addon/evcc-auto-mode/loadpoints/{self.loadpoint_id}/last_action/attributes"

    @property
    def ha_status_sensor_object_id(self) -> str:
        return f"evcc_auto_mode_loadpoint_{self.loadpoint_id}_automation_status"

    @property
    def ha_status_state_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/addon/evcc-auto-mode/loadpoints/{self.loadpoint_id}/automation_status/state"

    @property
    def ha_status_attributes_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/addon/evcc-auto-mode/loadpoints/{self.loadpoint_id}/automation_status/attributes"

    @property
    def ha_availability_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/addon/evcc-auto-mode/status"

    @property
    def ha_discovery_topic(self) -> str:
        return f"{self.ha_discovery_prefix}/sensor/{self.ha_sensor_object_id}/config"


def read_config() -> AddonConfig:
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as err:
        raise ConfigError(f"Missing add-on options at {OPTIONS_PATH}") from err

    runtime = read_runtime_config()
    raw.update(runtime)

    return AddonConfig(
        mqtt_host=raw["mqtt_host"],
        mqtt_port=int(raw["mqtt_port"]),
        mqtt_username=raw.get("mqtt_username", "") or "",
        mqtt_password=raw.get("mqtt_password", "") or "",
        mqtt_topic_prefix=(raw.get("mqtt_topic_prefix") or "evcc").rstrip("/"),
        loadpoint_id=int(raw.get("loadpoint_id", 1)),
        homeassistant_power_sensor_entity_id=str(raw.get("homeassistant_power_sensor_entity_id", "") or "").strip(),
        homeassistant_battery_power_sensor_entity_id=str(
            raw.get("homeassistant_battery_power_sensor_entity_id", "") or ""
        ).strip(),
        export_power_threshold_w=float(raw.get("export_power_threshold_w", -100.0)),
        import_power_threshold_w=float(raw.get("import_power_threshold_w", 100.0)),
        export_delay_seconds=int(raw.get("export_delay_seconds", 60)),
        import_delay_seconds=int(raw.get("import_delay_seconds", 30)),
        battery_discharge_power_threshold_w=float(raw.get("battery_discharge_power_threshold_w", 200.0)),
        battery_discharge_delay_seconds=int(raw.get("battery_discharge_delay_seconds", 60)),
        evcc_active_current_threshold=float(raw.get("evcc_active_current_threshold", 6.0)),
        auto_reset_on_restart=parse_config_bool(raw.get("auto_reset_on_restart", True)),
    )


def read_runtime_config() -> dict[str, Any]:
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def write_runtime_config(config: AddonConfig) -> None:
    with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config_to_dict(config), handle, indent=2)
        handle.write("\n")


def read_runtime_state() -> dict[str, Any]:
    try:
        with open(RUNTIME_STATE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def write_runtime_state(state: dict[str, Any]) -> None:
    with open(RUNTIME_STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")


def config_to_dict(config: AddonConfig) -> dict[str, Any]:
    return {
        "mqtt_host": config.mqtt_host,
        "mqtt_port": config.mqtt_port,
        "mqtt_username": config.mqtt_username,
        "mqtt_password": config.mqtt_password,
        "mqtt_topic_prefix": config.mqtt_topic_prefix,
        "loadpoint_id": config.loadpoint_id,
        "homeassistant_power_sensor_entity_id": config.homeassistant_power_sensor_entity_id,
        "homeassistant_battery_power_sensor_entity_id": config.homeassistant_battery_power_sensor_entity_id,
        "export_power_threshold_w": config.export_power_threshold_w,
        "import_power_threshold_w": config.import_power_threshold_w,
        "export_delay_seconds": config.export_delay_seconds,
        "import_delay_seconds": config.import_delay_seconds,
        "battery_discharge_power_threshold_w": config.battery_discharge_power_threshold_w,
        "battery_discharge_delay_seconds": config.battery_discharge_delay_seconds,
        "evcc_active_current_threshold": config.evcc_active_current_threshold,
        "auto_reset_on_restart": config.auto_reset_on_restart,
    }


def collect_runtime_env_flags() -> dict[str, bool]:
    relevant_keys = [
        "SUPERVISOR_TOKEN",
        "HASSIO_TOKEN",
        "HOMEASSISTANT_TOKEN",
        "HASSIO",
        "HASSIO_WS",
        "SUPERVISOR",
    ]
    return {key: bool(os.getenv(key)) for key in relevant_keys}


def config_from_payload(payload: dict[str, Any]) -> AddonConfig:
    return AddonConfig(
        mqtt_host=str(payload["mqtt_host"]).strip(),
        mqtt_port=int(payload["mqtt_port"]),
        mqtt_username=str(payload.get("mqtt_username", "") or ""),
        mqtt_password=str(payload.get("mqtt_password", "") or ""),
        mqtt_topic_prefix=str(payload.get("mqtt_topic_prefix", "evcc") or "evcc").rstrip("/"),
        loadpoint_id=int(payload.get("loadpoint_id", 1)),
        homeassistant_power_sensor_entity_id=str(payload.get("homeassistant_power_sensor_entity_id", "") or "").strip(),
        homeassistant_battery_power_sensor_entity_id=str(
            payload.get("homeassistant_battery_power_sensor_entity_id", "") or ""
        ).strip(),
        export_power_threshold_w=float(payload.get("export_power_threshold_w", -100.0)),
        import_power_threshold_w=float(payload.get("import_power_threshold_w", 100.0)),
        export_delay_seconds=int(payload.get("export_delay_seconds", 60)),
        import_delay_seconds=int(payload.get("import_delay_seconds", 30)),
        battery_discharge_power_threshold_w=float(payload.get("battery_discharge_power_threshold_w", 200.0)),
        battery_discharge_delay_seconds=int(payload.get("battery_discharge_delay_seconds", 60)),
        evcc_active_current_threshold=float(payload.get("evcc_active_current_threshold", 6.0)),
        auto_reset_on_restart=parse_config_bool(payload.get("auto_reset_on_restart", True)),
    )


class EvccAutoMode:
    def __init__(self, config: AddonConfig) -> None:
        self.config = config
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if config.mqtt_username:
            self.client.username_pw_set(config.mqtt_username, config.mqtt_password)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

        self.state_lock = threading.RLock()
        self.shutdown_event = threading.Event()

        self.connected = False
        self.plan_active = False
        self.current_mode = ""
        self.offered_current = 0.0
        self.grid_power = 0.0
        self.grid_power_source = "mqtt"
        self.grid_power_updated_at: float | None = None
        self.battery_power: float | None = None
        self.battery_power_source = "mqtt"
        self.battery_power_updated_at: float | None = None
        self.mqtt_battery_power: float | None = None
        self.mqtt_battery_power_updated_at: float | None = None
        self.buffer_soc: float | None = None
        self.battery_soc: float | None = None
        self.auto_mode_active = False
        self.automation_enabled = True
        self.export_timer_started_at: float | None = None
        self.import_timer_started_at: float | None = None
        self.battery_discharge_timer_started_at: float | None = None
        self.last_mqtt_message_at: float | None = None
        self.last_mode_command: str | None = None
        self.last_mode_command_at: float | None = None
        self.last_decision_reason = "waiting for MQTT data"
        self.last_restore_reason = "waiting for MQTT data"
        self.topic_values: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.mqtt_connected = False
        self.supervisor_token = os.getenv("SUPERVISOR_TOKEN", "")
        self.homeassistant_power_sensor_cache: list[dict[str, str]] = []
        self.homeassistant_power_sensor_cache_at: float | None = None
        self.homeassistant_power_sensor_error: str | None = None
        self.homeassistant_battery_power_sensor_error: str | None = None
        self.last_power_sensor_poll_at: float | None = None
        self.last_battery_power_sensor_poll_at: float | None = None
        self.simulation_enabled = False
        self.last_mode_command_simulated = False

        LOGGER.info(
            "Runtime environment flags: %s",
            json.dumps(collect_runtime_env_flags(), ensure_ascii=True, sort_keys=True),
        )
        self._restore_runtime_state()

    def run(self) -> None:
        server = DebugServer(self)
        server.start()
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()
        LOGGER.info("Started evcc auto mode worker")

        try:
            while not self.shutdown_event.wait(1):
                self.refresh_grid_power_source()
                self.refresh_battery_power_source()
                self.evaluate()
        finally:
            server.stop()
            self.client.loop_stop()
            self.client.disconnect()

    def stop(self, *_args: Any) -> None:
        LOGGER.info("Stopping worker")
        self.shutdown_event.set()

    def on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connect failed with code %s", reason_code)
            return

        with self.state_lock:
            self.mqtt_connected = True
        topics = self.config.topics
        for topic in topics.values():
            client.subscribe(topic)
        self.cleanup_home_assistant_discovery()
        LOGGER.info("Subscribed to %s topics", len(topics))

    def on_disconnect(self, _client: mqtt.Client, _userdata: Any, _disconnect_flags: Any, reason_code: Any, _properties: Any) -> None:
        with self.state_lock:
            self.mqtt_connected = False
        LOGGER.warning("Disconnected from MQTT with code %s", reason_code)

    def on_message(self, _client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        topics = self.config.topics

        with self.state_lock:
            self.last_mqtt_message_at = time.monotonic()
            self.topic_values[msg.topic] = {
                "payload": payload,
                "received_at": iso_utc_now(),
            }
            try:
                if msg.topic == topics["connected"]:
                    self.connected = parse_bool(payload)
                elif msg.topic == topics["plan_active"]:
                    self.plan_active = parse_bool(payload)
                elif msg.topic == topics["mode"]:
                    if not self.simulation_enabled:
                        previous_mode = self.current_mode
                        self.current_mode = payload
                        if payload != previous_mode:
                            self.record_event(
                                "mode_observed",
                                f"Observed evcc mode {payload}",
                                reason="mode topic updated",
                                details={"previous_mode": previous_mode or None, "current_mode": payload},
                            )
                        if payload != "minpv" and self.auto_mode_active:
                            LOGGER.info("Mode changed externally to %s; clearing auto flag", payload)
                            self.set_auto_mode_active(False, reason=f"evcc mode changed externally to {payload}", source="mqtt")
                elif msg.topic == topics["offered_current"]:
                    self.offered_current = parse_float(payload)
                elif msg.topic == topics["grid_power"]:
                    if not self.config.homeassistant_power_sensor_entity_id:
                        self.update_grid_power(parse_float(payload), source="mqtt")
                elif msg.topic == topics["battery_power"]:
                    value = parse_float(payload)
                    self.mqtt_battery_power = value
                    self.mqtt_battery_power_updated_at = time.monotonic()
                    if not self.config.homeassistant_battery_power_sensor_entity_id:
                        self.update_battery_power(value, source="mqtt")
                elif msg.topic == topics["buffer_soc"]:
                    self.buffer_soc = parse_optional_float(payload)
                elif msg.topic == topics["battery_soc"]:
                    self.battery_soc = parse_optional_float(payload)
            except ValueError:
                LOGGER.warning("Ignored invalid payload for %s: %r", msg.topic, payload)
                return

        self.evaluate()

    def evaluate(self) -> None:
        with self.state_lock:
            now = time.monotonic()

            if self.is_export_above_threshold():
                self.export_timer_started_at = self.export_timer_started_at or now
            else:
                self.export_timer_started_at = None

            if self.is_import_above_threshold():
                self.import_timer_started_at = self.import_timer_started_at or now
            else:
                self.import_timer_started_at = None

            if self.is_battery_discharge_above_threshold():
                self.battery_discharge_timer_started_at = self.battery_discharge_timer_started_at or now
            else:
                self.battery_discharge_timer_started_at = None

            if not self.connected and self.auto_mode_active:
                LOGGER.info("Vehicle disconnected; clearing auto mode state")
                self.set_auto_mode_active(False, reason="vehicle disconnected", source="automation")

            if not self.automation_enabled:
                self.last_decision_reason = "automation stopped by user"
                self.last_restore_reason = "automation stopped by user"
                return

            should_set_minpv, set_reason = self.should_set_minpv(now)
            self.last_decision_reason = set_reason
            if should_set_minpv:
                self.publish_mode("minpv", reason=set_reason)
                self.set_auto_mode_active(True, reason=f"set minpv: {set_reason}", source="automation")

            should_restore_pv, restore_reason = self.should_restore_pv(now)
            self.last_restore_reason = restore_reason
            if should_restore_pv:
                self.publish_mode("pv", reason=restore_reason)
                self.set_auto_mode_active(False, reason=f"restored pv: {restore_reason}", source="automation")

    def should_set_minpv(self, now: float) -> tuple[bool, str]:
        blockers: list[str] = []

        if not self.automation_enabled:
            blockers.append("automation stopped by user")
        if not self.connected:
            blockers.append("vehicle not connected")
        if self.grid_power_updated_at is None:
            blockers.append("grid power source has not delivered data yet")
        elif now - self.grid_power_updated_at > POWER_SENSOR_POLL_INTERVAL_SECONDS * 3:
            blockers.append("grid power data is stale")
        if self.plan_active:
            blockers.append("charging plan is active")
        if self.auto_mode_active:
            blockers.append("auto mode already active")
        if self.current_mode == "minpv":
            blockers.append("evcc already in minpv")
        if self.offered_current > self.config.evcc_active_current_threshold:
            blockers.append("evcc is actively regulating current")
        if self.export_timer_started_at is None:
            threshold = self.config.export_power_threshold_w
            direction = "at or above" if threshold >= 0 else "at or below"
            blockers.append(
                f"no sustained export detected {direction} {format_threshold(threshold)} W"
            )
        elif now - self.export_timer_started_at < self.config.export_delay_seconds:
            blockers.append("export delay not reached yet")
        if self.battery_soc is None or self.buffer_soc is None:
            blockers.append("battery or buffer SoC missing")
        elif self.battery_soc >= self.buffer_soc:
            blockers.append("battery SoC is not below buffer SoC")
        if blockers:
            return False, "; ".join(blockers)
        return True, "all activation conditions met"

    def should_restore_pv(self, now: float) -> tuple[bool, str]:
        if not self.auto_mode_active:
            return False, "auto mode not active"
        if self.current_mode == "pv":
            return False, "evcc already in pv"

        import_ready = self.is_grid_power_fresh(now) and self.import_timer_started_at is not None
        if import_ready and now - self.import_timer_started_at >= self.config.import_delay_seconds:
            return True, "sustained grid import threshold reached"

        battery_ready = self.is_battery_power_fresh(now) and self.battery_discharge_timer_started_at is not None
        if battery_ready and now - self.battery_discharge_timer_started_at >= self.config.battery_discharge_delay_seconds:
            return True, "sustained battery discharge threshold reached"

        blockers: list[str] = []
        if not self.is_grid_power_fresh(now):
            if self.grid_power_updated_at is None:
                blockers.append("grid power source has not delivered data yet")
            else:
                blockers.append("grid power data is stale")
        elif self.import_timer_started_at is None:
            threshold = self.config.import_power_threshold_w
            direction = "at or above" if threshold >= 0 else "at or below"
            blockers.append(
                f"no sustained grid import detected {direction} {format_threshold(threshold)} W"
            )
        else:
            blockers.append("import delay not reached yet")

        if not self.is_battery_power_fresh(now):
            if self.battery_power_updated_at is None:
                blockers.append("battery power source has not delivered data yet")
            else:
                blockers.append("battery power data is stale")
        elif self.battery_discharge_timer_started_at is None:
            threshold = self.config.battery_discharge_power_threshold_w
            direction = "at or above" if threshold >= 0 else "at or below"
            blockers.append(
                f"no sustained battery discharge detected {direction} {format_threshold(threshold)} W"
            )
        else:
            blockers.append("battery discharge delay not reached yet")

        return False, "; ".join(blockers)

    def update_grid_power(self, value: float, source: str) -> None:
        self.grid_power = value
        self.grid_power_source = source
        self.grid_power_updated_at = time.monotonic()

    def update_battery_power(self, value: float, source: str, observed_at: float | None = None) -> None:
        self.battery_power = value
        self.battery_power_source = source
        self.battery_power_updated_at = observed_at if observed_at is not None else time.monotonic()

    def is_grid_power_fresh(self, now: float) -> bool:
        return self.grid_power_updated_at is not None and now - self.grid_power_updated_at <= POWER_SENSOR_POLL_INTERVAL_SECONDS * 3

    def is_battery_power_fresh(self, now: float) -> bool:
        return (
            self.battery_power_updated_at is not None
            and now - self.battery_power_updated_at <= POWER_SENSOR_POLL_INTERVAL_SECONDS * 3
        )

    def is_battery_discharge_above_threshold(self) -> bool:
        if self.battery_power is None:
            return False
        threshold = self.config.battery_discharge_power_threshold_w
        if threshold >= 0:
            return self.battery_power >= threshold
        return self.battery_power <= threshold

    def is_export_above_threshold(self) -> bool:
        threshold = self.config.export_power_threshold_w
        if threshold >= 0:
            return self.grid_power >= threshold
        return self.grid_power <= threshold

    def is_import_above_threshold(self) -> bool:
        threshold = self.config.import_power_threshold_w
        if threshold >= 0:
            return self.grid_power >= threshold
        return self.grid_power <= threshold

    def refresh_grid_power_source(self) -> None:
        entity_id = self.config.homeassistant_power_sensor_entity_id
        if not entity_id:
            return

        now = time.monotonic()
        if self.last_power_sensor_poll_at is not None and now - self.last_power_sensor_poll_at < POWER_SENSOR_POLL_INTERVAL_SECONDS:
            return
        self.last_power_sensor_poll_at = now

        try:
            state = self.fetch_homeassistant_state(entity_id)
            value = parse_float(str(state["state"]))
        except Exception:
            LOGGER.exception("Failed to refresh Home Assistant power sensor %s", entity_id)
            return

        with self.state_lock:
            self.update_grid_power(value, source=f"homeassistant:{entity_id}")

    def refresh_battery_power_source(self) -> None:
        entity_id = self.config.homeassistant_battery_power_sensor_entity_id
        if not entity_id:
            return

        now = time.monotonic()
        if (
            self.last_battery_power_sensor_poll_at is not None
            and now - self.last_battery_power_sensor_poll_at < POWER_SENSOR_POLL_INTERVAL_SECONDS
        ):
            return
        self.last_battery_power_sensor_poll_at = now

        try:
            state = self.fetch_homeassistant_state(entity_id)
            value = parse_float(str(state["state"]))
        except Exception as err:
            self.homeassistant_battery_power_sensor_error = str(err)
            LOGGER.exception("Failed to refresh Home Assistant battery power sensor %s", entity_id)
            with self.state_lock:
                if self.mqtt_battery_power is not None and self.mqtt_battery_power_updated_at is not None:
                    self.update_battery_power(
                        self.mqtt_battery_power,
                        source="mqtt-fallback",
                        observed_at=self.mqtt_battery_power_updated_at,
                    )
            return

        with self.state_lock:
            self.homeassistant_battery_power_sensor_error = None
            self.update_battery_power(value, source=f"homeassistant:{entity_id}")

    def fetch_homeassistant_state(self, entity_id: str) -> dict[str, Any]:
        return self.homeassistant_api_get(f"/states/{entity_id}")

    def list_homeassistant_power_sensors(self) -> list[dict[str, str]]:
        now = time.monotonic()
        if self.homeassistant_power_sensor_cache_at is not None and now - self.homeassistant_power_sensor_cache_at < POWER_SENSOR_CACHE_TTL_SECONDS:
            return self.homeassistant_power_sensor_cache

        try:
            states = self.homeassistant_api_get("/states")
        except Exception as err:
            LOGGER.exception("Failed to list Home Assistant power sensors")
            self.homeassistant_power_sensor_error = str(err)
            return self.homeassistant_power_sensor_cache

        sensors: list[dict[str, str]] = []
        for state in states:
            entity_id = str(state.get("entity_id", ""))
            if not entity_id.startswith("sensor."):
                continue
            attributes = state.get("attributes", {})
            if str(attributes.get("unit_of_measurement", "")).strip() != "W":
                continue
            sensors.append(
                {
                    "entity_id": entity_id,
                    "name": str(attributes.get("friendly_name") or entity_id),
                }
            )
        sensors.sort(key=lambda item: item["name"].lower())
        self.homeassistant_power_sensor_cache = sensors
        self.homeassistant_power_sensor_cache_at = now
        self.homeassistant_power_sensor_error = None
        return sensors

    def homeassistant_api_get(self, path: str) -> Any:
        if not self.supervisor_token:
            env_flags = collect_runtime_env_flags()
            raise RuntimeError(
                "SUPERVISOR_TOKEN is not available; "
                f"env_flags={json.dumps(env_flags, ensure_ascii=True, sort_keys=True)}; "
                f"api_url={HOME_ASSISTANT_API_URL}"
            )

        req = request.Request(
            f"{HOME_ASSISTANT_API_URL}{path}",
            headers={
                "Authorization": f"Bearer {self.supervisor_token}",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as err:
            body = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Home Assistant API error {err.code}: {body}") from err

    def publish_mode(self, mode: str, reason: str, source: str = "automation") -> None:
        topic = self.config.topics["mode_set"]
        LOGGER.info("Publishing %s to %s", mode, topic)
        self.last_mode_command = mode
        self.last_mode_command_at = time.monotonic()
        self.last_mode_command_simulated = self.simulation_enabled
        if self.simulation_enabled:
            self.current_mode = mode
            self.record_event(
                "mode_command_simulated",
                f"Would publish mode {mode}",
                reason=reason,
                details={"topic": topic, "mode": mode, "source": source, "simulated": True},
            )
            self.persist_runtime_state()
            return
        result = self.client.publish(topic, payload=mode, qos=1, retain=False)
        event_details = {
            "topic": topic,
            "mode": mode,
            "source": source,
            "result_code": result.rc,
        }
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Failed publishing mode %s: rc=%s", mode, result.rc)
            self.record_event(
                "mode_command_failed",
                f"Failed to publish mode {mode}",
                reason=reason,
                details=event_details,
            )
            return

        self.record_event(
            "mode_command",
            f"Published mode {mode}",
            reason=reason,
            details=event_details,
        )

    def cleanup_home_assistant_discovery(self) -> None:
        topics = [
            self.config.ha_discovery_topic,
            f"{self.config.ha_discovery_prefix}/sensor/{self.config.ha_status_sensor_object_id}/config",
            self.config.ha_state_topic,
            self.config.ha_attributes_topic,
            self.config.ha_status_state_topic,
            self.config.ha_status_attributes_topic,
            self.config.ha_availability_topic,
        ]
        for topic in topics:
            self.client.publish(topic, payload="", qos=1, retain=True)

    def _restore_runtime_state(self) -> None:
        state = read_runtime_state()
        self.history = list(state.get("history", []))
        self.automation_enabled = parse_config_bool(state.get("automation_enabled", True))
        self.simulation_enabled = parse_config_bool(state.get("simulation_enabled", False))
        if self.config.auto_reset_on_restart:
            self.auto_mode_active = False
            self.persist_runtime_state()
            return

        self.auto_mode_active = parse_config_bool(state.get("auto_mode_active", False))

    def persist_runtime_state(self) -> None:
        write_runtime_state(
            {
                "auto_mode_active": self.auto_mode_active,
                "automation_enabled": self.automation_enabled,
                "simulation_enabled": self.simulation_enabled,
                "history": self.history,
                "updated_at": iso_utc_now(),
            }
        )

    def set_auto_mode_active(self, active: bool, reason: str, source: str) -> None:
        if self.auto_mode_active == active:
            return

        self.auto_mode_active = active
        self.record_event(
            "auto_mode_state",
            f"auto_mode_active set to {format_value(active)}",
            reason=reason,
            details={"source": source, "auto_mode_active": active},
        )
        self.persist_runtime_state()

    def set_automation_enabled(self, enabled: bool, reason: str, source: str) -> None:
        with self.state_lock:
            if self.automation_enabled == enabled:
                return

            self.automation_enabled = enabled
            if not enabled:
                self.auto_mode_active = False
                self.last_decision_reason = "automation stopped by user"
                self.last_restore_reason = "automation stopped by user"
            else:
                self.last_decision_reason = "automation re-enabled"
                self.last_restore_reason = "automation re-enabled"

            self.record_event(
                "automation_toggle",
                f"automation_enabled set to {format_value(enabled)}",
                reason=reason,
                details={"source": source, "automation_enabled": enabled},
            )
            self.persist_runtime_state()

    def record_event(
        self,
        event_type: str,
        message: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "timestamp": iso_utc_now(),
            "type": event_type,
            "message": message,
            "reason": reason,
            "details": details or {},
        }
        self.history = ([event] + self.history)[:MAX_HISTORY_ENTRIES]
        self.persist_runtime_state()

    def get_debug_snapshot(self) -> dict[str, Any]:
        with self.state_lock:
            now = time.monotonic()
            return {
                "generated_at": iso_utc_now(),
                "config": {
                    **config_to_dict(self.config),
                    "mqtt_password": mask_secret(self.config.mqtt_password),
                    "power_sensor_options": self.list_homeassistant_power_sensors(),
                    "power_sensor_error": self.homeassistant_power_sensor_error,
                    "battery_power_sensor_error": self.homeassistant_battery_power_sensor_error,
                },
                "topics": self.config.topics,
                "state": {
                    "mqtt_connected": self.mqtt_connected,
                    "simulation_enabled": self.simulation_enabled,
                    "connected": self.connected,
                    "plan_active": self.plan_active,
                    "current_mode": self.current_mode,
                    "offered_current": self.offered_current,
                    "grid_power": self.grid_power,
                    "grid_power_source": self.grid_power_source,
                    "battery_power": self.battery_power,
                    "battery_power_source": self.battery_power_source,
                    "buffer_soc": self.buffer_soc,
                    "battery_soc": self.battery_soc,
                    "automation_enabled": self.automation_enabled,
                    "auto_mode_active": self.auto_mode_active,
                    "last_decision_reason": self.last_decision_reason,
                    "last_restore_reason": self.last_restore_reason,
                    "last_mode_command": self.last_mode_command,
                    "last_mode_command_simulated": self.last_mode_command_simulated,
                    "last_mqtt_message_age_seconds": elapsed_seconds(self.last_mqtt_message_at, now),
                    "grid_power_age_seconds": elapsed_seconds(self.grid_power_updated_at, now),
                    "battery_power_age_seconds": elapsed_seconds(self.battery_power_updated_at, now),
                    "last_mode_command_age_seconds": elapsed_seconds(self.last_mode_command_at, now),
                    "export_timer_age_seconds": elapsed_seconds(self.export_timer_started_at, now),
                    "import_timer_age_seconds": elapsed_seconds(self.import_timer_started_at, now),
                    "battery_discharge_timer_age_seconds": elapsed_seconds(self.battery_discharge_timer_started_at, now),
                },
                "topic_values": self.topic_values,
                "history": self.history,
                "power_sensor_options": self.list_homeassistant_power_sensors(),
            }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("mqtt_password"):
            payload["mqtt_password"] = self.config.mqtt_password
        next_config = config_from_payload(payload)
        validate_config(next_config)
        config_changes = describe_config_changes(self.config, next_config)

        reconnect_required = (
            next_config.mqtt_host != self.config.mqtt_host
            or next_config.mqtt_port != self.config.mqtt_port
            or next_config.mqtt_username != self.config.mqtt_username
            or next_config.mqtt_password != self.config.mqtt_password
            or next_config.mqtt_topic_prefix != self.config.mqtt_topic_prefix
            or next_config.loadpoint_id != self.config.loadpoint_id
        )

        with self.state_lock:
            self.config = next_config
            self.topic_values = {}
            self.last_decision_reason = "configuration updated"
            self.last_restore_reason = "configuration updated"
            self.last_mqtt_message_at = None
            self.export_timer_started_at = None
            self.import_timer_started_at = None
            self.battery_discharge_timer_started_at = None
            self.current_mode = ""
            self.grid_power = 0.0
            self.grid_power_source = "mqtt"
            self.grid_power_updated_at = None
            self.battery_power = None
            self.battery_power_source = "mqtt"
            self.battery_power_updated_at = None
            self.mqtt_battery_power = None
            self.mqtt_battery_power_updated_at = None
            self.offered_current = 0.0
            self.connected = False
            self.plan_active = False
            self.buffer_soc = None
            self.battery_soc = None
            self.auto_mode_active = False
            self.homeassistant_power_sensor_cache_at = None
            self.homeassistant_power_sensor_error = None
            self.homeassistant_battery_power_sensor_error = None
            self.last_power_sensor_poll_at = None
            self.last_battery_power_sensor_poll_at = None

        write_runtime_config(next_config)
        self.record_event(
            "config_update",
            "Configuration updated via ingress UI",
            reason="user saved configuration",
            details={"changes": config_changes},
        )
        self.persist_runtime_state()
        if reconnect_required:
            self.reconnect_mqtt()

        LOGGER.info("Configuration updated via ingress UI")
        return self.get_debug_snapshot()

    def update_automation(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = parse_config_bool(payload["enabled"])
        reason = str(payload.get("reason") or "user pressed automation control").strip()
        self.set_automation_enabled(enabled, reason=reason, source="ui")
        return self.get_debug_snapshot()

    def update_simulation(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = parse_config_bool(payload["enabled"])
        reason = str(payload.get("reason") or "user pressed simulation control").strip()
        with self.state_lock:
            if self.simulation_enabled == enabled:
                return self.get_debug_snapshot()

            self.simulation_enabled = enabled
            self.last_mode_command_simulated = False
            self.auto_mode_active = False
            self.last_decision_reason = "simulation enabled" if enabled else "simulation disabled"
            self.last_restore_reason = "simulation enabled" if enabled else "simulation disabled"
            self.record_event(
                "simulation_toggle",
                f"simulation_enabled set to {format_value(enabled)}",
                reason=reason,
                details={"source": "ui", "simulation_enabled": enabled},
            )
            self.persist_runtime_state()

        self.evaluate()
        return self.get_debug_snapshot()

    def reconnect_mqtt(self) -> None:
        LOGGER.info("Reconnecting MQTT client to apply updated configuration")
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            LOGGER.exception("Failed to disconnect MQTT client cleanly")

        if self.config.mqtt_username:
            self.client.username_pw_set(self.config.mqtt_username, self.config.mqtt_password)
        else:
            self.client.username_pw_set(None, None)
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()


class DebugServer:
    def __init__(self, worker: EvccAutoMode, host: str = "", port: int = 8099) -> None:
        self.worker = worker
        self.server = ThreadingHTTPServer((host, port), self._build_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        worker = self.worker

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/api/state":
                    payload = json.dumps(worker.get_debug_snapshot(), indent=2).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if self.path in {"/", "/index.html"}:
                    html = render_debug_html(worker.get_debug_snapshot()).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return

                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

            def do_POST(self) -> None:
                if self.path not in {"/api/config", "/api/automation", "/api/simulation"}:
                    self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                    return

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(content_length)
                    payload = json.loads(raw.decode("utf-8"))
                    if self.path == "/api/config":
                        snapshot = worker.update_config(payload)
                    elif self.path == "/api/simulation":
                        snapshot = worker.update_simulation(payload)
                    else:
                        snapshot = worker.update_automation(payload)
                except (json.JSONDecodeError, ValueError, KeyError) as err:
                    response = json.dumps({"error": str(err)}).encode("utf-8")
                    self.send_response(HTTPStatus.BAD_REQUEST)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return
                except Exception as err:
                    LOGGER.exception("Failed to update config")
                    response = json.dumps({"error": str(err)}).encode("utf-8")
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return

                response = json.dumps(snapshot, indent=2).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return Handler

    def start(self) -> None:
        LOGGER.info("Starting debug web server on port 8099")
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "on", "yes"}:
        return True
    if normalized in {"false", "0", "off", "no"}:
        return False
    raise ValueError(f"Invalid boolean: {value}")


def parse_config_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return parse_bool(value)
    if isinstance(value, (int, float)):
        if value in {0, 1}:
            return bool(value)
    raise ValueError(f"Invalid configuration boolean: {value}")


def parse_float(value: str) -> float:
    return float(value)


def parse_optional_float(value: str) -> float | None:
    if value == "":
        return None
    return float(value)


def elapsed_seconds(started_at: float | None, now: float) -> float | None:
    if started_at is None:
        return None
    return round(now - started_at, 1)


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_history_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d/%m %H:%M:%S")


def describe_action_state(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    details = event.get("details", {})
    mode = str(details.get("mode") or "")

    if event_type == "mode_command":
        if mode == "minpv":
            return "switched_to_minpv"
        if mode == "pv":
            return "switched_to_pv"
        return "mode_command"

    if event_type == "mode_command_failed":
        if mode == "minpv":
            return "failed_to_switch_minpv"
        if mode == "pv":
            return "failed_to_switch_pv"
        return "mode_command_failed"

    return event_type or "unknown"


def render_debug_html(snapshot: dict[str, Any]) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>evcc Auto Mode</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f1ea;
      --panel: #fffdf8;
      --ink: #1f2a2e;
      --muted: #5b6a70;
      --line: #d8d1c3;
      --accent: #176b5a;
      --danger: #b53131;
      --warn: #9a5b00;
    }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: radial-gradient(circle at top, #fff7df 0, var(--bg) 45%);
      color: var(--ink);
    }}
    main {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    .grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 10px 30px rgba(31, 42, 46, 0.06);
    }}
    .label {{
      color: var(--muted);
      font-size: 0.88rem;
      margin-bottom: 4px;
    }}
    .value {{
      font-size: 1.3rem;
      font-weight: 600;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    code, pre {{
      font-family: "IBM Plex Mono", monospace;
      font-size: 0.92rem;
    }}
    pre {{
      white-space: pre-wrap;
      background: #f7f3ea;
      border-radius: 12px;
      padding: 14px;
      overflow-x: auto;
    }}
    .muted {{ color: var(--muted); }}
    .warn {{ color: var(--warn); }}
    .actions {{
      display: flex;
      gap: 12px;
      align-items: center;
      margin-top: 16px;
      flex-wrap: wrap;
    }}
    form {{
      display: grid;
      gap: 12px;
    }}
    .form-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }}
    label {{
      display: grid;
      gap: 6px;
      font-size: 0.9rem;
      color: var(--muted);
    }}
    input {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
      color: var(--ink);
      background: white;
    }}
    select {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
      color: var(--ink);
      background: white;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      background: var(--accent);
      color: white;
      font: inherit;
      padding: 12px 18px;
      cursor: pointer;
    }}
    .status {{
      font-size: 0.9rem;
      color: var(--muted);
    }}
    .button-danger {{
      background: var(--danger);
    }}
    .button-secondary {{
      background: #395761;
    }}
  </style>
</head>
<body>
  <main>
    <div class="card">
      <h1>evcc Auto Mode Debug</h1>
      <div class="muted">Generated at {snapshot["generated_at"]}</div>
      <div class="muted">JSON endpoint: <code>/api/state</code></div>
      <div class="actions">
        <button type="button" class="button-secondary" id="page-refresh">Refresh Now</button>
        <div class="status" id="refresh-status">Auto-refresh every 5s. Pauses while editing form fields.</div>
      </div>
    </div>
    <div class="grid" style="margin-top: 16px;">
      <section class="card">
        <h2>Current State</h2>
        {render_state_rows(snapshot["state"])}
      </section>
      <section class="card">
        <h2>MQTT Topics</h2>
        {render_topics_table(snapshot["topics"], snapshot["topic_values"])}
      </section>
    </div>
    <div class="grid" style="margin-top: 16px;">
      <section class="card">
        <h2>Decision Logic</h2>
        <div class="label">Activation</div>
        <div class="value">{escape_html(str(snapshot["state"]["last_decision_reason"]))}</div>
        <div class="label" style="margin-top: 12px;">Restore</div>
        <div class="value">{escape_html(str(snapshot["state"]["last_restore_reason"]))}</div>
      </section>
      <section class="card">
        <h2>Automation Control</h2>
        {render_automation_controls(snapshot["state"])}
      </section>
    </div>
    <div class="grid" style="margin-top: 16px;">
      <section class="card">
        <h2>Config</h2>
        {render_config_form(snapshot["config"])}
      </section>
      <section class="card">
        <h2>Simulation</h2>
        {render_simulation_controls(snapshot["state"])}
      </section>
    </div>
    <div class="grid" style="margin-top: 16px;">
      <section class="card">
        <h2>History</h2>
        {render_history_table(snapshot["history"])}
      </section>
    </div>
    <script>
      const form = document.getElementById("config-form");
      const status = document.getElementById("save-status");
      const automationStatus = document.getElementById("automation-status");
      const simulationStatus = document.getElementById("simulation-status");
      const simulationEnableButton = document.getElementById("simulation-enable");
      const simulationDisableButton = document.getElementById("simulation-disable");
      const stopButton = document.getElementById("automation-stop");
      const startButton = document.getElementById("automation-start");
      const refreshButton = document.getElementById("page-refresh");
      const refreshStatus = document.getElementById("refresh-status");
      const AUTO_REFRESH_MS = 5000;
      function isEditingForm() {{
        const active = document.activeElement;
        if (!active) {{
          return false;
        }}
        return active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT";
      }}
      async function refreshPageNow() {{
        window.location.reload();
      }}
      if (form) {{
        form.addEventListener("submit", async (event) => {{
          event.preventDefault();
          status.textContent = "Saving...";
          const formData = new FormData(form);
          const payload = {{
            mqtt_host: formData.get("mqtt_host"),
            mqtt_port: Number(formData.get("mqtt_port")),
            mqtt_username: formData.get("mqtt_username"),
            mqtt_password: formData.get("mqtt_password"),
            mqtt_topic_prefix: formData.get("mqtt_topic_prefix"),
            loadpoint_id: Number(formData.get("loadpoint_id")),
            homeassistant_power_sensor_entity_id: formData.get("homeassistant_power_sensor_entity_id"),
            homeassistant_battery_power_sensor_entity_id: formData.get("homeassistant_battery_power_sensor_entity_id"),
            export_power_threshold_w: Number(formData.get("export_power_threshold_w")),
            import_power_threshold_w: Number(formData.get("import_power_threshold_w")),
            export_delay_seconds: Number(formData.get("export_delay_seconds")),
            import_delay_seconds: Number(formData.get("import_delay_seconds")),
            battery_discharge_power_threshold_w: Number(formData.get("battery_discharge_power_threshold_w")),
            battery_discharge_delay_seconds: Number(formData.get("battery_discharge_delay_seconds")),
            evcc_active_current_threshold: Number(formData.get("evcc_active_current_threshold")),
            auto_reset_on_restart: formData.get("auto_reset_on_restart") === "true",
          }};
          try {{
            const response = await fetch("api/config", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify(payload),
            }});
            const raw = await response.text();
            let data = {{}};
            if (raw) {{
              try {{
                data = JSON.parse(raw);
              }} catch (_error) {{
                throw new Error(raw);
              }}
            }}
            if (!response.ok) {{
              throw new Error(data.error || "Save failed");
            }}
            status.textContent = "Saved. Reloading state...";
            window.location.reload();
          }} catch (error) {{
            status.textContent = `Save failed: ${{error.message}}`;
          }}
        }});
      }}
      async function toggleAutomation(enabled, reason) {{
        if (!automationStatus) {{
          return;
        }}
        automationStatus.textContent = enabled ? "Starting automation..." : "Stopping automation...";
        try {{
          const response = await fetch("api/automation", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ enabled, reason }}),
          }});
          const raw = await response.text();
          let data = {{}};
          if (raw) {{
            try {{
              data = JSON.parse(raw);
            }} catch (_error) {{
              throw new Error(raw);
            }}
          }}
          if (!response.ok) {{
            throw new Error(data.error || "Automation update failed");
          }}
          automationStatus.textContent = "Saved. Reloading state...";
          window.location.reload();
        }} catch (error) {{
          automationStatus.textContent = `Save failed: ${{error.message}}`;
        }}
      }}
      async function toggleSimulation(enabled, reason) {{
        if (!simulationStatus) {{
          return;
        }}
        simulationStatus.textContent = enabled ? "Enabling what-if mode..." : "Disabling what-if mode...";
        try {{
          const response = await fetch("api/simulation", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ enabled, reason }}),
          }});
          const raw = await response.text();
          let data = {{}};
          if (raw) {{
            try {{
              data = JSON.parse(raw);
            }} catch (_error) {{
              throw new Error(raw);
            }}
          }}
          if (!response.ok) {{
            throw new Error(data.error || "Simulation update failed");
          }}
          simulationStatus.textContent = "Saved. Reloading state...";
          window.location.reload();
        }} catch (error) {{
          simulationStatus.textContent = `Save failed: ${{error.message}}`;
        }}
      }}
      if (stopButton) {{
        stopButton.addEventListener("click", () => toggleAutomation(false, "user pressed STOP automation"));
      }}
      if (startButton) {{
        startButton.addEventListener("click", () => toggleAutomation(true, "user pressed START automation"));
      }}
      if (simulationEnableButton) {{
        simulationEnableButton.addEventListener("click", () => toggleSimulation(true, "user enabled what-if simulation"));
      }}
      if (simulationDisableButton) {{
        simulationDisableButton.addEventListener("click", () => toggleSimulation(false, "user disabled what-if simulation"));
      }}
      if (refreshButton) {{
        refreshButton.addEventListener("click", refreshPageNow);
      }}
      window.setInterval(() => {{
        if (isEditingForm()) {{
          if (refreshStatus) {{
            refreshStatus.textContent = "Auto-refresh paused while editing form fields.";
          }}
          return;
        }}
        if (refreshStatus) {{
          refreshStatus.textContent = "Refreshing...";
        }}
        refreshPageNow();
      }}, AUTO_REFRESH_MS);
    </script>
  </main>
</body>
</html>
"""


def render_state_rows(state: dict[str, Any]) -> str:
    rows = []
    for key, value in state.items():
        rows.append(
            f'<div class="label">{escape_html(key)}</div><div class="value">{escape_html(format_value(value))}</div>'
        )
    return "".join(rows)


def render_topics_table(topics: dict[str, str], topic_values: dict[str, dict[str, Any]]) -> str:
    rows = []
    for label, topic in topics.items():
        value = topic_values.get(topic, {})
        payload = value.get("payload", "no data")
        received_at = value.get("received_at", "never")
        rows.append(
            "<tr>"
            f"<td><strong>{escape_html(label)}</strong><br><code>{escape_html(topic)}</code></td>"
            f"<td><code>{escape_html(str(payload))}</code></td>"
            f"<td class=\"muted\">{escape_html(str(received_at))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Topic</th><th>Last Payload</th><th>Received At</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_config_form(config: dict[str, Any]) -> str:
    auto_reset_checked = "true" if config["auto_reset_on_restart"] else "false"
    selected_sensor = str(config.get("homeassistant_power_sensor_entity_id", ""))
    selected_battery_sensor = str(config.get("homeassistant_battery_power_sensor_entity_id", ""))
    sensor_options = render_power_sensor_options(
        selected_sensor,
        config.get("power_sensor_options", []),
    )
    power_sensor_error = str(config.get("power_sensor_error") or "")
    battery_power_sensor_error = str(config.get("battery_power_sensor_error") or "")
    status_messages = []
    if power_sensor_error:
        status_messages.append(f'<div class="status warn">Grid sensor list unavailable: {escape_html(power_sensor_error)}</div>')
    if battery_power_sensor_error:
        status_messages.append(
            f'<div class="status warn">Battery power sensor fallback active: {escape_html(battery_power_sensor_error)}</div>'
        )
    if not status_messages:
        status_messages.append(
            '<div class="status">Only Home Assistant sensors with unit `W` are suggested here. Manual entry is allowed.</div>'
        )
    sensor_status = "".join(status_messages)
    return f"""
<form id="config-form">
  <div class="form-grid">
    <label>MQTT Host<input name="mqtt_host" value="{escape_html(str(config["mqtt_host"]))}" required></label>
    <label>MQTT Port<input name="mqtt_port" type="number" min="1" max="65535" value="{escape_html(str(config["mqtt_port"]))}" required></label>
    <label>MQTT Username<input name="mqtt_username" value="{escape_html(str(config["mqtt_username"]))}"></label>
    <label>MQTT Password<input name="mqtt_password" type="password" value=""></label>
    <label>MQTT Prefix<input name="mqtt_topic_prefix" value="{escape_html(str(config["mqtt_topic_prefix"]))}" required></label>
    <label>Loadpoint ID<input name="loadpoint_id" type="number" min="1" value="{escape_html(str(config["loadpoint_id"]))}" required></label>
    <label>Home Assistant Power Sensor
      <input name="homeassistant_power_sensor_entity_id" list="power-sensor-options" value="{escape_html(selected_sensor)}" placeholder="sensor.power_meter_wirkleistung">
    </label>
    <label>Home Assistant Battery Power Sensor
      <input name="homeassistant_battery_power_sensor_entity_id" list="power-sensor-options" value="{escape_html(selected_battery_sensor)}" placeholder="sensor.battery_power">
    </label>
    <label>Export Threshold (W)<input name="export_power_threshold_w" type="number" step="1" value="{escape_html(str(config["export_power_threshold_w"]))}" required></label>
    <label>Import Threshold (W)<input name="import_power_threshold_w" type="number" step="1" value="{escape_html(str(config["import_power_threshold_w"]))}" required></label>
    <label>Export Delay (s)<input name="export_delay_seconds" type="number" min="1" value="{escape_html(str(config["export_delay_seconds"]))}" required></label>
    <label>Import Delay (s)<input name="import_delay_seconds" type="number" min="1" value="{escape_html(str(config["import_delay_seconds"]))}" required></label>
    <label>Battery Discharge Threshold (W)<input name="battery_discharge_power_threshold_w" type="number" step="1" value="{escape_html(str(config["battery_discharge_power_threshold_w"]))}" required></label>
    <label>Battery Discharge Delay (s)<input name="battery_discharge_delay_seconds" type="number" min="1" value="{escape_html(str(config["battery_discharge_delay_seconds"]))}" required></label>
    <label>evcc Active Threshold (A)<input name="evcc_active_current_threshold" type="number" step="0.1" min="0" value="{escape_html(str(config["evcc_active_current_threshold"]))}" required></label>
    <label>Reset Auto State On Restart
      <input name="auto_reset_on_restart" list="bool-values" value="{auto_reset_checked}" required>
    </label>
  </div>
  <datalist id="bool-values">
    <option value="true"></option>
    <option value="false"></option>
  </datalist>
  <datalist id="power-sensor-options">
    {sensor_options}
  </datalist>
  <div class="actions">
    <button type="submit">Save Config</button>
    <div class="status" id="save-status">Password stays unchanged if left empty. Leave either sensor empty to keep using evcc MQTT for that value.</div>
  </div>
  {sensor_status}
</form>
"""


def render_power_sensor_options(selected_entity_id: str, options: list[dict[str, str]]) -> str:
    rows = []
    if selected_entity_id:
        rows.append(f'<option value="{escape_html(selected_entity_id)}"></option>')
    for option in options:
        entity_id = option["entity_id"]
        label = f'{option["name"]} ({entity_id})'
        rows.append(
            f'<option value="{escape_html(entity_id)}">{escape_html(label)}</option>'
        )
    return "".join(rows)


def render_automation_controls(state: dict[str, Any]) -> str:
    enabled = bool(state["automation_enabled"])
    automation_text = "running" if enabled else "stopped"
    return f"""
<div class="label">Automation State</div>
<div class="value">{escape_html(automation_text)}</div>
<div class="actions">
  <button type="button" class="button-danger" id="automation-stop">STOP Automation</button>
  <button type="button" class="button-secondary" id="automation-start">Start Automation</button>
  <div class="status" id="automation-status">Stop clears the add-on's automation ownership and prevents further MQTT writes until restarted.</div>
</div>
"""


def render_simulation_controls(state: dict[str, Any]) -> str:
    enabled = bool(state["simulation_enabled"])
    simulation_text = "what-if active" if enabled else "live writes active"
    last_command = "none"
    if state.get("last_mode_command"):
        suffix = " (simulated)" if state.get("last_mode_command_simulated") else ""
        last_command = f'{state["last_mode_command"]}{suffix}'
    return f"""
<div class="label">Write Mode</div>
<div class="value">{escape_html(simulation_text)}</div>
<div class="label" style="margin-top: 12px;">Last Command</div>
<div class="value">{escape_html(last_command)}</div>
<div class="actions">
  <button type="button" class="button-secondary" id="simulation-enable">Enable What-If</button>
  <button type="button" class="button-danger" id="simulation-disable">Disable What-If</button>
  <div class="status" id="simulation-status">What-if uses the real incoming values and shows what the add-on would write, but suppresses the actual MQTT mode command.</div>
</div>
"""


def render_history_table(history: list[dict[str, Any]]) -> str:
    if not history:
        return '<div class="muted">No history recorded yet.</div>'

    def render_rows(entries: list[dict[str, Any]]) -> str:
        rows = []
        for entry in entries:
            details_json = json.dumps(entry.get("details", {}), ensure_ascii=True, indent=2)
            rows.append(
                "<tr>"
                f"<td class=\"muted\">{escape_html(format_history_timestamp(str(entry.get('timestamp', 'n/a'))))}</td>"
                f"<td><strong>{escape_html(str(entry.get('message', 'n/a')))}</strong><br><span class=\"muted\">{escape_html(str(entry.get('type', 'n/a')))}</span></td>"
                f"<td>{escape_html(str(entry.get('reason', 'n/a')))}</td>"
                f"<td><details><summary>Show</summary><pre><code>{escape_html(details_json)}</code></pre></details></td>"
                "</tr>"
            )
        return "".join(rows)

    visible_entries = history[:10]
    older_entries = history[10:25]
    table_head = "<table><thead><tr><th>Time</th><th>Event</th><th>Reason</th><th>Details</th></tr></thead><tbody>"
    visible_table = table_head + render_rows(visible_entries) + "</tbody></table>"

    if not older_entries:
        return visible_table

    older_table = table_head + render_rows(older_entries) + "</tbody></table>"
    return (
        visible_table
        + f'<details style="margin-top: 12px;"><summary>Show older entries ({len(older_entries)})</summary>{older_table}</details>'
    )


def format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def format_threshold(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "*" * 8


def describe_config_changes(current: AddonConfig, updated: AddonConfig) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    current_config = config_to_dict(current)
    updated_config = config_to_dict(updated)
    for key, current_value in current_config.items():
        updated_value = updated_config[key]
        if current_value == updated_value:
            continue
        if key == "mqtt_password":
            changes[key] = "updated"
            continue
        changes[key] = {"from": current_value, "to": updated_value}
    return changes


def validate_config(config: AddonConfig) -> None:
    if not config.mqtt_host:
        raise ValueError("mqtt_host must not be empty")
    if config.mqtt_port < 1 or config.mqtt_port > 65535:
        raise ValueError("mqtt_port must be between 1 and 65535")
    if not config.mqtt_topic_prefix:
        raise ValueError("mqtt_topic_prefix must not be empty")
    if config.loadpoint_id < 1:
        raise ValueError("loadpoint_id must be >= 1")
    if config.export_power_threshold_w == 0:
        raise ValueError("export_power_threshold_w must not be 0")
    if config.import_power_threshold_w == 0:
        raise ValueError("import_power_threshold_w must not be 0")
    if config.export_delay_seconds < 1:
        raise ValueError("export_delay_seconds must be >= 1")
    if config.import_delay_seconds < 1:
        raise ValueError("import_delay_seconds must be >= 1")
    if config.battery_discharge_power_threshold_w == 0:
        raise ValueError("battery_discharge_power_threshold_w must not be 0")
    if config.battery_discharge_delay_seconds < 1:
        raise ValueError("battery_discharge_delay_seconds must be >= 1")
    if config.evcc_active_current_threshold < 0:
        raise ValueError("evcc_active_current_threshold must be >= 0")


def main() -> int:
    try:
        config = read_config()
    except ConfigError as err:
        LOGGER.error("%s", err)
        return 1

    worker = EvccAutoMode(config)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)

    try:
        worker.run()
    except Exception:
        LOGGER.exception("Worker crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
