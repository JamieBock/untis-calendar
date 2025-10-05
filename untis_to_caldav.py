import sys
import pytz
from caldav import DAVClient
from caldav.lib.error import NotFoundError
from dotenv import load_dotenv

from untis_common import (
    build_event,
    determine_timerange,
    fetch_timetable,
    get_env,
    login_session,
    normalize_lessons,
    pick_scope,
    timerange_to_datetimes,
)

def get_icloud_calendar(client, name):
    principal = client.principal()
    try:
        calendars = principal.calendars()
    except Exception as e:
        print("Fehler beim Laden der Kalender:", e, file=sys.stderr)
        sys.exit(3)

    for c in calendars:
        if c.name == name:
            return c

    # Falls nicht vorhanden → erstellen
    return principal.make_calendar(name=name)

def main():
    load_dotenv()
    tzname = get_env("TIMEZONE", "Europe/Berlin")
    tz = pytz.timezone(tzname)

    session = login_session()
    try:
        start, end = determine_timerange(tz)

        print(f"Hole Untis-Stundenplan für {start} bis {end}...")

        scope = pick_scope(session)
        lesson_details = normalize_lessons(fetch_timetable(session, scope, start, end), tz)

        # iCloud CalDAV
        username = get_env("ICLOUD_USERNAME", required=True)
        app_pw = get_env("ICLOUD_APP_PASSWORD", required=True)
        cal_name = get_env("ICLOUD_CALENDAR_NAME", "Untis")

        # iCloud CalDAV URL (Entdeckung über Standard-Endpoint)
        # caldav lib findet die richtige URL via .principal()
        client = DAVClient(url="https://caldav.icloud.com/", username=username, password=app_pw)

        cal = get_icloud_calendar(client, cal_name)

        # Für idempotentes Update: existierende Events dieses Fensters löschen (einfachster Weg)
        # Alternativ: pro-UID updaten.
        try:
            window_start, window_end = timerange_to_datetimes(start, end, tz)
            existing = cal.date_search(start=window_start, end=window_end)
            for ev in existing:
                try:
                    ev.delete()
                except Exception:
                    pass
        except NotFoundError:
            pass

        # Neue Events erzeugen
        from ics import Calendar

        tmp_cal = Calendar()
        for details in lesson_details:
            tmp_cal.events.add(build_event(details))

        # Alles in einem Upload hinzufügen
        cal.add_event(tmp_cal)
        print("iCloud Kalender aktualisiert.")
    finally:
        try:
            session.logout()
        except Exception:
            pass

if __name__ == "__main__":
    main()
