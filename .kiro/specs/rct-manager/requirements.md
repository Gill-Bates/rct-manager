# Requirements Document

## Introduction

Dieses Dokument beschreibt die Anforderungen an einen neuen, eigenständigen Dienst
im Verzeichnis `rct-manager/`: einen Docker-Container, der die Daten eines oder
mehrerer RCT Power Wechselrichter über eine offene, token-geschützte REST-Schnittstelle
bereitstellt.

Im Innenverhältnis spricht die Python-3.13-Anwendung das proprietäre
**RCT Power Serial Communication Protocol** ausschließlich über TCP (Standardport 8899).
Im Außenverhältnis bietet sie klassisches REST (JSON über HTTP) auf Basis von
FastAPI, abgesichert durch Bearer-Token-Authentifizierung mit Rollentrennung und
Ratenbegrenzung mit einem eigenen Gleitfenster-Begrenzer.

Terminologische Klarstellung: Das Wechselrichter-Protokoll ist **kein Modbus**.
Es ist ein herstellerspezifisches Frame-Protokoll, das in
`docs/reference/6707-RCT-Power-Serial-Communication-Protocol.pdf` (Dokumentversion 1.14
vom 11.02.2022, RCT Power GmbH) spezifiziert ist. Modbus kommt im Gerät lediglich
als separate Objekt-Gruppe (z. B. `modbus.address` für die RS485-Adresse) vor und
ist nicht der Transport dieser Anwendung.

Zentrale Randbedingung: Der Wechselrichter hat keine eigene Zugangskontrolle, keinen
Brute-Force-Schutz, verträgt keine parallelen Clients und liefert unter Last
unvollständige, verzögerte oder veraltete Antworten. Alle Schutz-, Serialisierungs-
und Robustheitsmechanismen liegen deshalb vollständig in dieser Anwendung.

Zwei Eigenschaften des Protokolls prägen den Entwurf besonders stark und sind
deshalb hier vorangestellt:

- **Der Transport ist ein Byte-Strom, nicht eine Folge von Nachrichten.** TCP liefert
  weder genau einen Frame pro Lesevorgang noch garantierte Frame-Grenzen. Die
  Anwendung braucht deshalb einen inkrementellen Parser mit Reassemblierung,
  Resynchronisation und Längenbegrenzung (Requirement 2).
- **Antworten kommen nicht nur auf Anforderung.** Das Command-Byte `0x08`
  (`READ PERIODICALLY`) veranlasst das Gerät, einen Wert dauerhaft und
  unaufgefordert zu senden. Ein pauschales Verwerfen des Empfangspuffers oder jedes
  Frames mit abweichender Objekt_ID ist damit unvereinbar. Der Empfang läuft deshalb
  dauerhaft und klassifiziert jeden Frame (Requirement 3).

### Architekturentscheidung: Monitoring über eine Prometheus-kompatible Pull-Schnittstelle

Diese Entscheidung kehrt die frühere Festlegung um, keinen Prometheus-Endpunkt
anzubieten.

Der Leitgedanke des Entwurfs ist eine klare Trennung: Nach innen wird das
herstellerspezifische RCT-Protokoll als Sonderfall behandelt, nach außen stehen
ausschließlich Standardprotokolle. Eine Pull-Schnittstelle unter `/metrics` passt
genau dazu, weil die Anwendung Daten nur **bereitstellt**. Sie muss nicht wissen, ob
Prometheus, VictoriaMetrics oder ein anderer kompatibler Collector dahintersteht,
und sie verwaltet keine Ziel-URLs, keine Zugangsdaten zu einem Backend, keine
Wiederholversuche, keine Batches, keine Backpressure und kein Verhalten bei
Datenbankausfall. Ein direkter Push in ein Zeitreihen-Backend — etwa über das
InfluxDB-Line-Protocol oder gegen QuestDB — würde die Anwendung betrieblich an ein
konkretes Backend binden und ihr dessen Ausfall- und Rückstaubehandlung aufbürden.
Das wird ausdrücklich vermieden (Requirement 20 und Requirement 21).

Die wichtigste Härtung dieser Entscheidung ist, dass ein Scrape niemals eine
Transaktion gegen ein Gerät auslöst. Der Metrik_Endpunkt liest ausschließlich aus
dem Cache und aus internen Zählern. Andernfalls würde ein Scrape im Abstand von
15 Sekunden mit den Anfragen der REST_API um den serialisierten Gerätezugriff
konkurrieren und genau die Überlast erzeugen, die die Anwendung verhindern soll
(Requirement 20).

### Schnittstellenübersicht

- **REST über HTTP mit JSON** unter `/api/v1` — die primäre, universelle,
  herstellerneutrale Schnittstelle für Lesezugriffe und für die eng begrenzten
  Schreibzugriffe. Dieser Bereich enthält keine Objekt_IDs, keine Gerätadressen und
  keine Netzkennungen.
- **Herstellerspezifischer Diagnosebereich** unter `/api/v1/vendor/rct` —
  abschaltbar, ausdrücklich als herstellerspezifisch gekennzeichnet, für
  Objekt_IDs, Transportadressen und Netzkennungen. Vom herstellerneutralen Vertrag
  nicht referenziert (Requirement 30).
- **Metrik_Endpunkt** unter `/metrics` im Prometheus-Textformat — ausschließlich
  lesend, ausschließlich aus Cache und internen Zählern, für Monitoring und
  Alarmierung. Der Pfad `/metrics` ist vom fachlichen Endpunkt
  `GET /api/v1/metrics` zu unterscheiden, der die Messwertliste der Objekt_Registry
  als JSON ausgibt.
- **Fehlerantworten** einheitlich als Problem Details nach RFC 9457 mit dem
  Medientyp `application/problem+json` (Requirement 25).
- **RCT Power Serial Communication Protocol über TCP** — ausschließlich internes
  Implementierungsdetail. Es ist nach außen nicht sichtbar und nicht durchreichbar.
- **Push in ein Zeitreihen-Backend** — ausdrückliches Nicht-Ziel (Requirement 21).

### Quellen und Vorarbeiten

- `docs/reference/6707-RCT-Power-Serial-Communication-Protocol.pdf` — normative
  Protokollquelle (Frame-Aufbau, Command-Bytes, CRC16, Escaping, Objekt-IDs,
  Fehlerklassen). Alle protokollbezogenen Anforderungen dieses Dokuments sind gegen
  diese Quelle geprüft; Abweichungen und Lücken der Quelle sind im Abschnitt
  „Prüfung der Review-Befunde“ benannt.
- `rctpower/source/rct_inverter/` in diesem Repository — bestehende, erprobte
  Praxis für Socket-Handhabung, Puffer-Drain, Stale-Response-Erkennung,
  Wiederholversuche und Entschleunigung der Anfragen.
- `svalouch/python-rctclient`, `svalouch/rctmon`,
  `weltenwort/home-assistant-rct-power-integration`,
  `do-gooder/rctpower_writesupport`, `mlnoga/rct` — fremde Implementierungen, die
  ausschließlich als **Wissensquelle** dienen. Daraus dürfen Objekt-IDs, Datentypen
  und physikalische Einheiten inhaltlich übernommen werden, ebenso die beiden
  Erfahrungswerte, dass pro Gerät praktisch nur ein Client gleichzeitig sprechen
  darf und dass Schreibzugriffe riskant sind (Gefahr der Batterieschädigung, keine
  Plausibilitätsprüfung in der Firmware). Keines dieser Projekte wird als
  Code-Abhängigkeit eingebunden.
- OWASP Application Security Verification Standard 5.0.0 — Referenzrahmen für die
  Anforderungen an Transportsicherheit, Authentifizierung und Protokollierung.
  Referenzen erscheinen im Format `v5.0.0-<chapter>.<section>.<requirement>`.

### Hinweis zur Notation

Die EARS-Schlüsselwörter (`WHEN`, `WHILE`, `IF/THEN`, `WHERE`, `THE`, `SHALL`,
`FOR ALL`) bleiben englisch, da sie strukturelle Marker des Anforderungsmusters
sind. `FOR ALL` kennzeichnet dabei eine über alle Eingaben geltende
Korrektheitseigenschaft, die als Eigenschaftstest umsetzbar ist. Ebenso bleibt das
Gerüst der User Story (`As a`, `I want`, `so that`) englisch. Der Inhalt ist
deutsch.

## Glossary

- **REST_API**: Die nach außen exponierte HTTP-Schnittstelle der Anwendung, implementiert mit FastAPI.
- **Protokoll_Adapter**: Die Komponente, die Anfragen der REST_API in Protokoll-Frames übersetzt, über TCP an einen Wechselrichter sendet und Antworten in Python-Werte dekodiert.
- **Frame_Codec**: Teil des Protokoll_Adapters, der Frames serialisiert (Encoder) und deserialisiert (Decoder), einschließlich CRC-Berechnung und Escaping.
- **Stream_Parser**: Teil des Protokoll_Adapters, der aus dem TCP-Byte-Strom einzelne vollständige Frames gewinnt, einschließlich Reassemblierung über Lesevorgänge hinweg und Resynchronisation.
- **Empfangspfad**: Die je TCP-Verbindung dauerhaft laufende Komponente, die den Stream_Parser betreibt und jeden gewonnenen Frame dem Demultiplexer übergibt.
- **Demultiplexer**: Die Komponente, die jeden empfangenen Frame genau einer Frame_Klasse zuordnet und an die zuständige Verarbeitung weiterleitet.
- **Frame_Klasse**: Die Einordnung eines empfangenen Frames in `Transaktionsantwort`, `Periodischer_Wert` oder `Unerwarteter_Frame`.
- **Standard_Frame**: Ein Frame zur Kommunikation mit einem einzelnen Gerät, mit Command-Bytes aus dem Bereich `0x01` bis `0x08` und einem Längenfeld über `[ID, Data]`.
- **Plant_Frame**: Ein Frame zur Kommunikation mit einem Slave-Gerät im Anlagennetz, mit Command-Bytes mit gesetztem Bit 6 (`0x40`), einem 4 Byte großen Adressfeld und einem Längenfeld über `[Address, ID, Data]`.
- **Long_Command**: Ein Command-Byte, dessen Frame ein 2 Byte breites Längenfeld führt, konkret `0x03` (`LONG WRITE`), `0x06` (`LONG RESPONSE`), `0x43` (`LONG WRITE M`) und `0x46` (`LONG RESPONSE M`).
- **Frame**: Eine Protokolleinheit nach PDF-Spezifikation, bestehend aus Start-Byte `0x2B`, Command-Byte, Längenfeld, bei einem Plant_Frame dem Adressfeld, Objekt-ID (4 Byte), optionalen Daten und 2 Byte CRC.
- **Objekt_ID**: 32-Bit-Kennung einer Wechselrichter-Variable (z. B. `0x400F015B` für Batterieleistung).
- **Objekt_Registry**: Die Abbildung von sprechenden Namen auf Objekt_ID, Datentyp, Einheit und Idempotenz-Kennzeichnung, geführt als JSON-Datei im Projekt.
- **Idempotenz_Kennzeichnung**: Das Merkmal einer Objekt_ID, das angibt, ob ein Schreibvorgang auf diese Objekt_ID ohne Zustandsänderung wiederholbar ist.
- **Aktionsvariable**: Eine Objekt_ID, deren Beschreiben im Gerät eine Handlung auslöst, insbesondere `com_service` (`0x8FC89B10`).
- **Gerät**: Ein einzelner Wechselrichter, erreichbar über eine eigene TCP-Verbindung oder über das Anlagennetz.
- **Transport_Endpunkt**: Die Einheit des Transports, bestehend aus Zieladresse und Zielport einer TCP-Verbindung zu einem Gerät. Ein unmittelbar angebundener Wechselrichter bildet einen Transport_Endpunkt. Ein Master-Gerät bildet gemeinsam mit allen über das Anlagennetz erreichten Slave-Geräten genau einen Transport_Endpunkt.
- **Kommunikationsinstanz**: Die je Transport_Endpunkt genau einmal vorhandene Komponente, die alleiniger Eigentümer der TCP-Verbindung zu diesem Transport_Endpunkt ist und als einzige Bytes auf dieser Verbindung liest und schreibt.
- **Sendepfad**: Der in der Kommunikationsinstanz genau einmal vorhandene, serialisierte Weg, über den jeder ausgehende Request-Frame eines Transport_Endpunkts gesendet wird.
- **Send_Gate**: Die Stelle im Sendepfad, an der die Mindestpause zwischen zwei gesendeten Request-Frames desselben Transport_Endpunkts erzwungen wird.
- **Commit_Point**: Der Zeitpunkt, zu dem das Send_Gate das erste Byte eines Request-Frames an den Schreibkanal der TCP-Verbindung übergibt. Vor dem Commit_Point ist gesichert, dass kein Byte dieses Request-Frames das Programm verlassen hat; ab dem Commit_Point gilt der Ausgang der Transaktion als potenziell unklar.
- **Endpunktkennung**: Die konfigurierte, herstellerneutrale und über die Laufzeit stabile Kennung eines Transport_Endpunkts, die weder Zieladresse noch Zielport enthält.
- **Wartungszustand**: Die herstellerneutrale Außendarstellung eines Gerätezustands, in dem die Anwendung keine Frames an das betroffene Gerät sendet. Der Sperrzustand ist der einzige Anlass dieses Zustands; die protokollbezogene Ursache erscheint ausschließlich im Diagnosebereich.
- **Gerätekennung**: Die konfigurierte, in Pfaden der REST_API verwendete eindeutige Kennung eines Geräts.
- **Netzkennung**: Die 32-Bit-Adresse eines Slave-Geräts im Anlagennetz.
- **Slave_Struktur**: Die 108 Byte große Datenstruktur mit Little-Endian-Feldern (LSBF), die das Gerät auf eine Leseanfrage der Objekt_ID `0xC0A7074F` (`net.slave_data`) liefert.
- **Zugriffsserialisierer**: Die Komponente, die sicherstellt, dass je Gerät zu jedem Zeitpunkt höchstens eine Protokolltransaktion läuft.
- **Transaktion**: Ein vollständiger Zyklus aus Senden eines Request-Frames und Empfangen des zugehörigen Antwort-Frames.
- **Lesetransaktion**: Eine Transaktion mit dem Command-Byte `0x01` oder `0x41`.
- **Schreibtransaktion**: Eine Transaktion mit einem der Command-Bytes `0x02`, `0x03`, `0x42` oder `0x43`.
- **System_Schreibzugriff**: Eine Schreibtransaktion, die die Anwendung ausschließlich zur Steuerung des Protokolls selbst auslöst und die nicht über die REST_API anstoßbar ist.
- **Token_Verwaltung**: Die Komponente, die konfigurierte API-Token samt Token_Rolle lädt und eingehende Bearer-Token prüft.
- **Token_Rolle**: Die einem Token zugeordnete Berechtigungsstufe, entweder `read` (nur lesend) oder `read/write` (lesend und schreibend).
- **Token_Kennung**: Eine nicht umkehrbare, kurze Kennung eines Tokens zur Verwendung in Protokollausgaben.
- **Schreibfreigabe**: Die Umgebungsvariable `ENABLE_WRITE_SUPPORT`, die schreibende Endpunkte der REST_API global aktiviert oder deaktiviert.
- **Freigabeliste**: Die ausdrückliche Liste jener Objekt_IDs, die über die REST_API beschrieben werden dürfen, samt der je Objekt_ID zulässigen Werte.
- **Wertebereich**: Die in der Freigabeliste je Objekt_ID festgelegte Menge zulässiger Werte, beschrieben durch Datentyp, Minimum, Maximum, zulässige Enum-Werte und optionale Schrittweite.
- **Rate_Limiter**: Der eigene Gleitfenster-Begrenzer (`app/security/ratelimit.py`) zur Begrenzung der Anfragerate pro Aufrufer, der auch die Fehlversuchs-Sperre je Quell-IP-Adresse und eine begrenzte Schlüsseltabelle führt; SlowAPI wird nicht verwendet.
- **Vertrauensliste**: Die konfigurierte Liste jener IP-Adressen und Netze, deren Weiterleitungs-Header der Rate_Limiter auswerten darf.
- **Peer_Adresse**: Die IP-Adresse der unmittelbaren TCP-Gegenstelle einer HTTP-Anfrage.
- **Cache**: Der Zwischenspeicher für zuletzt gelesene Messwerte mit konfigurierbarer Gültigkeitsdauer.
- **Nachfrist**: Die Zeitspanne nach Ablauf der Gültigkeitsdauer, innerhalb derer ein zwischengespeicherter Wert noch als Ersatzantwort dienen darf. Sie wird ab dem Ende der Gültigkeitsdauer gerechnet; ihre Länge ist von der Länge der Gültigkeitsdauer unabhängig.
- **Einzelflug**: Die Eigenschaft, je Kombination aus Gerätekennung und Messwertname zu jedem Zeitpunkt höchstens eine durch Aufrufer ausgelöste Lesetransaktion ohne den Abfrageparameter `fresh` laufen zu lassen und alle weiteren gleichzeitigen Anfragen auf dieselbe Kombination an deren Ergebnis zu binden.
- **Heartbeat**: Die in konfigurierbarem Intervall ausgeführte Lesetransaktion, mit der die Anwendung die Erreichbarkeit eines Geräts auch ohne Aufrufer prüft.
- **Konfigurationslader**: Die Komponente, die Einstellungen aus Umgebungsvariablen und `settings.env` einliest und validiert.
- **Health_Endpunkt**: Der Endpunkt, der die Betriebsfähigkeit des HTTP-Servers meldet.
- **Bereitschafts_Endpunkt**: Der Endpunkt, der die Gerätebereitschaft meldet.
- **Dokumentations_Endpunkte**: Die interaktive Swagger-Seite und das OpenAPI-Dokument.
- **Bootloader_Magic**: Die 4 Byte große Folge `0x50F705AB` in MSBF-Reihenfolge, die ein Gerät im Bootloader-Betrieb bei Kommunikationsstörungen während eines Firmware-Updates alle 500 Millisekunden sendet.
- **Sperrzustand**: Der Zustand einer Verbindung, in dem die Anwendung keine Frames an das betroffene Gerät sendet.
- **Container_Image**: Das gebaute Docker-Image des Dienstes, veröffentlicht als `docker.cirrio.de/rct-api`.
- **Unerwarteter_Frame**: Ein empfangener, gültiger Frame, dessen Objekt_ID weder zur laufenden Transaktion noch zu einer angemeldeten periodischen Anforderung gehört.
- **Aufrufer**: Ein HTTP-Client, der die REST_API nutzt, identifiziert über Token_Kennung und Quell-IP-Adresse.
- **Metrik_Endpunkt**: Der Endpunkt `GET /metrics`, der Dienstmetriken und Messwertmetriken im Prometheus-Textformat ausgibt.
- **Dienstmetrik**: Eine Metrik über den Betrieb der Anwendung selbst, etwa Anfragezahl, Transaktionsdauer, Fehlerzahl, Warteschlangenlänge, Cache-Trefferquote, Anzahl verworfener Bytes, Anzahl unerwarteter Frames.
- **Messwertmetrik**: Eine Metrik, die einen aus dem Cache stammenden Messwert eines Geräts ausgibt.
- **Scrape_Vertrauensliste**: Die konfigurierte Liste jener IP-Adressen und Netze, von denen der Metrik_Endpunkt ohne Bearer-Token abgefragt werden darf.
- **Timeseries_Push**: Das aktive Schreiben von Messwerten in ein Zeitreihen-Backend durch die Anwendung, insbesondere über das InfluxDB-Line-Protocol oder gegen QuestDB. Ein ausdrückliches Nicht-Ziel.
- **Periodische_Anforderung**: Eine mit dem Command-Byte `0x08` oder `0x48` beim Gerät angemeldete Objekt_ID, deren Wert das Gerät danach unaufgefordert und dauerhaft sendet.
- **Periodik**: Die Betriebsart, in der die Anwendung periodische Anforderungen anmeldet und die unaufgefordert eintreffenden Werte verwertet.
- **Protokoll_Steuervariable**: Eine Objekt_ID, die ausschließlich das Verhalten des Protokolls selbst steuert, konkret `pas.period` (`0x9C8FE559`).
- **Konfigurationsvertrag**: Die abschließende Tabelle aller konfigurierbaren Größen mit Name der Umgebungsvariable, Typ, Vorgabewert, Grenzen und Einheit.
- **Herstellerneutraler_Vertrag**: Die Gesamtheit der Pfade, Ressourcen, Pflichtfelder, Feldbedeutungen, Fehlerschlüssel und Statuscodes unter `/api/v1` ohne den Diagnosebereich, ohne den Metrik_Endpunkt und ohne die Dokumentations_Endpunkte.
- **Diagnosebereich**: Der herstellerspezifische Bereich der REST_API unter dem Pfad-Präfix `/api/v1/vendor/rct`, der Objekt_IDs, Transportadressen und Netzkennungen ausgibt.
- **Problem_Details**: Das Format einer Fehlerantwort nach RFC 9457 mit dem Medientyp `application/problem+json`.
- **Fehlerschlüssel**: Die stabile, maschinenlesbare Kennung einer Fehlerursache, ausgegeben im Feld `code` der Problem_Details.
- **Korrelations_ID**: Die je HTTP-Anfrage erzeugte oder übernommene Kennung, die eine Fehlerantwort mit den Protokollausgaben derselben Anfrage verbindet.
- **Arbeitsbudget**: Die Obergrenze der je Transport_Endpunkt innerhalb eines Zeitfensters durch Aufrufer ausgelösten Transaktionen.
- **Aktionsendpunkt**: Der Endpunkt, über den eine Aktionsvariable beschrieben wird, getrennt von den schreibenden Messwert-Endpunkten.
- **Beobachtete_Frische**: Die Eigenschaft eines Messwerts, nach dem Senden des zugehörigen Request-Frames dieser Anfrage am Transport_Endpunkt beobachtet worden zu sein, unabhängig von der Ursache des Frames.
- **Abbaufrist**: Die Gesamtfrist ab dem Empfang eines Beendigungssignals, innerhalb derer der geordnete Abbau vollständig abgeschlossen ist. Sie ist eine echte Deadline und umfasst das Abarbeiten angenommener Arbeit, das Abmelden der Periodik und das Schließen der Verbindungen.
- **Abbaureserve**: Der am Ende der Abbaufrist zurückgehaltene Teil der Abbaufrist, der ausschließlich dem Abmelden der Periodik zur Verfügung steht.
- **Abbau_Deadline**: Der Zeitpunkt, zu dem die Abbaufrist abgelaufen ist und die Anwendung laufende Transaktionen abbricht, die Verbindungen schließt und sich beendet.
- **Fremdzugriff**: Der Zugriff eines nicht zu dieser Anwendung gehörenden Clients auf denselben Transport_Endpunkt.
- **Zeichenkodierung**: Der für die Dekodierung des Datentyps `t_string` und der Zeichenkettenfelder der Slave_Struktur verwendete Codec.

## Architekturprinzip und Vertragsgrenze

Dieser Abschnitt ist normativ. Er legt die Vertragsgrenze fest, gegen die jede
Anforderung dieses Dokuments und jede Entwurfsentscheidung zu prüfen ist. Bei einem
Widerspruch zwischen einer einzelnen Anforderung und diesem Abschnitt gilt dieser
Abschnitt, und die betroffene Anforderung ist zu korrigieren.

**Leitsatz.** Das RCT Power Serial Communication Protocol wird innen vollständig
gekapselt und das Gerät geschont. Nach außen steht eine klassische,
herstellerneutrale Standardschnittstelle.

Daraus folgen vier Grenzen:

1. **Kapselungsgrenze.** Protokolleigenschaften — Objekt_ID, Command-Byte,
   Frame-Aufbau, CRC, Escaping, Netzkennung, Transportadresse, Port, Datentypnamen
   des Protokolls — sind Implementierungsdetails. Sie erscheinen im
   Herstellerneutralen_Vertrag nicht. Wo sie betrieblich gebraucht werden, erscheinen
   sie ausschließlich im ausdrücklich gekennzeichneten Diagnosebereich.
2. **Schonungsgrenze.** Jede Last auf einem Gerät entsteht ausschließlich durch eine
   fachlich angeforderte Lese- oder Schreibtransaktion, durch einen Heartbeat, durch
   die Periodik, durch einen System_Schreibzugriff oder durch den Abbau beim Beenden.
   Kein Monitoring, kein Scrape, keine Dokumentationsseite und keine abgewiesene
   Anfrage erzeugt Gerätelast.
3. **Eigentumsgrenze.** Je Transport_Endpunkt besitzt genau eine
   Kommunikationsinstanz die TCP-Verbindung. Jeder Byte-Verkehr dieses
   Transport_Endpunkts läuft durch sie.
4. **Austauschbarkeitsgrenze.** Der Herstellerneutrale_Vertrag ist so gefasst, dass
   ein anderer Geräteadapter ihn ohne Änderung seiner Ressourcen, Pflichtfelder und
   Feldbedeutungen erfüllen kann.

Die Kapselungsgrenze, die Schonungsgrenze und die Austauschbarkeitsgrenze sind in
Requirement 30 als prüfbare Kriterien ausgeführt, die Eigentumsgrenze in
Requirement 24.

## Requirements

### Requirement 1: Protokoll-Frames kodieren und dekodieren

**User Story:** As a Entwickler, I want eine eigene Protokollschicht, die Frames exakt
nach Herstellerspezifikation erzeugt und liest, so that der Wechselrichter die
Anfragen akzeptiert und Antworten korrekt interpretiert werden, ohne von einer
Fremdbibliothek abhängig zu sein.

#### Acceptance Criteria

1. WHEN der Frame_Codec eine Leseanfrage für eine Objekt_ID als Standard_Frame kodiert, THE Frame_Codec SHALL einen Frame aus Start-Byte `0x2B`, Command-Byte `0x01`, Längenfeld mit Wert 4, der 4 Byte großen Objekt_ID in MSBF-Reihenfolge und 2 Byte CRC erzeugen.
2. WHEN der Frame_Codec einen Plant_Frame kodiert, THE Frame_Codec SHALL das Längenfeld als Summe aus 8 und der Länge der Nutzdaten setzen und das 4 Byte große Adressfeld mit der Netzkennung in MSBF-Reihenfolge zwischen Längenfeld und Objekt_ID einfügen.
3. THE Frame_Codec SHALL die CRC-Prüfsumme als CRC16-CCITT mit Polynom `0x1021` und Startwert `0xFFFF` berechnen.
4. WHEN der Frame_Codec die CRC-Prüfsumme eines Standard_Frames berechnet, THE Frame_Codec SHALL die Bytefolge aus Command-Byte, Längenfeld, Objekt_ID und Daten verwenden.
5. WHEN der Frame_Codec die CRC-Prüfsumme eines Plant_Frames berechnet, THE Frame_Codec SHALL die Bytefolge aus Command-Byte, Längenfeld, Adressfeld, Objekt_ID und Daten verwenden.
6. WHEN die der CRC-Berechnung zugrunde liegende Bytefolge eine ungerade Länge hat, THE Frame_Codec SHALL ein einzelnes Null-Byte als Auffüllung am Ende der Bytefolge berücksichtigen; dies ist am Gerät anhand von 20 Bytefolgen bestätigt.
7. WHEN ein Byte innerhalb von Command-Byte, Längenfeld, Adressfeld, Objekt_ID, Daten oder CRC den Wert `0x2B` oder `0x2D` hat, THE Frame_Codec SHALL dem Byte ein Stop-Byte `0x2D` voranstellen und dieses Stop-Byte nicht in die CRC-Berechnung einbeziehen.
8. WHEN der Frame_Codec eine Bytefolge dekodiert, THE Frame_Codec SHALL die Escaping-Sequenzen `0x2D 0x2D` und `0x2D 0x2B` auf je ein Nutzbyte zurückführen und für die CRC-Berechnung nur das Nutzbyte berücksichtigen.
9. THE Frame_Codec SHALL die Command-Bytes `0x01` (READ), `0x02` (WRITE), `0x03` (LONG WRITE), `0x05` (RESPONSE), `0x06` (LONG RESPONSE) und `0x08` (READ PERIODICALLY) sowie deren um Bit 6 erweiterte Entsprechungen `0x41`, `0x42`, `0x43`, `0x45`, `0x46` und `0x48` unterstützen.
10. WHEN ein Frame ein Command-Byte aus der Menge der Long_Commands trägt, THE Frame_Codec SHALL das Längenfeld als 2 Byte breiten Wert in MSBF-Reihenfolge auswerten und erzeugen.
11. WHEN ein Frame ein Command-Byte außerhalb der Menge der Long_Commands trägt, THE Frame_Codec SHALL das Längenfeld als 1 Byte breiten Wert auswerten und erzeugen.
12. IF ein empfangener Frame ein Command-Byte trägt, das der Frame_Codec nicht unterstützt, THEN THE Frame_Codec SHALL den Frame verwerfen und das unbekannte Command-Byte protokollieren.
13. WHEN ein empfangener Frame vollständig vorliegt, THE Frame_Codec SHALL die enthaltene CRC-Prüfsumme gegen die selbst berechnete Prüfsumme vergleichen.
14. IF die enthaltene CRC-Prüfsumme von der berechneten Prüfsumme abweicht, THEN THE Frame_Codec SHALL den Frame verwerfen und einen Protokollfehler mit Objekt_ID und erwarteter sowie empfangener Prüfsumme melden.
15. FOR ALL unterstützten Kombinationen aus Frame-Art, Command-Byte, Netzkennung, Objekt_ID und Nutzdaten SHALL das Dekodieren eines kodierten Frames dieselben Werte für Command-Byte, Netzkennung, Objekt_ID und Nutzdaten liefern (Round-Trip-Eigenschaft).
16. THE Frame_Codec SHALL ohne Fremdbibliothek für das RCT Power Serial Communication Protocol auskommen und ausschließlich die Python-Standardbibliothek für Kodierung, Dekodierung und CRC-Berechnung verwenden.

