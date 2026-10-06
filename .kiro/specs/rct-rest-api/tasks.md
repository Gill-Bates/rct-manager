# Implementation Plan: rct-rest-api

## Overview

Der Dienst wird streng von innen nach außen aufgebaut, damit jede Aufgabe gegen etwas
Lauffähiges prüfbar ist: Projektskelett und Konfiguration, dann die I/O-freie
Protokollbibliothek samt Eigenschaftstests, dann Transport, Zugriffssteuerung,
Catalog/Cache/Gateway, Sicherheit, HTTP-Schicht, Metrik_Endpunkt, Diagnosebereich und
zuletzt Auslieferung.

Die Protokollbibliothek steht bewusst **vor** allem anderen und wird mit Hypothesis
geprüft, bevor irgendeine Attrappe entsteht: Eine Attrappe, die nach dem Codec gebaut
wird, teilt dessen Irrtum und beweist nichts. Aus demselben Grund gibt es die
abschließende Aufgabe 16.1, die die im Design als Ableitung oder begründete Annahme
gekennzeichneten Protokollpunkte am echten Gerät verifiziert, bevor der Dienst
produktiv gesetzt wird.

Die schreibenden Endpunkte entstehen erst in Aufgabe 12, nachdem Freigabeliste (8.6),
Wertebereichsprüfung und Commit_Point-Semantik (5.2, 6.5, 6.6) fertig und geprüft
sind. Schreibzugriffe können die Batterie beschädigen, und die Firmware prüft keine
Plausibilität.

Sprache und Werkzeuge: Python 3.13+, asyncio, FastAPI, pydantic v2 und
pydantic-settings, python-dotenv, starlette; `pytest`, `pytest-asyncio`, `hypothesis`, `httpx` und
`ruff` im Extra `dev`. `rct-rest-api/pyproject.toml` ist die einzige Quelle für
Abhängigkeiten und Version; es gibt kein `requirements.txt` und keinen Typprüfer.

Verifikation je Aufgabe, aus `rct-rest-api/`:

```bash
ruff check .            # Konfiguration aus der Repository-Wurzel, line-length 120, py313
pytest                  # Eigenschafts-, Beispiel- und Smoke-Tests
python -m app validate   # nach Abschluss von Aufgabe 8.6: Konfiguration prüfen, ohne Gerät
```

## Tasks

