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
- optionaler Home-Assistant-Sensor fuer `battery_power`
- Export- und Import-Leistungsschwellen in Watt
- Export- und Import-Delays
- Batterientlade-Schwelle und Delay fuer die Rueckschaltung auf `pv`
- Schwelle fuer `offeredCurrent`
- Verhalten fuer Reset bei Neustart

Wenn mehrere Aktivierungsbedingungen gleichzeitig nicht erfuellt sind, zeigt die Debug-Ansicht jetzt alle aktiven Blocker gesammelt an statt nur des ersten Treffers.

Der interne Zustand `auto_mode_active` wird unter `/data/runtime_state.json` gespeichert. Mit `auto_reset_on_restart: false` kann das Add-on diesen Zustand ueber einen Neustart behalten, mit `true` wird er beim Start verworfen.

Neu in `0.3.1`:

- Auto-Refresh laeuft jetzt nur noch auf dem `Overview`-Tab
- aktiver Tab bleibt beim Navigieren und Neuladen erhalten

Neu in `0.3.0`:

- Ingress-UI auf mobile Nutzung und schnellere Uebersicht umgebaut
- neue Tabs fuer `Overview`, `Config`, `Topics`, `History` und `Debug`
- `Max PV` zeigt jetzt berechneten Sollstrom, dynamische Leistungsgrenze und Phasen direkt in der UI

Neu in `0.2.24`:

- erste `Max PV Mode`-Version hinzugefuegt
- regelt in `minpv` den `minCurrent` ueber `evcc/loadpoints/<id>/minCurrent/set`
- nutzt `evcc/site/homePower`, einen konfigurierbaren HA-Sensor fuer Inverter-Eingangsleistung und eine konfigurierbare Batterie-Entladegrenze
- Zielgrenze: `min(max_pv_inverter_power_w, inverter_input_power_w + max_pv_battery_discharge_power_w) - home_power_w`

Neu in `0.2.23`:

- Export- und Import-Schwellen unterstuetzen jetzt ebenfalls positive und negative Vorzeichen
- positiver Export-Schwellenwert bedeutet Einspeisung ab `>= Schwelle`
- negativer Import-Schwellenwert bedeutet Netzbezug ab `<= Schwelle`

Neu in `0.2.22`:

- Batterientlade-Schwelle unterstuetzt jetzt positive und negative Vorzeichen
- `200` bedeutet Entladung ab `>= 200 W`
- `-200` bedeutet Entladung ab `<= -200 W`

Neu in `0.2.21`:

- Startpfad auf `run.sh` mit `with-contenv` umgestellt
- folgt damit dem aktuellen Home-Assistant-App-Muster fuer Runtime-Umgebung
- soll fehlende Supervisor-/Home-Assistant-Env-Variablen im Prozess beheben

Neu in `0.2.20`:

- zusaetzliche Runtime-Diagnose fuer den Home-Assistant-/Supervisor-API-Zugriff
- Log-Ausgabe, ob relevante Env-Variablen wie `SUPERVISOR_TOKEN` vorhanden sind
- praezisere Fehlermeldung bei fehlendem `SUPERVISOR_TOKEN`

Neu in `0.2.19`:

- Rueckschaltung auf `pv` jetzt auch bei dauerhaft zu hoher Batterientladung
- optionaler Home-Assistant-Sensor fuer Batterieleistung mit Prioritaet vor `evcc/site/batteryPower`
- `evcc/site/batteryPower` wird fuer Batterieleistung nur noch als Fallback genutzt
- neue Runtime-Settings fuer Batterientlade-Schwelle und Batterientlade-Dauer
- Debug-Ansicht zeigt Batterieleistung, Quelle und Entlade-Timer an

Neu in `0.2.18`:

- `What-If`-Simulationsmodus in der Ingress-Oberflaeche
- nutzt weiter die real eingehenden MQTT- und Home-Assistant-Werte
- zeigt in Status und History, welchen Modus das Add-on schreiben wuerde
- unterdrueckt dabei den echten MQTT-Schreibzugriff auf `.../mode/set`