### Requirement 2: Frames aus dem TCP-Byte-Strom gewinnen

**User Story:** As a Entwickler, I want einen inkrementellen Parser, der aus dem
TCP-Byte-Strom verlässlich einzelne Frames gewinnt, so that willkürliche
Paketgrenzen, mehrere Frames in einem Lesevorgang und gestörte Daten die
Kommunikation nicht dauerhaft entgleisen lassen.

#### Acceptance Criteria

1. THE Stream_Parser SHALL empfangene Bytes in einen verbindungseigenen Puffer anfügen und Frames unabhängig von den Grenzen der Lesevorgänge erkennen.
2. WHEN ein Lesevorgang Bytes mehrerer vollständiger Frames liefert, THE Stream_Parser SHALL alle enthaltenen vollständigen Frames in Empfangsreihenfolge zurückgeben.
3. WHEN ein Lesevorgang einen Frame nur teilweise liefert, THE Stream_Parser SHALL die bereits empfangenen Bytes im Puffer behalten und den Frame erst nach Eintreffen der fehlenden Bytes zurückgeben.
4. WHEN eine Escaping-Sequenz an der Grenze zweier Lesevorgänge geteilt ist, THE Stream_Parser SHALL das Stop-Byte im Puffer behalten und die Sequenz nach Eintreffen des Folgebytes auflösen.
5. WHEN der Stream_Parser einen Frame beginnt zu lesen, THE Stream_Parser SHALL ein einzelnes dem Start-Byte vorangestelltes Null-Byte überlesen.
6. WHEN der Puffer vor dem ersten Start-Byte `0x2B` andere Bytes enthält, THE Stream_Parser SHALL diese Bytes bis zum nächsten Start-Byte verwerfen und die Anzahl der verworfenen Bytes je Verbindung zählen.
7. WHEN innerhalb eines unvollständigen Frames ein unmaskiertes Start-Byte `0x2B` auftritt, THE Stream_Parser SHALL den unvollständigen Frame verwerfen und das Start-Byte als Beginn eines neuen Frames behandeln.
8. IF eine Escaping-Sequenz ein Stop-Byte vor einem Byte außerhalb von `0x2B` und `0x2D` enthält, THEN THE Stream_Parser SHALL den betroffenen Frame verwerfen, den Fehler protokollieren und zum nächsten Start-Byte resynchronisieren.
9. THE Stream_Parser SHALL eine konfigurierbare Höchstgröße eines Frames anwenden, deren Vorgabewert 4096 Byte beträgt.
10. IF ein Frame die konfigurierte Höchstgröße überschreitet, THEN THE Stream_Parser SHALL den Frame verwerfen, den Fehler mit Command-Byte und gemeldeter Länge protokollieren und zum nächsten Start-Byte resynchronisieren.
11. IF der verbindungseigene Puffer das Doppelte der konfigurierten Höchstgröße eines Frames überschreitet, ohne dass ein vollständiger Frame gewonnen wurde, THEN THE Protokoll_Adapter SHALL die betroffene TCP-Verbindung schließen und neu aufbauen.
12. FOR ALL Zerlegungen der Bytefolge eines gültigen Frames in beliebig viele Teilstücke SHALL der Stream_Parser denselben Frame zurückgeben wie bei Übergabe der gesamten Bytefolge in einem Stück.
13. IF das empfangene Längenfeld eines Long_Frames nicht zur Rahmung passt, THEN THE Stream_Parser SHALL die Frame-Grenze aus dem nächsten unmaskierten Start-Byte oder, bei ausbleibendem Folgeframe, aus einem CRC-gültigen Pufferende bestimmen; ein einzelnes Null-Byte vor dem nächsten Start-Byte zählt nicht zum Frame.
14. WHEN der Stream_Parser ein Long_Frame mit korrigierter Grenze prüft, THE Stream_Parser SHALL die empfangenen Längenbytes unverändert in der CRC-Eingabe belassen, die kürzere Grenze ohne trennendes Null-Byte zuerst prüfen und den Frame nur bei gültiger CRC zurückgeben; bei Erfolg SHALL er die Korrekturen je Verbindung zählen und gemeldete sowie gemessene Länge protokollieren.
15. IF ein Long_Frame oder protokollfremde Bytes verworfen werden, THEN THE Stream_Parser SHALL ab Beginn des verworfenen Frames escape-bewusst zum nächsten unmaskierten Start-Byte resynchronisieren; die Protokollierung protokollfremder Bytes SHALL gezählt und ratenbegrenzt erfolgen.

### Requirement 3: Empfangspfad und Demultiplexen

**User Story:** As a Betreiber, I want einen dauerhaft laufenden Empfangspfad, der
jeden eintreffenden Frame einordnet, so that unaufgefordert eintreffende periodische
Werte nutzbar bleiben und nur tatsächlich unerwartete Frames verworfen werden.

#### Acceptance Criteria

1. THE Empfangspfad SHALL je Transport_Endpunkt dauerhaft laufen, solange die TCP-Verbindung dieses Transport_Endpunkts besteht, unabhängig davon, ob eine Transaktion läuft.
2. WHEN der Empfangspfad einen Frame mit gültiger CRC-Prüfsumme gewonnen hat, THE Demultiplexer SHALL diesen Frame genau einer Frame_Klasse zuordnen.
3. WHEN ein Frame die Objekt_ID und, bei einem Plant_Frame, die Netzkennung der laufenden Transaktion trägt, THE Demultiplexer SHALL den Frame der Frame_Klasse `Transaktionsantwort` zuordnen und der wartenden Transaktion zustellen.
4. THE Demultiplexer SHALL die Zuordnung nach Kriterium 3 ausschließlich aus Objekt_ID, Netzkennung und der zeitlichen Lage des Frames nach dem Senden des Request-Frames ableiten und keine Transaktionskennung des Protokolls voraussetzen.
5. WHEN ein Frame die Objekt_ID einer für das betroffene Gerät angemeldeten periodischen Anforderung trägt und keine Transaktion auf diese Objekt_ID wartet, THE Demultiplexer SHALL den Frame der Frame_Klasse `Periodischer_Wert` zuordnen und den dekodierten Wert dem Cache übergeben.
6. WHEN ein Frame sowohl zur laufenden Transaktion als auch zu einer angemeldeten periodischen Anforderung passt, THE Demultiplexer SHALL den Frame der Frame_Klasse `Transaktionsantwort` zuordnen, der wartenden Transaktion zustellen und den Wert zusätzlich dem Cache übergeben.
7. WHEN ein Frame weder zur laufenden Transaktion noch zu einer angemeldeten periodischen Anforderung gehört, THE Demultiplexer SHALL den Frame der Frame_Klasse `Unerwarteter_Frame` zuordnen, ihn verwerfen und je Transport_Endpunkt einen Zähler für unerwartete Frames erhöhen.
8. THE Demultiplexer SHALL einen Frame der Frame_Klasse `Periodischer_Wert` nicht als unerwarteten Frame behandeln.
9. WHILE für ein Gerät mindestens eine periodische Anforderung angemeldet ist, THE Protokoll_Adapter SHALL den Empfangspuffer des zugehörigen Transport_Endpunkts nicht pauschal verwerfen.
10. THE Protokoll_Adapter SHALL den Empfangspuffer eines Transport_Endpunkts ausschließlich dann verwerfen, wenn der Stream_Parser für diesen Transport_Endpunkt eine Resynchronisation nach einem Protokollfehler nach Requirement 2 ausführt oder die TCP-Verbindung neu aufgebaut wurde.
11. WHEN die Anzahl unerwarteter Frames eines Transport_Endpunkts, die keine wohlgeformten Antwort-Frames (Kommandos `0x05`, `0x06`, `0x45`, `0x46`) sind, eine konfigurierbare Obergrenze innerhalb eines konfigurierbaren Zeitfensters überschreitet, THE Protokoll_Adapter SHALL die betroffene TCP-Verbindung schließen, neu aufbauen und alle periodischen Anforderungen aller Geräte dieses Transport_Endpunkts erneut anmelden. Wohlgeformte unerwartete Antwort-Frames zählen nur für den Zähler unerwarteter Frames und den Verdacht auf Fremdzugriff nach Requirement 29: Der Wechselrichter spiegelt Antworten an alle verbundenen Clients, ein Neuaufbau beseitigt fremden Verkehr nicht (am Gerät beobachtet, 2026-10-02).
12. THE Protokoll_Adapter SHALL je Transport_Endpunkt den Zeitpunkt des letzten empfangenen Frames, den Zeitpunkt der letzten erfolgreichen Transaktion, die Anzahl verworfener Bytes und die Anzahl unerwarteter Frames vorhalten.

### Requirement 4: Objekt-Registry

**User Story:** As a Nutzer der REST_API, I want Messwerte unter sprechenden Namen
mit Einheit abrufen, so that ich keine Objekt-IDs und Rohbytes interpretieren muss.

#### Acceptance Criteria

1. THE Objekt_Registry SHALL für jeden unterstützten Messwert einen eindeutigen Namen, die zugehörige Objekt_ID, den Datentyp, die physikalische Einheit und die Idempotenz_Kennzeichnung bereitstellen.
2. THE Objekt_Registry SHALL die Datentypen `t_bool`, `t_uint8`, `t_int8`, `t_uint16`, `t_int16`, `t_uint32`, `t_int32`, `t_float`, `t_enum`, `t_string` und `t_struct` abbilden.
3. IF ein Eintrag der Objekt_Registry einen Datentyp außerhalb der in Kriterium 2 genannten Menge nennt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die den Namen des Eintrags und den unbekannten Datentyp nennt.
4. WHERE ein Eintrag der Objekt_Registry den Datentyp `t_struct` trägt, THE Objekt_Registry SHALL im Feld `struct` die Strukturkennung der anzuwendenden Dekodierung führen.
5. THE Objekt_Registry SHALL die Strukturkennung `slave_data` als einzige zugelassene Strukturkennung führen und sie dem Eintrag der Objekt_ID `0xC0A7074F` (`net.slave_data`) zuordnen.
6. IF ein Eintrag der Objekt_Registry den Datentyp `t_struct` ohne Feld `struct` oder mit einer unbekannten Strukturkennung führt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die den Namen des Eintrags und die abgelehnte Strukturkennung nennt.
7. THE Objekt_Registry SHALL je Eintrag das optionale Feld `byte_width` mit der Bytebreite der Nutzdaten dieser Objekt_ID führen können.
8. WHERE ein Eintrag der Objekt_Registry das Feld `byte_width` führt, THE Protokoll_Adapter SHALL die dort genannte Bytebreite anwenden und die Vorgabebreite des Datentyps nach Requirement 5 übergehen.
9. WHERE ein Eintrag der Objekt_Registry den Datentyp `t_enum` oder `t_bool` trägt und das Feld `byte_width` fehlt, THE Protokoll_Adapter SHALL die Vorgabebreite des Datentyps nach Requirement 5 anwenden.
10. IF ein Eintrag der Objekt_Registry das Feld `byte_width` mit einem Wert außerhalb der Menge 1, 2 und 4 für einen Datentyp außerhalb von `t_string` und `t_struct` führt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die den Namen des Eintrags und die abgelehnte Bytebreite nennt.
11. WHERE ein Eintrag der Objekt_Registry den Datentyp `t_enum` trägt, THE Objekt_Registry SHALL je zulässigem Rohwert eine symbolische Bezeichnung führen können.
12. THE Objekt_Registry SHALL je Eintrag angeben, ob ein Schreibvorgang auf die Objekt_ID idempotent ist.
13. THE Objekt_Registry SHALL je Eintrag angeben, ob die Objekt_ID eine Aktionsvariable ist.
14. THE Objekt_Registry SHALL die Objekt_ID `0x8FC89B10` (`com_service`) als nicht idempotente Aktionsvariable kennzeichnen.
15. THE Objekt_Registry SHALL je Eintrag das optionale Feld `prometheus_name` mit dem auf dem Metrik_Endpunkt zu verwendenden Metriknamen führen können.
16. IF ein Aufrufer einen Messwertnamen als Pfadsegment anfragt, der nicht in der Objekt_Registry enthalten ist, THEN THE REST_API SHALL den HTTP-Statuscode 404 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `unknown_metric` und dem unbekannten Namen zurückgeben.
17. THE Objekt_Registry SHALL als JSON-Datei im Verzeichnis `rct-manager/` außerhalb des Anwendungscodes vorliegen, sodass die Auswahl der Messwerte ohne Codeänderung erweitert werden kann.
18. THE Objekt_Registry SHALL ohne Laufzeitabhängigkeit zu einem fremden RCT-Client-Projekt auskommen.
19. WHEN die Anwendung startet, THE Konfigurationslader SHALL die Objekt_Registry gegen ihr Schema prüfen und den Start mit einer Fehlermeldung abbrechen, wenn ein Eintrag Name, Objekt_ID, Datentyp, Einheit oder Idempotenz_Kennzeichnung vermisst.
20. IF zwei Einträge der Objekt_Registry denselben Namen oder dieselbe Objekt_ID tragen, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die den doppelten Namen oder die doppelte Objekt_ID nennt.
21. THE Objekt_Registry SHALL je Eintrag angeben, ob der Messwert zur Vorauswahl nach Requirement 10 gehört.
22. THE Objekt_Registry SHALL die Vorauswahl als einzige Quelle für die auf dem Metrik_Endpunkt ausgegebenen und periodisch angeforderten Messwerte führen (Requirement 17 Kriterium 22, Requirement 20 Kriterium 37).

> Begründung zu den Kriterien 2 bis 6: Die Quelle führt `0xC0A7074F`
> (`net.slave_data`) in der ID-Tabelle als `t_string`, beschreibt denselben Wert in
> Tabelle 3 aber als binäre Struktur mit 104 Byte; am Gerät sind es 108 Byte (Requirement 18). Ein eigener Datentyp `t_struct`
> mit einer ausdrücklichen Strukturkennung löst diesen Widerspruch, ohne die
> abschließende Typprüfung aufzuweichen: Die Typmenge bleibt endlich und beim Start
> prüfbar, und die Strukturkennung benennt die anzuwendende Dekodierung eindeutig. Ein
> freies Feld `decoder` wäre gleichwertig, hätte aber keine abschließende Wertemenge
> und wäre deshalb nicht auf dieselbe Weise validierbar.

> Begründung zu den Kriterien 7 bis 10: Die Quelle legt für die Typnamen `t_enum` und
> `t_bool` keine Bytebreite fest. Ein globaler Vorgabewert allein wäre deshalb eine
> nicht korrigierbare Annahme. Das Feld `byte_width` erlaubt die Korrektur je
> Objekt_ID am Gerät, ohne den Anwendungscode zu ändern, und der globale Vorgabewert
> bleibt für alle Einträge wirksam, die keine eigene Breite nennen.

### Requirement 5: Wertdekodierung und Wertkodierung

**User Story:** As a Entwickler eines Fremdsystems, I want eine eindeutig
festgelegte Abbildung zwischen Protokolldatentypen und JSON-Werten, so that ich die
Antworten ohne Rückfragen interpretieren kann.

#### Acceptance Criteria

1. THE Protokoll_Adapter SHALL alle Mehrbyte-Werte in MSBF-Reihenfolge lesen und schreiben, mit Ausnahme der Felder der Slave_Struktur nach Requirement 18 Kriterium 6.
2. THE Protokoll_Adapter SHALL als Vorgabebreite 1 Byte für `t_bool`, `t_uint8` und `t_int8`, 2 Byte für `t_uint16` und `t_int16`, 4 Byte für `t_uint32`, `t_int32`, `t_float` und `t_enum`, eine variable Breite für `t_string` und die durch die Strukturkennung festgelegte Breite für `t_struct` anwenden.
3. THE Protokoll_Adapter SHALL die Datentypen `t_int8`, `t_int16` und `t_int32` als vorzeichenbehaftete Zweierkomplementwerte und die Datentypen `t_uint8`, `t_uint16`, `t_uint32` und `t_enum` als vorzeichenlose Werte dekodieren.
4. THE Protokoll_Adapter SHALL den Datentyp `t_float` als 4 Byte großen Gleitkommawert einfacher Genauigkeit nach IEEE 754 dekodieren.
5. THE Protokoll_Adapter SHALL den Datentyp `t_bool` mit dem Rohwert 0 auf `false` und mit jedem Rohwert ungleich 0 auf `true` abbilden.
6. THE Protokoll_Adapter SHALL den Datentyp `t_string` als Zeichenkette dekodieren und dabei das erste Null-Byte als Ende der Zeichenkette behandeln.
7. THE Protokoll_Adapter SHALL für die Dekodierung jeder Zeichenkette die konfigurierte Zeichenkodierung anwenden, deren Vorgabewert `utf-8` beträgt.
8. WHEN eine Bytefolge unter der konfigurierten Zeichenkodierung nicht dekodierbar ist, THE Protokoll_Adapter SHALL jedes nicht dekodierbare Byte durch das Ersetzungszeichen `U+FFFD` ersetzen und die Anzahl der ersetzten Bytes je Objekt_ID protokollieren.
9. THE Protokoll_Adapter SHALL für die Kodierung einer Zeichenkette dieselbe Zeichenkodierung anwenden wie für die Dekodierung.
10. WHEN der Protokoll_Adapter einen Wert des Datentyps `t_enum` dekodiert, THE REST_API SHALL den numerischen Rohwert ausgeben und zusätzlich die symbolische Bezeichnung ausgeben, sofern die Objekt_Registry für diesen Rohwert eine Bezeichnung führt.
11. IF die Nutzdatenlänge eines Antwort-Frames von der für den Eintrag der Objekt_Registry geltenden Bytebreite abweicht, THEN THE Protokoll_Adapter SHALL die Dekodierung abbrechen und einen Protokollfehler mit Objekt_ID, erwarteter und empfangener Länge melden.
12. IF ein dekodierter Wert des Datentyps `t_float` keine endliche Zahl ist, THEN THE REST_API SHALL den Messwert als nicht ermittelbar ausweisen und den Fehlerschlüssel `invalid_float` nennen.
13. FOR ALL unterstützten Datentypen und zulässigen Werten SHALL das Dekodieren der kodierten Nutzdaten denselben Wert liefern wie den ursprünglichen Wert, bei `t_float` bis auf die Genauigkeit der einfachen Genauigkeit (Round-Trip-Eigenschaft).
14. FOR ALL Bytefolgen SHALL das Dekodieren einer Zeichenkette ohne Ausnahme beenden und für dieselbe Bytefolge dieselbe Zeichenkette liefern (Korrektheitseigenschaft).
15. WHEN ein dekodierter Wert des Datentyps `t_float` subnormal ist (Betrag ungleich 0 und kleiner als 2^-126), THE Protokoll_Adapter SHALL den IEEE-754-Wert unverändert erhalten; die Protokollspezifikation erlaubt keine zentrale Nullsetzung (Nachtrag 2026-10-03, Begründung im Design).

> Begründete Annahme zu den Kriterien 7 bis 9: Die Quelle nennt für die
> Zeichenkettenfelder keine Zeichenkodierung; sie spricht in Tabelle 3 lediglich von
> `char` und von „max 23 symbols + 0-terminator“. `utf-8` ist als Vorgabe gewählt,
> weil es die reinen ASCII-Werte, die bei Seriennummern, Softwareversionen,
> IP-Adressen und Gerätenamen zu erwarten sind, deckungsgleich abbildet und darüber
> hinaus deterministisch bleibt. Das Ersetzen einzelner Bytes statt eines Abbruchs
> verhindert, dass ein einzelnes unerwartetes Byte einen ganzen Messwert
> unerreichbar macht. Die Kodierung ist konfigurierbar, damit ein am Gerät
> festgestelltes `latin-1`-Verhalten ohne Codeänderung nachgezogen werden kann.

### Requirement 6: Serialisierter Gerätezugriff

**User Story:** As a Betreiber, I want dass die Anwendung ein Gerät niemals parallel
anspricht, so that das Gerät keine vermischten oder veralteten Antworten liefert,
während mehrere Geräte unabhängig voneinander bedient werden.

#### Acceptance Criteria

1. THE Zugriffsserialisierer SHALL je Transport_Endpunkt zu jedem Zeitpunkt höchstens eine Transaktion ausführen.
2. WHEN Anfragen für verschiedene Transport_Endpunkte gleichzeitig eintreffen, THE Zugriffsserialisierer SHALL die Transaktionen dieser Transport_Endpunkte unabhängig voneinander ausführen.
3. WHEN mehrere Anfragen für denselben Transport_Endpunkt gleichzeitig eintreffen, THE Zugriffsserialisierer SHALL die Transaktionen in Eingangsreihenfolge ausführen.
4. WHILE eine Transaktion für einen Transport_Endpunkt läuft, THE Zugriffsserialisierer SHALL weitere Anfragen für diesen Transport_Endpunkt in eine Warteschlange dieses Transport_Endpunkts einstellen, deren Höchstlänge konfigurierbar ist.
5. IF die Warteschlange eines Transport_Endpunkts ihre konfigurierte Höchstlänge erreicht hat, THEN THE REST_API SHALL den HTTP-Statuscode 503, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `queue_full` und die zumutbare Wartezeit im Header `Retry-After` zurückgeben.
6. IF eine wartende Anfrage die konfigurierte Höchstwartezeit überschreitet, THEN THE REST_API SHALL den HTTP-Statuscode 504 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `queue_timeout`, der Gerätekennung und dem angefragten Messwertnamen zurückgeben.
7. WHEN eine Transaktion abgeschlossen oder fehlgeschlagen ist, THE Zugriffsserialisierer SHALL den Zugriff auf den betroffenen Transport_Endpunkt für die nächste wartende Anfrage freigeben.
8. THE Protokoll_Adapter SHALL die Mindestpause zwischen zwei gesendeten Request-Frames desselben Transport_Endpunkts nach Requirement 24 am Send_Gate einhalten.
9. WHEN ein Gerät über das Anlagennetz über ein Master-Gerät erreicht wird, THE Zugriffsserialisierer SHALL die Transaktionen aller über dieses Master-Gerät erreichten Geräte gegeneinander serialisieren, weil sie denselben Transport_Endpunkt belegen.
10. THE Zugriffsserialisierer SHALL je Transport_Endpunkt ein Arbeitsbudget führen, das die Anzahl der durch Aufrufer ausgelösten Transaktionen innerhalb eines konfigurierbaren Zeitfensters auf einen konfigurierbaren Wert begrenzt.
11. IF das Arbeitsbudget eines Transport_Endpunkts erschöpft ist, THEN THE REST_API SHALL den HTTP-Statuscode 429, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `device_budget_exhausted` und die zumutbare Wartezeit im Header `Retry-After` zurückgeben und keine Transaktion auslösen.
12. THE Zugriffsserialisierer SHALL Heartbeat-Transaktionen, System_Schreibzugriffe und Transaktionen des Abbaus beim Beenden im Arbeitsbudget auslassen.

> Begründung zu den Kriterien 10 bis 12: Das Arbeitsbudget begrenzt die Gerätelast
> unabhängig von der Anfragerate der REST_API, weil eine einzelne Anfrage mit dem
> Abfrageparameter `fresh` mehrere Transaktionen auslösen kann. Heartbeat, Periodik
> und Abbau bleiben ausgenommen, weil sie die Betriebsfähigkeit sichern und ihre Last
> durch eigene Intervalle bereits begrenzt ist. Ein von ihnen verbrauchtes Budget
> könnte die fachlichen Anfragen aussperren.

### Requirement 7: Deployment-Modell und Grenze der Serialisierung

**User Story:** As a Betreiber, I want wissen, unter welchen Betriebsbedingungen die
Serialisierung je Gerät tatsächlich gilt, so that ich den Dienst nicht in einer
Topologie betreibe, die diese Zusage unterläuft.

#### Acceptance Criteria

1. THE Anwendung SHALL die Serialisierung nach Requirement 6 und das Transport-Eigentum nach Requirement 24 ausschließlich innerhalb des eigenen Prozesses zusichern.
2. THE Dienstinstanz SHALL mit genau einem Anwendungsprozess und genau einem HTTP-Arbeiter für die gesamte konfigurierte Gerätegruppe betrieben werden.
3. THE Anwendung SHALL einen instanzübergreifenden Sperrmechanismus nicht vorsehen.
4. IF die Konfiguration mehr als einen HTTP-Arbeiter verlangt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die die Einschränkung auf einen HTTP-Arbeiter nennt.
5. THE Compose-Datei SHALL den Dienst mit genau einer Replik je Gerätegruppe beschreiben und dabei die Vorgaben nach Requirement 26 einhalten.
6. THE Projektdokumentation SHALL benennen, dass mehrere gleichzeitig gegen denselben Transport_Endpunkt betriebene Instanzen die Serialisierung nach Requirement 6 verletzen und zu vermischten oder veralteten Antworten führen.
7. WHEN die Anwendung startet, THE Anwendung SHALL die Anzahl der konfigurierten Transport_Endpunkte, die Anzahl der konfigurierten Geräte und die Betriebsart mit einem HTTP-Arbeiter protokollieren.
8. THE Anwendung SHALL je Transport_Endpunkt genau eine Gerätegruppe führen, die aus dem unmittelbar angebundenen Gerät und allen über dieses Gerät erreichten Slave-Geräten besteht.

### Requirement 8: Robustheit lesender Transaktionen

**User Story:** As a Betreiber, I want dass vereinzelte Aussetzer eines
Wechselrichters bei Lesezugriffen automatisch ausgeglichen werden, so that die
REST_API trotzdem verwertbare Antworten liefert.

#### Acceptance Criteria

1. THE Protokoll_Adapter SHALL für den Aufbau einer TCP-Verbindung einen konfigurierbaren Verbindungs-Zeitgrenzwert anwenden, dessen Vorgabewert 3 Sekunden beträgt.
2. THE Protokoll_Adapter SHALL für das Eintreffen einer Transaktionsantwort einen konfigurierbaren Antwort-Zeitgrenzwert anwenden, dessen Vorgabewert 5 Sekunden beträgt.
3. THE Protokoll_Adapter SHALL für eine Lesetransaktion einschließlich aller Wiederholungen einen konfigurierbaren Gesamt-Zeitgrenzwert anwenden, dessen Vorgabewert 20 Sekunden beträgt.
4. WHEN eine Lesetransaktion in einen Zeitgrenzwert, einen Protokollfehler oder einen Verbindungsabbruch läuft, THE Protokoll_Adapter SHALL einen Erstversuch und bis zu 4 Wiederholversuche ausführen, wobei die Anzahl der Wiederholversuche konfigurierbar ist.
5. WHEN der Protokoll_Adapter eine Lesetransaktion wiederholt, THE Protokoll_Adapter SHALL vor dem nächsten Versuch exponentiell ansteigend warten, beginnend bei 200 Millisekunden und begrenzt auf 5 Sekunden.
6. WHEN der Gesamt-Zeitgrenzwert einer Lesetransaktion erreicht ist, THE Protokoll_Adapter SHALL keinen weiteren Versuch beginnen.
7. IF alle Versuche einer Lesetransaktion fehlgeschlagen sind, THEN THE REST_API SHALL den HTTP-Statuscode 502 und Problem_Details nach Requirement 25 mit Gerätekennung, angefragtem Messwertnamen und Fehlerschlüssel zurückgeben.
8. IF die TCP-Verbindung zu einem Transport_Endpunkt abgebrochen ist, THEN THE Protokoll_Adapter SHALL diese Verbindung beim nächsten Versuch neu aufbauen und alle periodischen Anforderungen aller Geräte dieses Transport_Endpunkts erneut anmelden.
9. THE Protokoll_Adapter SHALL die TCP-Optionen `TCP_NODELAY` und `SO_KEEPALIVE` für jede Verbindung zu einem Transport_Endpunkt aktivieren.
10. THE Protokoll_Adapter SHALL ausschließlich TCP als Transport verwenden.
11. WHEN der Empfangspfad die Bytefolge des Bootloader_Magic in den Bytes eines Transport_Endpunkts erkennt, die außerhalb eines Frames liegen, THE Protokoll_Adapter SHALL unverzüglich alle Sendevorgänge an diesen Transport_Endpunkt einstellen und die Verbindung in den Sperrzustand versetzen; die Nutzlast eines gültigen Frames SHALL dabei unberücksichtigt bleiben, weil sie dieselbe Bytefolge zulässig enthalten kann.
12. WHILE eine Verbindung im Sperrzustand ist, THE REST_API SHALL Anfragen an jedes Gerät dieses Transport_Endpunkts mit dem HTTP-Statuscode 503 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `device_maintenance` abweisen und keine Transaktion auslösen.
13. WHEN eine Verbindung in den Sperrzustand versetzt wurde, THE Protokoll_Adapter SHALL frühestens nach einer konfigurierbaren Abkühlzeit, deren Vorgabewert 300 Sekunden beträgt, eine neue Verbindung aufbauen und mit einer einzelnen Lesetransaktion prüfen, ob das Gerät das Protokoll wieder bedient.
14. WHEN eine Verbindung in den Sperrzustand versetzt wurde, THE Anwendung SHALL dieses Ereignis mit Gerätekennung und Zeitpunkt protokollieren.
15. THE REST_API SHALL den Sperrzustand nach außen ausschließlich als Wartungszustand und als Fehlerschlüssel `device_maintenance` darstellen und die protokollbezogene Ursache ausschließlich im Diagnosebereich nach Requirement 30 ausgeben.

