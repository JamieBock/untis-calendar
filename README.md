# Untis → Kalender (stündlich)

Dieses Repo enthält eine kleine Automation, die deinen **WebUntis**-Stundenplan holt und
in deinen Kalender bringt. Zwei Wege sind vorbereitet:

1. **Variante A – ICS veröffentlichen (empfohlen, einfach):**
   - Script generiert `docs/untis.ics`
   - Mit GitHub Pages veröffentlichen → Apple Kalender **einmal abonnieren** (`https://<dein-user>.github.io/<repo>/untis.ics`)
   - Aktualisierung: GitHub Actions läuft **stündlich**, Apple Kalender lädt je nach Client in Intervallen nach
     (macOS kann z. B. 5 Minuten, iOS meist seltener).

2. **Variante B – Direkt in iCloud per CalDAV (live push):**
   - Script schreibt direkt in einen iCloud-Kalender (z. B. „Untis“)
   - Benötigt **App-spezifisches Passwort** (2FA) und speichert Events sofort.
   - Achtung: Zugangsdaten sind sensibel → als **Repository Secrets** hinterlegen.

> Hinweis: Manche Schulen deaktivieren die eingebaute ICS-Funktion in WebUntis.
> Diese Lösung umgeht das, indem sie den Stundenplan über die WebUntis-API holt.

---

## Schnellstart

1. **Repo vorbereiten**
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env
   # .env mit deinen Daten füllen
   ```

2. **Lokal testen**
   ```bash
   python untis_to_ics.py   # erzeugt docs/untis.ics
   # Optional:
   # python untis_to_caldav.py  # schiebt in iCloud-Kalender
   ```

   > Zeitraum anpassen? Über die Variablen `UNTIS_DAYS_BACK` (Vergangenheit) und `UNTIS_DAYS_FORWARD`
   > bzw. `UNTIS_LOOKAHEAD_DAYS` (Fallback, Standard 14) steuerst du, wie viel vom Stundenplan geladen wird.

3. **GitHub Actions aktivieren (stündlich):**
   - Repo auf GitHub pushen
   - Unter **Settings → Pages** → Branch `main`, Ordner `/docs` wählen
   - Unter **Settings → Secrets and variables → Actions → New repository secret** die folgenden Secrets anlegen (falls Variante B genutzt wird):
     - `ICLOUD_USERNAME`
     - `ICLOUD_APP_PASSWORD`
     - `ICLOUD_CALENDAR_NAME` (z. B. `Untis`)
   - `.env`-Werte für WebUntis als Secrets anlegen (empfohlen):  
     - `WEBUNTIS_SERVER`, `WEBUNTIS_SCHOOL`, `WEBUNTIS_USERNAME`, `WEBUNTIS_PASSWORD`, `WEBUNTIS_CLIENT`,
       optional `UNTIS_STUDENT_ID`, `UNTIS_CLASS_ID`, `UNTIS_TEACHER_ID`

4. **Apple Kalender**
   - **Variante A:** Kalender **abonnieren**: `https://<user>.github.io/<repo>/untis.ics`
   - **Variante B:** Der iCloud-Kalender „Untis“ erscheint automatisch auf allen Apple Geräten.

---

## Technische Details

- Holt den Plan für **heute bis +14 Tage** (per `UNTIS_DAYS_BACK`, `UNTIS_DAYS_FORWARD` bzw. `UNTIS_LOOKAHEAD_DAYS` konfigurierbar).
- Dedupliziert/aktualisiert via UID pro Event (Datum-Fach-Raum-Lehrer).
- Konvertiert WebUntis-Fächer/Kürzel in lesbare Titel (anpassbar).
- Zeitzone standardmäßig `Europe/Berlin`.

> **Rechtliches/IT:** Beachte die Nutzungsbedingungen deiner Schule. Verwende möglichst Schüler-Zugangsdaten
> und halte Passwörter sicher (Secrets).

Viel Erfolg! ✌️