- [x] 1. Projektskelett und Querschnittsmodule
  - [x] 1.1 Projektverzeichnis und `pyproject.toml` anlegen
    - `rct-rest-api/pyproject.toml` mit Paket `app`, Python ≥ 3.13, Laufzeitabhängigkeiten `fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `python-dotenv`, `starlette`, je mit unterer **und** oberer Versionsgrenze, ausschließlich aus dem Paketindex; der Exporter erzeugt das Textformat selbst
    - Extra `dev` mit `pytest`, `pytest-asyncio`, `hypothesis`, `httpx`, `ruff`, je mit unterer und oberer Grenze; kein Typprüfer, kein Zeitreihen-Client, keine Quellcodeverwaltungs-Adresse, kein `requirements.txt`
    - `app/__init__.py`, `run.py` als Shim nach Monorepo-Konvention, `tests/`, `rct-rest-api/.gitignore` mit `settings.env`, `rct-rest-api/.dockerignore`, leere `rct-rest-api/.trivyignore`
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 23.7, 23.8, 23.9, 23.10, 23.11, 26.22, 26.32, 26.33, 28.1, 28.2, 28.3_

  - [x] 1.2 Port `Clock` und `SystemClock` implementieren
    - `app/clock.py`: Protokoll `Clock` mit UTC-`now()`, `monotonic()` und asynchronem `sleep()`, `SystemClock` als Laufzeitimplementierung
    - `tests/conftest.py`: `ManualClock` als Testdoppel, damit Send_Gate, Cache, Budget und Abbaufrist ohne Realzeit prüfbar sind
    - _Requirements: 28.10, 28.19, 24.10_

  - [x] 1.3 Strukturierte Protokollierung aufsetzen
    - `app/logging_setup.py`: Ausgabe auf die Standardausgabe, Format `json` oder `text`, konfigurierbarer Mindestgrad, Feld für die Korrelations_ID vorbereiten
    - _Requirements: 16.14, 16.15, 25.11_

  - [x] 1.4 Interne Fehlerhierarchie anlegen
    - `app/errors.py`: `DeviceApiError` mit Feld `code`, Unterklassen `DeviceUnreachable`, `DeviceTimeout`, `ProtocolError`, `DecodeLengthMismatch`, `WriteOutcomeUnknown`, `ActionOutcomeUnknown`, `DeviceMaintenance`, `QueueFullError`, `QueueTimeout`, `BudgetExhausted`, `ConfigError`
    - Keine Ausnahme trägt einen nach außen gerichteten Text; der Text entsteht später aus dem Fehlerschlüssel
    - _Requirements: 25.3, 25.12, 25.13, 22.4_

- [x] 2. Konfigurationsvertrag
  - [x] 2.1 `Settings` nach dem Konfigurationsvertrag implementieren
    - `app/config.py`: `BaseSettings` mit `settings.env`, Vorrang der Umgebungsvariable, `extra="ignore"`, UTF-8-Dotenv und `enable_decoding=False`; explizite Vorvalidatoren für alle kommaseparierten Listen in Umgebung und Dotenv; je Einstellung des Vertrags genau ein Feld mit Typ, Vorgabewert und Grenzen; nur die Betreiber-Einstellungen (22.1) werden aus Umgebung und Dotenv gelesen, gesetzte Nicht-Betreiber-Einstellungen werden ignoriert und ohne Wert in einer Startwarnung genannt (22.17); `LOG_FORMAT` hat den Vorgabewert `text`
    - `TokenRole`, `LogFormat`, `FreshPeriodicMode`, `StringEncoding` als `StrEnum`; `TokenEntry`, `EndpointKey`, `DeviceKey`, `DeviceEntry`; `DEVICES` in Transport_Endpunkte verdichten und Endpunktkennungen aus der Gerätekennung des unmittelbar angebundenen Geräts ohne Adresse und Port bilden
    - _Requirements: 22.1, 22.2, 22.3, 22.5, 22.6, 22.10, 22.17, 22.18, 7.8, 20.19_

  - [x] 2.2 Querbedingungen und Startabbrüche als Validatoren umsetzen
    - `model_validator` für `MAX_FRESH_METRICS_PER_REQUEST ≤ MAX_METRICS_PER_REQUEST`, `READ_RETRY_BACKOFF_INITIAL_MS ≤ READ_RETRY_BACKOFF_MAX_MS`, `SHUTDOWN_PERIODIC_RESERVE_SECONDS < SHUTDOWN_GRACE_SECONDS`, `HTTP_WORKERS == 1`, Tokenlänge ≥ 32 und mindestens ein Token (außer bei `AUTH_REQUIRED=false`, 12.12), `FORWARDED_HEADER` nur mit `TRUSTED_PROXIES`, nicht-Loopback-Bindeadresse nur mit `BEHIND_REVERSE_PROXY`, höchstens 64 `PERIODIC_METRICS`, eindeutige Gerätekennungen und physische Geräteschlüssel, genau ein unmittelbar angebundenes Gerät je Transport_Endpunkt
    - `CACHE_TTL_SECONDS` und `CACHE_GRACE_SECONDS` bleiben ausdrücklich **unabhängig**; keine Beziehung zwischen ihren Werten prüfen
    - Jede Fehlermeldung nennt Name und zulässigen Bereich; den abgelehnten Wert nur für nicht geheime Einstellungen. Token-Parserfehler und Pydantic-Ausnahmen ohne rohe Eingabewerte ausgeben; Hinweis protokollieren, wenn die Umgebung eine Variable mit Namensanfang `INFLUXDB_` oder `QUESTDB_` führt, ohne den Start abzubrechen
    - _Requirements: 22.4, 22.12, 22.13, 22.14, 22.15, 22.16, 7.4, 12.9, 12.10, 13.12, 14.3, 14.4, 17.4, 21.6, 21.7_

  - [x] 2.3 Einstiegspunkt mit den Modi `serve` und `validate`
    - `app/__main__.py`: `python -m app serve` startet den HTTP-Server, `python -m app validate` prüft Konfiguration, Objekt_Registry und Freigabeliste und beendet sich mit 0 beziehungsweise ungleich 0, ohne ein Gerät zu berühren. Zunächst Einstiegspunkt und Konfigurationsprüfung vorbereiten; vollständiges `validate` erst mit Registry und Freigabeliste aus 8.1 und 8.6 verdrahten
    - Wirksame Konfiguration beim Start protokollieren, Token-Werte durch die Token_Kennung ersetzen
    - _Requirements: 22.7, 22.11, 28.4, 28.5_

  - [x] 2.4 Edge-Case-Tests der Startabbrüche
    - `tests/test_config_validation.py`: je ein Fall pro Startabbruch samt Prüfung, dass die Meldung Name und zulässigen Bereich nennt, nicht geheime Werte nennt und Token-Eingaben auch bei Parsefehlern vollständig verbirgt
    - Gegenbeweis, dass `CACHE_GRACE_SECONDS < CACHE_TTL_SECONDS` **nicht** abbricht
    - _Requirements: 22.4, 22.12, 22.13, 22.14, 22.15, 22.16, 7.4, 12.10, 13.12, 14.3, 17.4_

  - [x] 2.5 `settings.env.example` vollständig führen
    - Je Betreiber-Einstellung des Konfigurationsvertrags (Spalte `Betreiber = ja`) ein Eintrag; Token und Zugangsdaten ausschließlich mit leeren Werten
    - Kein `settings.env` mit echten Werten anlegen
    - _Requirements: 22.8, 22.9, 26.31_

- [x] 3. I/O-freie Protokollbibliothek
  - [x] 3.1 Protokolltypen festlegen
    - `app/protocol/types.py`: `Command` mit allen zwölf Command-Bytes, `PLANT_BIT`, `LONG_COMMANDS` einschließlich `0x06` und `0x46`, `WRITE_COMMANDS`, `START_BYTE`, `STOP_BYTE`, `BOOTLOADER_MAGIC`, `DataType`, `StructKind`, `FrameKind`
    - _Requirements: 1.9, 1.10, 1.11, 4.2, 4.5_

  - [x] 3.2 CRC und Escaping als reine Funktionen
    - `app/protocol/crc.py`: `crc16_ccitt` mit Polynom `0x1021`, Startwert `0xFFFF`, Null-Byte-Auffüllung bei ungerader Länge **innerhalb** der Funktion
    - `app/protocol/escaping.py`: `escape_body`, `unescape_body`; getrennt von der CRC-Berechnung, weil das eingefügte Stop-Byte nicht in die Prüfsumme eingeht
    - _Requirements: 1.3, 1.6, 1.7, 1.8, 1.16_

  - [x] 3.3 Frame kodieren und dekodieren
    - `app/protocol/frames.py`: `Frame`, `encode_frame`, `decode_frame`; gemeinsame Hilfsfunktion `_body_parts()` für Encoder und Decoder, damit Standard_Frame und Plant_Frame in beiden Richtungen identisch behandelt werden
    - Längenfeld 1 Byte, bei Long_Command 2 Byte MSBF; beim Plant_Frame `8 + len(data)` und Adressfeld zwischen Längenfeld und Objekt_ID; CRC-Eingabe je Frame-Art nach Design; unbekanntes Command-Byte verwerfen und protokollieren
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.10, 1.11, 1.12, 1.13, 1.14, 5.1_

  - [x] 3.4 Eigenschaftstest Frame-Round-Trip
    - `tests/test_protocol_properties.py`, mindestens 100 Durchläufe, Kommentar `# Feature: rct-rest-api, Property 1: …`
    - **Property 1: Frame-Round-Trip**
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.9, 1.10, 1.11, 1.15**

  - [x] 3.5 Eigenschaftstests Escaping und CRC
    - `tests/test_protocol_properties.py`, je mindestens 100 Durchläufe
    - **Property 2: Escaping-Round-Trip und Rahmenfreiheit**
    - **Property 3: CRC erkennt jede Einzelbyte-Verfälschung**
    - **Validates: Requirements 1.6, 1.7, 1.8, 1.13, 1.14**

  - [x] 3.6 `StreamParser` inkrementell implementieren
    - `app/protocol/stream.py`: `ParseStats`, `feed`, `reset`, `stats`, `buffered_bytes`, `bootloader_magic_seen`
    - Reassemblierung über Lesevorgänge, mehrere Frames je `feed` in Empfangsreihenfolge, geteilte Escaping-Sequenz, führendes Null-Byte überlesen, Resynchronisation, ungültige Escaping-Sequenz, Höchstgröße `MAX_FRAME_BYTES`, CRC-Fehler ohne Ausnahme nach außen, Bootloader_Magic auf dem rohen Strom
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 1.13, 1.14, 8.11_

  - [x] 3.7 Eigenschaftstest beliebige Stream-Zerlegung
    - `tests/test_stream_properties.py`, mindestens 100 Durchläufe; generierte Frame-Folgen und generierte Schnittpunkte, auch innerhalb von Escaping-Sequenz und Längenfeld
    - **Property 4: Beliebige Stream-Zerlegung ändert das Ergebnis nicht**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.12**

  - [x] 3.8 Eigenschaftstest Resynchronisation
    - `tests/test_stream_properties.py`, mindestens 100 Durchläufe, generierte Störpräfixe
    - **Property 5: Resynchronisation nach Störung**
    - **Validates: Requirements 2.6, 2.7, 2.8**

  - [x] 3.9 Wertkonvertierung implementieren
    - `app/protocol/values.py`: `DEFAULT_WIDTHS`, `decode_value`, `encode_value`; `byte_width` übersteuert die Vorgabebreite; MSBF, Zweierkomplement, IEEE 754 einfacher Genauigkeit, `t_bool`-Abbildung, Zeichenkette bis zum ersten Null-Byte, konfigurierte Zeichenkodierung mit Ersetzung durch `U+FFFD` statt Abbruch, Enum-Rohwert mit optionaler Bezeichnung und nicht endliche Messwerte als `invalid_float`; `DecodeLengthMismatch` mit erwarteter und empfangener Länge
    - _Requirements: 4.7, 4.8, 4.9, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.11, 5.10, 5.12_

  - [x] 3.10 Eigenschaftstests Wert-Round-Trip und Zeichenketten
    - `tests/test_values_properties.py`, je mindestens 100 Durchläufe
    - **Property 17: Wert-Round-Trip über alle Datentypen** (Anteil Skalartypen)
    - **Property 18: Zeichenketten-Dekodierung terminiert für jede Bytefolge**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 5.8, 5.9, 5.13, 5.14**

  - [x] 3.11 Slave_Struktur dekodieren und kodieren
    - `app/protocol/slave_data.py`: `SLAVE_DATA_SIZE = 108`, alle Felder Little-Endian (`<`), `SlaveData` mit allen Offsets nach Design, Offsets 88 bis 107 (20 Byte) reserviert und nicht ausgegeben, `decode_slave_data`, `encode_slave_data` ausschließlich für den Round-Trip-Test
    - _Requirements: 18.2, 18.3, 18.4, 18.5, 18.6, 18.7_

  - [x] 3.12 Eigenschaftstest Slave_Struktur
    - `tests/test_slave_data_properties.py`, mindestens 100 Durchläufe
    - **Property 17: Wert-Round-Trip** (Anteil Slave_Struktur)
    - **Validates: Requirements 18.3, 18.6, 18.16**

  - [x] 3.13 Edge-Case-Tests Höchstgröße und Pufferabbruch
    - `tests/test_stream_properties.py`: überlanger Frame wird verworfen und mit Command-Byte und gemeldeter Länge protokolliert; `buffered_bytes` überschreitet das Doppelte der Höchstgröße
    - _Requirements: 2.9, 2.10, 2.11_