### Requirement 9: Robustheit und Sicherheit schreibender Transaktionen

**User Story:** As a Betreiber, I want dass eine Schreibtransaktion mit unklarem
Ausgang niemals blind wiederholt wird, so that keine Handlung des Geräts
unbeabsichtigt mehrfach oder gar nicht ausgelöst wird.

#### Acceptance Criteria

1. THE Protokoll_Adapter SHALL für Schreibtransaktionen eine von Requirement 8 getrennte Wiederholungsregel anwenden.
2. THE Kommunikationsinstanz SHALL den Commit_Point einer Schreibtransaktion am Send_Gate setzen, nämlich bei der Übergabe des ersten Bytes des Request-Frames an den Schreibkanal der TCP-Verbindung.
3. IF eine Schreibtransaktion fehlschlägt, bevor ihr Commit_Point erreicht ist, THEN THE Protokoll_Adapter SHALL die Schreibtransaktion bis zu einer konfigurierbaren Anzahl von Wiederholversuchen wiederholen, deren Vorgabewert 2 beträgt.
4. IF eine Schreibtransaktion nach ihrem Commit_Point in einen Zeitgrenzwert, einen Protokollfehler oder einen Verbindungsabbruch läuft, THEN THE Protokoll_Adapter SHALL keinen weiteren Schreibvorgang auf dieselbe Objekt_ID senden und den Ausgang als potenziell unklar behandeln.
5. THE Protokoll_Adapter SHALL die Anzahl der an den Schreibkanal übergebenen Bytes, den Abschluss des Schreibvorgangs und jede Rückmeldung des Betriebssystems über den Sendepuffer nicht als Nachweis dafür verwenden, dass das Gerät den Request-Frame nicht empfangen hat.
6. WHEN eine Schreibtransaktion nach ihrem Commit_Point einen unklaren Ausgang hat, THE Protokoll_Adapter SHALL eine Lesetransaktion auf dieselbe Objekt_ID ausführen, um den Zustand des Geräts festzustellen.
7. WHEN die Lesetransaktion nach Kriterium 6 den angeforderten Wert liefert, THE REST_API SHALL den Vorgang als erfolgreich mit dem Hinweis auf den unbestätigten Sendevorgang ausweisen.
8. IF die Lesetransaktion nach Kriterium 6 einen abweichenden Wert liefert oder fehlschlägt, THEN THE REST_API SHALL den HTTP-Statuscode 502, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `write_outcome_unknown` und den zuletzt gelesenen Zustand zurückgeben.
9. IF die Objekt_Registry eine Objekt_ID als nicht idempotent kennzeichnet, THEN THE Protokoll_Adapter SHALL eine Schreibtransaktion auf diese Objekt_ID niemals automatisch wiederholen.
10. IF die Objekt_Registry eine Objekt_ID als Aktionsvariable kennzeichnet, THEN THE Protokoll_Adapter SHALL eine Schreibtransaktion auf diese Objekt_ID niemals automatisch wiederholen, auch dann nicht, wenn der Commit_Point dieser Schreibtransaktion nicht erreicht ist.
11. WHEN eine Schreibtransaktion auf eine Aktionsvariable ausgeführt wird, THE Anwendung SHALL den Vorgang vor dem Senden und nach dem Ergebnis je mit Token_Kennung, Gerätekennung, und Objekt_ID protokollieren; der Wert wird nach Kriterium 21 nie protokolliert.
12. WHEN eine Schreibtransaktion erfolgreich abgeschlossen oder mit unklarem Ausgang beendet wurde, THE Cache SHALL den Eintrag der betroffenen Objekt_ID dieses Geräts verwerfen.
13. WHEN der Cache-Eintrag nach Kriterium 12 verworfen wurde, THE Cache SHALL diesen Eintrag ausschließlich durch das Ergebnis der anschließenden Lesetransaktion neu setzen.
14. WHEN eine Schreibtransaktion auf eine Aktionsvariable ausgeführt wurde, THE REST_API SHALL das Ergebnis der Lesetransaktion nach Kriterium 6 als zurückgelesenen Wert ausweisen und nicht als Bestätigung der ausgelösten Handlung.
15. WHEN eine Schreibtransaktion auf eine Aktionsvariable ausgeführt wurde, THE REST_API SHALL das Feld `action_confirmed` mit dem Wert `false` und das Feld `action_note` mit dem Hinweis ausgeben, dass das Protokoll keine Rückmeldung über die Ausführung der Handlung vorsieht.
16. IF eine Schreibtransaktion auf eine Aktionsvariable nach ihrem Commit_Point einen unklaren Ausgang hat, THEN THE REST_API SHALL den HTTP-Statuscode 502, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `action_outcome_unknown` und den zuletzt gelesenen Wert zurückgeben.
17. FOR ALL Fehlermustern, die ab dem Commit_Point einer Schreibtransaktion auftreten, SHALL genau ein Request-Frame mit dem Command-Byte einer Schreibtransaktion und dieser Objekt_ID den Sendepfad des Transport_Endpunkts verlassen (Korrektheitseigenschaft).
18. WHEN eine Schreibtransaktion nach ihrem Commit_Point innerhalb von `WRITE_RESPONSE_TIMEOUT_MS` keine Antwort erhält, THE Kommunikationsinstanz SHALL die TCP-Verbindung beibehalten, den Ausgang als unbestätigt gesendet werten und den Read-back nach Kriterium 6 auf derselben Verbindung ausführen; ein Zeitgrenzwert einer Lesetransaktion verwirft die Verbindung unverändert nach `RESPONSE_TIMEOUT_SECONDS`. Der Wert von `WRITE_RESPONSE_TIMEOUT_MS` darf `RESPONSE_TIMEOUT_SECONDS` nicht überschreiten.
19. THE Protokoll_Adapter SHALL eine Schreibtransaktion und ihren Read-back nach Kriterium 6 je Gerät und Objekt_ID zusammenhängend ausführen, sodass keine andere Schreibtransaktion auf dieselbe Objekt_ID zwischen beiden liegt; Commit_Point, Budgetreservierung (zwei Einheiten) und das Verbot einer Wiederholung nach dem Commit_Point bleiben unberührt.
20. WHEN das Gerät eine Schreibtransaktion auf eine Aktionsvariable nicht beantwortet und der Read-back nach Kriterium 6 einen Wert liefert, THE REST_API SHALL den HTTP-Statuscode 200 mit `action_confirmed=false` ausgeben, weil das Gerät WRITE nie beantwortet und die Aktionsvariable vom Gerät selbst zurückgesetzt werden kann; nur ein fehlender Read-back ergibt `action_outcome_unknown` nach Kriterium 16.
21. THE Anwendung SHALL Schreib- und Aktionswerte auf keiner Protokollstufe protokollieren, auch nicht bei Ablehnung oder Fehler, weil Zeichenketten Geheimnisse enthalten können; Token_Kennung, Gerätekennung, Messwertname, Objekt_ID und Ergebnis sind zulässig.

> Begründung zu den Kriterien 2 bis 5: Auf TCP ist nicht beobachtbar, ob ein
> vollständig geschriebener Request-Frame das Gerät erreicht hat; ein erfolgreicher
> Schreibvorgang auf den Socket belegt lediglich die Übergabe an den Sendepuffer des
> Betriebssystems, und ein Fehler nach der ersten Byteübergabe lässt offen, wie viele
> Bytes bereits unterwegs sind. Eine Unterscheidung nach „vollständig gesendet“ wäre
> damit nicht entscheidbar. Der Commit_Point ist deshalb konservativ auf die erste
> Byteübergabe gelegt und liegt am Send_Gate, also an der einzigen Stelle, die
> überhaupt Bytes schreibt. Vor diesem Punkt ist eine Wiederholung nachweislich
> gefahrlos, danach ausgeschlossen.

> Begründung zu den Kriterien 13 bis 16: Bei einer Aktionsvariablen ist der
> zurückgelesene Wert kein Nachweis der Handlung. Die Quelle sagt, das Gerät handle
> ausschließlich bei einer Änderung des Werts; ein gelesener Wert kann damit sowohl
> aus dem gerade gesendeten Schreibvorgang als auch aus einem früheren stammen, und
> die Quelle sieht keine Rückmeldung über den Abschluss der Handlung vor. Die Antwort
> muss diesen Unterschied ausdrücklich ausweisen, statt ihn als Erfolg darzustellen.

### Requirement 10: REST-Vertrag und lesende Endpunkte

**User Story:** As a Entwickler eines Fremdsystems, I want konkrete Pfade, Methoden
und Schemata, so that ich einen Client ohne Rückfragen gegen die Schnittstelle
schreiben kann.

#### Acceptance Criteria

1. THE REST_API SHALL alle fachlichen Endpunkte unter dem Pfad-Präfix `/api/v1` bereitstellen.
2. THE REST_API SHALL unter `GET /api/v1/metrics` alle in der Objekt_Registry verfügbaren Messwerte mit Namen, Einheit, Werttyp, Schreibbarkeit und Vorauswahl-Kennzeichnung auflisten.
3. THE REST_API SHALL im Herstellerneutralen_Vertrag die Objekt_ID, die Datentypnamen des Protokolls, die Netzkennung, die Transportadresse und den Transportport auslassen.
4. THE REST_API SHALL als Werttyp eines Messwerts ausschließlich die herstellerneutralen Werte `boolean`, `integer`, `number`, `string`, `enum` und `object` ausgeben.
5. THE REST_API SHALL unter `GET /api/v1/devices` alle konfigurierten Geräte mit Gerätekennung, Anzeigename, Rolle und Bereitschaftszustand auflisten.
6. THE REST_API SHALL unter `GET /api/v1/devices/{device_id}/metrics/{metric_name}` den Wert eines einzelnen Messwerts zurückgeben.
7. THE REST_API SHALL unter `GET /api/v1/devices/{device_id}/metrics` mehrere Messwerte zurückgeben, deren Namen der Aufrufer im Abfrageparameter `names` als durch Komma getrennte Liste übergibt.
8. WHERE der Abfrageparameter `names` fehlt, THE REST_API SHALL alle Messwerte zurückgeben, die für das angefragte Gerät in der Objekt_Registry als Vorauswahl gekennzeichnet sind.
9. THE REST_API SHALL je Anfrage nach Kriterium 7 höchstens eine konfigurierbare Anzahl von Messwerten verarbeiten, deren Vorgabewert 32 beträgt.
10. IF eine Anfrage nach Kriterium 7 mehr Messwerte als die konfigurierte Höchstzahl nennt, THEN THE REST_API SHALL den HTTP-Statuscode 422, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `batch_too_large` und die zulässige Höchstzahl zurückgeben.
11. THE REST_API SHALL jede Messwertantwort mit den Feldern `name`, `value`, `unit`, `timestamp`, `age_seconds`, `stale` und `source` ausgeben.
12. THE REST_API SHALL das Feld `source` mit dem Wert `device` ausgeben, wenn der Wert aus einer Transaktion stammt, und mit dem Wert `cache` ausgeben, wenn der Wert aus dem Cache stammt.
13. WHEN eine Anfrage nach Kriterium 7 eintrifft, THE REST_API SHALL zuerst alle angefragten Messwertnamen und alle Abfrageparameter prüfen und erst danach eine Transaktion auslösen.
14. IF eine Anfrage nach Kriterium 7 mindestens einen Messwertnamen nennt, den die Objekt_Registry nicht führt, THEN THE REST_API SHALL den HTTP-Statuscode 422, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `unknown_metric` und die Liste aller unbekannten Namen zurückgeben und keine Transaktion auslösen.
15. WHEN eine Anfrage nach Kriterium 7 die Prüfung nach Kriterium 13 bestanden hat und für mindestens einen Messwert einen Wert liefert, THE REST_API SHALL den HTTP-Statuscode 200 zurückgeben und die fehlgeschlagenen Messwerte im Antwortfeld `errors` je mit Namen, Fehlerschlüssel und Beschreibung ausweisen.
16. IF eine Anfrage nach Kriterium 7 die Prüfung nach Kriterium 13 bestanden hat und für keinen angefragten Messwert einen Wert liefert, THEN THE REST_API SHALL den HTTP-Statuscode 502, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `device_unavailable` und im Feld `errors` alle fehlgeschlagenen Messwerte zurückgeben.
17. IF ein Aufrufer eine Gerätekennung anfragt, die nicht konfiguriert ist, THEN THE REST_API SHALL den HTTP-Statuscode 404, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `unknown_device` und die unbekannte Gerätekennung zurückgeben.
18. THE REST_API SHALL jeden Zeitstempel als zeitzonenbewussten Zeitpunkt in UTC nach ISO 8601 ausgeben.
19. WHERE ein Aufrufer den Abfrageparameter `fresh` mit dem Wert `true` übergibt, THE REST_API SHALL den Cache für die angefragten Messwerte übergehen und je Messwert eine Lesetransaktion über den Zugriffsserialisierer auslösen.
20. WHERE ein Aufrufer den Abfrageparameter `fresh` mit dem Wert `true` übergibt, THE REST_API SHALL je Anfrage höchstens eine konfigurierbare Anzahl von Messwerten verarbeiten, deren Vorgabewert 8 beträgt und die die Höchstzahl nach Kriterium 9 nicht überschreitet.
21. IF eine Anfrage mit dem Abfrageparameter `fresh` mehr Messwerte als die konfigurierte Höchstzahl nach Kriterium 20 nennt, THEN THE REST_API SHALL den HTTP-Statuscode 422, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `fresh_batch_too_large` und die zulässige Höchstzahl zurückgeben und keine Transaktion auslösen.
22. THE REST_API SHALL jede durch den Abfrageparameter `fresh` ausgelöste Lesetransaktion gegen das Arbeitsbudget des betroffenen Transport_Endpunkts nach Requirement 6 anrechnen.
23. THE REST_API SHALL ausschließlich die HTTP-Methode `GET` für lesende Endpunkte und die HTTP-Methoden `PUT` und `POST` für schreibende Endpunkte anbieten.
24. FOR ALL Anfragen, die an der Prüfung nach Kriterium 13 scheitern, SHALL die Anzahl der gegen einen Transport_Endpunkt ausgeführten Transaktionen unverändert bleiben (Korrektheitseigenschaft).
25. WHERE ein Aufrufer den Abfrageparameter `names` weglässt und `fresh` nicht `true` ist, THE REST_API SHALL die Vorauswahl der Objekt_Registry vollständig ausgeben und die Höchstzahl `MAX_METRICS_PER_REQUEST` nicht anwenden, weil die Größe der Vorauswahl durch die Registry und die Obergrenze 64 nach Requirement 17 Kriterium 23 begrenzt ist; mit `fresh=true` gilt `MAX_FRESH_METRICS_PER_REQUEST` unverändert, und eine größere Vorauswahl führt zu `fresh_batch_too_large`.

> Begründung zu den Kriterien 2 bis 5: Objekt_ID, Protokoll-Datentyp, Netzkennung,
> Transportadresse und Port sind Eigenschaften des RCT-Protokolls und seines
> Transports. Sie im herstellerneutralen Vertrag auszugeben, würde Clients an diese
> Eigenschaften binden und einen Austausch des Geräteadapters unmöglich machen. Wer
> sie betrieblich braucht, findet sie im Diagnosebereich nach Requirement 30.

> Begründung zu den Kriterien 13 bis 16: Eine Anfrage mit unbekannten Namen oder
> ungültigen Parametern ist ein Clientfehler und erhält deshalb 422, nicht 502. Der
> Statuscode 502 bleibt dem Fall vorbehalten, dass die Eingabe gültig war und
> ausschließlich die Gerätekommunikation gescheitert ist. Eine teilweise erfolgreiche
> Sammelabfrage liefert verwertbare Daten und erhält deshalb den Statuscode 200 mit
> einem ausdrücklichen Abschnitt `errors`; ein Fehlerstatus würde Clients veranlassen,
> gültige Werte zu verwerfen. Der gemischte Fall aus unbekannten und bekannten Namen
> fällt unter Kriterium 14 und wird vollständig abgewiesen, weil die Anfrage dann eine
> Eingabe enthält, die der Client zuerst korrigieren muss.

> Begründung zu den Kriterien 20 bis 22: Ohne eigenes Limit erzeugte eine einzelne
> HTTP-Anfrage mit 32 Namen und `fresh=true` bis zu 32 serialisierte
> Gerätetransaktionen und belegte den Transport_Endpunkt bei einer Mindestpause von
> 300 Millisekunden für mindestens 9,6 Sekunden. Das kleinere Limit und das
> Arbeitsbudget begrenzen diese Last unabhängig von der Anfragerate.

### Requirement 11: Dokumentations-Endpunkte

**User Story:** As a Entwickler eines Fremdsystems, I want eine im Browser benutzbare
Swagger-Seite, so that ich die Schnittstelle erkunden und mit meinem Token
ausprobieren kann, ohne die Angriffsfläche eines ungeschützten Geräts offenzulegen.

#### Acceptance Criteria

1. THE REST_API SHALL eine maschinenlesbare OpenAPI-Beschreibung aller Endpunkte bereitstellen.
2. THE REST_API SHALL die interaktive Swagger-Dokumentationsseite ausliefern.
3. THE OpenAPI-Beschreibung SHALL ein Sicherheitsschema für Bearer-Token enthalten und jeden fachlichen Endpunkt diesem Sicherheitsschema zuordnen, sodass die Swagger-Seite einen Autorisierungsdialog anbietet.
4. WHILE die Bindeadresse des HTTP-Servers eine Loopback-Adresse ist, THE REST_API SHALL die Dokumentations_Endpunkte ohne Token ausliefern.
5. WHERE die Einstellung `DOCS_PUBLIC` aktiviert ist, THE REST_API SHALL die Dokumentations_Endpunkte unabhängig von der Bindeadresse ohne Token ausliefern.
6. WHILE die Bindeadresse keine Loopback-Adresse ist und die Einstellung `DOCS_PUBLIC` deaktiviert ist, THE REST_API SHALL Anfragen an die Dokumentations_Endpunkte mit dem HTTP-Statuscode 404 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `docs_not_available` abweisen.
7. THE Dokumentations_Endpunkte SHALL keine Token-Werte, keine Gerätadressen und keine Inhalte der Freigabeliste mit Wertebereichen offenlegen, die über die Angaben der Objekt_Registry hinausgehen.
8. THE Rate_Limiter SHALL Anfragen an die Dokumentations_Endpunkte in die Anfragerate einbeziehen.

> Begründete Annahme, die die frühere Annahme zu Requirement 5.11 ersetzt: Eine
> Swagger-Seite, deren HTML selbst ein Bearer-Token verlangt, ist im Browser
> unbenutzbar, weil der Autorisierungsdialog nie erscheint. Die
> Dokumentations_Endpunkte sind deshalb tokenfrei, aber nur dort erreichbar, wo das
> Netz nachweislich lokal ist oder der Betreiber die Freigabe ausdrücklich
> konfiguriert hat. In allen anderen Fällen existieren sie nach außen nicht. Die
> fachlichen Endpunkte bleiben in jedem Fall token-geschützt. Die Annahme ist
> revidierbar, falls der Betrieb eine dauerhaft öffentliche Dokumentationsseite
> verlangt.

### Requirement 12: Token-Authentifizierung und Rollentrennung

**User Story:** As a Betreiber, I want den Zugriff auf die REST_API über Token mit
getrennten Rollen beschränken, so that die ungeschützten Wechselrichter nicht über
die Schnittstelle erreichbar werden und lesende Clients nicht schreiben können.

#### Acceptance Criteria

1. WHERE die Einstellung `AUTH_REQUIRED` nicht nach Kriterium 12 deaktiviert ist, THE REST_API SHALL für jeden Endpunkt außer dem Health_Endpunkt, den nach Requirement 11 freigegebenen Dokumentations_Endpunkten und dem nach Requirement 20 freigegebenen Metrik_Endpunkt ein gültiges Bearer-Token im HTTP-Header `Authorization` verlangen.
2. WHEN eine Anfrage ohne Header `Authorization` eintrifft, THE REST_API SHALL den HTTP-Statuscode 401 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `missing_token` zurückgeben.
3. IF ein übermitteltes Token keinem konfigurierten Token entspricht, THEN THE REST_API SHALL den HTTP-Statuscode 401 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `invalid_token` zurückgeben und den Grund der Ablehnung ohne das übermittelte Token protokollieren.
4. THE Token_Verwaltung SHALL übermittelte Token in laufzeitkonstanter Zeit mit den konfigurierten Token vergleichen (ASVS `v5.0.0-11.3.1`).
5. THE Token_Verwaltung SHALL jedem konfigurierten Token genau eine Token_Rolle aus `read` und `read/write` zuordnen.
6. WHEN ein Token ohne zugeordnete Token_Rolle konfiguriert ist, THE Konfigurationslader SHALL diesem Token die Token_Rolle `read` zuweisen.
7. IF ein Aufrufer mit der Token_Rolle `read` einen schreibenden Endpunkt oder den Diagnosebereich anfragt, THEN THE REST_API SHALL den HTTP-Statuscode 403 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `insufficient_scope` zurückgeben und die Anfrage ohne Transaktion abweisen.
8. THE Token_Verwaltung SHALL mindestens zwei gleichzeitig gültige Token unterstützen, damit ein Token ohne Dienstunterbrechung ersetzt werden kann.
9. THE Token_Verwaltung SHALL Token mit einer Länge von mindestens 32 Zeichen verlangen.
10. IF beim Start kein Token konfiguriert ist und die Einstellung `AUTH_REQUIRED` den Vorgabewert `true` trägt, THEN THE Konfigurationslader SHALL den Start der Anwendung mit einer Fehlermeldung abbrechen.
11. THE REST_API SHALL Token-Werte in Protokollausgaben und Fehlermeldungen durch die Token_Kennung ersetzen (ASVS `v5.0.0-16.4.1`).
12. WHERE der Betreiber die Einstellung `AUTH_REQUIRED` ausdrücklich auf `false` setzt, THE REST_API SHALL Anfragen ohne Header `Authorization` mit der Token_Rolle `read/write` annehmen, THE Konfigurationslader SHALL `API_TOKENS` nicht verlangen, und THE Anwendung SHALL bei jedem Start eine Warnung protokollieren, dass die Authentifizierung abgeschaltet ist. Ein übermitteltes Token wird weiterhin nach den Kriterien 3 und 4 geprüft. Schreibende Endpunkte bleiben an `ENABLE_WRITE_SUPPORT` und die Freigabeliste nach Requirement 19 gebunden.

> Begründung zu Kriterium 12: Vorgabe ist der geschlossene Zustand. Die Abschaltung ist eine
> ausdrücklich zugelassene Betriebsart für einen Betrieb auf der Loopback-Adresse oder hinter einem
> Reverse-Proxy, der Aufrufer selbst authentifiziert. Risiko: Wer den Port erreicht, liest alle Werte und
> schreibt bei `ENABLE_WRITE_SUPPORT=true` jeden in der Freigabeliste geführten Messwert; die
> Freigabeliste ist dann die einzige Schranke. Die Warnung bei jedem Start verhindert, dass die
> Abschaltung unbemerkt bleibt.

### Requirement 13: Ratenbegrenzung und Brute-Force-Abwehr

**User Story:** As a Betreiber, I want dass die REST_API Anfragefluten und
Token-Rateversuche abweist, so that die Wechselrichter nicht überlastet und kein
Token erraten wird.

#### Acceptance Criteria

1. THE Rate_Limiter SHALL alle angenommenen Anfragen eines Aufrufers unabhängig vom HTTP-Statuscode der Antwort zählen und auf einen konfigurierbaren Wert pro konfigurierbarem Zeitfenster begrenzen.
2. THE Konfigurationslader SHALL für die Anfragerate den vorläufigen Vorgabewert 60 Anfragen pro 60 Sekunden verwenden.
3. IF ein Aufrufer die konfigurierte Anfragerate überschreitet, THEN THE REST_API SHALL den HTTP-Statuscode 429, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `rate_limited` und die zumutbare Wartezeit im Header `Retry-After` zurückgeben.
4. THE Rate_Limiter SHALL fehlgeschlagene Authentifizierungsversuche in einem von der Anfragerate getrennten Zähler je Quell-IP-Adresse führen und auf einen konfigurierbaren Wert pro konfigurierbarem Zeitfenster begrenzen (ASVS `v5.0.0-2.2.1`).
5. THE Konfigurationslader SHALL für fehlgeschlagene Authentifizierungsversuche den vorläufigen Vorgabewert 5 Versuche pro 300 Sekunden und für die Sperrdauer den vorläufigen Vorgabewert 900 Sekunden verwenden.
6. IF eine Quell-IP-Adresse die konfigurierte Anzahl fehlgeschlagener Authentifizierungsversuche überschreitet, THEN THE REST_API SHALL weitere Anfragen dieser Quell-IP-Adresse für die konfigurierte Sperrdauer mit dem HTTP-Statuscode 429 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `rate_limited` abweisen.
7. WHEN eine Anfrage wegen Überschreitung einer Ratengrenze abgewiesen wird, THE REST_API SHALL keine Transaktion gegen ein Gerät auslösen.
8. THE Rate_Limiter SHALL die Quell-IP-Adresse im Vorgabezustand ausschließlich aus der Peer_Adresse ermitteln.
9. WHERE eine Vertrauensliste konfiguriert ist und die Peer_Adresse in der Vertrauensliste enthalten ist, THE Rate_Limiter SHALL die Quell-IP-Adresse aus dem konfigurierten Weiterleitungs-Header ermitteln.
10. IF die Peer_Adresse nicht in der Vertrauensliste enthalten ist, THEN THE Rate_Limiter SHALL jeden Weiterleitungs-Header unberücksichtigt lassen und die Peer_Adresse verwenden.
11. IF ein Weiterleitungs-Header mehrere Adressen enthält, THEN THE Rate_Limiter SHALL die letzte Adresse verwenden, die nicht in der Vertrauensliste enthalten ist.
12. IF ein Weiterleitungs-Header konfiguriert ist, ohne dass eine Vertrauensliste konfiguriert ist, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die die fehlende Vertrauensliste nennt.
13. THE Rate_Limiter SHALL aus seiner begrenzten Schlüsseltabelle ausschließlich abgelaufene Schlüssel entfernen und, wenn die Tabelle danach weiterhin belegt ist, die Anfrage eines neuen Schlüssels nach Kriterium 3 abweisen, anstatt den Zählerstand eines noch aktiven Schlüssels zu verwerfen.
14. IF die Schlüsseltabelle der Fehlversuchs-Sperre nach dem Entfernen abgelaufener Schlüssel weiterhin belegt ist, THEN THE Rate_Limiter SHALL weitere Fehlversuche in einem gemeinsamen Überlaufzähler führen und die Fehlversuche sowie Sperren der bereits verfolgten Quell-IP-Adressen unverändert erhalten.

> Hinweis zu den Kriterien 2 und 5: Die genannten Zahlenwerte sind vorläufige,
> begründete Vorgabewerte und keine fachliche Vorgabe. Sie leiten sich aus der
> Mindestpause von 300 Millisekunden je Gerät und der Cache-Gültigkeitsdauer von
> 10 Sekunden ab und sind nach dem ersten Betrieb zu revidieren.

### Requirement 14: Transportverschlüsselung und Betriebsumgebung

**User Story:** As a Betreiber, I want eine verbindliche Aussage zur
Transportverschlüsselung, so that die Bearer-Token nicht im Klartext über ein nicht
vollständig vertrauenswürdiges Netz laufen.

#### Acceptance Criteria

1. THE Anwendung SHALL die Terminierung von TLS einem vorgeschalteten Reverse Proxy überlassen und selbst ausschließlich unverschlüsseltes HTTP anbieten.
2. THE Projektdokumentation SHALL die TLS-Terminierung an einem vorgeschalteten Reverse Proxy als verbindlich benennen, sofern die Bindeadresse keine Loopback-Adresse ist (ASVS `v5.0.0-12.1.1`).
3. IF die Bindeadresse keine Loopback-Adresse ist und die Einstellung `BEHIND_REVERSE_PROXY` deaktiviert ist, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die die Bindeadresse, die fehlende Bestätigung des Betriebs hinter einem TLS-terminierenden Reverse-Proxy und den Namen der Einstellung nennt.
4. WHEN die Anwendung mit einer Bindeadresse startet, die keine Loopback-Adresse ist, und die Einstellung `BEHIND_REVERSE_PROXY` aktiviert ist, THE Anwendung SHALL protokollieren, dass der Betrieb hinter einem TLS-terminierenden Reverse-Proxy durch den Betreiber bestätigt wurde.
5. THE Projektdokumentation SHALL die Bindeadresse `0.0.0.0` ohne vorgeschalteten TLS-Proxy als Fehlkonfiguration benennen.
6. THE Konfigurationslader SHALL für die Bindeadresse den Vorgabewert `127.0.0.1` verwenden.
7. THE Compose-Datei SHALL die Veröffentlichung des Ports ausschließlich an eine Loopback-Adresse des Hosts binden und die TLS-Terminierung als Aufgabe des Reverse Proxy ausweisen.
8. THE REST_API SHALL den HTTP-Antwortheader `Cache-Control` mit dem Wert `no-store` für jede Antwort fachlicher Endpunkte setzen (ASVS `v5.0.0-3.4.5`).

> Begründung zu Kriterium 3: Eine Warnung, die den Start nicht verhindert, macht aus
> einer verbindlichen Vorgabe eine Empfehlung. Wer den Dienst an eine erreichbare
> Adresse bindet, ohne den Betrieb hinter einem TLS-terminierenden Reverse-Proxy bestätigt zu haben, legt Bearer-Token im
> Klartext offen. Der Start wird deshalb abgebrochen. Die Einstellung
> `BEHIND_REVERSE_PROXY` bleibt die einzige Stelle, an der der Betreiber die
> Verantwortung ausdrücklich übernimmt.

