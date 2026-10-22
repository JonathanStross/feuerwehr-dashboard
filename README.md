# Feuerwehr Dashboard

Alarm-Monitor für die Wache (Wanddisplay) plus eine passwortgeschützte **Maintenance-Seite**, auf der
(fiktive) Einsätze angelegt, Fahrzeuge zugeordnet, Einsätze zeitgesteuert ausgelöst und Einstellungen
verwaltet werden. Die Maintenance-Seite ist außerdem die Grundlage für den späteren Echtbetrieb
(Leitstellen-Mail → parsen → Dashboard, siehe [Roadmap](#roadmap-leitstellen-mail)).

| Adresse | Zweck |
|---|---|
| `http://<host>:8080` | **Dashboard** – Anzeige (Alarm, Karte, Fahrzeuge, Wetter, Uhr, Historie) |
| `http://<host>:8081` | **Maintenance** – Einsätze anlegen/planen, Vorlagen, Fahrzeuge, Historie, Einstellungen (Passwort) |

## Schnellstart

```bash
cp .env.example .env        # Passwort setzen!
docker compose up -d --build
```

* Dashboard: <http://localhost:8080> · Maintenance: <http://localhost:8081>
* Ohne `.env` gilt das **Standard-Passwort `feuerwehr`** (die Maintenance-Seite zeigt dann einen Warnhinweis).
* Alle Daten liegen in `./data` (wird als Volume eingebunden) → einfach zu sichern.

Logs: `docker compose logs -f` · Stoppen: `docker compose down` · Update: `git pull && docker compose up -d --build`

### Raspberry Pi (WLAN)
* Docker + Compose-Plugin installieren, Repo klonen, wie oben starten. `restart: unless-stopped` startet alles nach einem Neustart automatisch.
* Wanddisplay im Kiosk-Modus (Ton ohne Klick erlauben):
  `chromium-browser --kiosk --autoplay-policy=no-user-gesture-required http://<pi>:8080`
  Ohne dieses Flag blockiert der Browser den Alarmton, bis einmal geklickt wurde – das Dashboard zeigt dann „🔇 Ton freigeben“.
* Der Host sollte eine feste IP bzw. einen festen Hostnamen im WLAN haben.
* Der Link „Dashboard öffnen“ in der Maintenance geht auf Port 8080 desselben Hosts.

## Funktionen

### Dashboard (Port 8080)
* Leerlauf: Zeit seit letztem Alarm, Statistik (heute/24 h/7 Tage), Historie. Aktiv: Stichwort, Zeiten, Infos, Adresse, Karte mit Route, Fahrzeuge mit Live-Status.
* Alarme kommen per WebSocket – die Seite muss nie neu geladen werden (Auto-Reconnect, Heartbeat, Hinweis „Keine Verbindung“). Nach einem Reconnect wird der laufende Alarm nicht erneut „ausgelöst“ (kein doppelter Ton).
* **Wetter** (Open-Meteo, kein API-Key; 10 min Cache, bei Ausfall letzter bekannter Wert) und Uhr in der Kopfzeile.
* **Übung-Badge**, wenn ein Einsatz als fiktiv markiert ist.
* **Hell/Dunkel**: siehe unten.
* Offline-Fallbacks: Leaflet ist lokal eingebunden (`web/vendor`), Schriften sind Systemschriften. Ohne Internet fehlen nur Kartenkacheln und Wetter – die Route (bei fehlendem Routing als gestrichelte **Luftlinie**, mit „≈“) und alle Alarmdaten funktionieren weiter. Abgelaufene Alarme werden auch nach einem Neustart korrekt beendet.

### Maintenance (Port 8081)
* **Einsatz anlegen:** Stichwort, Adresse (Koordinaten per Adresssuche *oder* Kartenklick *oder* manuell), Zusatzinfos, Gültigkeit, Fahrzeuge aus dem Katalog mit Reihenfolge und Startstatus, „Übung“-Kennzeichnung, Alarmton an/aus.
* **Auslösen:** *sofort*, *in X Minuten* oder *um Uhrzeit*. Geplante Einsätze lassen sich unter **Live & Geplant** ansehen, abbrechen oder vorzeitig auslösen. Der Zeitplan ist gespeichert und überlebt Neustarts (ein mehr als 2 min überfälliger Eintrag wird als „verpasst“ markiert, statt viel zu spät auszulösen).
* **Live-Steuerung:** Fahrzeugstatus (alarmiert → rückt aus → auf Anfahrt → vor Ort → einsatzbereit) per Dropdown ändern – erscheint sofort im Dashboard. Einsatz vorzeitig beenden.
* **Vorlagen:** anpassbar (Formular füllen → „Neue Vorlage“ / „Vorlage überschreiben“). Historie-Einträge lassen sich ebenfalls wieder ins Formular laden.
* **Fahrzeugkatalog**, **Historie**, **Einstellungen** (Wache, Koordinaten, Standard-Gültigkeit, Alarmton, Wetter, Darstellung, Passwort ändern).
* Ein neuer Einsatz **überschreibt** immer den aktuellen (es gibt nur eine aktive Alarmierung).

### Hell/Dunkel-Modus
* **Auto** (Standard): hell zwischen Sonnenaufgang und Sonnenuntergang, sonst dunkel. Berechnet im Browser aus den Wache-Koordinaten (offline, kein Internet nötig), Prüfung alle 30 s.
* **Hell / Dunkel**: manuell und dauerhaft. Der Umschalter sitzt oben rechts in der Maintenance (und unter „Einstellungen“). Er gilt sofort auch für das Dashboard und bleibt gespeichert. „Auto“ stellt zurück auf Sonnenstand.
* Die Kartenkacheln werden im Dunkelmodus abgedunkelt.

### Passwortschutz
* Maintenance verlangt ein Passwort. Nach der Anmeldung bleibt ein signiertes Cookie **~10 Jahre** gültig, das Gerät muss sich also nie erneut anmelden (bis das Passwort geändert wird oder man sich abmeldet).
* Passwort: `MAINTENANCE_PASSWORD` (Env/`.env`) oder in den Einstellungen ändern (wird dann gehasht in `data/config.json` gespeichert und hat Vorrang).
* Das Dashboard ist ohne Passwort lesbar (nur für das lokale Netz gedacht, **nicht ins Internet stellen**). Schreibende Dashboard-Endpunkte (`/api/alarm` usw.) verlangen den Header `X-API-Key`; der Schlüssel wird beim ersten Start automatisch in `data/secrets.json` erzeugt und von beiden Containern geteilt.

## Architektur

```
 Browser ──8081──► maintenance (FastAPI) ──HTTP + X-API-Key──► dashboard (FastAPI) ──WebSocket──► Wanddisplay/Browser
                     │  Login, Formular, Zeitplan-Scheduler          │  Routing (OSRM), Wetter, Alarm-Ablauf
                     └──────────── gemeinsames Volume ./data ◄───────┘
```
* **Ein Image, zwei Container** (`docker-compose.yml`). Code: `app/common` (Storage, Modelle, Auth), `app/dashboard`, `app/maintenance`. Frontend: `web/dashboard`, `web/maintenance`, `web/shared` (Theme), `web/sounds`, `web/vendor`.
* Schreibrechte sind getrennt: Dashboard → `state.json`, `history.json`; Maintenance → `config.json` (Einstellungen, Fahrzeuge, Vorlagen, Zeitplan). Das Dashboard bemerkt Änderungen an `config.json` und pusht sie live an alle Anzeigen.
* Alarmtöne: Dateien in `web/sounds/` (mp3/wav/ogg/m4a) erscheinen automatisch in den Einstellungen (nach dem Hinzufügen neu bauen).
* Es ist bewusst **ein Worker pro Dienst** (WebSocket-Verbindungen und Scheduler liegen im Prozess).

## Konfiguration (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `MAINTENANCE_PASSWORD` | `feuerwehr` | Startpasswort der Maintenance-Seite |
| `DASHBOARD_PORT` / `MAINTENANCE_PORT` | `8080` / `8081` | Host-Ports |
| `TZ` | `Europe/Berlin` | Zeitzone |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Routing-Server (der öffentliche Demo-Server ist nur für geringe Last gedacht; eigener OSRM möglich) |
| `OSRM_TIMEOUT` | `8.0` | Timeout in Sekunden, danach Luftlinie |

Alles Weitere (Wache, Koordinaten, Gültigkeit, Ton, Wetter, Theme) wird in der Maintenance-Seite eingestellt.

## API (für Skripte / späteren Mail-Parser)

Schreibzugriffe aufs Dashboard brauchen `X-API-Key: <internal_token aus data/secrets.json>`:

```bash
curl -X POST http://localhost:8080/api/alarm \
  -H "X-API-Key: $(python3 -c 'import json;print(json.load(open("data/secrets.json"))["internal_token"])')" \
  -H "Content-Type: application/json" \
  -d '{"stichwort":"F2 – Test","adresse":"Musterstr. 1","ziel":{"lat":49.69,"lon":8.65},
       "infos":["Rauch"],"fleet":[{"order":1,"name":"TSFW 7-48-1","status":"alarmiert"}],
       "valid_minutes":60,"exercise":true}'
```

| Endpunkt | Zweck |
|---|---|
| `POST /api/alarm` 🔑 | Alarm auslösen (überschreibt den aktuellen) |
| `POST /api/vehicle-status` 🔑 | `{"order":1,"status":"vor Ort"}` |
| `POST /api/expire-now` 🔑 · `DELETE /api/history` 🔑 | Einsatz beenden · Historie löschen |
| `GET /api/state`, `/api/settings`, `/api/weather`, `/history`, `/healthz` | lesend |
| `WS /ws` | Live-Push (`hello`, `alarm`, `fleet`, `idle`, `settings`) |

Die Maintenance-API (`/api/...` auf Port 8081) ist per Session-Cookie geschützt.

## Roadmap Leitstellen-Mail
Der Tab „Leitstellen-Mail“ ist ein Platzhalter. Geplanter Ablauf: IMAP-Abruf → Parser mit konfigurierbarem Feld-Mapping
(Mail-Feld → Stichwort/Adresse/Koordinaten/Infos/Fahrzeuge) → `POST /api/alarm`. Die Einstellungen dafür kommen in
`config.json` und werden in der Maintenance-Seite gepflegt. Testmails lassen sich dort vor dem Livebetrieb prüfen.
Weitere Ideen: automatischer Status-Verlauf der Fahrzeuge, mehrere Anzeigen, eigener OSRM/Kartenkacheln für echten Offlinebetrieb.

## Entwicklung ohne Docker

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export DATA_DIR=./data WEB_DIR=./web DASHBOARD_URL=http://127.0.0.1:8080 MAINTENANCE_PASSWORD=test
uvicorn app.dashboard.main:app --port 8080 &
uvicorn app.maintenance.main:app --port 8081
```

## Lizenzen
Leaflet (BSD-2-Clause, `web/vendor/leaflet/LICENSE`), Kartendaten © OpenStreetMap-Mitwirkende, Wetter: Open-Meteo, Routing: OSRM.
