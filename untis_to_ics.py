import os
import pytz
from ics import Calendar
from dotenv import load_dotenv

from untis_common import (
    build_event,
    determine_timerange,
    fetch_timetable,
    get_env,
    login_session,
    normalize_lessons,
    pick_scope,
)

def main():
    load_dotenv()
    tzname = get_env("TIMEZONE", "Europe/Berlin")
    tz = pytz.timezone(tzname)

    out_path = get_env("ICS_OUTPUT_PATH", "./docs/untis.ics")
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    session = login_session()
    try:
        start, end = determine_timerange(tz)

        print(f"Hole Untis-Stundenplan für {start} bis {end}...")

        scope = pick_scope(session)
        lesson_details = normalize_lessons(fetch_timetable(session, scope, start, end), tz)

        cal = Calendar()
        for details in lesson_details:
            cal.events.add(build_event(details))

        with open(out_path, "w", encoding="utf-8") as f:
            f.writelines(cal.serialize_iter())

        print(f"ICS geschrieben: {out_path}")
    finally:
        try:
            session.logout()
        except Exception:
            pass

if __name__ == "__main__":
    main()