### Requirement 15: Zwischenspeicherung von Messwerten

**User Story:** As a Betreiber, I want dass wiederholte Abfragen desselben Messwerts
aus einem Zwischenspeicher bedient werden, so that die Wechselrichter auch bei vielen
Aufrufern nicht überlastet werden.

#### Acceptance Criteria

1. WHEN der Protokoll_Adapter einen Messwert erfolgreich gelesen hat, THE Cache SHALL den Wert zusammen mit Gerätekennung und Zeitstempel der Messung ablegen.
2. WHEN ein Aufrufer einen Messwert anfragt, dessen zwischengespeicherter Wert für dieses Gerät höchstens so alt wie die konfigurierte Gültigkeitsdauer ist, THE REST_API SHALL den zwischengespeicherten Wert zurückgeben und keine Transaktion auslösen.
3. THE Cache SHALL eine je Messwert konfigurierbare Gültigkeitsdauer unterstützen, deren Vorgabewert 10 Sekunden beträgt.
4. THE REST_API SHALL in jeder Messwertantwort das mit der monotonen Uhr gemessene Alter des Werts im Feld `age_seconds` in Sekunden ausweisen.
5. THE Cache SHALL eine konfigurierbare Nachfrist unterstützen, deren Vorgabewert 120 Sekunden beträgt.
6. IF eine Transaktion fehlschlägt und ein zwischengespeicherter Wert innerhalb der Nachfrist vorliegt, THEN THE REST_API SHALL den HTTP-Statuscode 200, den zwischengespeicherten Wert, das Feld `stale` mit dem Wert `true`, das Feld `source` mit dem Wert `cache` und im Feld `stale_reason` einen maschinenlesbaren Grund der fehlgeschlagenen Aktualisierung zurückgeben.
7. IF eine Transaktion fehlschlägt und kein zwischengespeicherter Wert innerhalb der Nachfrist vorliegt, THEN THE REST_API SHALL den HTTP-Statuscode 502 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel der Fehlerursache zurückgeben.
8. THE REST_API SHALL das Feld `stale` mit dem Wert `false` ausgeben, wenn der gelieferte Wert innerhalb der konfigurierten Gültigkeitsdauer liegt.
9. THE REST_API SHALL als Werte des Feldes `stale_reason` die Schlüssel `device_timeout`, `device_unreachable`, `protocol_error`, `queue_timeout` und `device_maintenance` verwenden.
10. WHEN ein periodisch gelieferter Wert eintrifft, THE Cache SHALL diesen Wert mit Gerätekennung und Zeitstempel ablegen.
11. THE Cache SHALL die Nachfrist ab dem Ende der Gültigkeitsdauer rechnen und einen zwischengespeicherten Wert bis zum Ablauf der Summe aus Gültigkeitsdauer und Nachfrist als Ersatzantwort zulassen, unabhängig davon, ob die Nachfrist kürzer oder länger als die Gültigkeitsdauer ist.
12. WHEN mehrere Anfragen ohne den Abfrageparameter `fresh` gleichzeitig dieselbe Kombination aus Gerätekennung und Messwertname betreffen und der zwischengespeicherte Wert dieser Kombination die Gültigkeitsdauer überschritten hat, THE Anwendung SHALL höchstens eine Lesetransaktion für diese Kombination auslösen und alle weiteren dieser Anfragen mit dem Ergebnis dieser Lesetransaktion beantworten.
13. WHEN eine nach Kriterium 12 ausgelöste Lesetransaktion unmittelbar vor der Übergabe ihres Request-Frames an das Send_Gate steht, THE Anwendung SHALL den Cache erneut prüfen und die Übergabe auslassen, sofern inzwischen ein Wert innerhalb der Gültigkeitsdauer vorliegt.
14. WHEN eine nach Kriterium 13 ausgelassene Übergabe vorliegt, THE REST_API SHALL den zwischengespeicherten Wert mit dem Feld `source` und dem Wert `cache` zurückgeben.
15. FOR ALL Anzahlen gleichzeitiger Anfragen ohne `fresh` auf dieselbe Kombination SHALL höchstens ein gemeinsamer Lesevorgang laufen; Wiederholversuche dieses Vorgangs folgen Requirement 8. Je Lesevorgang wird höchstens ein Request-Frame gesendet: keiner bei erfolgreicher Cache-Nachprüfung nach Kriterium 13, sonst genau einer (ohne Wiederholversuche; Wiederholversuche folgen Requirement 8).
16. WHEN ein einzelner Aufrufer den Einzelflug verlässt oder abbricht, THE Anwendung SHALL den gemeinsamen Lesevorgang für die übrigen Warter fortsetzen und den Eintrag erst nach dessen Abschluss entfernen.

> Begründung zu Kriterium 6: Eine Ersatzantwort aus dem Cache ist eine erfolgreich
> beantwortete Anfrage mit eingeschränkter Aktualität, kein gescheiterter Aufruf. Ein
> Fehlerstatus würde Clients den Wert entziehen, obwohl er innerhalb der Nachfrist
> fachlich brauchbar ist. Die Einschränkung wird deshalb nicht über den Statuscode,
> sondern über die Felder `stale`, `age_seconds`, `source` und `stale_reason`
> transportiert, die ein Client auswerten muss.

### Requirement 16: Betriebszustand, Bereitschaft und Heartbeat

**User Story:** As a Betreiber, I want den Zustand des Dienstes und der
Geräteverbindungen von außen prüfen können, so that ich Störungen früh erkenne, ohne
bei ruhendem Betrieb Fehlalarme zu erhalten.

#### Acceptance Criteria

1. THE Health_Endpunkt SHALL unter `GET /health` ohne Token-Authentifizierung erreichbar sein.
2. WHEN der Health_Endpunkt aufgerufen wird, THE Health_Endpunkt SHALL den HTTP-Statuscode 200 zurückgeben, solange der HTTP-Server Anfragen annimmt.
3. THE REST_API SHALL unter `GET /api/v1/readiness` je Gerät den Zeitpunkt der letzten erfolgreichen Transaktion, den Zeitpunkt des letzten Heartbeats, die Anzahl der aufeinanderfolgenden Fehlversuche, die aktuelle Länge der Warteschlange, den Gerätezustand nach Kriterium 11 und den Verdacht auf Fremdzugriff ausweisen.
4. THE Protokoll_Adapter SHALL je Gerät in einem konfigurierbaren Intervall, dessen Vorgabewert 60 Sekunden beträgt, einen Heartbeat als Lesetransaktion auf einen konfigurierbaren Messwert der Objekt_Registry ausführen.
5. WHEN im Heartbeat-Intervall bereits eine erfolgreiche Transaktion für ein Gerät stattgefunden hat, THE Protokoll_Adapter SHALL den Heartbeat für dieses Intervall überspringen.
6. WHEN im Heartbeat-Intervall für ein Gerät mindestens ein Frame der Frame_Klasse `Periodischer_Wert` mit gültiger CRC-Prüfsumme eingetroffen ist, THE Protokoll_Adapter SHALL den Heartbeat für dieses Intervall überspringen und diesen Frame als Nachweis der Gerätekommunikation werten.
7. WHEN ein Heartbeat nach Kriterium 6 übersprungen wurde, THE Bereitschafts_Endpunkt SHALL für dieses Gerät den Zustand `ok` ausweisen und im Feld `liveness_source` den Wert `periodic` ausgeben.
8. IF der letzte Heartbeat eines Geräts fehlgeschlagen ist und die Anzahl der aufeinanderfolgenden fehlgeschlagenen Heartbeats eine konfigurierbare Obergrenze erreicht, deren Vorgabewert 3 beträgt, THEN THE Bereitschafts_Endpunkt SHALL den HTTP-Statuscode 503 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `not_ready` zurückgeben.
9. WHEN der Bereitschafts_Endpunkt den HTTP-Statuscode 503 zurückgibt, THE Bereitschafts_Endpunkt SHALL die Angaben nach Kriterium 3 je Gerät im Erweiterungsfeld `devices` der Problem_Details ausgeben.
10. WHILE alle konfigurierten Geräte einen erfolgreichen letzten Heartbeat aufweisen, THE Bereitschafts_Endpunkt SHALL den HTTP-Statuscode 200 zurückgeben, auch wenn kein Aufrufer Anfragen stellt.
11. THE Bereitschafts_Endpunkt SHALL einen Zustand ohne Verkehr von einem Kommunikationsfehler unterscheiden und je Gerät den Zustand `ok`, `degraded`, `unreachable` oder `maintenance` ausweisen.
12. WHILE ein Transport_Endpunkt im Sperrzustand ist, THE Bereitschafts_Endpunkt SHALL für jedes Gerät dieses Transport_Endpunkts den Zustand `maintenance` ausweisen und die protokollbezogene Ursache auslassen.
13. WHILE ein Gerät seit dem Start noch keinen Heartbeat abgeschlossen hat, THE Bereitschafts_Endpunkt SHALL für dieses Gerät den Zustand `starting` ausweisen und den HTTP-Statuscode 503 mit Problem_Details nach Kriterium 8 und Kriterium 9 zurückgeben.
14. THE REST_API SHALL jede eingehende Anfrage mit Zeitstempel, Methode, Pfad, HTTP-Statuscode, Bearbeitungsdauer, Korrelations_ID und Token_Kennung protokollieren (ASVS `v5.0.0-16.2.1`).
15. THE REST_API SHALL Protokollausgaben in einem konfigurierbaren Format mit einem konfigurierbaren Mindest-Protokollierungsgrad auf die Standardausgabe schreiben.
16. THE Anwendung SHALL je Gerät die Anzahl der Transaktionen, die Anzahl der Fehlversuche und die Anzahl der Treffer und Fehlschläge des Cache über den Bereitschafts_Endpunkt ausweisen.
17. THE Bereitschafts_Endpunkt SHALL die Anzahl verworfener Bytes, die Anzahl unerwarteter Frames, die Anzahl angemeldeter periodischer Anforderungen, Objekt_IDs, Transportadressen und Netzkennungen auslassen und diese Angaben dem Diagnosebereich nach Requirement 30 vorbehalten.

> Begründung zu den Kriterien 6 und 7: Ein eingetroffener periodischer Wert belegt die
> Erreichbarkeit des Geräts genauso gut wie eine selbst ausgelöste Lesetransaktion,
> erzeugt aber keine zusätzliche Last. Einen Heartbeat trotzdem zu senden,
> widerspräche der Schonungsgrenze des Architekturprinzips.

> Begründung zu den Kriterien 11, 12 und 17: Der Bereitschafts_Endpunkt gehört zum
> Herstellerneutralen_Vertrag. Ein Zustandswert, der den Bootloader eines bestimmten
> Herstellers benennt, und ein Zähler über Protokoll-Frames wären dort
> Protokolldetails und würden einen Monitoring-Client an dieses Gerät binden. Nach
> außen genügt die fachliche Aussage, dass das Gerät vorübergehend nicht angesprochen
> wird; das ist der Wartungszustand. Wer die Ursache braucht, findet sie im Protokoll
> und im Diagnosebereich nach Requirement 30.

### Requirement 17: Periodische Anforderungen und System-Schreibzugriffe

**User Story:** As a Betreiber, I want dass die Anwendung periodisch gesendete Werte
nutzen kann, ohne dafür die Schreibfreigabe für Anlagenparameter zu öffnen, so that
ich die Last auf den Wechselrichtern senke und trotzdem keine von außen auslösbaren
Schreibwege entstehen.

#### Acceptance Criteria

1. WHERE die Einstellung `ENABLE_PERIODIC_READS` aktiviert ist, THE Protokoll_Adapter SHALL für jeden in der Einstellung `PERIODIC_METRICS` genannten Messwert je Gerät eine periodische Anforderung mit dem Command-Byte `0x08` beziehungsweise `0x48` anmelden.
2. WHERE die Einstellung `ENABLE_PERIODIC_READS` deaktiviert ist, THE Protokoll_Adapter SHALL keine periodische Anforderung anmelden und die Objekt_ID `0x9C8FE559` (`pas.period`) nicht beschreiben.
3. THE Protokoll_Adapter SHALL je Gerät höchstens 64 periodische Anforderungen gleichzeitig angemeldet halten.
4. IF die Einstellung `PERIODIC_METRICS` für ein Gerät mehr als 64 Messwerte nennt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die die Gerätekennung, die genannte Anzahl und die Obergrenze 64 nennt.
5. THE Protokoll_Adapter SHALL je Verbindung und Objekt_ID genau eine periodische Anforderung senden.
6. WHEN der Protokoll_Adapter die Periodik für ein Gerät in Betrieb nimmt, THE Protokoll_Adapter SHALL die Objekt_ID `0x9C8FE559` (`pas.period`) als `t_uint32` mit dem konfigurierten Intervall in Sekunden beschreiben, bevor die erste periodische Anforderung gesendet wird.
7. WHEN die Anwendung beendet wird und für ein Gerät mindestens eine periodische Anforderung angemeldet ist oder der eigene Schreibzugriff auf die Objekt_ID `0x9C8FE559` erfolgreich war oder seinen Commit_Point erreicht hat, THE Protokoll_Adapter SHALL die Objekt_ID `0x9C8FE559` mit dem Wert 0 beschreiben, um alle periodischen Anforderungen dieses Geräts abzumelden und das gesetzte Intervall zurückzunehmen.
8. THE Anwendung SHALL System_Schreibzugriffe auf eine im Anwendungscode fest verankerte Liste beschränken, die ausschließlich die Protokoll_Steuervariable `pas.period` (`0x9C8FE559`) enthält.
9. THE Anwendung SHALL System_Schreibzugriffe unabhängig vom Zustand der Schreibfreigabe ausführen.
10. THE REST_API SHALL dokumentierte Protokoll_Steuervariablen bei aktivierter Schreibfreigabe über den normalen Schreibendpunkt zulassen; die Liste der internen System_Schreibzugriffe bleibt unveränderlich.
11. WHEN eine Verbindung zu einem Gerät neu aufgebaut wurde und die Periodik aktiviert ist, THE Protokoll_Adapter SHALL das Intervall erneut setzen und alle periodischen Anforderungen dieses Geräts erneut anmelden.
12. IF ein System_Schreibzugriff auf `pas.period` fehlschlägt, THEN THE Protokoll_Adapter SHALL die Periodik für das betroffene Gerät als nicht verfügbar führen, das Ereignis mit Gerätekennung und Fehlerursache protokollieren und die lesenden Transaktionen unverändert weiter bedienen.
13. WHEN ein periodisch gelieferter Wert eintrifft, THE Cache SHALL diesen Wert nach Requirement 15 ablegen, ohne eine Transaktion auszulösen.
14. THE REST_API SHALL den Abfrageparameter `fresh` als Beobachtete_Frische auslegen, nämlich als Zusage, einen nach dem Senden des Request-Frames dieser Anfrage am Transport_Endpunkt beobachteten Wert zu liefern.
15. WHERE ein Aufrufer den Abfrageparameter `fresh` mit dem Wert `true` übergibt, THE REST_API SHALL den Cache übergehen, eine Lesetransaktion auslösen und den ersten nach dem Senden des Request-Frames eintreffenden Frame mit passender Objekt_ID und, bei einem Plant_Frame, passender Netzkennung als Antwort annehmen, unabhängig davon, ob das Gerät diesen Frame auf die Leseanfrage oder aus der Periodik gesendet hat.
16. WHERE ein Aufrufer den Abfrageparameter `fresh` mit dem Wert `true` übergibt, THE REST_API SHALL in der Messwertantwort das Feld `freshness` mit dem Wert `observed` und das Feld `source` mit dem Wert `device` ausgeben.
17. WHERE die Einstellung `FRESH_PERIODIC_MODE` den Wert `reject` trägt und ein Aufrufer den Abfrageparameter `fresh` mit dem Wert `true` für einen Messwert mit angemeldeter periodischer Anforderung übergibt, THE REST_API SHALL den HTTP-Statuscode 409, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `fresh_not_available_for_periodic_metric` und den Namen des betroffenen Messwerts zurückgeben und keine Transaktion auslösen.
18. THE Konfigurationslader SHALL für die Einstellung `FRESH_PERIODIC_MODE` den Vorgabewert `observe` verwenden.
19. THE REST_API SHALL eine Zusage über die Ursache eines Frames auslassen und die Bedeutung des Abfrageparameters `fresh` in der OpenAPI-Beschreibung als zeitlich bestimmte Frische benennen.
20. IF `pas.period` erfolgreich beschrieben wurde, aber die Anmeldung einer einzelnen periodischen Anforderung fehlschlägt, THEN THE Protokoll_Adapter SHALL das Ereignis mit Gerätekennung, Messwertname und Fehlerursache protokollieren und diese Anmeldung innerhalb derselben Verbindung nicht wiederholen, weil Kriterium 5 je Verbindung und Objekt_ID genau eine Anforderung zulässt; die Anmeldung wird mit der vollständigen erneuten Anmeldung nach dem nächsten Verbindungsneuaufbau nach Kriterium 11 erneut versucht.
21. THE Protokoll_Adapter SHALL die Periodik eines Geräts als verfügbar führen, sobald mindestens eine periodische Anforderung angemeldet ist.
20. THE REST_API SHALL je Gerät die Anzahl der angemeldeten periodischen Anforderungen und die Verfügbarkeit der Periodik ausschließlich im Diagnosebereich nach Requirement 30 ausweisen.
22. WHERE die Einstellung `ENABLE_PERIODIC_READS` aktiviert ist (Vorgabe im Code) und `PERIODIC_METRICS` leer ist, THE Protokoll_Adapter SHALL je Gerät alle in der Objekt_Registry als Vorauswahl gekennzeichneten Messwerte mit numerischem Wertetyp periodisch anfordern; eine nicht leere Einstellung `PERIODIC_METRICS` hat Vorrang.
23. IF die Registry mehr als 64 numerische Messwerte als Vorauswahl kennzeichnet und `PERIODIC_METRICS` leer ist, THEN THE Konfigurationslader SHALL den Start mit dem Fehler `too_many_periodic_metrics` abbrechen, dessen Text die Anzahl und die Obergrenze 64 nennt.
24. THE Betriebsdokumentation SHALL darauf hinweisen, dass `pas.period` geräteglobal gilt und die Periodik anderer Clients am selben Gerät beeinflusst.
25. THE Protokoll_Adapter SHALL den Heartbeat (Requirement 16) unabhängig von der Periodik führen: Der Heartbeat liest `HEARTBEAT_METRIC_NAME` weiter als eigene, budgetfreie Lesetransaktion, auch wenn dieser Messwert (Vorgabe `inverter_state`) periodisch angemeldet ist.
26. THE Cache SHALL keinen periodisch angemeldeten Messwert unbegrenzt als frisch führen. Ersetzt die in design.md (Nachtrag 2026-10-03, Cache-Gültigkeit) beschriebene Regel, ein Eintrag gelte frisch, solange seine Anmeldung auf der aktuellen Verbindung besteht (`pin_when`); diese Regel entfällt.
27. WHILE ein Messwert auf der aktuellen Verbindung periodisch angemeldet ist und sein letztes Update älter ist als das Doppelte von `pas.period`, THE Protokoll_Adapter SHALL ihn in einem Hintergrundzyklus (Intervall 10 s) mit einer Lesetransaktion des Ursprungs `SYSTEM` nachlesen, ältesten Wert zuerst, und ihn je Intervall `pas.period` höchstens einmal anfragen; ein Wert, den das Gerät selbst regelmäßig liefert, löst keine Lesetransaktion aus.
28. THE Protokoll_Adapter SHALL je Zyklus und Gerät höchstens 8 Nachlesetransaktionen senden, den Zyklus beenden, sobald ein Aufrufer in der Warteschlange steht oder zwei Nachlesetransaktionen in Folge fehlschlagen, und die Nachlesetransaktionen weder gegen das Arbeitsbudget (Requirement 6) zählen noch von ihm abhängig machen, weil sie wie Heartbeat und Anmeldung durch ihre eigene Grenze und die Mindestpause am Send_Gate begrenzt sind und Aufrufer nicht verdrängen dürfen. Bei 40 Messwerten und ausschließlich stillen Werten entspricht das höchstens 40 Lesetransaktionen je 60 s.
29. THE Cache SHALL einen auf der aktuellen Verbindung periodisch angemeldeten Messwert als `FRESH` führen, solange sein letztes Update (periodisch oder per Lesetransaktion) höchstens das Dreifache von `pas.period` zurückliegt; danach gilt die Nachfrist nach Requirement 15 Kriterium 11 ab dem Ende dieses Fensters, `rct_device_metric_age_seconds` wird nach Requirement 20 ausgegeben und der Wert verlässt den Export mit Ablauf der Nachfrist. Eine verlorene Anmeldung (neue Verbindung) beendet das Fenster; es gilt dann die Gültigkeitsdauer nach Requirement 15.
30. WHEN ein Nachlesen scheitert, THE Protokoll_Adapter SHALL den Cache nicht verändern und den Wert nach Kriterium 29 altern lassen.

> Begründung zu den Kriterien 26 bis 30: Messung am Gerät (2026-10-03, 00:38 bis 00:52):
> `energy_e_load_day` und `energy_e_grid_load_day` standen seit dem Start unverändert in
> `/metrics`, ein `fresh=true`-Read lieferte rund 38 Wh mehr. Das Gerät sendet manche
> Werte nach der Anmeldung nicht mehr; die unbegrenzte Frische machte veraltete Werte
> unsichtbar. Gewählt ist ein begrenztes Fenster (3 x `pas.period`) plus Nachlesen ab
> 2 x `pas.period`, damit jeder Wert höchstens etwa 2 bis 3 Minuten alt ist.

> Begründung zu den Kriterien 14 bis 19: Die Quelle beschreibt den Frame-Aufbau als
> Start-Byte, Command-Byte, Längenfeld, wahlweise Adressfeld, Objekt_ID, Daten und
> CRC. Eine Transaktions-, Sequenz- oder Anfragekennung kommt darin nicht vor, und die
> unaufgefordert gesendeten Antworten einer periodischen Anforderung treffen nach
> Tabelle 5 als gewöhnliche `RESPONSE`-Frames ein. Bei gleicher Objekt_ID ist deshalb
> nicht feststellbar, ob ein eingetroffener Frame auf die gerade gesendete Leseanfrage
> oder auf den Periodik-Zeitgeber des Geräts zurückgeht. Die bisherige Zusage, einen
> periodisch gelieferten Wert bei `fresh=true` nicht zu verwenden, war damit nicht
> erfüllbar.
>
> Gewählt ist die zeitlich bestimmte Frische. Sie ist ohne Transaktionskennung
> umsetzbar, weil sie allein auf der Reihenfolge am Transport_Endpunkt beruht: Der
> Request-Frame wird gesendet, und der erste danach eintreffende passende Frame gilt
> als Antwort. Sie macht einen periodisch angemeldeten Messwert nicht unerreichbar und
> erzeugt keine zusätzliche Gerätelast, weil die Quelle für `READ PERIODICALLY` ohnehin
> festhält, dass spätere gleichartige Anforderungen ignoriert werden und die erste
> Antwort unmittelbar eintrifft. Der Aufrufer erfährt über das Feld `freshness`
> ausdrücklich, dass die Frische zeitlich und nicht ursächlich bestimmt ist.
>
> Die strengere Variante bleibt über `FRESH_PERIODIC_MODE` mit dem Wert `reject`
> wählbar. Sie ist nicht die Vorgabe, weil sie genau jene Messwerte unerreichbar
> macht, die der Betreiber zur Entlastung des Geräts in die Periodik aufgenommen hat.

> Begründung zu den Kriterien 8 bis 10: Die Periodik ist ohne Schreibzugriff auf
> `pas.period` nicht einrichtbar und beim Beenden nicht abbaubar. Dieser Schreibweg
> bleibt dennoch von der Schreibfreigabe ausgenommen, weil er auf genau eine
> Protokoll_Steuervariable begrenzt ist, keinen Anlagenparameter und keinen
> Batterieparameter verändert, nicht von außen parametrierbar ist und über die
> REST_API nicht auslösbar ist. Wer die Periodik nicht wünscht, schaltet sie über
> `ENABLE_PERIODIC_READS` ab; dann findet kein System_Schreibzugriff statt.

### Requirement 18: Anlagennetz und Slave-Struktur

**User Story:** As a Betreiber einer Anlage mit mehreren Wechselrichtern, I want die
im Anlagennetz erreichbaren Geräte samt Kennung und Kerndaten abrufen, so that ich
die Netzkennungen für die Konfiguration nicht raten muss.

#### Acceptance Criteria

1. THE REST_API SHALL unter `GET /api/v1/vendor/rct/devices/{device_id}/slaves` im Diagnosebereich nach Requirement 30 die im Anlagennetz des angefragten Master-Geräts erfassten Slave-Geräte samt Netzkennung ausgeben.
2. THE Protokoll_Adapter SHALL die Antwort auf eine Leseanfrage der Objekt_ID `0xC0A7074F` (`net.slave_data`) anhand des Datentyps `t_struct` mit der Strukturkennung `slave_data` nach Requirement 4 als Slave_Struktur mit 108 Byte dekodieren und nicht als Zeichenkette.
3. THE Protokoll_Adapter SHALL die Slave_Struktur mit den Feldern Netzkennung (Offset 0, `t_uint32`), Gerätename (Offset 4, 24 Byte Zeichenkette), AC-Leistung in Watt (Offset 28, `t_float`), Batterieleistung in Watt (Offset 32, `t_float`), Batterieladezustand als Verhältniswert (Offset 36, `t_float`), Fehlerindex (Offset 40, `t_uint16`), bitkodierte Ausstattungsangabe (Offset 42, `t_uint8`), Gerätezustand (Offset 43, `t_uint8`), externe Leistung in Watt (Offset 44, `t_float`), Softwareversion (Offset 48, 16 Byte Zeichenkette), Seriennummer (Offset 64, 16 Byte Zeichenkette), Vollständigkeitsanzeige (Offset 80, `t_uint32`) und Softwareversion des Batteriemanagements (Offset 84, `t_uint32`) dekodieren.
4. THE Protokoll_Adapter SHALL die 20 Byte ab Offset 88 der Slave_Struktur als reserviert behandeln und nicht ausgeben.
5. THE Protokoll_Adapter SHALL die Zeichenkettenfelder der Slave_Struktur am ersten Null-Byte enden lassen und die Zeichenkodierung nach Requirement 5 anwenden.
6. THE Protokoll_Adapter SHALL alle Mehrbyte-Felder der Slave_Struktur in Little-Endian-Reihenfolge (LSBF) und die Gleitkommafelder nach IEEE 754 mit einfacher Genauigkeit ebenfalls in Little-Endian-Reihenfolge dekodieren. Das weicht bewusst von der MSBF-Reihenfolge der Frames und der übrigen Werte nach Requirement 5 Kriterium 1 ab, die unverändert gilt.
7. IF die Nutzdatenlänge einer Antwort auf die Objekt_ID `0xC0A7074F` von 108 Byte abweicht, THEN THE Protokoll_Adapter SHALL die Dekodierung abbrechen und einen Protokollfehler mit erwarteter und empfangener Länge melden.
8. THE Protokoll_Adapter SHALL jede Antwort auf die Objekt_ID `0xC0A7074F` als Angabe zu genau einem Slave-Gerät behandeln.
9. WHEN die REST_API die Slave-Geräte eines Master-Geräts ausgibt, THE Protokoll_Adapter SHALL die Leseanfrage der Objekt_ID `0xC0A7074F` wiederholen, bis in einer konfigurierbaren Anzahl aufeinanderfolgender Abrufe keine neue Netzkennung auftritt, deren Vorgabewert 3 beträgt.
10. THE Protokoll_Adapter SHALL die Abrufe nach Kriterium 9 spätestens nach 31 verschiedenen Netzkennungen und spätestens nach einer konfigurierbaren Höchstzahl von Abrufen beenden, deren Vorgabewert 40 beträgt.
11. THE Protokoll_Adapter SHALL jeden Abruf nach Kriterium 9 als Lesetransaktion über den Zugriffsserialisierer ausführen und die Mindestpause nach Requirement 6 einhalten.
12. IF ein Abruf nach Kriterium 9 fehlschlägt, THEN THE REST_API SHALL den HTTP-Statuscode 200, die bis dahin erfassten Slave-Geräte, das Feld `complete` mit dem Wert `false` und den Fehlerschlüssel des fehlgeschlagenen Abrufs zurückgeben.
13. WHEN die Abrufe nach Kriterium 9 ohne Fehler beendet wurden, THE REST_API SHALL das Feld `complete` mit dem Wert `true` zurückgeben.
14. THE REST_API SHALL die bitkodierte Ausstattungsangabe aus Offset 42 in die benannten Wahrheitswerte `battery_supported` (Bit 0), `battery_connected` (Bit 1), `dc_supported` (Bit 2) und `external_power` (Bit 3) zerlegen.
15. THE Cache SHALL das Ergebnis der Abrufe nach Kriterium 9 mit einer eigenen, konfigurierbaren Gültigkeitsdauer ablegen, deren Vorgabewert 300 Sekunden beträgt.
16. FOR ALL gültigen Belegungen der Slave_Struktur SHALL das Dekodieren der kodierten 108 Byte dieselben Feldwerte liefern wie die ursprünglichen Feldwerte, bei Gleitkommafeldern bis auf die Genauigkeit der einfachen Genauigkeit (Round-Trip-Eigenschaft).

