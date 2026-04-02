import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("evcc_auto_mode")


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
    export_delay_seconds: int
    import_delay_seconds: int
    evcc_active_current_threshold: float
    auto_reset_on_restart: bool

    @property
    def loadpoint_prefix(self) -> str:
        return f"{self.mqtt_topic_prefix}/loadpoints/{self.loadpoint_id}"

    @property
    def topics(self) -> dict[str, str]:
        return {
            "grid_power": f"{self.mqtt_topic_prefix}/site/grid/power",
            "buffer_soc": f"{self.mqtt_topic_prefix}/site/bufferSoc",
            "battery_soc": f"{self.mqtt_topic_prefix}/site/batterySoc",
            "connected": f"{self.loadpoint_prefix}/connected",
            "mode": f"{self.loadpoint_prefix}/mode",
            "mode_set": f"{self.loadpoint_prefix}/mode/set",
            "offered_current": f"{self.loadpoint_prefix}/offeredCurrent",
            "plan_active": f"{self.loadpoint_prefix}/planActive",
        }


def read_config() -> AddonConfig:
    options_path = "/data/options.json"
    try:
        with open(options_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as err:
        raise ConfigError(f"Missing add-on options at {options_path}") from err

    return AddonConfig(
        mqtt_host=raw["mqtt_host"],
        mqtt_port=int(raw["mqtt_port"]),
        mqtt_username=raw.get("mqtt_username", "") or "",
        mqtt_password=raw.get("mqtt_password", "") or "",
        mqtt_topic_prefix=(raw.get("mqtt_topic_prefix") or "evcc").rstrip("/"),
        loadpoint_id=int(raw.get("loadpoint_id", 1)),
        export_delay_seconds=int(raw.get("export_delay_seconds", 60)),
        import_delay_seconds=int(raw.get("import_delay_seconds", 30)),
        evcc_active_current_threshold=float(raw.get("evcc_active_current_threshold", 6.0)),
        auto_reset_on_restart=bool(raw.get("auto_reset_on_restart", True)),
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

        self.state_lock = threading.Lock()
        self.shutdown_event = threading.Event()

        self.connected = False
        self.plan_active = False
        self.current_mode = ""
        self.offered_current = 0.0
        self.grid_power = 0.0
        self.buffer_soc: float | None = None
        self.battery_soc: float | None = None
        self.auto_mode_active = False
        self.export_timer_started_at: float | None = None
        self.import_timer_started_at: float | None = None

        if config.auto_reset_on_restart:
            self.auto_mode_active = False

    def run(self) -> None:
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()
        LOGGER.info("Started evcc auto mode worker")

        try:
            while not self.shutdown_event.wait(1):
                self.evaluate()
        finally:
            self.client.loop_stop()
            self.client.disconnect()

    def stop(self, *_args: Any) -> None:
        LOGGER.info("Stopping worker")
        self.shutdown_event.set()

    def on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connect failed with code %s", reason_code)
            return

        topics = self.config.topics
        for topic in topics.values():
            client.subscribe(topic)
        LOGGER.info("Subscribed to %s topics", len(topics))

    def on_disconnect(self, _client: mqtt.Client, _userdata: Any, _disconnect_flags: Any, reason_code: Any, _properties: Any) -> None:
        LOGGER.warning("Disconnected from MQTT with code %s", reason_code)

    def on_message(self, _client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        topics = self.config.topics

        with self.state_lock:
            try:
                if msg.topic == topics["connected"]:
                    self.connected = parse_bool(payload)
                elif msg.topic == topics["plan_active"]:
                    self.plan_active = parse_bool(payload)
                elif msg.topic == topics["mode"]:
                    self.current_mode = payload
                    if payload != "minpv" and self.auto_mode_active:
                        LOGGER.info("Mode changed externally to %s; clearing auto flag", payload)
                        self.auto_mode_active = False
                elif msg.topic == topics["offered_current"]:
                    self.offered_current = parse_float(payload)
                elif msg.topic == topics["grid_power"]:
                    self.grid_power = parse_float(payload)
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

            if self.grid_power < 0:
                self.export_timer_started_at = self.export_timer_started_at or now
            else:
                self.export_timer_started_at = None

            if self.grid_power > 0:
                self.import_timer_started_at = self.import_timer_started_at or now
            else:
                self.import_timer_started_at = None

            if not self.connected and self.auto_mode_active:
                LOGGER.info("Vehicle disconnected; clearing auto mode state")
                self.auto_mode_active = False

            if self.should_set_minpv(now):
                self.publish_mode("minpv")
                self.auto_mode_active = True

            if self.should_restore_pv(now):
                self.publish_mode("pv")
                self.auto_mode_active = False

    def should_set_minpv(self, now: float) -> bool:
        if not self.connected:
            return False
        if self.plan_active:
            return False
        if self.auto_mode_active:
            return False
        if self.current_mode == "minpv":
            return False
        if self.offered_current > self.config.evcc_active_current_threshold:
            return False
        if self.export_timer_started_at is None:
            return False
        if now - self.export_timer_started_at < self.config.export_delay_seconds:
            return False
        if self.battery_soc is None or self.buffer_soc is None:
            return False
        return self.battery_soc < self.buffer_soc

    def should_restore_pv(self, now: float) -> bool:
        if not self.auto_mode_active:
            return False
        if self.import_timer_started_at is None:
            return False
        if now - self.import_timer_started_at < self.config.import_delay_seconds:
            return False
        if self.current_mode == "pv":
            return False
        return True

    def publish_mode(self, mode: str) -> None:
        topic = self.config.topics["mode_set"]
        LOGGER.info("Publishing %s to %s", mode, topic)
        result = self.client.publish(topic, payload=mode, qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Failed publishing mode %s: rc=%s", mode, result.rc)


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "on", "yes"}:
        return True
    if normalized in {"false", "0", "off", "no"}:
        return False
    raise ValueError(f"Invalid boolean: {value}")


def parse_float(value: str) -> float:
    return float(value)


def parse_optional_float(value: str) -> float | None:
    if value == "":
        return None
    return float(value)


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
