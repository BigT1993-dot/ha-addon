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
- optionaler Home-Assistant-Leistungssensor fuer `grid_power`
- Export- und Import-Leistungsschwellen in Watt
- Export- und Import-Delays
- Schwelle fuer `offeredCurrent`
- Verhalten fuer Reset bei Neustart

Wenn mehrere Aktivierungsbedingungen gleichzeitig nicht erfuellt sind, zeigt die Debug-Ansicht jetzt alle aktiven Blocker gesammelt an statt nur des ersten Treffers.

Der interne Zustand `auto_mode_active` wird unter `/data/runtime_state.json` gespeichert. Mit `auto_reset_on_restart: false` kann das Add-on diesen Zustand ueber einen Neustart behalten, mit `true` wird er beim Start verworfen.

Neu in `0.2.14`:

- grosse `STOP Automation`-Schaltflaeche in der Ingress-Oberflaeche
- persistente Historie fuer Moduswechsel, Konfigurationsaenderungen und Start/Stop der Automatik
- protokollierter Grund bei automatischen MQTT-Schreibvorgaengen
- relative Ingress-API-Aufrufe, damit `STOP Automation` und Konfig-Speichern sauber im Add-on landen
- Export- und Import-Hysterese ueber konfigurierbare Leistungsschwellen, Standard `-100 W` und `+100 W`
- kompaktere History mit Zeitformat `dd/mm hh:mm:ss` und einklappbaren `details`
- Home-Assistant-Add-on-Schema fuer die neuen Schwellen auf gueltige Typdefinitionen korrigiert
- MQTT Discovery Sensor fuer die letzte automatische Add-on-Aktion in Home Assistant
- Ingress-Debugseite mit automatischem Refresh und zusaetzlichem `Refresh Now`-Knopf
- Aktivierung und Rueckstellung erst nach zwei aufeinanderfolgenden `grid_power`-MQTT-Zyklen ueber bzw. unter Schwellwert
- History zeigt standardmaessig nur die letzten 10 Eintraege, aeltere Eintraege erst nach Aufklappen
- eigener MQTT-Discovery-Sensor fuer den Automatik-Zustand mit State `started` oder `stopped`
- optionaler Home-Assistant-Leistungssensor als Quelle fuer `grid_power`, in der UI auf Sensoren mit Einheit `W` gefiltert
- bei Nutzung des Home-Assistant-Leistungssensors wieder zeitbasierte Schaltlogik mit Export-/Import-Delay
- Home-Assistant-Leistungssensor kann jetzt auch manuell per Entity-ID eingetragen werden, falls die Vorschlagsliste leer bleibt

## Home Assistant Sensor

Das Add-on veroeffentlicht per MQTT Discovery einen Sensor fuer die letzte automatische Aktion:

- Entity-Name: `evcc Auto Mode Last Action`
- State: ISO-Zeitstempel der letzten automatischen Modus-Aktion
- Attribute:
  - `message`
  - `reason`
  - `type`
  - `details`

Der Sensor wird aktualisiert, wenn das Add-on selbst einen Modus per MQTT schreibt, also z. B. bei `minpv` oder `pv`. Darauf kann in Home Assistant direkt eine eigene Benachrichtigungs-Automation triggern.

Zusatzlich veroeffentlicht das Add-on einen zweiten Sensor fuer den allgemeinen Automatik-Zustand:

- Entity-Name: `evcc Auto Mode Automation`
- State: `started` oder `stopped`
- Attribute:
  - `reason`
  - `automation_enabled`
  - `auto_mode_active`
  - `updated_at`

Dieser Sensor aendert sich immer dann, wenn die Automatisierung im Add-on gestartet oder gestoppt wird, und ist damit der bessere Trigger fuer einfache Home-Assistant-Automationen.

Wenn `STOP Automation` gedrueckt wird, schreibt das Add-on keine weiteren automatischen Moduswechsel mehr, bis die Automatik wieder explizit gestartet wird. Dabei wird auch die interne Eigentuemerschaft `auto_mode_active` geloescht, damit spaetere automatische Rueckstellungen nicht mehr aus altem Zustand heraus passieren.

## Verhalten in v1

- Schaltet auf `minpv`, wenn:
  - das Fahrzeug verbunden ist
  - `grid_power` fuer die konfigurierte Export-Dauer bei oder unter der Export-Schwelle liegt
  - `batterySoc < bufferSoc`
  - kein aktiver Ladeplan vorliegt
  - `evcc` nicht bereits selbst ueber den konfigurierten Mindeststrom hinaus regelt
- Schaltet nur dann wieder auf `pv`, wenn das Add-on `minpv` selbst gesetzt hat und anschliessend `grid_power` fuer die konfigurierte Import-Dauer bei oder ueber der Import-Schwelle liegt

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
homeassistant_power_sensor_entity_id: ""
export_power_threshold_w: -100
import_power_threshold_w: 100
export_delay_seconds: 60
import_delay_seconds: 30
evcc_active_current_threshold: 6.0
auto_reset_on_restart: true
```

Wenn `homeassistant_power_sensor_entity_id` gesetzt ist, verwendet das Add-on diesen Home-Assistant-Sensor als Quelle fuer `grid_power`. In der Ingress-UI werden dafuer Sensoren mit Einheit `W` vorgeschlagen. Falls die Vorschlagsliste leer bleibt, kann die Entity-ID auch direkt manuell eingetragen werden. Bleibt das Feld leer, nutzt das Add-on weiter `evcc/site/grid/power` per MQTT.