> Begründung zu den Kriterien 2, 4, 6 und 7: Die Quelle beschreibt 104 Byte und gibt die
> Byte-Reihenfolge der Struktur nicht abweichend von den Frames an. Am Gerät (Firmware
> 2.3.5687) wurde geprüft: `net.slave_data` liefert 108 Byte, alle numerischen Felder und
> die Gleitkommazahlen einfacher Genauigkeit liegen in Little-Endian-Reihenfolge, der
> Wert an Offset 0 stimmte mit `net.id` überein, die Werte an den Offsets 28, 32 und 36
> mit den Livewerten, und die 20 Byte ab Offset 88 waren null (reserviert). Die
> Anforderungen folgen deshalb dem Gerät und nicht der Quelle.

### Requirement 19: Schreibende Endpunkte, Freigabeliste und Wertprüfung

**User Story:** As a Betreiber, I want eine Wertprüfung je freigegebener
Objekt_ID, so that ein fehlerhafter Aufruf die Batterie und die Anlage nicht
beschädigen kann.

#### Acceptance Criteria

1. WHERE die Schreibfreigabe aktiviert ist, THE REST_API SHALL unter `PUT /api/v1/devices/{device_id}/metrics/{metric_name}` das Beschreiben eines Messwerts anbieten.
2. WHERE die Schreibfreigabe deaktiviert ist, THE REST_API SHALL keinen schreibenden Endpunkt registrieren und Anfragen an die Schreibpfade nach Kriterium 1 und 7 mit der jeweils vorgesehenen HTTP-Methode mit dem HTTP-Statuscode 404 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `write_disabled` abweisen.
3. THE REST_API SHALL für jede Anfrage an einen schreibenden Endpunkt die Token_Rolle `read/write` nach Requirement 12 verlangen.
4. THE Freigabeliste SHALL je freigegebenem Messwert den Datentyp, den Wertebereich mit Minimum und Maximum beziehungsweise die zulässigen Enum-Werte und wahlweise eine Schrittweite festlegen.
5. IF der angefragte Messwert nicht in der Freigabeliste enthalten ist, THEN THE REST_API SHALL den HTTP-Statuscode 403, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `write_not_allowed` und den angefragten Messwertnamen zurückgeben.
6. IF die Objekt_Registry den angefragten Messwert als Aktionsvariable kennzeichnet und die Anfrage an den Endpunkt nach Kriterium 1 gerichtet ist, THEN THE REST_API SHALL den HTTP-Statuscode 409, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `metric_is_action` und den Pfad des Aktionsendpunkts zurückgeben und keine Transaktion auslösen.
7. WHERE die Schreibfreigabe aktiviert ist, THE REST_API SHALL unter `POST /api/v1/devices/{device_id}/actions/{action_name}` den Aktionsendpunkt für Aktionsvariablen anbieten.
8. THE REST_API SHALL über den Aktionsendpunkt ausschließlich jene Aktionsvariablen zulassen, die die Freigabeliste mit einzeln aufgezählten zulässigen Werten führt.
9. WHEN eine Anfrage an den Aktionsendpunkt angenommen wird, THE REST_API SHALL die Antwort nach Requirement 9 mit den Feldern `action_confirmed` und `action_note` ausgeben und den zurückgelesenen Wert nicht als Bestätigung der Handlung darstellen.
10. WHEN eine Anfrage an einen schreibenden Endpunkt eintrifft, THE REST_API SHALL den übergebenen Wert gegen den Wertebereich der Freigabeliste prüfen, bevor der Zugriffsserialisierer in Anspruch genommen wird.
11. IF der übergebene Wert außerhalb des Wertebereichs liegt, THEN THE REST_API SHALL den HTTP-Statuscode 422, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `value_out_of_range` und den zulässigen Wertebereich zurückgeben.
12. IF der übergebene Wert nicht dem Datentyp der Objekt_Registry entspricht, THEN THE REST_API SHALL den HTTP-Statuscode 422, Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `value_type_mismatch` und den erwarteten Werttyp zurückgeben.
13. IF der übergebene Wert des Datentyps `t_float` keine endliche Zahl ist, THEN THE REST_API SHALL den HTTP-Statuscode 422 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `value_not_finite` zurückgeben.
14. WHERE die Freigabeliste für einen Messwert eine Schrittweite festlegt und der übergebene Wert kein Vielfaches dieser Schrittweite ist, THE REST_API SHALL den HTTP-Statuscode 422 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `value_step_mismatch` zurückgeben.

> Begründung zu den Kriterien 6 bis 9: Ein `PUT` sagt zu, dass die Ressource danach
> den übergebenen Wert trägt. Bei einer Aktionsvariablen trifft das nicht zu: Das
> Gerät handelt nach der Quelle ausschließlich bei einer Änderung des Werts, und der
> Wert muss vor einer erneuten Ausführung erst auf 0 zurückgesetzt werden. Ein
> zurückgelesener Zahlenwert beweist deshalb nichts über die Handlung. Ein eigener
> Endpunkt mit der Methode `POST` benennt den Vorgang als Auslösung einer Handlung,
> nicht als Zustandsänderung, und macht die fehlende Bestätigung im Vertrag sichtbar.

15. WHERE die Freigabeliste für einen Messwert den Datentyp `t_enum` festlegt, THE Freigabeliste SHALL einen Rohwertebereich oder einzeln aufgezählte Rohwerte erlauben; Aktionsvariablen verlangen weiterhin Einzelwerte.
16. WHERE die Freigabeliste eine Aktionsvariable enthält, THE Freigabeliste SHALL die zulässigen Werte dieser Aktionsvariablen einzeln aufzählen.
17. WHEN die Anwendung startet, THE Konfigurationslader SHALL die Freigabeliste gegen ihr Schema prüfen und den Start mit einer Fehlermeldung abbrechen, wenn ein Eintrag den Datentyp oder bei numerischen Typen den Wertebereich beziehungsweise Einzelwerte vermisst.
18. IF die Freigabeliste einen Messwert nennt, den die Objekt_Registry nicht führt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die diesen Messwertnamen nennt.
19. IF der Datentyp eines Eintrags der Freigabeliste vom Datentyp desselben Eintrags der Objekt_Registry abweicht, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die beide Datentypen nennt.
20. THE Freigabeliste SHALL auch dokumentierte Protokoll_Steuervariablen zulassen; ein expliziter Schreibaufruf auf `pas_period` kann die periodischen Anforderungen verändern.
21. WHEN eine Anfrage an einen schreibenden Endpunkt den HTTP-Statuscode 403, 404, 409 oder 422 erhält, THE REST_API SHALL keine Transaktion auslösen.
22. WHEN eine Anfrage an einen schreibenden Endpunkt angenommen wird, THE Anwendung SHALL den Vorgang nach Requirement 9 mit Token_Kennung, Gerätekennung, Messwertnamen und Wert protokollieren.
23. THE Freigabeliste SHALL als JSON-Datei außerhalb des Anwendungscodes vorliegen.
24. FOR ALL Werten, die den Wertebereich der Freigabeliste verletzen, SHALL die Anzahl der gegen einen Transport_Endpunkt ausgeführten Schreibtransaktionen unverändert bleiben (Korrektheitseigenschaft).
25. THE Projekt SHALL alle 895 Objekt_IDs der ID-Tabelle des mitgelieferten Protokoll-PDFs v1.14 in `objects_read.json` und alle 894 skalaren Einträge in `objects_write_allowed.json` ausliefern; beide Dateien SHALL im Git-Repository und unter `/app` im Docker-Image enthalten sein. `net.slave_data` bleibt eine lesbare Diagnosestruktur.
26. THE Freigabeliste SHALL für Zahlen den darstellbaren Wertebereich des Datentyps zulassen und für `t_string` Zeichenketten ohne eingebettetes Null-Byte akzeptieren; diese Grenzen sind keine Zusage sicherer Betriebswerte oder geräteseitiger Schreibbarkeit, da die Quelle keine Schreibrechte je Objekt nennt.
27. THE REST_API SHALL Schreibzugriffe über `ENABLE_WRITE_SUPPORT=true` aktivieren und über `ENABLE_WRITE_SUPPORT=false` vollständig deaktivieren; die Vorgabe bleibt `false`, Token-Rollen bleiben wirksam.
28. THE Protokoll_Adapter SHALL für lange Zeichenketten LONG WRITE (0x03/0x43) wählen und String-Readback anhand des dekodierten Werts prüfen, damit Null-Terminierung oder Padding die Bestätigung nicht verhindern.
29. THE Freigabeliste SHALL für `com_service` die Werte 0 bis 20 gemäß Tabelle 8 zulassen und reservierte Werte als intern kennzeichnen; eine Wiederholung derselben Aktion verlangt einen expliziten Aufruf mit 0 vor der erneuten Auslösung.
30. THE Freigabeliste SHALL für `t_bool` und `t_string` keine Angaben zu Minimum, Maximum, Schrittweite oder zulässigen Werten tragen; ein solcher Eintrag bricht den Start ab, da diese Angaben nicht geprüft würden. Für ganzzahlige Typen und `t_enum` SHALL die Wertprüfung ausschließlich JSON-Ganzzahlen annehmen und einen Gleitkommawert, auch einen ganzzahligen wie `1.0`, mit `value_type_mismatch` abweisen.
31. THE Protokoll_Adapter SHALL die Bestätigung eines Schreibvorgangs anhand des dekodierten Werts prüfen: der gesendete Payload wird dekodiert und mit dem dekodierten Readback verglichen, sodass ein Wahrheitswert mit beliebigem Nicht-Null-Byte und die Rundung auf `t_float` (einfache Genauigkeit) keine Bestätigung verhindern.

### Requirement 20: Prometheus-kompatibler Metrik-Endpunkt

**User Story:** As a Betreiber, I want die Dienst- und Messwerte über eine
Prometheus-kompatible Pull-Schnittstelle abgreifen, so that ich den Dienst mit einem
Standard-Collector überwachen kann, ohne dass das Abgreifen zusätzliche Last auf den
Wechselrichtern erzeugt.

#### Acceptance Criteria

1. WHERE die Einstellung `ENABLE_METRICS_ENDPOINT` aktiviert ist, THE Metrik_Endpunkt SHALL unter `GET /metrics` Metriken im Prometheus-Textformat ausgeben.
2. THE Metrik_Endpunkt SHALL keine Transaktion auslösen.
3. THE Metrik_Endpunkt SHALL ausschließlich Einträge des Cache und interne Zähler lesen.
4. THE Metrik_Endpunkt SHALL den Zugriffsserialisierer nicht in Anspruch nehmen.
5. FOR ALL Abfragen des Metrik_Endpunkts SHALL die Anzahl der je Gerät ausgeführten Transaktionen unverändert bleiben (Korrektheitseigenschaft).
6. THE Metrik_Endpunkt SHALL die Dienstmetriken `rct_api_requests_total` als Counter, `rct_api_cache_hits_total` als Counter, `rct_api_cache_misses_total` als Counter, `rct_device_request_duration_seconds` als Histogram, `rct_device_errors_total` als Counter, `rct_device_last_success_timestamp_seconds` als Gauge, `rct_device_periodic_registrations` als Gauge, `rct_transport_queue_length` als Gauge, `rct_transport_budget_remaining` als Gauge, `rct_transport_bytes_discarded_total` als Counter und `rct_transport_unexpected_frames_total` als Counter ausgeben.
7. THE Metrik_Endpunkt SHALL jede Messwertmetrik als Gauge ausgeben.
8. THE Metrik_Endpunkt SHALL für die Metrik `rct_device_request_duration_seconds` die vom Prometheus-Textformat vorgegebenen Zeitreihen mit den Namensendungen `_bucket`, `_sum` und `_count` ausgeben.
9. THE Metrik_Endpunkt SHALL für die Zeitreihen mit der Namensendung `_bucket` zusätzlich das Label `le` ausgeben.
10. WHERE ein Eintrag der Objekt_Registry das Feld `prometheus_name` führt, THE Metrik_Endpunkt SHALL den dort genannten Namen als Metriknamen verwenden.
11. WHERE ein Eintrag der Objekt_Registry das Feld `prometheus_name` vermisst, THE Metrik_Endpunkt SHALL den Metriknamen aus dem Präfix `rct_`, dem normalisierten Namen des Messwerts und dem Namen der Basiseinheit bilden, beispielsweise `rct_battery_power_watts`, `rct_battery_soc_ratio` und `rct_grid_power_watts`.
12. THE Metrik_Endpunkt SHALL den Namen eines Messwerts normalisieren, indem jedes Zeichen außerhalb der Mengen `a` bis `z`, `0` bis `9` und `_` durch `_` ersetzt wird, Großbuchstaben in Kleinbuchstaben überführt werden, aufeinanderfolgende `_` zu einem einzelnen `_` zusammengefasst werden und ein führendes oder abschließendes `_` entfernt wird.
13. WHEN die Anwendung startet, THE Konfigurationslader SHALL alle Metriknamen nach den Kriterien 10 bis 12 bilden und den Start mit einer Fehlermeldung abbrechen, wenn zwei Messwerte denselben Metriknamen ergeben, wobei die Fehlermeldung beide Messwertnamen und den gemeinsamen Metriknamen nennt.
14. IF ein nach den Kriterien 10 bis 12 gebildeter Metrikname nicht dem Namensschema des Prometheus-Textformats entspricht, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die den Messwertnamen und den abgelehnten Metriknamen nennt.
15. THE Metrik_Endpunkt SHALL jede Messwertmetrik mit dem Label `device` ausgeben, dessen Wert die Gerätekennung ist.
16. THE Metrik_Endpunkt SHALL jede Metrik einer Größe, die zum Transport_Endpunkt gehört, namentlich die Länge der Warteschlange, das verbleibende Arbeitsbudget, die Anzahl verworfener Bytes und die Anzahl unerwarteter Frames, mit dem Namenspräfix `rct_transport_` und mit dem Label `endpoint` ausgeben, dessen Wert die Endpunktkennung ist.
17. THE Metrik_Endpunkt SHALL eine Metrik nach Kriterium 16 ohne das Label `device` ausgeben.
18. THE Metrik_Endpunkt SHALL jede Metrik einer Größe, die zu einem einzelnen Gerät gehört, mit dem Label `device` und ohne das Label `endpoint` ausgeben.
19. THE Metrik_Endpunkt SHALL als Wert des Labels `endpoint` die Endpunktkennung ausgeben und die Zieladresse sowie den Zielport eines Transport_Endpunkts auslassen.
20. THE Metrik_Endpunkt SHALL jeden Wert in der Basiseinheit seiner Größe ausgeben und Verhältniswerte mit der Namensendung `_ratio` führen.
21. THE Metrik_Endpunkt SHALL jede monoton steigende Zählermetrik mit der Namensendung `_total` führen.
22. THE Metrik_Endpunkt SHALL ausschließlich die Labelnamen `device`, `endpoint`, `metric`, `state` und `le` verwenden, wobei `state` ausschließlich an Aufzählungsmetriken nach Kriterium 39 erscheint und `le` ausschließlich an den Zeitreihen mit der Namensendung `_bucket` erscheint.
23. THE Metrik_Endpunkt SHALL Zeitstempel, Fehlertexte, Token_Kennungen, Objekt_IDs und Rohwerte einer Objekt_ID als Labelwert auslassen.
24. THE Metrik_Endpunkt SHALL einen Messwert ohne Eintrag im Cache auslassen.
25. THE Metrik_Endpunkt SHALL einen ausgelassenen Messwert weder mit dem Wert 0 noch mit dem Wert `NaN` ausgeben.
26. WHERE für einen Messwert ein Eintrag im Cache vorliegt, dessen Alter die Gültigkeitsdauer überschreitet und die Nachfrist einhält, THE Metrik_Endpunkt SHALL den Wert ausgeben und zusätzlich die Metrik `rct_device_metric_age_seconds` als Gauge mit den Labels `device` und `metric` ausgeben.
27. WHEN ein Cache-Eintrag nach Requirement 9 verworfen wurde, THE Metrik_Endpunkt SHALL den betroffenen Messwert auslassen, bis die anschließende Lesetransaktion den Eintrag neu gesetzt hat.
28. WHERE die Einstellung `METRICS_REQUIRE_TOKEN` aktiviert ist und die Peer_Adresse der Anfrage nicht in der Scrape_Vertrauensliste enthalten ist, THE Metrik_Endpunkt SHALL ein gültiges Bearer-Token nach Requirement 12 verlangen.
29. WHERE die Peer_Adresse einer Anfrage an den Metrik_Endpunkt in der Scrape_Vertrauensliste enthalten ist, THE Metrik_Endpunkt SHALL die Anfrage unabhängig von der Einstellung `METRICS_REQUIRE_TOKEN` ohne Bearer-Token beantworten.
30. WHERE die Einstellung `METRICS_REQUIRE_TOKEN` deaktiviert ist, THE Metrik_Endpunkt SHALL die Anfrage unabhängig von der Peer_Adresse ohne Bearer-Token beantworten.
31. THE Rate_Limiter SHALL Anfragen an den Metrik_Endpunkt in einem von der Anfragerate der fachlichen Endpunkte getrennten Zähler führen und auf einen konfigurierbaren Wert pro konfigurierbarem Zeitfenster begrenzen, dessen Vorgabewert 120 Anfragen pro 60 Sekunden beträgt.
32. THE Rate_Limiter SHALL Anfragen an den Metrik_Endpunkt im Zähler der Anfragerate der fachlichen Endpunkte auslassen.
33. THE Metrik_Endpunkt SHALL Token-Werte und Gerätadressen in seiner Ausgabe auslassen.
34. THE Metrik_Endpunkt SHALL die Auswahl der ausgegebenen Messwerte aus der Einstellung `METRICS_EXPOSED_NAMES` übernehmen und bei leerer Einstellung alle in der Objekt_Registry als Vorauswahl gekennzeichneten Messwerte ausgeben.
35. THE REST_API SHALL den HTTP-Antwortheader `Cache-Control` mit dem Wert `no-store` für die Antwort des Metrik_Endpunkts setzen.
36. IF die Einstellung `METRICS_EXPOSED_NAMES` einen nicht exportierbaren Messwertnamen oder denselben Messwertnamen mehrfach nennt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die die betroffenen Namen nennt, anstatt den Eintrag auszulassen oder die Metrik-Familie mehrfach auszugeben.
37. THE Objekt_Registry SHALL über das Feld `preselected` bestimmen, welche Messwerte der Metrik_Endpunkt bei leerer Einstellung `METRICS_EXPOSED_NAMES` ausgibt; die Auswahl wird nicht über Betreibereinstellungen gesteuert, und `METRICS_EXPOSED_NAMES`, `ENABLE_PERIODIC_READS` und `PERIODIC_METRICS` bleiben aus `settings.env` und Umgebung ausgeschlossen.
38. THE Metrik_Endpunkt SHALL einen Messwert mit dem Wert `string` oder `object` nicht ausgeben, auch wenn die Registry ihn als Vorauswahl kennzeichnet; Aufzählungswerte ohne `enum_labels` werden als Zahl ausgegeben, Aufzählungswerte mit `enum_labels` nach Kriterium 39.
39. THE Metrik_Endpunkt SHALL einen Aufzählungswert, für den die Registry `enum_labels` führt (etwa `inverter_state`), als StateSet ausgeben: je Bezeichner aus `enum_labels` eine Gauge-Zeitreihe desselben Metriknamens mit den Labels `device` und `state` (Wert = Bezeichner unverändert), mit dem Wert 1 für den aktuellen Zustand und 0 für alle anderen; Codes mit gleichem Bezeichner ergeben eine Zeitreihe; der numerische Code wird nicht ausgegeben. Ein Code ohne Bezeichner ergibt zusätzlich zu den Nullwerten die Zeitreihe `state="unknown"` mit dem Wert 1 und eine einmalige WARNING-Logzeile je Code; ein ausgelassener oder abgelaufener Wert nach den Kriterien 24 bis 27 erzeugt keine Zeitreihen.

> Begründung zu den Kriterien 2 bis 5: Ein Collector greift die Schnittstelle in
> festem Takt ab, typischerweise alle 15 Sekunden. Löste ein Scrape Transaktionen
> aus, so konkurrierte er dauerhaft mit den Anfragen der Aufrufer um den
> serialisierten Gerätezugriff und erzeugte genau die Überlast, die Requirement 6 und
> Requirement 15 verhindern. Der Metrik_Endpunkt ist deshalb eine reine Projektion
> des bereits vorhandenen Zustands.

> Begründung zu Kriterium 39: Ein Zahlencode ist in Dashboards nicht lesbar. Das StateSet-Muster
> (Prometheus/OpenMetrics) hält die Kardinalität fest (Anzahl Bezeichner plus `unknown`); der
> Heartbeat (Requirement 16) liest den Wert unabhängig vom Metrik_Endpunkt, daher entfällt keine
> numerische Variante. Der Code eines unbekannten Zustands bleibt nur im Log, damit eine neue
> Firmware keine Zeitreihen erzeugt. Die REST_API liefert weiter `enum_value` und `enum_label`.

> Begründung zu den Kriterien 24 und 25: Der Wert 0 ist ein Messwert und würde in
> Diagrammen und Alarmregeln als Messung gelesen, etwa als Leistung von null Watt.
> Ein fehlender Cache-Eintrag ist aber keine Messung, sondern das Fehlen einer
> Messung. `NaN` wäre zwar formal unterscheidbar, beendet aber in vielen
> Auswertungen eine Zeitreihe nicht erkennbar. Das Auslassen der Zeitreihe ist die
> einzige Darstellung, die ein Collector als Lücke und nicht als Wert behandelt.

> Begründete Annahme zu den Kriterien 28 bis 32: Der Metrik_Endpunkt verlangt im
> Vorgabezustand ein Bearer-Token, weil er Betriebsdaten und Messwerte offenlegt und
> Prometheus in der Scrape-Konfiguration eine `authorization`-Angabe unterstützt.
> Für Umgebungen, in denen der Collector kein Token mitführen kann, gibt die
> Scrape_Vertrauensliste eine auf Quelladressen begrenzte Freigabe. Die eigene,
> großzügigere Ratengrenze verhindert, dass ein regelmäßiger Scrape das Anfragebudget
> eines fachlichen Aufrufers verbraucht. Die Annahme ist revidierbar, falls die
> Zielumgebung den Endpunkt ausschließlich über ein getrenntes Netz anbietet.
>
> Die Präzedenz ist eindeutig: Ein Token ist erforderlich, es sei denn die
> Peer_Adresse liegt in der Scrape_Vertrauensliste. Die Scrape_Vertrauensliste ist
> damit die engere Ausnahme und nicht eine zweite, gleichrangige Regel.

> Begründung zu den Kriterien 8, 9 und 22: Ein Histogram erzeugt im
> Prometheus-Textformat zwingend die Zeitreihen `_bucket`, `_sum` und `_count` sowie
> am `_bucket` das Label `le`. Eine Labelliste, die `le` ausschließt, wäre mit einem
> Histogram unvereinbar. Die Metrik `rct_device_request_duration_seconds` ist dennoch
> als Histogram festgelegt, weil die Verteilung der Antwortzeiten eines Geräts die
> fachlich interessante Größe ist und ein Gauge nur den letzten Wert zeigt. Das Label
> `le` ist deshalb ausdrücklich zugelassen und auf die Zeitreihen mit der Namensendung
> `_bucket` begrenzt. Die Zeitreihen `_sum` und `_count` tragen kein zusätzliches
> Label und berühren die Labelliste nicht.

### Requirement 21: Nicht-Ziel Push in ein Zeitreihen-Backend

**User Story:** As a Betreiber, I want dass die Anwendung Messwerte nur bereitstellt
und nie selbst in eine Datenbank schreibt, so that der Dienst nicht an ein konkretes
Backend gebunden ist und bei dessen Ausfall nichts puffern oder wiederholen muss.

#### Acceptance Criteria

1. THE Anwendung SHALL Messwerte ausschließlich auf Anforderung über die REST_API und über den Metrik_Endpunkt bereitstellen.
2. THE Anwendung SHALL einen Timeseries_Push auslassen.
3. THE Anwendung SHALL eine Schreibverbindung zu InfluxDB und zu QuestDB auslassen.
4. THE Anwendung SHALL eine Ausgabe im InfluxDB-Line-Protocol auslassen.
5. THE Konfigurationsvertrag SHALL keine Einstellung für die Adresse, die Zugangsdaten, die Datenbank oder die Tabelle eines Zeitreihen-Backends führen.
6. THE Konfigurationslader SHALL jede Umgebungsvariable, die der Konfigurationsvertrag nicht führt, unberücksichtigt lassen und den Start nicht wegen ihres Vorhandenseins abbrechen.
7. WHEN die Anwendung startet und die Umgebung eine Variable mit dem Namensanfang `INFLUXDB_` oder `QUESTDB_` enthält, THE Anwendung SHALL einen Hinweis protokollieren, der den Namen der Variable nennt und den Timeseries_Push als Nicht-Ziel des Dienstes benennt.
8. THE Projektdokumentation SHALL den Timeseries_Push als Nicht-Ziel benennen und für die dauerhafte Speicherung auf den Metrik_Endpunkt und einen davon abfragenden Collector verweisen.

> Begründung zu den Kriterien 6 und 7: Umgebungsvariablen eines Zeitreihen-Backends
> können aus einer gemeinsam genutzten Umgebung stammen, etwa aus einer geteilten
> `env_file` oder aus den Variablen des Hosts. Den Start deswegen abzubrechen würde den
> Dienst in einer Umgebung unbrauchbar machen, in der er nichts falsch macht.
> Entscheidend für das Nicht-Ziel ist nicht die Abwesenheit solcher Variablen, sondern
> dass der Konfigurationsvertrag sie nicht führt und die Anwendung sie deshalb nicht
> auswertet. Der Hinweis im Protokoll macht eine vermutlich irrtümliche Erwartung
> sichtbar, ohne den Betrieb zu verhindern.

### Requirement 22: Konfigurationsvertrag

**User Story:** As a Betreiber, I want eine vollständige Übersicht aller
Einstellungen mit Typ, Vorgabewert, Grenzen und Einheit, so that ich den Dienst ohne
Blick in den Quellcode konfigurieren kann.

#### Acceptance Criteria

1. THE Konfigurationslader SHALL die im Konfigurationsvertrag mit `Betreiber = ja` gekennzeichneten Einstellungen aus Umgebungsvariablen und aus der Datei `settings.env` lesen und für alle übrigen Einstellungen den Vorgabewert des Konfigurationsvertrags im Anwendungscode fest verwenden.
2. WHEN eine Einstellung sowohl als Umgebungsvariable gesetzt ist als auch in `settings.env` vorkommt, THE Konfigurationslader SHALL den Wert der Umgebungsvariable verwenden.
3. WHEN die Anwendung startet, THE Konfigurationslader SHALL jede Einstellung gegen den im Konfigurationsvertrag festgelegten Typ und die dort festgelegten Grenzen prüfen.
4. IF der Wert einer Einstellung den festgelegten Typ oder die festgelegten Grenzen verletzt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die den Namen der Einstellung und den zulässigen Bereich nennt. Bei nicht geheimen Einstellungen nennt sie zusätzlich den abgelehnten Wert; bei `API_TOKENS` und anderen Zugangsdaten bleibt der Eingabewert vollständig verborgen, auch bei Parser- und Validierungsfehlern.
5. WHERE eine Einstellung fehlt und der Konfigurationsvertrag einen Vorgabewert nennt, THE Konfigurationslader SHALL den Vorgabewert verwenden.
6. IF eine Einstellung ohne Vorgabewert fehlt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die den Namen der Einstellung nennt.
7. WHEN die Anwendung startet, THE Anwendung SHALL die wirksame Konfiguration protokollieren und dabei Token-Werte durch die Token_Kennung ersetzen.
8. THE Projektdokumentation SHALL den Konfigurationsvertrag vollständig abbilden und für jede Einstellung einen Eintrag führen und in `settings.env.example` die Einstellungen mit `Betreiber = ja` auflisten.
9. THE Datei `settings.env.example` SHALL für Token und Zugangsdaten ausschließlich leere Werte führen.
10. THE Konfigurationslader SHALL ausschließlich die im Konfigurationsvertrag geführten Einstellungen auswerten und aus Umgebung und `settings.env` ausschließlich die mit `Betreiber = ja` gekennzeichneten übernehmen.
11. THE Anwendung SHALL einen Aufrufmodus bereitstellen, der die Konfiguration, die Objekt_Registry und die Freigabeliste prüft und sich ohne Start des HTTP-Servers mit dem Rückgabewert 0 bei fehlerfreier Prüfung und mit einem Rückgabewert ungleich 0 bei einem Fehler beendet.
12. IF die Einstellung `MAX_FRESH_METRICS_PER_REQUEST` einen größeren Wert als die Einstellung `MAX_METRICS_PER_REQUEST` trägt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die beide Namen und beide Werte nennt.
13. IF die Einstellung `READ_RETRY_BACKOFF_INITIAL_MS` einen größeren Wert als die Einstellung `READ_RETRY_BACKOFF_MAX_MS` trägt, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die beide Namen und beide Werte nennt.
14. THE Konfigurationslader SHALL die Einstellungen `CACHE_TTL_SECONDS` und `CACHE_GRACE_SECONDS` unabhängig voneinander innerhalb ihrer je eigenen Grenzen zulassen und keine Beziehung zwischen ihren Werten verlangen.
15. IF die Einstellung `SHUTDOWN_PERIODIC_RESERVE_SECONDS` einen Wert trägt, der nicht kleiner als die Einstellung `SHUTDOWN_GRACE_SECONDS` ist, THEN THE Konfigurationslader SHALL den Start mit einer Fehlermeldung abbrechen, die beide Namen und beide Werte nennt.
16. THE Konfigurationslader SHALL doppelte Gerätekennungen, doppelte physische Geräteschlüssel aus Transport_Endpunkt und Netzkennung sowie Gerätegruppen ohne genau ein unmittelbar angebundenes Gerät ohne Netzkennung beim Start abweisen.
17. IF eine Einstellung des Konfigurationsvertrags mit `Betreiber = nein` in der Umgebung oder in `settings.env` gesetzt ist, THEN THE Konfigurationslader SHALL den Wert ignorieren und den Namen der Einstellung, ohne ihren Wert, in einer Warnung beim Start nennen.
18. THE Bezeichnung "konfigurierbar" in diesem Dokument SHALL "im Konfigurationsvertrag festgelegt" bedeuten und nicht "vom Betreiber setzbar"; ob der Betreiber eine Einstellung setzen kann, bestimmt allein die Spalte `Betreiber`.