- [x] 4. Checkpoint - Protokollbibliothek ist abgeschlossen
  - Alle bis hier implementierten Pflichtaufgaben und zugehörigen Tests prüfen; Fehler beheben, bevor abhängige Aufgaben beginnen.

- [x] 5. Transportschicht und Eigentumsgrenze
  - [x] 5.1 Zähler je Transport_Endpunkt
    - `app/transport/counters.py`: `EndpointCounters` mit Zeitpunkten, verworfenen Bytes, unerwarteten Frames samt Zeitfenster, Transaktions- und Fehlerzahl
    - _Requirements: 3.12, 24.10_

  - [x] 5.2 `SendGate` mit Mindestpause und Commit_Point
    - `app/transport/send_gate.py`: `SendOutcome`, `SendGate.send()` als **einzige** Aufrufstelle von `writer.write` und `writer.drain`
    - Dreiteiliger Ablauf: vor dem Commit_Point Mindestpause und Zustandsprüfung (`committed=False`), Commit_Point unmittelbar vor `write()` ohne `await` dazwischen, danach `drain()` ohne Änderung von `committed`
    - Uhr über den Port `Clock` injiziert; Mindestpause gilt auch über einen Verbindungsneuaufbau
    - _Requirements: 6.8, 9.2, 9.5, 24.5, 24.6, 24.7, 24.8, 24.10, 24.11_

  - [x] 5.3 Eigenschaftstest Mindestpause
    - `tests/test_send_gate_properties.py`, mindestens 100 Durchläufe, `ManualClock`, Attrappen-Writer, generierte Mischung aus Anfragen, Wiederholversuchen, Heartbeats, Periodik-Anmeldungen, System_Schreibzugriffen und Abbau-Transaktionen
    - **Property 7: Mindestpause am Send_Gate**
    - **Validates: Requirements 6.8, 24.6, 24.7, 24.8, 24.10, 24.12**

  - [x] 5.4 Demultiplexer implementieren
    - `app/transport/demux.py`: `PendingTransaction`, `classify`, `dispatch` in der normativen Reihenfolge Transaktionsantwort, Transaktionsantwort mit zusätzlicher Cache-Ablage, Periodischer_Wert, Unerwarteter_Frame
    - Zuordnung ausschließlich aus Objekt_ID, Netzkennung und zeitlicher Lage; keine Transaktionskennung voraussetzen
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

  - [x] 5.5 Eigenschaftstest Frame_Klassen-Zuordnung
    - `tests/test_stream_properties.py`, mindestens 100 Durchläufe, generierte Periodik-Menge und wahlweise laufende Transaktion
    - **Property 6: Frame_Klassen-Zuordnung ist eindeutig und vollständig**
    - **Validates: Requirements 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

  - [x] 5.6 Empfangspfad implementieren
    - `app/transport/receiver.py`: dauerhaft laufender Task je Verbindung, alleiniger Aufrufer von `reader.read()` und alleiniger Nutzer des `StreamParser` dieser Verbindung
    - Kein pauschaler Puffer-Drain; Verwerfen ausschließlich bei Resynchronisation nach Protokollfehler oder nach Verbindungsneuaufbau; Eskalation bei Überschreiten von `UNEXPECTED_FRAME_LIMIT` im Zeitfenster
    - _Requirements: 3.1, 3.9, 3.10, 3.11, 3.12, 29.4, 29.5, 29.7_

  - [x] 5.7 `TransportEndpoint` als Kommunikationsinstanz
    - `app/transport/endpoint.py`: `EndpointState`, `LockReason`, `start`, `close`, `execute`, `register_periodic`, `unregister_all_periodic`, `status`, `maintenance`
    - `execute()` ist die einzige Stelle, die Gerätelast erzeugt; `asyncio.open_connection` wird ausschließlich hier aufgerufen; `TCP_NODELAY` und `SO_KEEPALIVE` auf dem rohen Socket; zweiter Verbindungsaufbau wird unterlassen und protokolliert
    - _Requirements: 8.9, 8.10, 24.1, 24.2, 24.3, 24.4, 24.9, 30.16_

  - [x] 5.8 Eigenschaftstest Verbindungs- und Transaktionsexklusivität
    - `tests/test_send_gate_properties.py`, mindestens 100 Durchläufe, generierte Folgen aus gleichzeitigen Anfragen, Abbrüchen und Neuaufbauten
    - **Property 8: Höchstens eine Verbindung und höchstens eine Transaktion je Endpunkt**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.9, 24.1, 24.2, 24.3, 24.13**

  - [x] 5.9 Sperrzustand und Wartungszustand
    - `app/transport/endpoint.py`: Bootloader_Magic versetzt den Endpunkt unverzüglich in den Sperrzustand, Abkühlzeit `BOOTLOADER_COOLDOWN_SECONDS`, danach genau **eine** einzelne Lesetransaktion zur Prüfung; Ereignis mit Gerätekennung und Zeitpunkt protokollieren
    - `maintenance()` als herstellerneutrale Projektion; `state` und `lock_reason` bleiben dem Diagnosebereich vorbehalten
    - _Requirements: 8.11, 8.12, 8.13, 8.14, 8.15, 16.12, 30.10, 30.20_

