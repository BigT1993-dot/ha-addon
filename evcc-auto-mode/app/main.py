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
        self.last_mqtt_message_at: float | None = None
        self.last_mode_command: str | None = None
        self.last_mode_command_at: float | None = None
        self.last_decision_reason = "waiting for MQTT data"
        self.last_restore_reason = "waiting for MQTT data"
        self.topic_values: dict[str, dict[str, Any]] = {}

        if config.auto_reset_on_restart:
            self.auto_mode_active = False

    def run(self) -> None:
        server = DebugServer(self)
        server.start()
        self.client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()
        LOGGER.info("Started evcc auto mode worker")

        try:
            while not self.shutdown_event.wait(1):
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

            should_set_minpv, set_reason = self.should_set_minpv(now)
            self.last_decision_reason = set_reason
            if should_set_minpv:
                self.publish_mode("minpv")
                self.auto_mode_active = True

            should_restore_pv, restore_reason = self.should_restore_pv(now)
            self.last_restore_reason = restore_reason
            if should_restore_pv:
                self.publish_mode("pv")
                self.auto_mode_active = False

    def should_set_minpv(self, now: float) -> tuple[bool, str]:
        if not self.connected:
            return False, "vehicle not connected"
        if self.plan_active:
            return False, "charging plan is active"
        if self.auto_mode_active:
            return False, "auto mode already active"
        if self.current_mode == "minpv":
            return False, "evcc already in minpv"
        if self.offered_current > self.config.evcc_active_current_threshold:
            return False, "evcc is actively regulating current"
        if self.export_timer_started_at is None:
            return False, "no sustained export detected"
        if now - self.export_timer_started_at < self.config.export_delay_seconds:
            return False, "export delay not reached yet"
        if self.battery_soc is None or self.buffer_soc is None:
            return False, "battery or buffer SoC missing"
        if self.battery_soc >= self.buffer_soc:
            return False, "battery SoC is not below buffer SoC"
        return True, "all activation conditions met"

    def should_restore_pv(self, now: float) -> tuple[bool, str]:
        if not self.auto_mode_active:
            return False, "auto mode not active"
        if self.import_timer_started_at is None:
            return False, "no sustained grid import detected"
        if now - self.import_timer_started_at < self.config.import_delay_seconds:
            return False, "import delay not reached yet"
        if self.current_mode == "pv":
            return False, "evcc already in pv"
        return True, "all restore conditions met"

    def publish_mode(self, mode: str) -> None:
        topic = self.config.topics["mode_set"]
        LOGGER.info("Publishing %s to %s", mode, topic)
        self.last_mode_command = mode
        self.last_mode_command_at = time.monotonic()
        result = self.client.publish(topic, payload=mode, qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Failed publishing mode %s: rc=%s", mode, result.rc)

    def get_debug_snapshot(self) -> dict[str, Any]:
        with self.state_lock:
            now = time.monotonic()
            return {
                "generated_at": iso_utc_now(),
                "config": {
                    "mqtt_host": self.config.mqtt_host,
                    "mqtt_port": self.config.mqtt_port,
                    "mqtt_topic_prefix": self.config.mqtt_topic_prefix,
                    "loadpoint_id": self.config.loadpoint_id,
                    "export_delay_seconds": self.config.export_delay_seconds,
                    "import_delay_seconds": self.config.import_delay_seconds,
                    "evcc_active_current_threshold": self.config.evcc_active_current_threshold,
                },
                "topics": self.config.topics,
                "state": {
                    "connected": self.connected,
                    "plan_active": self.plan_active,
                    "current_mode": self.current_mode,
                    "offered_current": self.offered_current,
                    "grid_power": self.grid_power,
                    "buffer_soc": self.buffer_soc,
                    "battery_soc": self.battery_soc,
                    "auto_mode_active": self.auto_mode_active,
                    "last_decision_reason": self.last_decision_reason,
                    "last_restore_reason": self.last_restore_reason,
                    "last_mode_command": self.last_mode_command,
                    "last_mqtt_message_age_seconds": elapsed_seconds(self.last_mqtt_message_at, now),
                    "last_mode_command_age_seconds": elapsed_seconds(self.last_mode_command_at, now),
                    "export_timer_age_seconds": elapsed_seconds(self.export_timer_started_at, now),
                    "import_timer_age_seconds": elapsed_seconds(self.import_timer_started_at, now),
                },
                "topic_values": self.topic_values,
            }


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
  </style>
</head>
<body>
  <main>
    <div class="card">
      <h1>evcc Auto Mode Debug</h1>
      <div class="muted">Generated at {snapshot["generated_at"]}</div>
      <div class="muted">JSON endpoint: <code>/api/state</code></div>
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
        <h2>Config</h2>
        <pre>{escape_html(json.dumps(snapshot["config"], indent=2))}</pre>
      </section>
    </div>
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


def format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