> Begründung zu den Kriterien 1, 10, 17 und 18: Die übrigen Werte sind Vorgabewerte des Vertrags, die sich mit dem Code ändern und nicht mit der Bereitstellung. Eine fest im Code geführte Vorgabe hält Grenzen und Querbedingungen prüfbar und verkleinert die vom Betreiber zu pflegende Fläche; die Warnung macht einen vermutlich irrtümlich gesetzten Wert sichtbar, ohne den Start zu verhindern.

> Begründung zu Kriterium 14: Die Nachfrist ist nach dem Glossary die Zeitspanne
> **nach** Ablauf der Gültigkeitsdauer. Beide Größen liegen damit hintereinander und
> nicht übereinander: Mit `CACHE_TTL_SECONDS` von 60 Sekunden und
> `CACHE_GRACE_SECONDS` von 10 Sekunden gilt ein Wert bis 60 Sekunden als frisch und
> von 60 bis 70 Sekunden als Ersatzantwort. Eine Forderung, die Nachfrist müsse
> mindestens so lang wie die Gültigkeitsdauer sein, hätte diese Reihenfolge als
> Überdeckung gelesen und eine fachlich sinnvolle Konfiguration verhindert.

Konfigurationsvertrag (Spalte `Betreiber`: `ja` = aus Umgebung und `settings.env` lesbar, `nein` = fester Vorgabewert im Code, siehe Kriterien 1, 10 und 17):

| Umgebungsvariable | Typ | Vorgabewert | Minimum | Maximum | Einheit | Betreiber |
| --- | --- | --- | --- | --- | --- | --- |
| `BIND_ADDRESS` | IP-Adresse | `127.0.0.1` | — | — | — | ja |
| `BIND_PORT` | Ganzzahl | `8000` | 1024 | 65535 | — | ja |
| `HTTP_WORKERS` | Ganzzahl | `1` | 1 | 1 | Arbeiter | nein |
| `LOG_LEVEL` | Auswahl aus `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` | — | — | — | ja |
| `LOG_FORMAT` | Auswahl aus `json`, `text` | `text` | — | — | — | nein |
| `DOCS_PUBLIC` | Wahrheitswert | `false` | — | — | — | nein |
| `BEHIND_REVERSE_PROXY` | Wahrheitswert | `false` | — | — | — | ja |
| `AUTH_REQUIRED` | Wahrheitswert | `true` | — | — | — | ja |
| `API_TOKENS` | Liste aus `token:rolle`, durch Komma getrennt | kein Vorgabewert (Pflicht, außer bei `AUTH_REQUIRED=false`) | 1 Eintrag | 32 Einträge | — | ja |
| `TRUSTED_PROXIES` | Liste aus IP-Adressen und Netzen | leer | — | 32 Einträge | — | ja |
| `FORWARDED_HEADER` | Zeichenkette | leer | — | — | — | ja |
| `RATE_LIMIT_REQUESTS` | Ganzzahl | `60` | 1 | 10000 | Anfragen | nein |
| `RATE_LIMIT_WINDOW_SECONDS` | Ganzzahl | `60` | 1 | 3600 | Sekunden | nein |
| `AUTH_FAIL_LIMIT` | Ganzzahl | `5` | 1 | 100 | Versuche | nein |
| `AUTH_FAIL_WINDOW_SECONDS` | Ganzzahl | `300` | 10 | 3600 | Sekunden | nein |
| `AUTH_FAIL_BLOCK_SECONDS` | Ganzzahl | `900` | 10 | 86400 | Sekunden | nein |
| `DEVICES` | Liste aus `kennung=host:port[@netzkennung]` | kein Vorgabewert | 1 Eintrag | 32 Einträge | — | ja |
| `OBJECT_REGISTRY_PATH` | Pfad | `/app/objects_read.json` | — | — | — | nein |
| `STRING_ENCODING` | Auswahl aus `utf-8`, `latin-1` | `utf-8` | — | — | — | nein |
| `CONNECT_TIMEOUT_SECONDS` | Gleitkommazahl | `3` | 0.5 | 30 | Sekunden | nein |
| `RESPONSE_TIMEOUT_SECONDS` | Gleitkommazahl | `5` | 0.5 | 60 | Sekunden | nein |
| `WRITE_RESPONSE_TIMEOUT_MS` | Ganzzahl | `300` | 50 | 5000 | Millisekunden | nein |
| `READ_TOTAL_TIMEOUT_SECONDS` | Gleitkommazahl | `20` | 1 | 300 | Sekunden | nein |
| `READ_RETRIES` | Ganzzahl | `4` | 0 | 10 | Wiederholversuche | nein |
| `READ_RETRY_BACKOFF_INITIAL_MS` | Ganzzahl | `200` | 10 | 10000 | Millisekunden | nein |
| `READ_RETRY_BACKOFF_MAX_MS` | Ganzzahl | `5000` | 10 | 60000 | Millisekunden | nein |
| `WRITE_RETRIES` | Ganzzahl | `2` | 0 | 5 | Wiederholversuche | nein |
| `MIN_REQUEST_INTERVAL_MS` | Ganzzahl | `300` | 0 | 10000 | Millisekunden | nein |
| `QUEUE_MAX_LENGTH` | Ganzzahl | `32` | 1 | 1000 | Anfragen | nein |
| `QUEUE_MAX_WAIT_SECONDS` | Gleitkommazahl | `10` | 0.5 | 120 | Sekunden | nein |
| `DEVICE_BUDGET_TRANSACTIONS` | Ganzzahl | `60` | 1 | 10000 | Transaktionen | nein |
| `DEVICE_BUDGET_WINDOW_SECONDS` | Ganzzahl | `60` | 1 | 3600 | Sekunden | nein |
| `SHUTDOWN_GRACE_SECONDS` | Gleitkommazahl | `20` | 1 | 300 | Sekunden | nein |
| `SHUTDOWN_PERIODIC_RESERVE_SECONDS` | Gleitkommazahl | `5` | 0 | 60 | Sekunden | nein |
| `MAX_FRAME_BYTES` | Ganzzahl | `4096` | 64 | 65535 | Byte | nein |
| `UNEXPECTED_FRAME_LIMIT` | Ganzzahl | `50` | 1 | 10000 | Frames | nein |
| `UNEXPECTED_FRAME_WINDOW_SECONDS` | Ganzzahl | `60` | 1 | 3600 | Sekunden | nein |
| `BOOTLOADER_COOLDOWN_SECONDS` | Ganzzahl | `300` | 10 | 86400 | Sekunden | nein |
| `HEARTBEAT_INTERVAL_SECONDS` | Ganzzahl | `60` | 10 | 3600 | Sekunden | nein |
| `HEARTBEAT_METRIC_NAME` | Messwertname aus der Objekt_Registry | `inverter_state` | — | — | — | nein |
| `HEARTBEAT_FAILURE_THRESHOLD` | Ganzzahl | `3` | 1 | 100 | Fehlversuche | nein |
| `CACHE_TTL_SECONDS` | Gleitkommazahl | `10` | 0 | 3600 | Sekunden | nein |
| `CACHE_GRACE_SECONDS` | Gleitkommazahl | `120` | 0 | 86400 | Sekunden | nein |
| `SLAVE_CACHE_TTL_SECONDS` | Ganzzahl | `300` | 0 | 86400 | Sekunden | nein |
| `SLAVE_DISCOVERY_STABLE_READS` | Ganzzahl | `3` | 1 | 31 | Abrufe | nein |
| `SLAVE_DISCOVERY_MAX_READS` | Ganzzahl | `40` | 1 | 200 | Abrufe | nein |
| `MAX_METRICS_PER_REQUEST` | Ganzzahl | `32` | 1 | 256 | Messwerte | nein |
| `MAX_FRESH_METRICS_PER_REQUEST` | Ganzzahl | `8` | 1 | 64 | Messwerte | nein |
| `FRESH_PERIODIC_MODE` | Auswahl aus `observe`, `reject` | `observe` | — | — | — | nein |
| `ENABLE_VENDOR_DIAGNOSTICS` | Wahrheitswert | `false` | — | — | — | nein |
| `PROBLEM_TYPE_BASE_URI` | Zeichenkette | `urn:device-api:problem` | — | — | — | nein |
| `CORRELATION_ID_HEADER` | Zeichenkette | `X-Request-Id` | — | — | — | nein |
| `FOREIGN_ACCESS_FRAME_THRESHOLD` | Ganzzahl | `10` | 1 | 10000 | Frames | nein |
| `ENABLE_WRITE_SUPPORT` | Wahrheitswert | `false` | — | — | — | ja |
| `WRITE_ALLOWLIST_PATH` | Pfad | `/app/objects_write_allowed.json` | — | — | — | nein |
| `ENABLE_PERIODIC_READS` | Wahrheitswert | `false` | — | — | — | nein |
| `PERIODIC_METRICS` | Liste aus Messwertnamen, durch Komma getrennt | leer | — | 64 Einträge | — | nein |
| `PERIODIC_INTERVAL_SECONDS` | Ganzzahl | `30` | 1 | 3600 | Sekunden | nein |
| `ENABLE_METRICS_ENDPOINT` | Wahrheitswert | `true` | — | — | — | ja |
| `METRICS_REQUIRE_TOKEN` | Wahrheitswert | `true` | — | — | — | nein |
| `METRICS_TRUSTED_SOURCES` | Liste aus IP-Adressen und Netzen | leer | — | 32 Einträge | — | nein |
| `METRICS_EXPOSED_NAMES` | Liste aus Messwertnamen, durch Komma getrennt | leer | — | 256 Einträge | — | nein |
| `METRICS_RATE_LIMIT_REQUESTS` | Ganzzahl | `120` | 1 | 10000 | Anfragen | nein |
| `METRICS_RATE_LIMIT_WINDOW_SECONDS` | Ganzzahl | `60` | 1 | 3600 | Sekunden | nein |

### Requirement 23: Abhängigkeiten und Paketverwaltung

**User Story:** As a Entwickler, I want die Abhängigkeiten an einer Stelle und
ausschließlich aus dem Paketindex, so that der Bau des Container_Image
nachvollziehbar und ohne Zugriff auf fremde Quellcodeverwaltungen gelingt.

#### Acceptance Criteria

1. THE Projekt SHALL alle Laufzeit- und Entwicklungsabhängigkeiten ausschließlich in `rct-manager/pyproject.toml` führen.
2. THE Projekt SHALL direkt importierte Laufzeitpakete wie `python-dotenv` und `starlette` deklarieren; der Metrik_Endpunkt erzeugt das Prometheus-Textformat selbst und benötigt kein `prometheus-client`.
3. THE Projekt SHALL jede Abhängigkeit aus dem Paketindex beziehen.
4. THE Projekt SHALL für jede Abhängigkeit eine Versionsangabe mit unterer und oberer Grenze führen.
5. THE Projekt SHALL eine Abhängigkeit über eine Quellcodeverwaltungs-Adresse auslassen.
6. THE Projekt SHALL eine Abhängigkeit zu einem fremden RCT-Client-Projekt auslassen.
7. THE Projekt SHALL eine Abhängigkeit zu einem Client eines Zeitreihen-Backends auslassen.
8. THE Projekt SHALL die Datei `requirements.txt` auslassen.
9. THE Projekt SHALL Python in der Version 3.13 oder höher verlangen.
10. THE Projekt SHALL in `rct-manager/pyproject.toml` ein Extra `dev` führen, das `ruff` mit einer Versionsangabe mit unterer und oberer Grenze festlegt.
11. THE Projekt SHALL eine Abhängigkeit zu einem Typprüfer auslassen.

> Begründung zu Kriterium 11: Dieses Repository führt keinen Typprüfer. Eine
> Abhängigkeit zu einem Typprüfer hier einzuführen würde eine Prüfstufe schaffen, die
> in den übrigen Projekten fehlt und deshalb weder gepflegt noch in einem
> Prüflauf erwartet wird. Die Prüfung erfolgt mit `ruff check .` und den im Design festgelegten `pytest`-Tests des neuen Dienstes.

### Requirement 24: Exklusives Transport-Eigentum und Send-Gate

**User Story:** As a Betreiber, I want dass genau eine Komponente der Anwendung die
Verbindung zu einem Wechselrichter besitzt und jeden Sendevorgang durch dieselbe
Engstelle führt, so that keine zweite Stelle im Programm die Serialisierung und die
Schonfrist umgehen kann.

#### Acceptance Criteria

1. THE Anwendung SHALL je Transport_Endpunkt genau eine Kommunikationsinstanz mit höchstens einer aktiven TCP-Verbindung betreiben.
2. THE Kommunikationsinstanz SHALL alleiniger Eigentümer der TCP-Verbindung ihres Transport_Endpunkts sein.
3. THE Anwendung SHALL Bytes auf der TCP-Verbindung eines Transport_Endpunkts ausschließlich durch die Kommunikationsinstanz dieses Transport_Endpunkts lesen und schreiben.
4. THE Anwendung SHALL jede Lesetransaktion, jede Schreibtransaktion, jeden Wiederholversuch, jede Lesetransaktion zur Feststellung des Zustands nach Requirement 9, jeden Heartbeat, jede Anmeldung und Abmeldung einer periodischen Anforderung und jeden System_Schreibzugriff ausschließlich über die Kommunikationsinstanz des betroffenen Transport_Endpunkts ausführen.
5. THE Kommunikationsinstanz SHALL sämtliche ausgehenden Request-Frames durch genau einen Sendepfad führen.
6. THE Kommunikationsinstanz SHALL zwischen zwei über ihren Sendepfad gesendeten Request-Frames am Send_Gate mindestens die durch `MIN_REQUEST_INTERVAL_MS` konfigurierte Zeitspanne einhalten, deren Vorgabewert 300 Millisekunden beträgt.
7. THE Kommunikationsinstanz SHALL die Zeitspanne nach Kriterium 6 ohne Ausnahme für Wiederholversuche, Heartbeats, System_Schreibzugriffe, Anmeldungen periodischer Anforderungen und Transaktionen des Abbaus beim Beenden einhalten.
8. WHEN eine TCP-Verbindung neu aufgebaut wurde, THE Kommunikationsinstanz SHALL die Zeitspanne nach Kriterium 6 auch vor dem ersten Request-Frame der neuen Verbindung einhalten.
9. IF für einen Transport_Endpunkt eine zweite Verbindung aufgebaut werden soll, THEN THE Anwendung SHALL den Aufbau unterlassen und das Ereignis mit der Kennung des Transport_Endpunkts protokollieren.
10. THE Anwendung SHALL je Transport_Endpunkt den Zeitpunkt des letzten gesendeten Request-Frames vorhalten.
11. THE Send_Gate SHALL den Commit_Point jeder Transaktion nach Requirement 9 setzen und der aufrufenden Verarbeitung mitteilen, ob der Commit_Point dieser Transaktion erreicht wurde.
12. FOR ALL Folgen von Anfragen, Wiederholversuchen, Heartbeats, Anmeldungen periodischer Anforderungen und System_Schreibzugriffen SHALL der zeitliche Abstand zwischen zwei an denselben Transport_Endpunkt gesendeten Request-Frames mindestens die konfigurierte Mindestpause betragen (Korrektheitseigenschaft).
13. FOR ALL Zeitpunkten des Betriebs SHALL die Anzahl der für einen Transport_Endpunkt offenen TCP-Verbindungen höchstens 1 betragen (Korrektheitseigenschaft).

> Begründung zu den Kriterien 6 und 7: Eine Schonfrist, die zwischen Transaktionen
> gilt, lässt offen, ob ein Wiederholversuch, ein Read-back, ein Heartbeat oder eine
> Periodik-Anmeldung eine eigene Transaktion ist. Genau diese Vorgänge entstehen aber
> gehäuft, wenn ein Gerät bereits überlastet ist. Die Invariante gehört deshalb an die
> Stelle, die niemand umgehen kann: an das Send_Gate, unmittelbar vor dem
> Schreibvorgang auf dem Socket. Dort ist sie unabhängig davon wirksam, welcher
> Programmteil den Request-Frame veranlasst hat.

### Requirement 25: Einheitlicher Fehlervertrag

**User Story:** As a Entwickler eines Fremdsystems, I want jede Fehlerantwort in
demselben Format mit einer stabilen Kennung, so that ich die Fehlerbehandlung meines
Clients einmal schreibe und sie für alle Endpunkte gilt.

#### Acceptance Criteria

1. THE REST_API SHALL jede Fehlerantwort mit einem HTTP-Statuscode ab 400 als Problem_Details nach RFC 9457 mit dem Medientyp `application/problem+json` ausgeben.
2. THE REST_API SHALL jede Fehlerantwort mit den Feldern `type`, `title`, `status`, `detail`, `instance`, `code`, `correlation_id` und `timestamp` ausgeben.
3. THE REST_API SHALL das Feld `code` mit einem Fehlerschlüssel aus der Tabelle der Fehlerschlüssel dieses Requirements belegen.
4. THE REST_API SHALL das Feld `type` aus der Einstellung `PROBLEM_TYPE_BASE_URI` und dem Fehlerschlüssel bilden.
5. THE Konfigurationslader SHALL für die Einstellung `PROBLEM_TYPE_BASE_URI` den Vorgabewert `urn:device-api:problem` verwenden, der den Namen des Herstellers und den Namen des Protokolls nach Requirement 30 Kriterium 2 auslässt.
6. THE REST_API SHALL das Feld `status` mit demselben Wert belegen wie den HTTP-Statuscode der Antwort.
7. THE REST_API SHALL das Feld `timestamp` mit dem Zeitpunkt des Auftretens des Fehlers als zeitzonenbewussten Zeitpunkt in UTC nach ISO 8601 belegen.
8. WHEN eine Anfrage den in der Einstellung `CORRELATION_ID_HEADER` genannten Header mit einem Wert aus den Zeichen `a` bis `z`, `A` bis `Z`, `0` bis `9`, `-` und `_` mit höchstens 64 Zeichen führt, THE REST_API SHALL diesen Wert als Korrelations_ID übernehmen.
9. IF eine Anfrage den in der Einstellung `CORRELATION_ID_HEADER` genannten Header vermisst oder mit einem Wert führt, der Kriterium 8 verletzt, THEN THE REST_API SHALL eine eigene Korrelations_ID erzeugen.
10. THE REST_API SHALL die Korrelations_ID in jeder Antwort im Header nach der Einstellung `CORRELATION_ID_HEADER` ausgeben.
11. THE REST_API SHALL die Korrelations_ID in jeder Protokollausgabe ausgeben, die zu der zugehörigen Anfrage gehört.
12. THE REST_API SHALL in einer Fehlerantwort Protokoll-Rohdaten, Frame-Inhalte, Dateipfade, Programmzustände, Stapelabbilder und Namen interner Komponenten auslassen.
13. THE REST_API SHALL in einer Fehlerantwort Token-Werte, Transportadressen, Transportports und Objekt_IDs auslassen.
14. WHEN die REST_API den HTTP-Statuscode 500 zurückgibt, THE REST_API SHALL im Feld `detail` ausschließlich einen gleichbleibenden Text ohne Angaben zur Ursache ausgeben und im Feld `correlation_id` die Korrelations_ID der Anfrage nennen.
15. WHEN die REST_API den HTTP-Statuscode 500 zurückgibt, THE Anwendung SHALL den vollständigen Fehlerverlauf einschließlich Ausnahmeart, Meldung und Stapelabbild unter derselben Korrelations_ID protokollieren.
16. WHEN die REST_API den HTTP-Statuscode 422 zurückgibt, THE REST_API SHALL im Feld `errors` je fehlerhafter Eingabe den Namen des Parameters, den Fehlerschlüssel und eine Beschreibung ausgeben.
17. THE REST_API SHALL eine teilweise erfolgreiche Sammelantwort nach Requirement 10 nicht als Problem_Details ausgeben und die fehlgeschlagenen Messwerte im Feld `errors` der Erfolgsantwort je mit Fehlerschlüssel aus der Tabelle dieses Requirements ausweisen.
18. THE REST_API SHALL eine Fehlerantwort mit einem HTTP-Statuscode ab 400 um ein Erweiterungsfeld nach RFC 9457 ergänzen können, sofern das Erweiterungsfeld den Vorgaben der Kriterien 12 und 13 genügt.
19. WHEN der Bereitschafts_Endpunkt den HTTP-Statuscode 503 mit dem Fehlerschlüssel `not_ready` zurückgibt, THE REST_API SHALL die Gerätezustände nach Requirement 16 im Erweiterungsfeld `devices` derselben Problem_Details ausgeben.
20. THE REST_API SHALL in einem Erweiterungsfeld ausschließlich herstellerneutrale Angaben nach Requirement 30 ausgeben.
21. THE OpenAPI-Beschreibung SHALL für jeden Endpunkt die möglichen Fehlerschlüssel, die möglichen Erweiterungsfelder und den Medientyp `application/problem+json` nennen.
22. FOR ALL Fehlerantworten mit einem HTTP-Statuscode ab 400 SHALL die Antwort den Medientyp `application/problem+json` und ein nicht leeres Feld `code` tragen (Korrektheitseigenschaft).
23. WHEN eine Sammelabfrage ohne verwertbare Werte mit `device_unavailable` scheitert, THE REST_API SHALL im Feld `errors` eine Liste von Messwertfehlern mit `name`, `code` und `detail` ausgeben; bei 422 enthält dasselbe Feld ausschließlich Eingabefehler mit `parameter`, `code` und `detail`.
24. WHEN eine Schreib- oder Aktionsanfrage mit `write_outcome_unknown` oder `action_outcome_unknown` scheitert, THE REST_API SHALL das Erweiterungsfeld `readback_value` mit dem zuletzt zurückgelesenen Wert oder `null` bei fehlendem Read-back ausgeben.
25. THE REST_API SHALL unbekannte Pfade als `not_found`, unzulässige Methoden als `method_not_allowed` und fehlerhafte Anfragekörper als `invalid_request` nach der Fehlerschlüssel-Tabelle ausgeben; bei 401 bleibt der Header `WWW-Authenticate: Bearer` erhalten.
26. THE REST_API SHALL einen Anfragekörper von mehr als 512 KiB vor der Authentifizierung und ohne vollständiges Einlesen mit dem HTTP-Statuscode 422 und dem Fehlerschlüssel `invalid_request` ablehnen.

> Begründung zu den Kriterien 18 bis 20: Der Bereitschafts_Endpunkt braucht im
> Fehlerfall mehr als einen Fehlerschlüssel, weil ein Betreiber wissen muss, welches
> Gerät nicht bereit ist. Eine eigene, von Problem_Details abweichende Antwortform für
> diesen einen Endpunkt hätte Requirement 25 Kriterium 1 zur Ausnahme gemacht und jeden
> Client gezwungen, zwei Fehlerformate zu behandeln. RFC 9457 sieht Erweiterungsfelder
> ausdrücklich vor; damit bleibt der Fehlervertrag ausnahmslos gültig.

Tabelle der Fehlerschlüssel:

| Fehlerschlüssel | Statuscode | Bedeutung |
| --- | --- | --- |
| `missing_token` | 401 | Der Header `Authorization` fehlt. |
| `invalid_token` | 401 | Das übermittelte Bearer-Token entspricht keinem konfigurierten Token. |
| `insufficient_scope` | 403 | Die Token_Rolle des Aufrufers deckt den angefragten Endpunkt nicht. |
| `write_not_allowed` | 403 | Der angefragte Messwert steht nicht in der Freigabeliste. |
| `not_found` | 404 | Der angefragte Pfad ist nicht registriert. |
| `method_not_allowed` | 405 | Die HTTP-Methode ist für diesen Pfad unzulässig; der Header `Allow` nennt die zulässigen Methoden. |
| `invalid_request` | 422 | Der Anfragekörper fehlt, ist kein gültiges JSON oder verletzt das Eingabeschema. |
| `unknown_device` | 404 | Die angefragte Gerätekennung ist nicht konfiguriert. |
| `unknown_metric` | 404 oder 422 | Der angefragte Messwertname steht nicht in der Objekt_Registry. 404 bei einem Pfadsegment, 422 bei einem Eintrag des Abfrageparameters `names`. |
| `write_disabled` | 404 | Die Schreibfreigabe ist deaktiviert; es ist kein schreibender Endpunkt registriert. |
| `docs_not_available` | 404 | Die Dokumentations_Endpunkte sind in dieser Betriebsumgebung nicht freigegeben. |
| `metric_is_action` | 409 | Der angefragte Messwert ist eine Aktionsvariable und über den Aktionsendpunkt zu beschreiben. |
| `fresh_not_available_for_periodic_metric` | 409 | Der Messwert ist periodisch angemeldet und `FRESH_PERIODIC_MODE` trägt den Wert `reject`. |
| `batch_too_large` | 422 | Die Anfrage nennt mehr Messwerte als `MAX_METRICS_PER_REQUEST`. |
| `fresh_batch_too_large` | 422 | Die Anfrage mit `fresh=true` nennt mehr Messwerte als `MAX_FRESH_METRICS_PER_REQUEST`. |
| `value_out_of_range` | 422 | Der übergebene Wert liegt außerhalb des Wertebereichs der Freigabeliste. |
| `value_type_mismatch` | 422 | Der übergebene Wert entspricht nicht dem Werttyp des Messwerts. |
| `value_not_finite` | 422 | Der übergebene Gleitkommawert ist keine endliche Zahl. |
| `value_step_mismatch` | 422 | Der übergebene Wert ist kein Vielfaches der festgelegten Schrittweite. |
| `invalid_parameter` | 422 | Ein Abfrageparameter trägt einen unzulässigen Wert. |
| `rate_limited` | 429 | Der Aufrufer hat die Anfragerate oder die Grenze fehlgeschlagener Authentifizierungsversuche überschritten. |
| `device_budget_exhausted` | 429 | Das Arbeitsbudget des Transport_Endpunkts ist innerhalb des Zeitfensters erschöpft. |
| `internal_error` | 500 | Ein unerwarteter Fehler der Anwendung. Die Ursache steht ausschließlich im Protokoll. |
| `device_unreachable` | 502 | Die TCP-Verbindung zum Transport_Endpunkt ist nicht herstellbar. |
| `device_timeout` | 502 | Das Gerät hat innerhalb der Zeitgrenzwerte nicht geantwortet. |
| `protocol_error` | 502 | Die Antwort des Geräts war nicht dekodierbar oder hat die CRC-Prüfung verletzt. |
| `device_unavailable` | 502 | Keiner der angefragten Messwerte einer gültigen Sammelabfrage war ermittelbar. |
| `write_outcome_unknown` | 502 | Eine Schreibtransaktion hat nach dem Senden einen unklaren Ausgang und der Read-back bestätigt den Wert nicht. |
| `action_outcome_unknown` | 502 | Eine Aktion hat nach dem Senden einen unklaren Ausgang. |
| `queue_full` | 503 | Die Warteschlange des Transport_Endpunkts hat ihre Höchstlänge erreicht. |
| `device_maintenance` | 503 | Das Gerät ist vorübergehend im Wartungszustand und wird nicht angesprochen. Die Ursache steht ausschließlich im Protokoll und im Diagnosebereich. |
| `not_ready` | 503 | Der Bereitschafts_Endpunkt meldet mindestens ein Gerät als nicht bereit. Die Antwort führt die Gerätezustände im Erweiterungsfeld `devices`. |
| `queue_timeout` | 504 | Eine wartende Anfrage hat die Höchstwartezeit überschritten. |

Feldbezogene Fehlerschlüssel, die ausschließlich im Feld `errors` einer Antwort und
nicht als Fehlerschlüssel einer Problem_Details-Antwort erscheinen:

| Fehlerschlüssel | Bedeutung |
| --- | --- |
| `invalid_float` | Der dekodierte Gleitkommawert ist keine endliche Zahl. |
| `decode_length_mismatch` | Die Nutzdatenlänge der Antwort weicht von der erwarteten Bytebreite ab. |

### Requirement 26: Containerisierung und Auslieferung

**User Story:** As a Betreiber, I want ein kleines, nachvollziehbar gebautes und
gehärtetes Container_Image samt Compose-Datei, so that ich den Dienst reproduzierbar
ausrollen kann, ohne Zugangsdaten im Image und ohne Rechte, die der Dienst nicht
braucht.

#### Acceptance Criteria