- [x] 6. Zugriffssteuerung
  - [x] 6.1 Zugriffsserialisierer mit Warteschlange und Worker
    - `app/scheduling/serializer.py`: `TransactionOrigin`, `TransactionRequest`, `AccessSerializer` mit genau einem Worker-Task, begrenzter `asyncio.Queue` und `asyncio.Future` je Transaktion
    - FIFO je Endpunkt, Endpunkte unabhängig, `queue_full` bei Überlauf samt `Retry-After`, Höchstwartezeit nur bis zum Startsignal des Workers über `asyncio.wait_for(asyncio.shield(started), ...)` mit Markierung `abandoned`; Ausführungsfristen anschließend getrennt behandeln, Freigabe im `try/finally`, breite Ausnahmebehandlung ausschließlich an dieser Außengrenze
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.9_

  - [x] 6.2 Arbeitsbudget je Transport_Endpunkt
    - `app/scheduling/budget.py`: `WorkBudget` mit gleitendem Zeitfenster, `try_consume`, `remaining`, `retry_after`; Prüfung **vor** dem Einstellen in die Warteschlange; Sammelanfrage mit `fresh` prüft einmal für alle Namen; Heartbeat, System_Schreibzugriffe und Abbau zählen nicht mit
    - _Requirements: 6.10, 6.11, 6.12, 10.22_

  - [x] 6.3 Einzelflug je Gerät und Messwert
    - `app/scheduling/singleflight.py`: `SingleFlight.run` mit `asyncio.shield`, Weitergabe der Ausnahme an alle Warter, gemeinsamer Task auch für den ersten Aufrufer mit `shield`, Entfernen des Eintrags ausschließlich beim Task-Abschluss, Ausnahme abrufen auch wenn alle Warter abbrechen
    - Greift ausschließlich für Lesezugriffe **ohne** `fresh`; erneute Cache-Prüfung unmittelbar vor der Übergabe an das Send_Gate über `recheck_cache`
    - _Requirements: 15.12, 15.13, 15.14, 15.16_

  - [x] 6.4 Eigenschaftstest Einzelflug
    - `tests/test_singleflight_properties.py`, mindestens 100 Durchläufe, `asyncio.gather` mit generiertem N, optionaler periodischer Wert während des Fluges, Abbruch des ersten und weiterer Warter, zählender Attrappen-Transport
    - **Property 15: Einzelflug — N gleichzeitige Anfragen teilen genau einen Lesevorgang**
    - **Validates: Requirements 15.12, 15.13, 15.14, 15.15**

  - [x] 6.5 Wiederholungsregeln lesend und schreibend getrennt
    - `app/scheduling/retry.py`: lesend Erstversuch plus `READ_RETRIES` mit exponentiellem Backoff und drei gleichzeitig wirkenden Zeitgrenzwerten; schreibend ausschließlich nach `SendOutcome.committed`, nicht nach Ausnahmetyp
    - Ab dem Commit_Point niemals ein zweiter Frame mit einem Command-Byte aus `WRITE_COMMANDS` auf dieselbe Objekt_ID; Aktionsvariable und nicht idempotente Objekt_ID auch **vor** dem Commit_Point nie automatisch wiederholen; Ausgang ausschließlich über eine Lesetransaktion feststellen; Cache-Eintrag nach dem Schreiben verwerfen und nur durch die Lesetransaktion neu setzen
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 9.1, 9.3, 9.4, 9.5, 9.6, 9.9, 9.10, 9.12, 9.13_

  - [x] 6.6 Eigenschaftstest genau ein WRITE-Frame ab dem Commit_Point
    - `tests/test_write_commit_properties.py`, mindestens 100 Durchläufe, Attrappen-Writer mit generierter Fehlerposition, Zähler je Objekt_ID über `WRITE_COMMANDS`, generierte Belegungen von Idempotenz- und Aktionskennzeichnung
    - **Property 11: Genau ein WRITE-Frame je Schreibvorgang ab dem Commit_Point**
    - **Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.9, 9.10, 9.17, 24.11**

  - [x] 6.7 Heartbeat-Task
    - `app/scheduling/heartbeat.py`: je Gerät eine Lesetransaktion auf `HEARTBEAT_METRIC_NAME` im Intervall; überspringen, wenn im Intervall eine erfolgreiche Transaktion stattfand **oder** mindestens ein Frame der Klasse `Periodischer_Wert` eintraf; im zweiten Fall `liveness_source = periodic`
    - _Requirements: 16.4, 16.5, 16.6, 16.7_

  - [x] 6.8 Periodik-Verwaltung
    - `app/scheduling/periodic.py`: `PAS_PERIOD_OBJECT_ID`, im Code fest verankerte `SYSTEM_WRITABLE_OBJECT_IDS`, `MAX_PERIODIC_PER_DEVICE = 64`
    - Erst `pas.period` als `t_uint32` beschreiben, dann je Messwert eine Anforderung mit `0x08`/`0x48`; genau eine Anforderung je Verbindung und Objekt_ID; Fehlschlag einer einzelnen Anmeldung wird protokolliert und erst nach dem nächsten Verbindungsneuaufbau wiederholt (17.20); erneutes Setzen nach Verbindungsneuaufbau; Fehlschlag macht die Periodik für dieses Gerät nicht verfügbar, ohne die lesenden Transaktionen zu stören; `pas.period` folgt derselben Commit_Point-Regel
    - _Requirements: 17.1, 17.2, 17.3, 17.5, 17.6, 17.8, 17.9, 17.10, 17.11, 17.12, 17.13_

  - [x] 6.9 Abbauphasen implementieren
    - `app/scheduling/shutdown.py`: `ShutdownPhase`, `ShutdownPlan` mit `deadline` und `work_deadline`; vier Phasen `Annahmestopp`, `Restarbeit`, `Periodikabbau`, `Abschluss` in dieser Reihenfolge und ohne Rücksprung
    - Abbaufrist als echte Gesamtdeadline, Abbaureserve schneidet das Fenster des Periodikabbaus vom Ende ab; `pas.period = 0` über die Kommunikationsinstanz und das Send_Gate; Auslassen samt Protokollierung, wenn die Deadline beim Phasenbeginn erreicht ist; Rückgabewert stets 0
    - _Requirements: 17.7, 27.1, 27.2, 27.3, 27.4, 27.5, 27.6, 27.11, 27.12, 27.13, 27.14, 27.15, 27.16, 27.17, 27.18, 27.19_

  - [x] 6.10 Eigenschaftstest Abbaufrist als Gesamtdeadline
    - `tests/test_shutdown_properties.py`, mindestens 100 Durchläufe, `ManualClock`, Attrappen-Transport mit generierten Antwortverzögerungen einschließlich „nie“, generierte Warteschlangenfüllungen und Periodik-Anzahlen
    - **Property 16: Abbaufrist ist eine echte Gesamtdeadline**
    - **Validates: Requirements 27.3, 27.6, 27.12, 27.17, 27.19, 27.20**

- [x] 7. Checkpoint - Transport und Zugriffssteuerung sind abgeschlossen
  - Alle bis hier implementierten Pflichtaufgaben und zugehörigen Tests prüfen; Fehler beheben, bevor abhängige Aufgaben beginnen.

- [x] 8. Catalog, Cache, Freigabeliste und Geräteadapter
  - [x] 8.1 Port `MetricCatalog` und `RegistryCatalog`
    - `app/catalog/base.py` mit `describe`, `names`, `preselected`, `exists` und neutralen Deskriptoren; `app/catalog/registry.py` mit `RegistryEntry`, `ObjectRegistry`, Indizes Name → Eintrag und Objekt_ID → Eintrag sowie der adapterseitigen Zusatzmethode `object_entry`
    - `rct-rest-api/objects_read.json` als Datei neben dem Code anlegen, mit `net.slave_data` als `t_struct`/`slave_data` und `com_service` als nicht idempotente Aktionsvariable
    - Startprüfungen: Pflichtfelder, doppelte Namen und Objekt_IDs, unbekannter Datentyp, `t_struct` ohne oder mit unbekannter Strukturkennung, unzulässige `byte_width`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.10, 4.11, 4.12, 4.13, 4.14, 4.15, 4.17, 4.18, 4.19, 4.20, 4.21, 10.4_

  - [x] 8.2 Port `ValueStore` und `MemoryCache`
    - `app/cache.py`: Schlüssel `(Gerätekennung, Messwertname)`, Wert mit Messzeitpunkt und Herkunft `transaction` oder `periodic`; `classify` mit `FRESH`, `GRACE`, `EXPIRED`
    - Nachfrist ab dem **Ende** der Gültigkeitsdauer und unabhängig von deren Länge; Alter stets aus monotoner Empfangszeit und aktueller monotoner Zeit; UTC separat für Ausgabe; periodische Werte gleichberechtigt; `invalidate` für den Schreibpfad
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.10, 15.11, 17.13, 9.12, 9.13_

  - [x] 8.3 Eigenschaftstest Cache-Konsistenz
    - `tests/test_values_properties.py`, mindestens 100 Durchläufe, `ManualClock`, generierte Alter, TTL und Nachfrist einschließlich `grace < ttl` und Systemzeitsprüngen
    - **Property 14: Cache-Felder sind untereinander konsistent**
    - **Validates: Requirements 10.11, 10.12, 15.2, 15.4, 15.6, 15.7, 15.8, 15.9, 15.11**

  - [x] 8.4 Port `DeviceGateway` mit neutralen DTOs
    - `app/gateway/base.py`: `MetricReading`, `WriteOutcome`, `ActionOutcome`, `DeviceStatus` ohne Frame-Zähler, Sperrursache, Objekt_ID und Netzkennung; `read_metric`, `write_metric`, `trigger_action`, `device_status`
    - _Requirements: 16.17, 30.17, 30.19, 30.21_

  - [x] 8.5 Adapter `RctGateway`
    - `app/gateway/rct.py`: löst Messwertnamen über `MetricCatalog` in Objekt_IDs auf, baut Frames, führt den Einzelflug, reicht an den Zugriffsserialisierer des zuständigen Endpunkts und wandelt das Ergebnis in `MetricReading`
    - Beobachtete_Frische: Sendezeitpunkt merken, ersten danach eintreffenden passenden Frame annehmen, `freshness = observed` und `source = device`; `FRESH_PERIODIC_MODE = reject` als Alternative
    - _Requirements: 10.19, 15.6, 15.7, 15.8, 15.9, 17.14, 17.15, 17.16, 17.17, 17.18, 17.19, 30.17_

  - [x] 8.6 Freigabeliste laden und prüfen
    - `app/allowlist.py` mit `AllowlistEntry`; `rct-rest-api/objects_write_allowed.json` mit allen 894 skalaren Schreibfreigaben und Datentypgrenzen außerhalb des Codes; vollständigen Prüfmodus aus 2.3 verdrahten
    - Startabbruch bei fehlendem Datentyp oder Wertebereich, bei einem Messwert außerhalb der Objekt_Registry, bei abweichendem Datentyp ; Protokoll_Steuervariablen explizit zulassen; für Aktionsvariablen zulässige Werte einzeln aufzählen, für `t_enum` auch Rohwertebereiche zulassen
    - _Requirements: 19.4, 19.15, 19.16, 19.17, 19.18, 19.19, 19.20, 19.23, 19.25_

