# evcc Auto Mode

Dieses Home-Assistant-Add-on verbindet sich per MQTT mit `evcc` und schaltet den Lademodus eines konfigurierten Ladepunkts automatisch zwischen `pv` und `minpv`.

## Debug-Ansicht im Add-on

Beim Oeffnen des Add-ons in Home Assistant steht eine Ingress-Weboberflaeche zur Verfuegung. Dort werden unter anderem angezeigt:

- alle verwendeten MQTT-Topics
- die letzten empfangenen Payloads je Topic
- aktueller interner Zustand wie `connected`, `grid_power`, `offeredCurrent`, `batterySoc`, `bufferSoc`
- Timer fuer Einspeisung und Netzbezug
- letzter Entscheidungsgrund fuer Aktivierung oder Rueckstellung

Zusatzlich gibt es einen JSON-Endpunkt unter `/api/state`.

Direkt in der Ingress-Oberflaeche koennen ausserdem die Laufzeitwerte angepasst und gespeichert werden:

- MQTT Host, Port, Benutzername und Passwort
- Topic-Prefix
- Loadpoint-ID
- Export- und Import-Delays
- Schwelle fuer `offeredCurrent`
- Verhalten fuer Reset bei Neustart

Wenn mehrere Aktivierungsbedingungen gleichzeitig nicht erfuellt sind, zeigt die Debug-Ansicht jetzt alle aktiven Blocker gesammelt an statt nur des ersten Treffers.

Der interne Zustand `auto_mode_active` wird unter `/data/runtime_state.json` gespeichert. Mit `auto_reset_on_restart: false` kann das Add-on diesen Zustand ueber einen Neustart behalten, mit `true` wird er beim Start verworfen.

## Verhalten in v1

- Schaltet auf `minpv`, wenn:
  - das Fahrzeug verbunden ist
  - Einspeisung laenger als konfiguriert anliegt
  - `batterySoc < bufferSoc`
  - kein aktiver Ladeplan vorliegt
  - `evcc` nicht bereits selbst ueber den konfigurierten Mindeststrom hinaus regelt
- Schaltet nur dann wieder auf `pv`, wenn das Add-on `minpv` selbst gesetzt hat und anschliessend laenger Netzbezug anliegt

## MQTT-Topics

Standardmaessig wird mit Prefix `evcc` gearbeitet. Fuer `loadpoint_id: 1` nutzt das Add-on unter anderem:

- `evcc/site/grid/power`
- `evcc/site/bufferSoc`
- `evcc/site/batterySoc`
- `evcc/loadpoints/1/connected`
- `evcc/loadpoints/1/mode`
- `evcc/loadpoints/1/mode/set`
- `evcc/loadpoints/1/offeredCurrent`
- `evcc/loadpoints/1/planActive`

## Konfiguration

Beispieloptionen:

```yaml
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_username: ""
mqtt_password: ""
mqtt_topic_prefix: evcc
loadpoint_id: 1
export_delay_seconds: 60
import_delay_seconds: 30
evcc_active_current_threshold: 6.0
auto_reset_on_restart: true
```