1. THE Projekt SHALL das Container_Image aus einer Datei `rct-manager/docker/Dockerfile` mit einem mehrstufigen Build aus einer Build-Stage und einer Runtime-Stage bauen.
2. THE Datei `rct-manager/docker/Dockerfile` SHALL in der ersten Zeile die Angabe `# syntax=docker/dockerfile:1` führen.
3. THE Datei `rct-manager/docker/Dockerfile` SHALL als Basis-Image `python:3.13-slim` mit einer bewusst gewählten Minor-Version verwenden.
4. THE Datei `rct-manager/docker/Dockerfile` SHALL die Angaben `latest` und einen Major-Alias als Basis-Image auslassen.
5. THE Datei `rct-manager/docker/Dockerfile` SHALL das Basis-Image zusätzlich über seinen Digest festlegen.
6. THE Build-Stage und die Runtime-Stage SHALL dieselbe Python-Minor-Version verwenden.
7. THE Projekt SHALL die Aktualisierung der Pins des Basis-Image und seines Digests über Renovate oder Dependabot vorsehen und eine Aktualisierung von Hand auslassen.
8. WHERE die Datei `rct-manager/docker/Dockerfile` Pakete des Betriebssystems installiert, THE Datei SHALL im selben `RUN` zuerst `apt-get update`, dann `apt-get upgrade -y`, dann die Installation mit `--no-install-recommends` und abschließend `rm -rf /var/lib/apt/lists/*` ausführen.
9. THE Build-Stage SHALL eine virtuelle Umgebung unter `/opt/venv` anlegen.
10. THE Build-Stage SHALL `pip install --upgrade pip` ausführen und die Abhängigkeiten mit `--upgrade --upgrade-strategy eager` installieren.
11. THE Runtime-Stage SHALL die virtuelle Umgebung aus `/opt/venv` aus der Build-Stage übernehmen.
12. THE Runtime-Stage SHALL `pip` entfernen, sofern die Laufzeit `pip` nicht benötigt.
13. THE Datei `rct-manager/docker/Dockerfile` SHALL die Umgebungsvariablen `PYTHONDONTWRITEBYTECODE=1` und `PYTHONUNBUFFERED=1` setzen.
14. THE Datei `rct-manager/docker/Dockerfile` SHALL `ENTRYPOINT` und `CMD` in der Exec-Form angeben, sodass der Anwendungsprozess die Prozesskennung 1 erhält und Signale unmittelbar empfängt.
15. THE Datei `rct-manager/docker/Dockerfile` SHALL Cache-Mounts von BuildKit für den Paket-Cache von pip und für den Paket-Cache des Betriebssystems verwenden.
16. THE Datei `rct-manager/docker/Dockerfile` SHALL eine Gruppe und einen Benutzer mit der festen Kennung 10001 anlegen und den Container mit `USER 10001:10001` betreiben.
17. THE Datei `rct-manager/docker/Dockerfile` SHALL den Anwendungscode für den Laufzeitnutzer als nicht beschreibbar ablegen.
18. THE Datei `rct-manager/docker/Dockerfile` SHALL die OCI-Labels `org.opencontainers.image.source`, `org.opencontainers.image.version` und `org.opencontainers.image.revision` aus Build-Args belegen.
19. THE Datei `rct-manager/docker/Dockerfile` SHALL eine `HEALTHCHECK`-Angabe führen, die den Health_Endpunkt über einen Einzeiler des im Image vorhandenen Python-Interpreters abfragt.
20. THE Datei `rct-manager/docker/Dockerfile` SHALL in der `HEALTHCHECK`-Angabe `curl` und `wget` auslassen.
21. THE Datei `rct-manager/docker/Dockerfile` SHALL Zugangsdaten in `ARG`, in `ENV` und in kopierten Dateien auslassen.
22. THE Datei `rct-manager/.dockerignore` SHALL `.git`, `settings.env`, Dateien mit Zugangsdaten, virtuelle Umgebungen, Caches und Testartefakte vom Build-Kontext ausschließen.
23. THE Projekt SHALL das Container_Image mit `docker buildx build --platform linux/amd64 --pull -f docker/Dockerfile --push` bauen und veröffentlichen.
24. THE Projekt SHALL das Container_Image als `docker.cirrio.de/rct-api:latest` und zusätzlich mit der aus `rct-manager/pyproject.toml` gelesenen Version als `docker.cirrio.de/rct-api:<version>` veröffentlichen.
25. THE Projekt SHALL die Compose-Datei unter dem Namen `rct-manager/docker/compose.yaml` nach der Compose Specification führen.
26. THE Datei `rct-manager/docker/compose.yaml` SHALL den Schlüssel `version` auslassen.
27. THE Datei `rct-manager/docker/compose.yaml` SHALL den Port des Dienstes ausschließlich an eine Loopback-Adresse des Hosts veröffentlichen.
28. THE Datei `rct-manager/docker/compose.yaml` SHALL `privileged` und ein Mount des Docker-Sockets auslassen.
29. THE Datei `rct-manager/docker/compose.yaml` SHALL `cap_drop` mit dem Wert `ALL`, `security_opt` mit dem Wert `no-new-privileges:true` und `read_only` mit dem Wert `true` führen.
30. THE Datei `rct-manager/docker/compose.yaml` SHALL eine Neustartregel, eine Begrenzung der Prozesszahl, Grenzwerte für Hauptspeicher und Prozessorzeit und eine Begrenzung der Protokollgröße führen.
31. THE Datei `rct-manager/docker/compose.yaml` SHALL die Konfiguration über `env_file` aus `settings.env` beziehen.
32. THE Datei `rct-manager/.gitignore` SHALL `settings.env` von der Versionsverwaltung ausschließen.
33. THE Projekt SHALL eine Datei `rct-manager/.trivyignore` führen und sie ohne Einträge einchecken, solange keine Ausnahme bestehen soll.
34. WHERE die Datei `rct-manager/.trivyignore` einen Eintrag führt, THE Datei SHALL je Eintrag eine Begründung und ein Ablaufdatum als Kommentar führen.
35. THE Datei `rct-manager/docker/compose.yaml` SHALL die Festlegungen nach Requirement 7 und Requirement 14 einhalten, namentlich genau eine Replik je Gerätegruppe und die Veröffentlichung des Ports ausschließlich an eine Loopback-Adresse.

> Klarstellung zu Kriterium 24: Der Name des Projektverzeichnisses und der Name des
> Container_Image weichen hier bewusst voneinander ab. Das Verzeichnis heißt
> `rct-manager/`, das Image `docker.cirrio.de/rct-api`. Das entspricht der Praxis der
> übrigen Projekte dieses Repositorys, in dem etwa `talsperren/` als `damflux` und
> `rctpower/` als `rct-collector` veröffentlicht wird. Die Abweichung ist keine
> Unstimmigkeit und nicht zu „korrigieren“.

> Begründung zu den Kriterien 19 und 20: Das Basis-Image `python:3.13-slim` enthält
> weder `curl` noch `wget`. Eine `HEALTHCHECK`-Angabe mit einem dieser Kommandos würde
> stets fehlschlagen und den Container als dauerhaft ungesund melden. Beide Werkzeuge
> nachzuinstallieren würde das Image vergrößern und zusätzliche Angriffsfläche
> schaffen, obwohl der ohnehin vorhandene Python-Interpreter dieselbe Prüfung mit der
> Standardbibliothek erledigt. Der Einzeiler ist deshalb die kleinere und
> verlässlichere Lösung.

### Requirement 27: Geordnetes Beenden

**User Story:** As a Betreiber, I want dass der Dienst auf ein Beendigungssignal
geordnet herunterfährt, so that keine Transaktion mitten im Senden abbricht und die
periodischen Anforderungen auf den Geräten nicht zurückbleiben.

#### Acceptance Criteria

1. WHEN die Anwendung das Signal `SIGTERM` empfängt, THE Anwendung SHALL den Abbau beginnen.
2. WHEN die Anwendung das Signal `SIGINT` empfängt, THE Anwendung SHALL denselben Abbau wie bei `SIGTERM` beginnen.
3. WHEN der Abbau begonnen hat, THE Anwendung SHALL die Abbau_Deadline als Summe aus dem Zeitpunkt des Beendigungssignals und der Abbaufrist festlegen.
4. THE Anwendung SHALL die Abbaufrist aus der Einstellung `SHUTDOWN_GRACE_SECONDS` übernehmen, deren Vorgabewert 20 Sekunden beträgt.
5. THE Anwendung SHALL die Abbaureserve aus der Einstellung `SHUTDOWN_PERIODIC_RESERVE_SECONDS` übernehmen, deren Vorgabewert 5 Sekunden beträgt.
6. THE Anwendung SHALL den Abbau in die Phasen `Annahmestopp`, `Restarbeit`, `Periodikabbau` und `Abschluss` gliedern und diese Phasen in dieser Reihenfolge und ohne Rücksprung durchlaufen.
7. WHILE der Abbau in der Phase `Annahmestopp`, `Restarbeit`, `Periodikabbau` oder `Abschluss` ist, THE REST_API SHALL eingehende HTTP-Anfragen weiterhin annehmen und beantworten.
8. WHILE der Abbau läuft, THE REST_API SHALL eine Anfrage, die eine neue Transaktion auslösen würde, mit dem HTTP-Statuscode 503 und Problem_Details nach Requirement 25 mit dem Fehlerschlüssel `not_ready` abweisen und keine Transaktion auslösen.
9. WHILE der Abbau läuft, THE REST_API SHALL Anfragen an den Health_Endpunkt, den Bereitschafts_Endpunkt und den Metrik_Endpunkt weiterhin beantworten.
10. WHILE der Abbau läuft, THE Health_Endpunkt SHALL den HTTP-Statuscode 503 zurückgeben.
11. WHEN die Phase `Restarbeit` beginnt, THE Zugriffsserialisierer SHALL die bereits laufende Transaktion je Transport_Endpunkt und die bereits in der Warteschlange stehenden Anfragen bis zur Abbau_Deadline abzüglich der Abbaureserve weiter abarbeiten.
12. WHEN alle Transaktionen der Phase `Restarbeit` beendet sind oder die Abbau_Deadline abzüglich der Abbaureserve erreicht ist, THE Anwendung SHALL die Phase `Restarbeit` beenden und die Phase `Periodikabbau` beginnen.
13. WHILE die Phase `Periodikabbau` läuft und die Abbau_Deadline nicht erreicht ist, THE Protokoll_Adapter SHALL für jedes Gerät mit mindestens einer angemeldeten periodischen Anforderung die Objekt_ID `0x9C8FE559` mit dem Wert 0 beschreiben.
14. IF die Abbau_Deadline beim Beginn der Phase `Periodikabbau` bereits erreicht ist, THEN THE Protokoll_Adapter SHALL den Schreibvorgang nach Kriterium 13 auslassen und die Anzahl der nicht abgemeldeten periodischen Anforderungen protokollieren.
15. THE Anwendung SHALL den Schreibvorgang nach Kriterium 13 als System_Schreibzugriff nach Requirement 17 und über die Kommunikationsinstanz nach Requirement 24 ausführen.
16. IF der Schreibvorgang nach Kriterium 13 fehlschlägt, THEN THE Anwendung SHALL das Ereignis mit Gerätekennung und Fehlerursache protokollieren und die Phase `Periodikabbau` für die übrigen Geräte fortsetzen.
17. WHEN alle Schreibvorgänge nach Kriterium 13 abgeschlossen oder ausgelassen sind oder die Abbau_Deadline erreicht ist, THE Anwendung SHALL die Phase `Periodikabbau` beenden und die Phase `Abschluss` beginnen.
18. WHEN die Phase `Abschluss` beginnt, THE Anwendung SHALL alle zu diesem Zeitpunkt noch laufenden Transaktionen abbrechen, deren Anzahl protokollieren und alle TCP-Verbindungen zu allen Transport_Endpunkten schließen.
19. WHEN alle TCP-Verbindungen geschlossen sind, THE Anwendung SHALL die Dauer des Abbaus, die erreichte Phase, die Anzahl der abgemeldeten periodischen Anforderungen und die Anzahl der abgebrochenen Transaktionen protokollieren und sich mit dem Rückgabewert 0 beenden.
20. FOR ALL Abläufen des Abbaus SHALL die Zeitspanne zwischen dem Empfang des Beendigungssignals und dem Beenden des Prozesses die Abbaufrist nicht überschreiten (Korrektheitseigenschaft).

> Begründung zu den Kriterien 6 bis 20: Ein Abbau, der auf eine nicht antwortende
> Gegenstelle wartet, blockiert den Neustart des Dienstes und führt dazu, dass die
> Laufzeitumgebung den Prozess hart beendet. Die Abbaufrist ist deshalb eine echte
> Gesamtdeadline und keine Frist je Teilschritt; Kriterium 20 hält das als
> Korrektheitseigenschaft fest.
>
> Die vier Phasen trennen zwei Fragen, die zuvor vermischt waren. Die Annahme von
> HTTP-Anfragen endet **nicht** mit dem Beendigungssignal — andernfalls könnte der
> Dienst die eigene 503-Antwort nach Kriterium 8 nicht mehr geben und auch der
> Health_Endpunkt nach Kriterium 10 wäre unerreichbar. Abgewiesen wird ausschließlich
> neue fachliche Arbeit, also jede Anfrage, die eine Transaktion auslösen würde.
>
> Der Periodikabbau braucht ein eigenes, zugesichertes Zeitfenster, weil er selbst
> Transaktionen und damit die Mindestpause am Send_Gate kostet. Die Abbaureserve
> schneidet dieses Fenster vom Ende der Abbaufrist ab, sodass die Restarbeit es nicht
> aufbrauchen kann. Reicht die Zeit dennoch nicht, wird der Periodikabbau ausgelassen
> statt die Deadline zu überschreiten: Die nicht abgemeldeten periodischen
> Anforderungen sind kein Fehler der Anwendung und werden nach Requirement 17 beim
> nächsten Verbindungsaufbau neu gesetzt. Der Rückgabewert bleibt in jedem Fall 0, weil
> der Abbau planmäßig verlief.

### Requirement 28: Projektstruktur und Qualitätssicherung

**User Story:** As a Entwickler, I want ein eigenständiges Projekt mit den
Konventionen dieses Repositorys, so that der Dienst unabhängig von den übrigen
Projekten gebaut, geprüft und betrieben werden kann.

#### Acceptance Criteria

1. THE Projekt SHALL vollständig im Verzeichnis `rct-manager/` liegen.
2. THE Projekt SHALL einen Import aus einem anderen Projekt dieses Repositorys auslassen.
3. THE Projekt SHALL den Anwendungscode im Paket `rct-manager/app/` führen.
4. THE Anwendung SHALL über `python -m app` startbar sein.
5. THE Anwendung SHALL den Aufrufmodus nach Requirement 22 bereitstellen, der die Konfiguration prüft und sich ohne Start des HTTP-Servers beendet.
6. THE Projekt SHALL `ruff check .` mit der Konfiguration des Repository-Wurzelverzeichnisses ohne Befunde bestehen.
7. THE Projekt SHALL eine Zeilenlänge von höchstens 120 Zeichen einhalten.
8. THE Projekt SHALL Kommentare und Docstrings in englischer Sprache führen.
9. THE Projekt SHALL benutzerseitige Zeichenketten in deutscher Sprache führen, soweit sie an einen Betreiber gerichtet sind.
10. THE Projekt SHALL jeden Zeitstempel als zeitzonenbewussten Zeitpunkt führen.
11. THE Projekt SHALL `pathlib` für Pfadoperationen verwenden.
12. THE Projekt SHALL Vereinigungstypen mit dem Zeichen `|` schreiben.
13. THE Projekt SHALL `StrEnum` für Aufzählungen aus Zeichenketten verwenden.
14. THE Projekt SHALL `typing.Self` für Rückgaben des eigenen Typs verwenden.
15. THE Projekt SHALL `typing.Optional`, `typing.List` und `os.path` auslassen.
16. THE Projekt SHALL einen Kompatibilitätsbehelf für eine Python-Version unterhalb von 3.13 auslassen.
17. THE Projekt SHALL eine Datei `rct-manager/README.md` führen, die Zweck, Konfiguration, Betrieb, Prüfbefehle und die Nicht-Ziele nach Requirement 21 benennt.
18. THE Projektdokumentation SHALL den Bau und die Veröffentlichung nach Requirement 26 beschreiben. Die persönliche Datei `setup.conf` wird ausschließlich auf ausdrücklichen Wunsch des Maintainers angelegt oder geändert.
19. THE Anwendung SHALL Fristen, Mindestpausen, Zeitfenster und Cache-Alter mit einer monotonen Uhr messen; Änderungen der Systemzeit dürfen diese Größen nicht verkürzen oder verlängern. Ausgegebene Zeitstempel bleiben UTC-Zeitpunkte.


### Requirement 29: Fremdzugriff auf einen Transport-Endpunkt

**User Story:** As a Betreiber, I want erkennen können, ob ein anderer Client
denselben Wechselrichter anspricht, so that ich die Ursache vermischter oder
veralteter Antworten finde, die die Anwendung selbst nicht verhindern kann.

#### Acceptance Criteria

1. THE Projektdokumentation SHALL benennen, dass der Betreiber den ausschließlichen Zugriff dieses Dienstes auf den Transport_Endpunkt und dessen Port 8899 sicherzustellen hat.
2. THE Projektdokumentation SHALL die Herstellerapplikation, eine Integration eines Heimautomatisierungssystems und jeden weiteren RCT-Client als mögliche Fremdzugriffe benennen.
3. THE Anwendung SHALL den ausschließlichen Zugriff auf einen Transport_Endpunkt nicht erzwingen.
4. WHEN die Anzahl unerwarteter Frames eines Transport_Endpunkts innerhalb des Zeitfensters nach `UNEXPECTED_FRAME_WINDOW_SECONDS` die Schwelle nach `FOREIGN_ACCESS_FRAME_THRESHOLD` erreicht, THE Anwendung SHALL einen Verdacht auf Fremdzugriff für diesen Transport_Endpunkt setzen.
5. WHEN ein Verdacht auf Fremdzugriff gesetzt wird, THE Anwendung SHALL das Ereignis mit der Kennung des Transport_Endpunkts, der Anzahl unerwarteter Frames und dem Zeitfenster protokollieren.
6. WHILE ein Verdacht auf Fremdzugriff für einen Transport_Endpunkt besteht, THE Bereitschafts_Endpunkt SHALL für jedes Gerät dieses Transport_Endpunkts das Feld `foreign_access_suspected` mit dem Wert `true` ausgeben.
7. WHEN innerhalb eines vollen Zeitfensters kein unerwarteter Frame eines Transport_Endpunkts eintrifft, THE Anwendung SHALL den Verdacht auf Fremdzugriff für diesen Transport_Endpunkt aufheben.
8. THE Metrik_Endpunkt SHALL die Dienstmetrik `rct_transport_foreign_access_suspected` als Gauge mit dem Label `endpoint` nach Requirement 20 Kriterium 16 ausgeben.
9. THE Anwendung SHALL aus einem Verdacht auf Fremdzugriff keine Änderung des Bereitschaftszustands und keine Abweisung von Anfragen ableiten.

> Begründung zu den Kriterien 3 und 9: Die Anwendung kann nicht feststellen, wer sonst
> auf dem Gerät spricht; sie sieht nur Frames, die zu keiner eigenen Anforderung
> passen. Ein solcher Befund ist ein Hinweis, kein Beweis — eine gestörte Leitung
> erzeugt dasselbe Bild. Daraus eine Abweisung von Anfragen abzuleiten würde den
> Dienst aufgrund einer Vermutung unbrauchbar machen. Der Verdacht wird deshalb
> ausschließlich sichtbar gemacht.

### Requirement 30: Herstellerneutrale Vertragsgrenze, Diagnosebereich und Austauschbarkeit

**User Story:** As a Entwickler eines Fremdsystems, I want eine Schnittstelle, die
nichts über den Wechselrichter-Hersteller voraussetzt, so that mein Client auch dann
unverändert weiterläuft, wenn hinter dem Dienst ein anderes Gerät steht.

#### Acceptance Criteria

1. THE Herstellerneutrale_Vertrag SHALL die Objekt_ID, das Command-Byte, den Frame-Aufbau, die CRC-Prüfsumme, die Escaping-Regeln, die Netzkennung, die Transportadresse, den Transportport und die Datentypnamen des Protokolls auslassen.
2. THE Herstellerneutrale_Vertrag SHALL den Namen des Herstellers und den Namen des Protokolls in Pfaden, Feldnamen und Feldwerten auslassen.
3. THE REST_API SHALL jede Angabe nach Kriterium 1, die betrieblich gebraucht wird, ausschließlich im Diagnosebereich unter dem Pfad-Präfix `/api/v1/vendor/rct` ausgeben.
4. WHERE die Einstellung `ENABLE_VENDOR_DIAGNOSTICS` aktiviert ist, THE REST_API SHALL den Diagnosebereich registrieren.
5. WHERE die Einstellung `ENABLE_VENDOR_DIAGNOSTICS` deaktiviert ist, THE REST_API SHALL den Diagnosebereich nicht registrieren und Anfragen an das Pfad-Präfix `/api/v1/vendor/rct` mit dem HTTP-Statuscode 404 abweisen.
6. THE REST_API SHALL für jede Anfrage an den Diagnosebereich die Token_Rolle `read/write` nach Requirement 12 verlangen.
7. THE REST_API SHALL im Diagnosebereich unter `GET /api/v1/vendor/rct/objects` je Messwert den Namen, die Objekt_ID, den Datentyp des Protokolls, die geltende Bytebreite und die Idempotenz_Kennzeichnung ausgeben.
8. THE REST_API SHALL im Diagnosebereich unter `GET /api/v1/vendor/rct/transports` je Transport_Endpunkt die Endpunktkennung, die Zieladresse, den Zielport, die zugeordneten Gerätekennungen und die Netzkennungen ausgeben.
9. THE REST_API SHALL im Diagnosebereich unter `GET /api/v1/vendor/rct/transports` je Transport_Endpunkt zusätzlich die Anzahl verworfener Bytes, die Anzahl unerwarteter Frames, den Sperrzustand, die Ursache des Sperrzustands, den Zeitpunkt des letzten empfangenen Frames und die Anzahl der je Gerät angemeldeten periodischen Anforderungen samt Verfügbarkeit der Periodik ausgeben.
10. THE REST_API SHALL den Sperrzustand, dessen Ursache und jeden Frame-Zähler ausschließlich im Diagnosebereich ausgeben.
11. THE REST_API SHALL im Diagnosebereich den Abruf der Slave-Geräte nach Requirement 18 bereitstellen.
12. THE Herstellerneutrale_Vertrag SHALL den Diagnosebereich nicht referenzieren.
13. THE OpenAPI-Beschreibung SHALL jeden Endpunkt des Diagnosebereichs als herstellerspezifisch kennzeichnen und als nicht Teil des Herstellerneutralen_Vertrags benennen.
14. THE REST_API SHALL im Diagnosebereich Token-Werte auslassen.
15. THE Anwendung SHALL einen Scrape des Metrik_Endpunkts, einen Abruf der Dokumentations_Endpunkte und eine wegen Authentifizierung, Autorisierung, Ratengrenze, Arbeitsbudget oder Wertprüfung abgewiesene Anfrage ohne Transaktion gegen einen Transport_Endpunkt beantworten.
16. THE Anwendung SHALL Gerätelast ausschließlich aus einer fachlich angeforderten Lesetransaktion, einer fachlich angeforderten Schreibtransaktion, einem Heartbeat, der Periodik, einem System_Schreibzugriff und dem Abbau beim Beenden erzeugen.
17. THE Herstellerneutrale_Vertrag SHALL so gefasst sein, dass der Austausch des Protokoll_Adapters gegen einen Adapter eines anderen Geräteherstellers keine Änderung der Pfade, der Pflichtfelder, der Feldbedeutungen, der Fehlerschlüssel und der Statuscodes des Herstellerneutralen_Vertrags verlangt.
18. THE Projektdokumentation SHALL je Ressource des Herstellerneutralen_Vertrags benennen, welche Felder ein beliebiger Geräteadapter zu füllen hat.
19. THE Herstellerneutrale_Vertrag SHALL Frame-Zähler, den Sperrzustand, die Anzahl angemeldeter periodischer Anforderungen und das Bootloader_Magic als Feld, als Feldwert und als Fehlerschlüssel auslassen.
20. THE Herstellerneutrale_Vertrag SHALL einen Gerätezustand, in dem die Anwendung keine Frames an das Gerät sendet, als Wartungszustand mit dem Wert `maintenance` und mit dem Fehlerschlüssel `device_maintenance` ausgeben.
21. FOR ALL Antworten des Herstellerneutralen_Vertrags SHALL die Antwort keine Objekt_ID, keine Netzkennung, keine Transportadresse, keinen Transportport, keinen Datentypnamen des Protokolls, keinen Frame-Zähler und keinen Verweis auf den Bootloader enthalten (Korrektheitseigenschaft).
22. FOR ALL Anfragen an den Metrik_Endpunkt, an die Dokumentations_Endpunkte und an den Health_Endpunkt SHALL die Anzahl der gegen einen Transport_Endpunkt ausgeführten Transaktionen unverändert bleiben (Korrektheitseigenschaft).

> Begründung zu den Kriterien 4 bis 6: Der Diagnosebereich legt genau jene Angaben
> offen, die der herstellerneutrale Vertrag bewusst verbirgt — Objekt_IDs,
> Transportadressen und Netzkennungen. Er ist deshalb im Vorgabezustand abgeschaltet
> und verlangt, wenn er eingeschaltet ist, die stärkere Token_Rolle. Dass er die Rolle
> `read/write` verlangt, obwohl er nur liest, ist beabsichtigt: Die Angaben sind für
> einen Betreiber bestimmt, nicht für einen lesenden Fachclient.

## Offene Punkte

Diese Punkte bleiben bewusst offen und sind vor oder während der Entwurfsphase zu
entscheiden. Durch diese Überarbeitung entschiedene Punkte stehen hier nicht mehr.

1. **Konkrete Ratengrenzen.** Die Werte in Requirement 13 und Requirement 20 sind
   begründete Vorgabewerte, keine fachliche Vorgabe. Sie sind nach dem ersten
   Betrieb anhand der tatsächlichen Zahl der Aufrufer, der Scrape-Frequenz des
   Collectors und der beobachteten Antwortzeiten der Geräte zu revidieren.
2. **Inhalt der Freigabeliste samt Wertebereichen.** Requirement 19 legt die Form
   fest, nicht den Inhalt. Welche Messwerte überhaupt beschreibbar sein sollen und
   welche Minima, Maxima, Enum-Werte und Schrittweiten je Messwert gelten, ist
   fachlich zu klären, da die Firmware keine Plausibilitätsprüfung vornimmt. Offen
   ist zugleich, welche Aktionsvariablen über den Aktionsendpunkt nach
   Requirement 19 Kriterium 7 freigegeben werden sollen.
3. **TLS- und Deployment-Modell in der Zielumgebung.** Requirement 14 verlangt die
   TLS-Terminierung an einem vorgeschalteten Reverse Proxy. Welcher Proxy das in der
   Zielumgebung ist, unter welchem Namen der Dienst erreichbar ist und wie die
   Zertifikate verwaltet werden, ist offen.
4. **Auswahl der auf dem Metrik_Endpunkt ausgegebenen Messwerte.** Requirement 20
   legt Benennung, Labels und Auslassungsregel fest. Welche Messwerte tatsächlich
   ausgegeben werden und welche davon periodisch angefordert werden, ist nach dem
   ersten Betrieb festzulegen.
5. **Auswahl der Messwerte für die Vorauswahl der Objekt_Registry.** Welche
   Messwerte eine Anfrage ohne den Abfrageparameter `names` nach Requirement 10
   zurückgibt, ist offen.
6. **Mehrere Anlagennetze.** Geräte stammen ausschließlich aus `DEVICES`. Die
   Slave-Erfassung ist Diagnose und übernimmt keine Geräte automatisch; jedes
   Anlagennetz besitzt seinen eigenen konfigurierten Master und Transport_Endpunkt.
7. **Bytebreite von `t_enum` und `t_bool` je Objekt_ID.** Requirement 4 Kriterium 7
   führt das Feld `byte_width` ein, Requirement 5 Kriterium 2 die Vorgabebreiten. Für
   welche Objekt_IDs die Vorgabebreite am Gerät nicht zutrifft, ist durch Messung am
   Gerät festzustellen. Bis dahin bleibt das Feld in der Objekt_Registry leer.
8. **Zeichenkodierung der Zeichenkettenfelder.** Requirement 5 Kriterium 7 legt
   `utf-8` als begründete Vorgabe fest. Ob das Gerät tatsächlich `utf-8` oder
   `latin-1` liefert, ist am Gerät zu prüfen; die Einstellung `STRING_ENCODING` hält
   die Entscheidung offen.
9. **Umfang des Diagnosebereichs.** Requirement 30 legt drei Endpunkte fest. Ob der
   Betrieb weitere herstellerspezifische Angaben braucht, etwa rohe Frame-Mitschnitte
   zur Fehlersuche, ist offen. Ein Mitschnitt wäre nur mit einer eigenen Einstellung
   und mit einer Begrenzung der Aufbewahrung vertretbar.
10. **Grenzwerte des Histograms der Antwortzeiten.** Requirement 20 Kriterium 6 legt
    `rct_device_request_duration_seconds` als Histogram fest. Welche Grenzwerte die
    Klassen haben, ist nach den ersten gemessenen Antwortzeiten der Geräte
    festzulegen.
11. **Höhe des Arbeitsbudgets und des `fresh`-Batchlimits.** Requirement 6
    Kriterium 10 und Requirement 10 Kriterium 20 nennen begründete Vorgabewerte. Sie
    sind nach dem ersten Betrieb an der tatsächlichen Zahl der Aufrufer und an der
    beobachteten Antwortzeit der Geräte zu revidieren.