- [x] 9. Sicherheit
  - [x] 9.1 Token_Verwaltung
    - `app/security/tokens.py`: Token als `token:rolle`, Mindestlänge 32, mindestens ein konfiguriertes Token (außer bei der ausdrücklichen Abschaltung `AUTH_REQUIRED=false` mit Startwarnung; anonyme Anfragen erhalten dann `read/write`, Schreiben bleibt an Schreibfreigabe und Freigabeliste gebunden, 12.12) und Unterstützung mehrerer gleichzeitig gültiger Token zur Rotation, fehlende Rolle wird `read`, Vergleich über `hmac.compare_digest` gegen **jeden** Token ohne vorzeitigen Abbruch, Token_Kennung als Präfix eines SHA-256-Hexwerts
    - _Requirements: 12.4, 12.5, 12.6, 12.8, 12.9, 12.11_

  - [x] 9.2 Quell-IP-Adresse und Vertrauenslisten
    - `app/security/client_ip.py`: im Vorgabezustand ausschließlich die Peer_Adresse; Weiterleitungs-Header nur bei Peer_Adresse in der Vertrauensliste; bei mehreren Adressen die letzte außerhalb der Vertrauensliste; bei unbestimmbarer Adresse die strengste Grenze
    - _Requirements: 13.8, 13.9, 13.10, 13.11_

  - [x] 9.3 Ratenbegrenzung mit drei getrennten Zählern
    - `app/security/ratelimit.py`: eigener Gleitfenster-Begrenzer (Aufrufer = Token_Kennung plus Quell-IP-Adresse, kein SlowAPI) mit begrenzter Schlüsseltabelle; fachliche Anfragen je Aufrufer unabhängig vom Statuscode, fehlgeschlagene Authentifizierung je Quell-IP mit Sperrdauer, Scrapes in einem eigenen Zähler, der nicht im fachlichen Zähler mitzählt
    - Abgewiesene Anfrage löst keine Transaktion aus; nicht lesbarer Zustand führt zur Ablehnung, nicht zur Freigabe
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 20.31, 20.32, 11.8_

  - [x] 9.4 Autorisierungs-Dependencies
    - `app/security/dependencies.py`: `require_read`, `require_write`, `require_vendor`; `require_write` prüft in dieser Reihenfolge Schreibfreigabe, Token_Rolle, Freigabeliste, Aktionsvariable am richtigen Endpunkt, Wertebereich; deny by default, kein `except Exception: pass` in diesen Pfaden
    - _Requirements: 12.1, 12.2, 12.3, 12.7, 19.3, 30.6_

  - [x] 9.5 Strukturelle Smoke-Tests der Grenzen
    - Verwendung von `hmac.compare_digest` ohne vorzeitigen Abbruch; Abwesenheit eines pauschalen Puffer-Drains; `asyncio.open_connection` nur in `app/transport/endpoint.py`; kein Import von `app/api/models_vendor.py` in `app/api/models.py`; kein Import von `app/gateway/rct.py` in `app/api/routers/`; Socket-Optionen gegen einen lokalen Lauschsocket
    - _Requirements: 12.4, 3.9, 3.10, 8.9, 8.10, 24.3, 30.12, 30.17_