Neu in `0.2.17`:

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
- optionaler Home-Assistant-Leistungssensor als Quelle fuer `grid_power`, in der UI auf Sensoren mit Einheit `W` gefiltert
- bei Nutzung des Home-Assistant-Leistungssensors wieder zeitbasierte Schaltlogik mit Export-/Import-Delay
- Home-Assistant-Leistungssensor kann jetzt auch manuell per Entity-ID eingetragen werden, falls die Vorschlagsliste leer bleibt
- beim Start werden frueher angelegte MQTT-Discovery-Sensoren des Add-ons aktiv wieder entfernt
- Supervisor-/Home-Assistant-API-Rechte explizit erweitert, damit `SUPERVISOR_TOKEN` fuer den Sensorzugriff verfuuegbar ist

Wenn `STOP Automation` gedrueckt wird, schreibt das Add-on keine weiteren automatischen Moduswechsel mehr, bis die Automatik wieder explizit gestartet wird. Dabei wird auch die interne Eigentuemerschaft `auto_mode_active` geloescht, damit spaetere automatische Rueckstellungen nicht mehr aus altem Zustand heraus passieren.

## Verhalten in v1

- Schaltet auf `minpv`, wenn:
  - das Fahrzeug verbunden ist
  - `grid_power` fuer die konfigurierte Export-Dauer bei oder unter der Export-Schwelle liegt
  - `batterySoc < bufferSoc`
  - kein aktiver Ladeplan vorliegt
  - `evcc` nicht bereits selbst ueber den konfigurierten Mindeststrom hinaus regelt
- Schaltet nur dann wieder auf `pv`, wenn das Add-on `minpv` selbst gesetzt hat und anschliessend mindestens eine dieser Bedingungen greift:
  - `grid_power` liegt fuer die konfigurierte Import-Dauer bei oder ueber der Import-Schwelle
  - `battery_power` liegt fuer die konfigurierte Entlade-Dauer bei oder ueber der Batterientlade-Schwelle

Hinweis:

- Die Batterierueckschaltung folgt dem Vorzeichen der konfigurierten Schwelle.
- Positiver Schwellenwert: Entladung ab `>= Schwelle`
- Negativer Schwellenwert: Entladung ab `<= Schwelle`

## MQTT-Topics

Standardmaessig wird mit Prefix `evcc` gearbeitet. Fuer `loadpoint_id: 1` nutzt das Add-on unter anderem:

- `evcc/site/grid/power`
- `evcc/site/batteryPower`
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
homeassistant_battery_power_sensor_entity_id: ""
export_power_threshold_w: -100
import_power_threshold_w: 100
export_delay_seconds: 60
import_delay_seconds: 30
battery_discharge_power_threshold_w: 200
battery_discharge_delay_seconds: 60
evcc_active_current_threshold: 6.0
auto_reset_on_restart: true
```

Die Grid-Logik folgt dem Vorzeichen der konfigurierten Schwellen:

- positiver Export-Schwellenwert: Einspeisung ab `>= Schwelle`
- negativer Export-Schwellenwert: Einspeisung ab `<= Schwelle`
- positiver Import-Schwellenwert: Netzbezug ab `>= Schwelle`
- negativer Import-Schwellenwert: Netzbezug ab `<= Schwelle`

Wenn `homeassistant_power_sensor_entity_id` gesetzt ist, verwendet das Add-on diesen Home-Assistant-Sensor als Quelle fuer `grid_power`. In der Ingress-UI werden dafuer Sensoren mit Einheit `W` vorgeschlagen. Falls die Vorschlagsliste leer bleibt, kann die Entity-ID auch direkt manuell eingetragen werden. Bleibt das Feld leer, nutzt das Add-on weiter `evcc/site/grid/power` per MQTT.

Wenn `homeassistant_battery_power_sensor_entity_id` gesetzt ist, verwendet das Add-on diesen Home-Assistant-Sensor als Quelle fuer `battery_power`. Falls der Sensor nicht gelesen werden kann, faellt das Add-on fuer die Batterieleistung auf `evcc/site/batteryPower` zurueck.