12. **Digest des Basis-Image.** Requirement 26 Kriterium 5 verlangt das Festlegen des
    Digests. Der konkrete Digest ist beim Anlegen des Projekts zu ermitteln und
    danach von Renovate oder Dependabot zu pflegen.

13. **Höhe der Abbaureserve.** Requirement 27 Kriterium 5 nennt 5 Sekunden als
    begründeten Vorgabewert. Wie lange das Abmelden der Periodik bei der gewählten
    Anzahl von Geräten und periodischen Anforderungen tatsächlich dauert, ist nach dem
    ersten Betrieb zu messen; die Reserve ist daran anzupassen.

Durch diese Überarbeitung entschieden und deshalb nicht mehr offen: die Bedeutung des
Abfrageparameters `fresh` bei periodisch angemeldeten Messwerten (Requirement 17),
die Behandlung des Datentyps von `net.slave_data` (Requirement 4 und
Requirement 18), das Format der Fehlerantworten (Requirement 25), das Verhalten bei
nicht bestätigter TLS-Terminierung (Requirement 14), die Behandlung fremder
Umgebungsvariablen (Requirement 21), die Grenze der automatischen Wiederholung einer
Schreibtransaktion am Commit_Point (Requirement 9), die Phasenfolge des geordneten
Beendens (Requirement 27), das Verhältnis von Gültigkeitsdauer und Nachfrist
(Requirement 15 und Requirement 22) und die Zuordnung der Metriken eines
Transport_Endpunkts (Requirement 20).

## Prüfung der Review-Befunde

Alle protokollbezogenen Behauptungen dieses Dokuments sind gegen
`docs/reference/6707-RCT-Power-Serial-Communication-Protocol.pdf` (Dokumentversion
1.14 vom 11.02.2022) geprüft. Bestätigt und ohne Abweichung übernommen sind: die
CRC-Eingabe `[Command, Length, Address, ID, Data]` beim Plant_Frame (Tabelle 2 und
Änderungseintrag 1.9), das 2 Byte breite Längenfeld für Long_Commands, die Struktur
von `net.slave_data` (`0xC0A7074F`) genau einem Slave-Gerät je
Abruf (Größe und Byte-Reihenfolge: siehe unten), die Datentypen `t_int8`, `t_int16` und `t_bool`, die MSBF-Reihenfolge der
Frames und aller Werte außer der Slave_Struktur, Gleitkommazahlen nach IEEE 754 mit 4 Byte, das Bootloader_Magic `0x50F705AB`
im Abstand von 500 Millisekunden, das optionale führende Null-Byte vor dem
Start-Byte, `com_service` (`0x8FC89B10`) als aktionsauslösende Variable, die
Obergrenze von 64 gleichzeitig bedienten periodischen Anforderungen, `pas.period`
(`0x9C8FE559`, `t_uint32`, Vorgabe 30 Sekunden, Wert 0 löscht die Liste) sowie das
Command-Byte `0x08` für `READ PERIODICALLY`. Nachfolgend stehen ausschließlich die
Befunde, die abweichen, zu ergänzen waren oder in der Quelle nicht geregelt sind.

1. **Befund 4, Aufzählung der Long_Commands unvollständig.** Der Befund nennt
   `0x03`, `0x43` und `0x46`. Die Quelle führt als Long_Commands `LONG WRITE 0x03`
   und `LONG RESPONSE 0x06` (Tabelle 5) sowie deren um Bit 6 erweiterte
   Entsprechungen `LONG WRITE M 0x43` und `LONG RESPONSE M 0x46` (Tabelle 6).
   `0x06` fehlt im Befund. Requirement 1 Kriterium 10 nennt deshalb alle vier
   Command-Bytes.
2. **Befund 2, Idempotenz von `com_service` genauer als im Befund.** Die Quelle
   sagt, das Gerät handle ausschließlich bei einer *Änderung* von `com_service`; um
   denselben Befehl erneut auszuführen, muss der Wert zuerst auf 0 zurückgesetzt
   werden. Ein wörtlich identischer zweiter Schreibvorgang löst die Handlung somit
   nicht erneut aus; die Aussage „nicht idempotent“ trifft also nur auf die Folge
   `Wert → 0 → Wert` zu. Die Regel aus Requirement 9 Kriterium 9, eine als nicht
   idempotent gekennzeichnete Objekt_ID niemals automatisch zu wiederholen, bleibt
   dennoch bestehen: Ein Wiederholversuch kann nicht feststellen, ob der vorige
   Schreibvorgang den Wert bereits geändert hat, und eine Wiederholung nach einem
   Rücksetzen auf 0 löst die Handlung erneut aus. Die Begründung der Anforderung ist
   entsprechend geschärft, die Anforderung selbst unverändert.
3. **Befund 19, Bootloader_Magic hat keine Objekt_ID.** Der Befund fragt nach
   „Objekt_ID bzw. Kontext“. Das Bootloader_Magic ist keine Variable und trägt keine
   Objekt_ID. Es ist eine rohe 4-Byte-Folge, die der Bootloader außerhalb des
   normalen Frame-Aufbaus in den Datenstrom schreibt. Requirement 8 Kriterium 11
   knüpft deshalb an die Byte-Folge im Datenstrom an, nicht an eine Objekt_ID.
4. **Befund 10, Datentyp von `net.slave_data` weicht von der Quelle ab.** Die
   ID-Tabelle der Quelle führt `0xC0A7074F` als `t_string`, die Strukturbeschreibung
   in Tabelle 3 beschreibt denselben Wert als binäre Struktur mit 104 Byte. Die
   Quelle ist hier in sich widersprüchlich. Dieses Dokument folgt Tabelle 3 in Aufbau
   und Offsets, weicht aber in Größe und Byte-Reihenfolge nach Requirement 18
   Kriterium 2 und 6 ab, weil das Gerät sie so liefert; die
   Objekt_Registry führt für `0xC0A7074F` deshalb einen eigenen Strukturtyp und
   nicht `t_string` (Requirement 18 Kriterium 2).
5. **Befund 9, Bytebreite von `t_enum` und `t_bool` ist in der Quelle nicht
   festgelegt.** Die Quelle nennt Bytebreiten ausschließlich in der
   Strukturbeschreibung der Slave_Struktur (`uint_8` 1 Byte, `uint_16` 2 Byte,
   `uint_32` 4 Byte, `float` 4 Byte) und benennt dort IEEE 754. Für die Typnamen
   `t_enum` und `t_bool` der ID-Tabelle gibt sie keine Breite an. Die Festlegungen in
   Requirement 5 Kriterium 2 (4 Byte für `t_enum`, 1 Byte für `t_bool`) sind damit
   eine frühere Fallback-Annahme, keine bestätigte Aussage der Quelle. Der
   zusätzliche Abgleich mit `rctclient==0.0.3` am 2026-10-02 belegt für alle 15
   ausgelieferten Enum-Objekte 1 Byte; diese führen jetzt explizit `byte_width: 1`.
   Abweichungen sind weiterhin am Gerät zu
   überprüfen und gegebenenfalls je Objekt_ID in der Objekt_Registry zu
   korrigieren; die Objekt_Registry führt die Bytebreite deshalb ableitbar je Eintrag.
6. **Befund 8, Datentypenliste ist vollständig und abgeschlossen.** Die Typ-Spalte
   der ID-Tabelle verwendet ausschließlich die zehn Typen `t_float`, `t_uint32`,
   `t_string`, `t_uint8`, `t_int32`, `t_bool`, `t_uint16`, `t_enum`, `t_int16` und
   `t_int8`. Requirement 4 Kriterium 2 deckt damit alle in der Quelle vorkommenden
   Typen ab; weitere Typen sind nicht zu erwarten, und Requirement 4 Kriterium 3
   behandelt einen unbekannten Typ als Startfehler.
7. **Befund 5, TCP-Streaming ist in der Quelle nicht geregelt.** Die Quelle nennt
   lediglich TCP/IP mit dem Vorgabeport 8899 und trifft keine Aussage zu
   Reassemblierung, Resynchronisation, Höchstgröße eines Frames oder geteilten
   Escaping-Sequenzen. Requirement 2 ist deshalb keine Wiedergabe der Quelle, sondern
   eine eigene Festlegung, die der Quelle nicht widerspricht. Als Anhaltspunkt für
   die Höchstgröße dient die Aussage der Quelle, dass Variablen mit einer Länge bis
   251 Byte mit dem gewöhnlichen `RESPONSE` beantwortet werden.
8. **Escaping der CRC-Bytes ist eine Ableitung.** Die Quelle beschreibt die
   Escaping-Regeln als Regeln des Byte-Stroms und schließt die CRC-Bytes nicht
   ausdrücklich ein; beide Beispiele der Quelle enthalten keine CRC-Bytes mit den
   Werten `0x2B` oder `0x2D`. Requirement 1 Kriterium 7 bezieht die CRC-Bytes in das
   Escaping ein, weil ein unmaskiertes `0x2B` in der Prüfsumme sonst als Beginn eines
   neuen Frames gelesen würde. In 296 gemessenen Frames mit 168 verschiedenen
   CRC-Werten trat weder `0x2B` noch `0x2D` in der Prüfsumme auf; der Fall bleibt
   daher offen. Bei Gleichverteilung wären etwa 2,6 Treffer zu erwarten (etwa 7 %
   Wahrscheinlichkeit für keinen Treffer). Diese Ableitung ist am Gerät zu überprüfen.
9. **Frame-Aufbau der periodischen Befehle ist eine Ableitung.** Die
   Strukturtabellen der Quelle sind mit „commands 0x01...0x06“ beziehungsweise
   „commands 0x41...0x46“ überschrieben, während die Befehlstabellen `READ
   PERIODICALLY 0x08` und `READ PERIODICALLY M 0x48` führen. Der Frame-Aufbau für
   `0x08` und `0x48` ist in der Quelle damit nicht ausdrücklich geregelt. Dieses
   Dokument nimmt an, dass er dem Aufbau des jeweiligen Standard_Frame
   beziehungsweise Plant_Frame entspricht und dass die unaufgeforderten Antworten
   als `RESPONSE 0x05` beziehungsweise `RESPONSE M 0x45` eintreffen.
10. **Teil A Punkt 8, Metrik-Abhängigkeit geprüft.** Der Exporter erzeugt das
    Prometheus-Textformat selbst und importiert `prometheus_client` nicht. Die
    tatsächlich direkt importierten Pakete `python-dotenv` und `starlette` sind
    gemäß Requirement 23 zu deklarieren.
11. **Teil A, keine zu entfernende Festlegung gefunden.** Die Vorgabe, jede
    Formulierung zu entfernen, die einen Prometheus-Endpunkt ausschließt, war
    gegenstandslos: Die Fassung vor dieser Überarbeitung enthielt keine Aussage zu
    Prometheus, zu InfluxDB, zu QuestDB oder zu einem Zeitreihen-Push. Requirement 21
    schreibt das Nicht-Ziel nun ausdrücklich fest, damit es nicht später eingebaut
    wird.

### Befunde der zweiten Review-Runde

12. **P0.6 bestätigt: Das Protokoll kennt keine Transaktionskennung.** Geprüft gegen
    Tabelle 1 und Tabelle 2 der Quelle. Ein Standard_Frame besteht aus Start-Byte,
    Command-Byte, Längenfeld, Objekt_ID, Daten und CRC; ein Plant_Frame ergänzt
    ausschließlich das Adressfeld. Eine Sequenznummer, eine Anfragekennung oder ein
    Korrelationsfeld kommt in keiner der beiden Strukturen vor. Die Quelle sagt zu
    `READ PERIODICALLY` zudem, die Antwort komme „periodisch für immer“, die erste
    Antwort unmittelbar, und eine erneute Anforderung derselben Objekt_ID werde
    ignoriert; sie nennt dafür keinen eigenen Antwort-Command. Ein eintreffender
    `RESPONSE`-Frame ist deshalb bei gleicher Objekt_ID nicht einer Ursache
    zuordenbar. Die bisherige Anforderung, bei `fresh=true` einen periodisch
    gelieferten Wert nicht zu verwenden, war damit nicht erfüllbar und ist durch die
    zeitlich bestimmte Frische nach Requirement 17 Kriterium 14 bis 19 ersetzt. Die
    strengere Variante bleibt über `FRESH_PERIODIC_MODE` wählbar.
13. **P2.2 bestätigt: Die Quelle nennt keine Zeichenkodierung.** Geprüft gegen die
    gesamte Quelle. Tabelle 3 beschreibt die Zeichenkettenfelder der Slave_Struktur
    als `char` mit einer Feldlänge und einem Null-Abschluss; die Begriffe ASCII,
    UTF-8, Latin-1, Codepage und Zeichensatz kommen in der Quelle nicht vor. Die
    Festlegung auf `utf-8` mit dem Ersetzungszeichen `U+FFFD` in Requirement 5
    Kriterium 7 und 8 ist deshalb eine begründete Annahme und keine Aussage der
    Quelle. Sie ist über `STRING_ENCODING` revidierbar.
14. **P0.7 gelöst, Widerspruch zu Befund 4 beseitigt.** Befund 4 dieses Abschnitts
    hielt fest, dass die Objekt_Registry für `0xC0A7074F` einen eigenen Strukturtyp
    führt, während Requirement 4 zehn Datentypen abschließend aufzählte. Requirement 4
    Kriterium 2 führt nun `t_struct` als elften Datentyp und Kriterium 4 bis 6 die
    Strukturkennung `slave_data`. Die Typmenge bleibt abschließend und beim Start
    prüfbar. `t_struct` ist kein Typ der Quelle, sondern eine Festlegung dieses
    Dokuments zur Auflösung des Widerspruchs in der Quelle selbst.
15. **P0.8 gelöst, Befund 5 nachgezogen.** Befund 5 dieses Abschnitts bezeichnet die
    Bytebreiten von `t_enum` und `t_bool` als unbestätigte Annahme. Requirement 4
    Kriterium 7 bis 10 führen deshalb das Feld `byte_width` als normatives,
    validierbares Registry-Feld ein; Requirement 5 Kriterium 2 gilt nur noch als
    Vorgabebreite für Einträge ohne eigenes Feld. Die Quelle nennt Bytebreiten
    weiterhin ausschließlich in Tabelle 3.
16. **`com_service` ist in der ID-Tabelle als `t_enum` geführt.** Geprüft an Eintrag
    512 der ID-Tabelle. Die Vorgabebreite von 4 Byte für `t_enum` gilt damit auch für
    `0x8FC89B10`. Die Werte 0 bis 20 der Tabelle 8 passen in jede der zugelassenen
    Bytebreiten; die Festlegung bleibt über `byte_width` korrigierbar.
17. **`prim_sm.state` ist in der ID-Tabelle als `t_uint8` geführt.** Geprüft an
    Eintrag 330 der ID-Tabelle sowie an Offset 43 der Slave_Struktur, wo die Quelle
    denselben Wert als `uint_8` beschreibt. Der Vorgabewert `inverter_state` der
    Einstellung `HEARTBEAT_METRIC_NAME` ist damit auf eine Objekt_ID mit kleiner
    Nutzlast gerichtet, was für einen Heartbeat erwünscht ist.
18. **P1.9, die Quelle bestätigt die fehlende Rückmeldung einer Aktion.** Die Quelle
    beschreibt `com_service` als Variable, die regelmäßig abgefragt wird und bei einer
    Änderung eine Handlung auslöst; sie nennt keine Rückmeldung über den Abschluss der
    Handlung und empfiehlt, den Wert vor einer Änderung auf 0 zurückzusetzen. Ein
    Read-back kann die Ausführung deshalb nicht belegen. Requirement 9 Kriterium 14 bis
    16 und Requirement 19 Kriterium 6 bis 9 ziehen die Folgerung: eigener
    Aktionsendpunkt, ausdrücklich unbestätigte Antwort.
19. **P0.4, der Diagnosebereich ist keine Protokolldurchreichung.** Requirement 30
    gibt im Diagnosebereich Objekt_IDs, Transportadressen und Netzkennungen aus, aber
    weder Command-Bytes, noch Frames, noch rohe Nutzdaten. Die Aussage der
    Introduction, das Protokoll sei nach außen nicht durchreichbar, bleibt damit
    unberührt.
20. **Verzeichnis- und Imagename am Ist-Zustand geprüft.** Das Projektverzeichnis
    besteht als `rct-manager/` und enthält derzeit ausschließlich die Protokollquelle
    `docs/reference/6707-RCT-Power-Serial-Communication-Protocol.pdf`. Ein Verzeichnis
    `rct-api/` besteht nicht. Dieses Dokument benennt das Projektverzeichnis deshalb
    durchgängig als `rct-manager/`. Das Container_Image heißt abweichend davon
    `docker.cirrio.de/rct-api` (Requirement 26 Kriterium 24). Beide Namen sind bewusst
    unterschiedlich, wie auch in den übrigen Projekten dieses Repositorys, und es
    besteht keine Abweichung zwischen Dokument und Repository mehr.
21. **Compose-Festlegungen auf Widersprüche geprüft.** Requirement 7 Kriterium 5
    verlangt genau eine Replik je Gerätegruppe, Requirement 14 Kriterium 7 die
    Veröffentlichung des Ports ausschließlich an eine Loopback-Adresse,
    Requirement 26 Kriterium 25 bis 31 den Dateinamen, das Fehlen des Schlüssels
    `version` und die Härtung. Die Festlegungen überschneiden sich in der
    Loopback-Bindung und der Replik; Requirement 26 Kriterium 35 verweist deshalb
    ausdrücklich auf Requirement 7 und Requirement 14, statt die Aussagen zu
    wiederholen. Ein inhaltlicher Widerspruch besteht nicht.
22. **Fehlerschlüssel auf Vollständigkeit geprüft.** Alle im Dokument verstreut
    eingeführten Fehlerschlüssel sind in der Tabelle des Requirement 25 geführt:
    `device_maintenance`, `write_outcome_unknown`, `batch_too_large`,
    `write_not_allowed`, `value_out_of_range`, `value_type_mismatch`,
    `value_not_finite`, `value_step_mismatch`, `queue_full`, `queue_timeout`,
    `device_timeout`, `device_unreachable`, `protocol_error`, `unknown_metric`,
    `unknown_device`, `write_disabled`, `metric_is_action`, `action_outcome_unknown`,
    `device_unavailable`, `device_budget_exhausted`, `fresh_batch_too_large` und
    `fresh_not_available_for_periodic_metric`. Die Schlüssel `invalid_float` und
    `decode_length_mismatch` stehen in der zweiten Tabelle, weil sie je Messwert im
    Feld `errors` und nicht als Statuscode einer Anfrage erscheinen. Die Werte des
    Feldes `stale_reason` nach Requirement 15 Kriterium 9 sind bewusst eine eigene,
    kleinere Menge, weil sie den Grund einer erfolgreichen Ersatzantwort benennen und
    keinen Fehler.

### Befunde der dritten Review-Runde

23. **Der Abschluss eines Schreibvorgangs auf dem Socket ist kein Nachweis.** Geprüft
    gegen die Eigenschaften von TCP, nicht gegen die Quelle: Ein erfolgreicher
    Schreibvorgang belegt ausschließlich die Übernahme der Bytes in den Sendepuffer des
    Betriebssystems, ein Fehler nach der ersten Byteübergabe lässt offen, wie viele
    Bytes die Gegenstelle erreicht haben. Die frühere Unterscheidung nach „vor“ und
    „nach dem vollständigen Senden“ war damit nicht entscheidbar. Requirement 9
    Kriterium 2 bis 5 legen den Commit_Point deshalb konservativ auf die erste
    Byteübergabe am Send_Gate; Requirement 24 Kriterium 11 verankert ihn dort.
24. **Protokollbegriffe im öffentlichen Vertrag beseitigt.** Der
    Bereitschafts_Endpunkt führte mit der Anzahl unerwarteter Frames, dem Sperrzustand
    und dem Zustandswert `bootloader` drei Größen, die Requirement 30 Kriterium 1 und 2
    im Herstellerneutralen_Vertrag ausschließen. Der Zustand heißt nach außen nun
    `maintenance`, der Fehlerschlüssel `device_maintenance`; die Frame-Zähler, die
    Ursache des Sperrzustands und die Anzahl angemeldeter periodischer Anforderungen
    stehen ausschließlich im Diagnosebereich (Requirement 16 Kriterium 11, 12 und 17,
    Requirement 30 Kriterium 9, 10, 19 und 20).
25. **Vorgabewert der Problem-Typ-URI neutralisiert.** Der frühere Vorgabewert
    `urn:rct-api:problem` nannte das Protokoll im Feld `type` jeder Fehlerantwort und
    verletzte damit Requirement 30 Kriterium 2 wörtlich. Der Vorgabewert ist
    `urn:device-api:problem` (Requirement 25 Kriterium 5 und Konfigurationsvertrag).
26. **Abbauablauf widerspruchsfrei gefasst.** Der frühere Ablauf verlangte zugleich das
    Einstellen der Anfragenannahme und das Beantworten eingehender Anfragen mit 503,
    und er verlangte das Abmelden der Periodik auch dann, wenn die Abbaufrist bereits
    abgelaufen war. Requirement 27 Kriterium 6 bis 20 trennen deshalb vier Phasen, lassen
    die HTTP-Annahme bestehen, weisen ausschließlich neue fachliche Arbeit ab und
    behandeln die Abbaufrist als Gesamtdeadline mit einer eigenen Abbaureserve für den
    Periodikabbau.
27. **Gültigkeitsdauer und Nachfrist liegen hintereinander.** Die frühere
    Startbedingung verlangte eine Nachfrist, die mindestens so lang wie die
    Gültigkeitsdauer ist, und widersprach damit der Begriffsbestimmung im Glossary.
    Requirement 15 Kriterium 11 und Requirement 22 Kriterium 14 halten nun fest, dass
    beide Größen aufeinander folgen und voneinander unabhängig sind.
28. **Transportgrößen sind keine Gerätemetriken.** Warteschlangenlänge, Arbeitsbudget,
    verworfene Bytes und unerwartete Frames gehören zum Transport_Endpunkt und nicht zu
    einem Gerät; bei einer Gerätegruppe hinter einem Master-Gerät hätte ein Gerätelabel
    denselben Wert mehrfach ausgegeben. Requirement 20 Kriterium 6 und 16 bis 19 führen
    diese Größen unter dem Präfix `rct_transport_` mit dem Label `endpoint`.
29. **Gleichzeitige Anfragen auf denselben Messwert.** Nach Ablauf der Gültigkeitsdauer
    hätten N gleichzeitige Anfragen auf dieselbe Kombination aus Gerät und Messwert N
    Lesetransaktionen erzeugt und die Warteschlange des Transport_Endpunkts mit
    identischer Arbeit gefüllt. Requirement 15 Kriterium 12 bis 15 begrenzen das auf
    genau eine Lesetransaktion und verlangen eine erneute Cache-Prüfung unmittelbar vor
    der Übergabe an das Send_Gate.
30. **Längenfeld langer Geräteantworten widerspricht der Protokollquelle.** Am
    RCT Power DC 10.0 mit Firmware 2.3.5687 wurden am 2026-10-02 von 18 langen
    Frames nur zwei mit korrekter Länge gemessen: 14 waren um 16 Byte zu klein,
    zwei um 472 beziehungsweise 480 Byte zu groß. Alle 278 kurzen Frames hatten
    ein korrektes Längenfeld. Die Hypothese, das Feld zähle nur `[Data]`, erklärt
    diese Abweichungen nicht. Requirement 2.13 bis 2.15 gewinnen deshalb nur bei
    langen Frames die Grenze aus der Rahmung und prüfen die CRC erneut.
31. **Protokollfremde Bytes zwischen Frames.** Die serielle Brücke sendete einen
    27-Byte-Block mit dem Anfang `2b 2b 2b 0d 2b 2b 2b f8`. Requirement 2.6
    deckt das Verwerfen ab; Zählung und begrenzte Protokollierung liegen im
    Empfangspfad. Die Menge zulässiger Command-Bytes wird nicht erweitert.
32. **`net.slave_data` bleibt ein kurzer Frame.** Zwei weitere gemessene Antworten
    waren kurze `0x05`-Frames mit Längenfeld 112, 108 Byte Nutzdaten und gültiger
    CRC. Requirement 18.2 bleibt unverändert.
33. **Ratenbegrenzung bleibt werkzeugneutral.** Der eigene Begrenzer in
    `app/security/ratelimit.py` führt getrennte Zähler, begrenzt die Schlüsseltabelle,
    verwirft bei Überlast sicher und bündelt IPv6-Adressen auf /64. Dafür ist keine
    zusätzliche SlowAPI-Abhängigkeit nötig.
34. **Paketliste an direkte Importe angepasst.** `app/config.py` importiert
    `python-dotenv`, die API importiert `starlette`; der Exporter baut das
    Prometheus-Textformat ohne `prometheus-client`. Requirement 23.2 bildet diese
    tatsächlichen Laufzeitabhängigkeiten ab.

### Abgleich mit rctpower_writesupport (2026-10-02)

Zusätzliche Implementierungsquelle: [rct.py, Commit 4a0d2e9](https://github.com/do-gooder/rctpower_writesupport/blob/4a0d2e9296b8d45abfaa1dfe437e6a7945b2b619/rct.py),
mit der dort festgelegten Abhängigkeit `rctclient==0.0.3` offline verglichen.
Alle zehn dort angebotenen Schreibparameter sind im ausgelieferten Katalog
vorhanden: SOC-Strategie und -Ziel, externe Batterieleistung, SOC-Minimum und
-Maximum, Ladeleistung und -Schwelle, `p_rec_lim[1]`, Netzstromfreigabe und
externe Leistungsreduktion. Objekt-IDs und skalare Kodierungen stimmen überein.

`rctclient` kodiert und dekodiert alle 15 `t_enum`-Objekte unseres PDF-Katalogs
als 1 Byte. Die ausgelieferte Registry führt deshalb für diese Objekte ausdrücklich
`byte_width: 1`; ihre Freigabelisten erlauben Rohwerte 0..255, für `com_service`
weiterhin ausschließlich 0..20. Das ist Implementierungsevidenz, kein neuer
Gerätenachweis. Der allgemeine Fallback bleibt überschreibbar.

Die Skriptgrenzen sind Werkzeugregeln und werden nicht als allgemeine
Anlagengrenzen übernommen: Beispielsweise nennt dessen README für `soc_min`
0..1, während der Code 0.05..1 prüft; für Leistungsreduktion unterscheiden sich
die genannten Dezimalstellen. Unsere API verändert oder klemmt Werte nicht
stillschweigend. Die dortigen Szenarien sind explizite Folgen einzelner
Schreibaufrufe, keine automatisch ausgeführten API-Aktionen. Das Referenzskript
bestätigt beim Schreiben lediglich das Senden; unsere API behält Readback und
`write_outcome_unknown` bei.


## Nachtrag 2026-10-03: Periodik am echten Gerät, Secrets, fresh ohne names

**Requirement 17 (Ergänzung).**

26. IF der Schreibzugriff auf `pas.period` seinen Commit_Point erreicht, aber keine Antwort eintrifft (das Gerät beantwortet WRITE nie, Requirement 9.18), THEN THE Protokoll_Adapter SHALL den Wert per READ auf `0x9C8FE559` zurücklesen und bei Übereinstimmung mit dem Sollwert die Periodik fortsetzen; nur ein Fehler vor dem Commit_Point oder ein abweichender Wert gilt als Fehlschlag.
27. THE Protokoll_Adapter SHALL beim Beenden nach einem committed-unbestätigten Schreibzugriff `pas.period = 0` höchstens einen begrenzten Readback ausführen und den Schreibzugriff andernfalls als gesendet werten und protokollieren.
28. WHEN die Einrichtung der Periodik fehlschlägt, THE Protokoll_Adapter SHALL den nächsten Versuch mit begrenztem exponentiellem Abstand (10 s bis höchstens 300 s) ausführen, höchstens einen Einrichtungsversuch je Gerät gleichzeitig zulassen und den Abstand erst nach einem Erfolg zurücksetzen; `pas.period` darf nicht alle 10 s neu geschrieben werden.
29. THE Protokoll_Adapter SHALL eine Anmeldung als erfolgreich werten, wenn das Gerät den Frame für die Objekt_ID liefert (die sofortige Erstantwort), und die Objekt_ID vor dem Senden der Anmeldung als periodisch führen, damit die Erstantwort den Cache erreicht.
30. THE Cache SHALL einen periodisch gelieferten Wert als frisch führen, solange seine Anmeldung auf der aktuellen Verbindung besteht, weil das Gerät manche Werte nach der Anmeldung nicht oder nur selten erneut sendet (Messung: einzelne Monats- und Jahreswerte genau einmal).

**Requirement 4 und 19 (Ergänzung).**

23. THE Objekt_Registry und die Freigabeliste SHALL keine Zugangsdaten führen; beim Start bricht ein Eintrag mit dem Namen `wifi_password` oder der Objekt_ID `0x14C0E627` in einer der beiden Dateien den Start mit `ConfigError` ab. Dies ersetzt die Zählung in Requirement 19 Kriterium 25: ausgeliefert werden 894 Objekt_IDs und 893 skalare Freigaben.

**Requirement 10 (Ergänzung).**

26. WHERE ein Aufrufer `fresh=true` ohne `names` übergibt, THE REST_API SHALL den HTTP-Statuscode 422 mit dem Fehlerschlüssel `invalid_request` und dem Detail "fresh=true requires an explicit names list of at most N metrics" zurückgeben und keine Transaktion auslösen. Dies ersetzt die Aussage zu `fresh_batch_too_large` in Kriterium 25.