- [x] 10. HTTP-Schicht und lesende Endpunkte
  - [x] 10.1 Herstellerneutrale Antwortmodelle
    - `app/api/models.py`: `NeutralValueType`, `DeviceState` mit `maintenance`, `MetricDescriptor`, `DeviceDescriptor`, `MetricValue`, `MetricError`, `MetricCollection`, `WriteResult`, `ActionResult` mit `action_confirmed: Literal[False]`, `DeviceReadiness`, `ReadinessResponse`, `StaleReason`
    - Kein Feld für Objekt_ID, Netzkennung, Transportadresse, Transportport, Protokoll-Datentyp, Frame-Zähler, Sperrzustand oder Periodik-Anzahl
    - _Requirements: 10.3, 10.4, 10.11, 10.12, 10.18, 16.11, 16.17, 9.7, 9.14, 9.15, 30.1, 30.19, 30.20, 30.21_

  - [x] 10.2 Problem_Details nach RFC 9457
    - `app/api/problems.py`: `ProblemDetails`, `FieldError`, `MetricError`, `ErrorCode` als `StrEnum` mit genau den Schlüsseln der Tabelle plus `invalid_float` und `decode_length_mismatch` für das Feld `errors`; Zuordnung Schlüssel → Statuscode als Mapping, `status` aus Schlüssel und Kontext (`unknown_metric`: Pfad 404, Query 422); `errors` statusabhängig als Eingabe- oder Messwertfehler, `readback_value` bei unklarem Schreib-/Aktionsausgang
    - `type` aus `PROBLEM_TYPE_BASE_URI` und Fehlerschlüssel; Auslassungsregeln; bei 500 gleichbleibender Text und vollständiger Verlauf im Protokoll; globaler Exception-Handler, damit auch Middleware- und Validierungsfehler als Problem_Details enden
    - _Requirements: 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.7, 25.12, 25.13, 25.14, 25.15, 25.16, 25.17, 25.18, 25.19, 25.20, 25.23, 25.24, 25.25_

  - [x] 10.3 Middleware für Korrelations_ID und Cache-Control
    - `app/api/middleware.py`: Korrelations_ID aus `CORRELATION_ID_HEADER` übernehmen, wenn sie `[A-Za-z0-9_-]` und höchstens 64 Zeichen erfüllt, sonst selbst erzeugen; in jeder Antwort als Header und in jeder zugehörigen Protokollausgabe; `Cache-Control: no-store`; Anfrageprotokollierung mit Methode, Pfad, Statuscode, Dauer und Token_Kennung
    - _Requirements: 14.8, 16.14, 25.8, 25.9, 25.10, 25.11_

  - [x] 10.4 `create_app()` und Lifespan mit Port-Bindung
    - `app/api/app_factory.py`: Startreihenfolge nach Design — Konfiguration, Objekt_Registry samt Metriknamen, Freigabeliste, Token, Transport_Endpunkte und Gerätegruppen, Port-Bindung, Endpunkt- und Worker-Tasks, nebenläufiger und nicht blockierender Verbindungsaufbau, Heartbeat, Periodik
    - Dienst startet auch bei nicht erreichbarem Gerät; Signalbehandlung für `SIGTERM` und `SIGINT` vor Uvicorns Listener-Abbau koordinieren; lokaler Prozess-Smoke-Test für erreichbare 503 während des Abbaus und fristgerechtes Beenden; Prüfkette der Dependencies in der normativen Reihenfolge verdrahten
    - _Requirements: 7.7, 7.8, 10.13, 10.24, 16.13, 19.10, 19.21, 27.1, 27.2, 27.7, 30.15_

  - [x] 10.5 Health- und Bereitschafts-Endpunkt
    - `app/api/routers/health.py`: `/health` ohne Token, 200 solange der Server annimmt, 503 ab der Phase `Annahmestopp`; `/api/v1/readiness` mit Gerätezuständen, 503 als Problem_Details mit dem Erweiterungsfeld `devices`, `starting` vor dem ersten Heartbeat, `foreign_access_suspected`
    - _Requirements: 16.1, 16.2, 16.3, 16.8, 16.9, 16.10, 16.11, 16.12, 16.13, 16.16, 16.17, 27.9, 27.10, 29.6_

  - [x] 10.6 Katalog-Endpunkte
    - `app/api/routers/catalog.py`: `GET /api/v1/metrics` mit Namen, Einheit, neutralem Werttyp, Schreibbarkeit und Vorauswahl; `GET /api/v1/devices` mit Gerätekennung, Anzeigename, Rolle und Bereitschaft
    - _Requirements: 10.1, 10.2, 10.4, 10.5, 10.23_

  - [x] 10.7 Lesende Messwert-Endpunkte
    - `app/api/routers/values.py`: Einzelwert und Sammelabfrage über `names`, Vorauswahl ohne `names`, `MAX_METRICS_PER_REQUEST` und `MAX_FRESH_METRICS_PER_REQUEST`, Teilerfolg mit 200 und Feld `errors`, 502 `device_unavailable` ohne einen einzigen Wert, 404 bei unbekannter Gerätekennung, 422 bei unbekanntem Namen im Abfrageparameter
    - Sammelabruf sequenziell über die Warteschlange des Endpunkts, nicht über `asyncio.gather`; alle Prüfungen vor jeder Transaktion
    - _Requirements: 10.6, 10.7, 10.8, 10.9, 10.10, 10.13, 10.14, 10.15, 10.16, 10.17, 10.19, 10.20, 10.21, 10.22, 4.16, 15.2, 15.6, 15.7_

  - [x] 10.8 Eigenschaftstests Vertragsgrenze und Fehlervertrag
    - `tests/test_contract_properties.py`, je mindestens 100 Durchläufe, generierte Anfragen einschließlich unbekannter Pfade, fehlerhafter Rümpfe und unzulässiger Methoden
    - **Property 12: Der herstellerneutrale Vertrag gibt keine Protokolldetails aus**
    - **Property 13: Jede Fehlerantwort erfüllt den Fehlervertrag**
    - **Validates: Requirements 8.15, 10.3, 10.4, 16.11, 16.12, 16.17, 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.10, 25.12, 25.13, 25.18, 25.20, 25.22, 30.1, 30.2, 30.17, 30.19, 30.20, 30.21**

  - [x] 10.9 Eigenschaftstest Lastfreiheit abgewiesener Anfragen
    - `tests/test_contract_properties.py`, mindestens 100 Durchläufe, `TestClient` plus zählender Attrappen-Transport
    - **Property 9: Lastfreie Endpunkte und abgewiesene Anfragen erzeugen keine Gerätelast**
    - **Validates: Requirements 8.12, 10.13, 10.24, 12.7, 13.7, 17.17, 20.2, 20.3, 20.4, 20.5, 27.8, 30.15, 30.22**

  - [x] 10.10 Beispiel- und Edge-Case-Tests der HTTP-Schicht
    - `tests/test_api_examples.py`: unbekannter Pfad/405 mit `Allow`, ungültiger JSON-Körper, 401 mit `WWW-Authenticate`, deaktivierte PUT-/POST-Pfade, `errors`-Schema bei vollständigem Batchfehlschlag, Teilerfolg, Ersatzantwort innerhalb der Nachfrist, Readiness-503 als Problem_Details mit `devices`, Verhalten der vier Abbauphasen gegenüber Health, Readiness, Metrik_Endpunkt und einer fachlichen Anfrage, Schwellen- und Fristverhalten
    - _Requirements: 15.6, 16.8, 16.9, 25.19, 27.7, 27.9, 27.10, 6.5, 6.6, 6.11, 29.4, 29.7_

  - [x] 10.11 Dokumentations_Endpunkte und OpenAPI-Sicherheitsschema
    - `app/api/app_factory.py`: OpenAPI mit Bearer-Sicherheitsschema und Zuordnung jedes fachlichen Endpunkts; tokenfreie Auslieferung nur bei Loopback-Bindeadresse oder `DOCS_PUBLIC`, sonst 404 `docs_not_available`; keine Token-Werte, Gerätadressen oder Wertebereiche offenlegen; `fresh` in der Beschreibung als zeitlich bestimmte Frische benennen
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 17.19, 25.21, 30.13_

- [x] 11. Checkpoint - lesender Vertrag ist vollständig
  - Alle bis hier implementierten Pflichtaufgaben und zugehörigen Tests prüfen; Fehler beheben, bevor abhängige Aufgaben beginnen.

- [x] 12. Schreibende Endpunkte freischalten
  - [x] 12.1 `PUT`-Endpunkt für Messwerte
    - `app/api/routers/writes.py`: Router nur bei aktiver Schreibfreigabe registrieren, sonst pfad- und methodengenau über die HTTP-Fehlerabbildung 404 `write_disabled` einschließlich des sonst möglichen Framework-405; Token_Rolle `read/write`; Freigabeliste und Wertebereich **vor** Inanspruchnahme des Zugriffsserialisierers prüfen; 403, 404, 409 und 422 lösen keine Transaktion aus; Vorgang mit Token_Kennung, Gerätekennung, Messwertnamen und Wert protokollieren
    - Antwort mit `written_value`, `readback_value`, `confirmed` und `send_unconfirmed`; bei abweichendem oder gescheitertem Read-back 502 `write_outcome_unknown`
    - _Requirements: 19.1, 19.2, 19.3, 19.5, 19.6, 19.10, 19.11, 19.12, 19.13, 19.14, 19.21, 19.22, 9.7, 9.8_

  - [x] 12.2 `POST`-Aktionsendpunkt
    - `app/api/routers/writes.py`: `POST /api/v1/devices/{device_id}/actions/{action_name}` ausschließlich für Aktionsvariablen mit einzeln aufgezählten zulässigen Werten; `action_confirmed = false` und `action_note`; 502 `action_outcome_unknown` bei unklarem Ausgang; Protokollierung vor dem Senden und nach dem Ergebnis
    - _Requirements: 19.7, 19.8, 19.9, 9.10, 9.11, 9.14, 9.15, 9.16_

  - [x] 12.3 Eigenschaftstest Wertprüfung ohne Schreibtransaktion
    - `tests/test_contract_properties.py`, mindestens 100 Durchläufe, generierte Freigabeliste und verletzende Werte
    - **Property 10: Ein unzulässiger Schreibwert erzeugt keine Schreibtransaktion**
    - **Validates: Requirements 19.10, 19.11, 19.12, 19.13, 19.14, 19.21, 19.24**

  - [x] 12.4 Beispieltests der Schreibpfade
    - `tests/test_api_examples.py`: Read-back bestätigt, Read-back abweichend, Read-back gescheitert, Aktionsantwort mit `action_confirmed = false`, 502 mit `readback_value` beziehungsweise `null`, Cache-Verwerfen nach einem Schreibvorgang und Neusetzen durch die Lesetransaktion
    - _Requirements: 9.7, 9.8, 9.11, 9.12, 9.13, 9.14, 9.15, 9.16_

