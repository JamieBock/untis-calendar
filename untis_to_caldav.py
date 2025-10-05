import os
import sys
import pytz
import webuntis
from datetime import datetime, timedelta
from dateutil.parser import isoparse
from caldav import DAVClient
from caldav.lib.error import NotFoundError
from ics import Event
from dotenv import load_dotenv

def get_env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        print(f"Missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return v

def login_session():
    def login_session():
    s = webuntis.Session(
        server=get_env("WEBUNTIS_SERVER", required=True),
        school=get_env("WEBUNTIS_SCHOOL", required=True),
        username=get_env("WEBUNTIS_USERNAME", required=True),
        password=get_env("WEBUNTIS_PASSWORD", required=True),
        useragent=get_env("WEBUNTIS_CLIENT", "untis-cal-sync"),
        )
        return s.login()


def pick_scope(session):
    try:
        me = session.get_current_user()
        if me and hasattr(me, "personType") and me.personType and me.personId:
            return {"personType": me.personType, "personId": me.personId}
    except Exception:
        pass
    sid = os.getenv("UNTIS_STUDENT_ID")
    tid = os.getenv("UNTIS_TEACHER_ID")
    cid = os.getenv("UNTIS_CLASS_ID")
    if sid:
        return {"studentId": int(sid)}
    if tid:
        return {"teacherId": int(tid)}
    if cid:
        return {"classId": int(cid)}
    raise RuntimeError("Konnte keinen Scope bestimmen.")

def fetch_timetable(session, scope, start, end):
    kw = {"start": start, "end": end}
    if "studentId" in scope:
        kw["studentId"] = scope["studentId"]
    elif "teacherId" in scope:
        kw["teacherId"] = scope["teacherId"]
    elif "classId" in scope:
        kw["klasseId"] = scope["classId"]
    elif "personType" in scope and "personId" in scope:
        kw["personType"] = scope["personType"]
        kw["personId"] = scope["personId"]
    return session.timetable(**kw)

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
        start = datetime.now(tz).date()
        end = (datetime.now(tz) + timedelta(days=14)).date()

        scope = pick_scope(session)
        lessons = fetch_timetable(session, scope, start, end)

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
            existing = cal.date_search(
                start=datetime.now(tz) - timedelta(days=1),
                end=datetime.now(tz) + timedelta(days=30)
            )
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

        for l in lessons:
            try:
                begin = tz.localize(l.start)
                finish = tz.localize(l.end)
            except Exception:
                begin = l.start
                finish = l.end

            subject = getattr(l, "subject", None)
            subject_name = getattr(subject, "long_name", None) or getattr(subject, "name", None) or "Unterricht"
            room = ", ".join(r.name for r in getattr(l, "rooms", []) if hasattr(r, "name")) or ""
            teachers = ", ".join(t.long_name or t.name for t in getattr(l, "teachers", []) if hasattr(t, "name"))

            title = subject_name
            if room:
                title += f" · {room}"

            e = Event()
            e.name = title
            e.begin = begin
            e.end = finish
            e.location = room or None

            notes_parts = []
            if teachers:
                notes_parts.append(f"Lehrkraft: {teachers}")
            code = getattr(l, "code", None)
            if code:
                notes_parts.append(f"Code: {code}")
            if getattr(l, "substText", None):
                notes_parts.append(f"Hinweis: {l.substText}")
            if notes_parts:
                e.description = "\n".join(notes_parts)

            uid_key = f"{begin.isoformat()}|{subject_name}|{room}|{teachers}"
            e.uid = uid_key

            tmp_cal.events.add(e)

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