- [x] 13. Metrik_Endpunkt
  - [x] 13.1 Metriknamen bilden und beim Start prüfen
    - `app/observability/names.py`: `prometheus_name` hat Vorrang, sonst `rct_` plus normalisierter Name plus Basiseinheit; Normalisierung nach Design; Kollisionsprüfung und Schemaprüfung **beim Start** mit Nennung beider Messwertnamen und des gemeinsamen Metriknamens
    - _Requirements: 20.10, 20.11, 20.12, 20.13, 20.14, 4.15_

  - [x] 13.2 Exporter als reine Projektion
    - `app/observability/exporter.py`: bekommt ausschließlich `ValueStore` und `EndpointCounters` injiziert, kein Verweis auf `DeviceGateway` oder `AccessSerializer`
    - Dienst- und Messwertmetriken nach Tabelle des Designs; Trennung `rct_transport_` mit Label `endpoint` gegen gerätebezogene Metriken mit Label `device`; nur Labelnamen `device`, `endpoint`, `metric` und `le`, letzteres nur an `_bucket`; Auslassungsregel ohne `0` und ohne `NaN`, auch für den nach einem Schreibvorgang verworfenen Eintrag; `rct_device_metric_age_seconds` innerhalb der Nachfrist
    - _Requirements: 20.2, 20.3, 20.4, 20.6, 20.7, 20.8, 20.9, 20.15, 20.16, 20.17, 20.18, 20.19, 20.20, 20.21, 20.22, 20.23, 20.24, 20.25, 20.26, 20.27, 20.33, 20.34, 29.8_

  - [x] 13.3 Router `/metrics`
    - `app/api/routers/metrics.py`: nur bei `ENABLE_METRICS_ENDPOINT`; Token nach `METRICS_REQUIRE_TOKEN`, Ausnahme ausschließlich für die Scrape_Vertrauensliste; eigener Ratenzähler; `Cache-Control: no-store`; Pfad `/metrics` bleibt vom fachlichen `GET /api/v1/metrics` getrennt
    - _Requirements: 20.1, 20.28, 20.29, 20.30, 20.31, 20.32, 20.35_

  - [x] 13.4 Eigenschaftstest Metriknamen und Labels
    - `tests/test_metrics_endpoint.py`, mindestens 100 Durchläufe, generierte Cache- und Zählerzustände, Parser des Textformats
    - **Property 19: Metriknamen und Labels trennen Gerät und Transport_Endpunkt**
    - **Validates: Requirements 20.7, 20.8, 20.9, 20.15, 20.16, 20.17, 20.18, 20.19, 20.21, 20.22, 20.23, 20.24, 20.25, 20.27, 29.8**

- [x] 14. Diagnosebereich
  - [x] 14.1 Modelle des Diagnosebereichs
    - `app/api/models_vendor.py`: `VendorObjectDescriptor`, `VendorTransportDescriptor`, `VendorSlaveDescriptor` mit zerlegter Ausstattungsangabe, `VendorSlaveCollection`; getrennte Datei, damit ein Verweis aus `models.py` sofort auffällt
    - _Requirements: 18.14, 30.7, 30.8, 30.9, 30.10_

  - [x] 14.2 Router für Objekte und Transporte
    - `app/api/routers/vendor.py`: nur bei `ENABLE_VENDOR_DIAGNOSTICS` registrieren, sonst 404; Token_Rolle `read/write`; `GET /api/v1/vendor/rct/objects` und `GET /api/v1/vendor/rct/transports` mit Endpunktkennung, Adresse, Port, Netzkennungen, Frame-Zählern, Sperrzustand samt Ursache und Periodik-Status; keine Token-Werte
    - _Requirements: 17.20, 30.3, 30.4, 30.5, 30.6, 30.7, 30.8, 30.9, 30.10, 30.12, 30.14_

  - [x] 14.3 Slave-Erfassung im Diagnosebereich
    - `app/api/routers/vendor.py`: `GET /api/v1/vendor/rct/devices/{device_id}/slaves`; wiederholte Lesetransaktionen auf `net.slave_data` über den Zugriffsserialisierer samt Mindestpause; Abbruch bei `SLAVE_DISCOVERY_STABLE_READS`, 31 Netzkennungen oder `SLAVE_DISCOVERY_MAX_READS`; gescheiterter Abruf ergibt 200 mit `complete = false`; eigene Gültigkeitsdauer `SLAVE_CACHE_TTL_SECONDS`
    - _Requirements: 18.1, 18.8, 18.9, 18.10, 18.11, 18.12, 18.13, 18.15, 30.11_

  - [x] 14.4 Statische Prüfungen der Projektartefakte
    - `pyproject.toml` auf Versionsgrenzen, fehlenden Typprüfer, fehlenden Zeitreihen-Client und fehlende Quellcodeverwaltungs-Adresse; `docker/Dockerfile` und `compose.yaml` auf die Vorgaben des Designs; jede spätere `.trivyignore`-Ausnahme mit Begründung und Ablaufdatum
    - _Requirements: 21.2, 21.3, 21.4, 21.5, 23.3, 23.4, 23.5, 23.6, 23.7, 23.8, 23.11, 26.34_

- [x] 15. Container, Compose und Projektdokumentation
  - [x] 15.1 `docker/Dockerfile` schreiben
    - Mehrstufig, `# syntax=docker/dockerfile:1`, `python:3.13-slim` mit fixierter Minor-Version und Digest, gleiche Minor-Version in beiden Stages, `/opt/venv`, `pip` in der Runtime entfernen, `PYTHONDONTWRITEBYTECODE` und `PYTHONUNBUFFERED`, Exec-Form, BuildKit-Cache-Mounts, `USER 10001:10001`, nicht beschreibbarer Anwendungscode, OCI-Labels, `HEALTHCHECK` über einen Python-Einzeiler ohne `curl` und `wget`, keine Zugangsdaten
    - _Requirements: 26.1, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7, 26.8, 26.9, 26.10, 26.11, 26.12, 26.13, 26.14, 26.15, 26.16, 26.17, 26.18, 26.19, 26.20, 26.21_

  - [x] 15.2 `compose.yaml` schreiben
    - Compose Specification ohne Schlüssel `version`, Port ausschließlich an eine Loopback-Adresse, genau eine Replik je Gerätegruppe, kein `privileged` und kein Docker-Socket, `cap_drop: ALL`, `no-new-privileges:true`, `read_only: true`, Neustartregel, Prozess-, Speicher-, CPU- und Protokollgrenzen, `env_file` aus `settings.env`
    - _Requirements: 7.5, 14.7, 26.25, 26.26, 26.27, 26.28, 26.29, 26.30, 26.31, 26.35_

  - [x] 15.3 Bau und Veröffentlichung in `README.md` dokumentieren
    - `setup.conf` als persönliche Maintainer-Datei nur auf ausdrücklichen Wunsch anlegen oder ändern; Bau und Veröffentlichung über `docker buildx build --platform linux/amd64 --pull -f docker/Dockerfile --push` als `docker.cirrio.de/rct-api:latest` und `:<version>` aus `pyproject.toml`
    - _Requirements: 26.23, 26.24, 28.18_

  - [x] 15.4 `README.md` schreiben
    - Zweck, Konfigurationsvertrag, Betrieb, Prüfbefehle, Nicht-Ziel Timeseries_Push, verbindliche TLS-Terminierung am Reverse Proxy und `0.0.0.0` ohne Proxy als Fehlkonfiguration, Verletzung der Serialisierung durch mehrere Instanzen, Pflicht des Betreibers zum ausschließlichen Zugriff auf Port 8899 samt möglicher Fremdzugriffe, je Ressource die von einem beliebigen Geräteadapter zu füllenden Felder
    - _Requirements: 7.6, 14.2, 14.5, 21.8, 28.17, 29.1, 29.2, 30.18, 7.1, 7.2, 7.3, 14.1, 14.6, 21.1, 29.3, 29.9_

  - [x] 15.5 Abschließender Prüflauf der Repo-Konventionen
    - `ruff check .` aus der Repository-Wurzel ohne Befunde, Zeilenlänge 120, keine `typing.Optional`, `typing.List` oder `os.path`, kein Kompatibilitätsbehelf unterhalb 3.13, englische Kommentare, deutsche Betreibertexte, zeitzonenbewusste Zeitstempel
    - _Requirements: 28.6, 28.7, 28.8, 28.9, 28.10, 28.11, 28.12, 28.13, 28.14, 28.15, 28.16_

- [ ] 16. Protokollannahmen am Gerät verifizieren
  - [ ] 16.1 Ableitungen und begründete Annahmen am echten Wechselrichter prüfen
    - Vor der Produktivsetzung gegen ein echtes Gerät zu klären, weil kein Test gegen eine selbst gebaute Attrappe sie findet: Attrappe und Codec würden denselben Irrtum teilen
    - Zu prüfen: Escaping der CRC-Bytes, Frame-Aufbau von `0x08` und `0x48` samt Antwort als `0x05`/`0x45`, Bytebreite von `t_enum` und `t_bool`, Zeichenkodierung der Zeichenketten, `net.slave_data` als Struktur (am Gerät mit Firmware 2.3.5687 bereits verifiziert: 108 Byte, Little-Endian)
    - Längenfelder langer Frames am Gerät geprüft: 16 von 18 weichen vom gemessenen Frame-Ende ab; die Korrektur steht in Requirement 2.13–2.15
    - Je Befund die vorgesehene Korrekturstelle nutzen: `escape_body` in `app/protocol/escaping.py`, `encode_frame` in `app/protocol/frames.py`, Feld `byte_width` in `objects_read.json`, Einstellung `STRING_ENCODING`, Strukturkennung `slave_data`; Ergebnis in `README.md` festhalten
    - _Requirements: 1.7, 1.8, 4.7, 4.8, 4.9, 5.2, 5.7, 5.9, 17.1, 17.5, 18.2, 18.3_

- [ ] 17. Finaler Checkpoint
  - Alle bis hier implementierten Pflichtaufgaben und zugehörigen Tests prüfen; Fehler beheben, bevor abhängige Aufgaben beginnen.

## Notes

- Alle Aufgaben einschließlich Sicherheits-, Eigenschafts- und Vertragstests sind verpflichtend für den finalen Checkpoint. Hardware-Verifikation bleibt ein gesondertes Produktivsetzungsgate.
- Je Correctness Property des Designs mindestens ein Eigenschaftstest (Property 17 getrennt für Skalartypen und Slave_Struktur) in der dort benannten Testdatei, mindestens 100 Durchläufe, mit dem Kommentar `# Feature: rct-rest-api, Property {Nummer}: {Eigenschaftstext}`.
- Zeitabhängige Eigenschaften (7, 14, 15, 16) verwenden `ManualClock` über den Port `Clock`; Lastfreiheitseigenschaften (9, 10, 11, 15) einen zählenden Attrappen-Transport.
- Verifikation je Aufgabe mit `ruff check .` und `pytest` aus `rct-rest-api/`, nach Abschluss von Aufgabe 8.6 zusätzlich `python -m app validate`. Ein Typprüfer existiert in diesem Repository nicht und wird nicht eingeführt.
- Es werden keine Aufgaben für ein Deployment auf Fremdsysteme geführt, und `settings.env` wird nicht mit echten Zugangsdaten befüllt; gepflegt wird ausschließlich `settings.env.example`.
- Aufgabe 16.1 ist die einzige Aufgabe, die ein echtes Gerät braucht, und bleibt bis zur Produktivsetzung offen.

## Task Dependency Graph

Wellen werden der Reihe nach abgearbeitet; Einträge derselben Welle dürfen parallel
entstehen, sofern sie keine Dateien gemeinsam verändern. Checkpoints sind eigene
Gates. 2.3 bereitet den Prüfmodus vor, 8.6 vervollständigt ihn; 13.1 liegt vor
der App-Initialisierung, 14.4 nach den Containerartefakten. Tests der HTTP-Schicht
werden nach Freischaltung der Schreibpfade erneut ausgeführt. Ohne Hardware kann
der lokale Prüflauf abgeschlossen werden, der finale Produktivsetzungsgate 17
bleibt bis zum Nachweis aus 16.1 offen.

```json
{
  "waves": [
    {"id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4"]},
    {"id": 1, "tasks": ["2.1", "3.1"]},
    {"id": 2, "tasks": ["2.2", "3.2"]},
    {"id": 3, "tasks": ["2.3", "2.4", "2.5", "3.3"]},
    {"id": 4, "tasks": ["3.4", "3.5", "3.6", "3.9"]},
    {"id": 5, "tasks": ["3.7", "3.8", "3.10", "3.11", "3.13"]},
    {"id": 6, "tasks": ["3.12"]},
    {"id": 7, "tasks": ["4"]},
    {"id": 8, "tasks": ["5.1", "5.2", "8.1", "8.2"]},
    {"id": 9, "tasks": ["5.3", "5.4", "8.6", "13.1"]},
    {"id": 10, "tasks": ["5.5", "5.6", "8.3"]},
    {"id": 11, "tasks": ["5.7"]},
    {"id": 12, "tasks": ["5.8", "5.9", "6.1"]},
    {"id": 13, "tasks": ["6.2", "6.3", "6.5"]},
    {"id": 14, "tasks": ["6.4", "6.6", "6.7", "6.8"]},
    {"id": 15, "tasks": ["6.9", "8.4"]},
    {"id": 16, "tasks": ["6.10"]},
    {"id": 17, "tasks": ["7", "8.5", "9.1", "9.2", "10.1"]},
    {"id": 18, "tasks": ["9.3", "10.2"]},
    {"id": 19, "tasks": ["9.4", "10.3"]},
    {"id": 20, "tasks": ["10.4"]},
    {"id": 21, "tasks": ["10.5", "10.6", "10.7", "13.2", "14.1"]},
    {"id": 22, "tasks": ["10.11", "13.3", "14.2"]},
    {"id": 23, "tasks": ["9.5", "10.8", "10.9", "10.10", "13.4", "14.3"]},
    {"id": 24, "tasks": ["11"]},
    {"id": 25, "tasks": ["12.1"]},
    {"id": 26, "tasks": ["12.2", "12.3", "12.4"]},
    {"id": 27, "tasks": ["15.1", "15.2", "15.3", "15.4"]},
    {"id": 28, "tasks": ["14.4", "15.5"]},
    {"id": 29, "tasks": ["16.1"]},
    {"id": 30, "tasks": ["17"]}
  ]
}
```

### Vollständiger Protokollkatalog (2026-10-02)

- [x] Alle 895 PDF-IDs registrieren, bestehende Namen erhalten, `net_slave_data` strukturiert lesen.
- [x] Alle 894 skalaren Objekte inklusive Strings, Enums und `pas_period` freigeben; `com_service` Tabelle 8 abgleichen.
- [x] LONG WRITE und dekodierten String-Readback unterstützen; Schreiben mit `ENABLE_WRITE_SUPPORT=false` deaktivieren.
- [x] Beide JSON-Dateien im Repository und Docker-Image ausliefern.

- [x] Zusätzliche Quelle `do-gooder/rctpower_writesupport` (4a0d2e9) abgleichen; alle zehn Schreibparameter testen und 15 Enum-Objekte anhand `rctclient==0.0.3` auf explizit 1 Byte korrigieren.
