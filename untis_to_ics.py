import os
import sys
import pytz
import webuntis
from datetime import datetime, timedelta
from ics import Calendar, Event
from dotenv import load_dotenv

def get_env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        print(f"Missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return v

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
    # 1) IDs aus ENV (empfohlen bei neuem Perseus-Frontend / SSO)
    sid = os.getenv("UNTIS_STUDENT_ID")
    tid = os.getenv("UNTIS_TEACHER_ID")
    cid = os.getenv("UNTIS_CLASS_ID")
    if sid:
        return {"studentId": int(sid)}
    if tid:
        return {"teacherId": int(tid)}
    if cid:
        return {"classId": int(cid)}

    # 2) Fallback: current user (funktioniert bei klassischem Login)
    try:
        me = session.get_current_user()
        if me and hasattr(me, "personType") and me.personType and me.personId:
            return {"personType": me.personType, "personId": me.personId}
    except Exception:
        pass

    raise RuntimeError("Konnte keinen Scope bestimmen (weder ENV-IDs noch get_current_user()).")

def fetch_timetable(session, scope, start, end):
    # webuntis 0.1.x erwartet 'student' / 'klasse' / 'teacher' als Keyword
    kw = {"start": start, "end": end}
    if "studentId" in scope:
        kw["student"] = int(scope["studentId"])
    elif "teacherId" in scope:
        kw["teacher"] = int(scope["teacherId"])
    elif "classId" in scope:
        kw["klasse"] = int(scope["classId"])
    elif "personType" in scope and "personId" in scope:
        if int(scope["personType"]) == 5:
            kw["student"] = int(scope["personId"])
        elif int(scope["personType"]) == 2:
            kw["teacher"] = int(scope["personId"])
        elif int(scope["personType"]) == 1:
            kw["klasse"] = int(scope["personId"])
    else:
        raise RuntimeError("Unbekannter Scope für timetable().")
    return session.timetable(**kw)

def main():
    load_dotenv()
    tzname = get_env("TIMEZONE", "Europe/Berlin")
    tz = pytz.timezone(tzname)

    out_path = get_env("ICS_OUTPUT_PATH", "./docs/untis.ics")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    session = login_session()
    try:
        start = datetime.now(tz).date()
        end = (datetime.now(tz) + timedelta(days=14)).date()

        scope = pick_scope(session)
        lessons = fetch_timetable(session, scope, start, end)

        cal = Calendar()

        for l in lessons:
            # Zeiten (defensiv)
            try:
                begin = tz.localize(l.start)
                finish = tz.localize(l.end)
            except Exception:
                begin = getattr(l, "start", None)
                finish = getattr(l, "end", None)
            if not begin or not finish:
                continue  # ohne Zeiten kein Termin

            # Fach (defensiv)
            subject_name = "Unterricht"
            try:
                subject = getattr(l, "subject", None)
                subject_name = (
                    getattr(subject, "long_name", None)
                    or getattr(subject, "name", None)
                    or "Unterricht"
                )
            except Exception:
                subject_name = "Unterricht"

            # Räume (defensiv)
            room = ""
            try:
                rs = getattr(l, "rooms", None)
                if rs:
                    rnames = []
                    for r in rs:
                        try:
                            nm = getattr(r, "name", None) or getattr(r, "long_name", None)
                            if nm:
                                rnames.append(nm)
                        except Exception:
                            continue
                    room = ", ".join(rnames)
            except Exception:
                room = ""

            # Lehrkräfte (defensiv)
            teachers = ""
            try:
                ts = getattr(l, "teachers", None)
                if ts:
                    tnames = []
                    for t in ts:
                        try:
                            nm = getattr(t, "long_name", None) or getattr(t, "name", None)
                            if nm:
                                tnames.append(nm)
                        except Exception:
                            continue
                    teachers = ", ".join(tnames)
            except Exception:
                teachers = ""

            title = subject_name + (f" · {room}" if room else "")

            e = Event()
            e.name = title
            e.begin = begin
            e.end = finish
            e.location = room or None

            notes = []
            if teachers:
                notes.append(f"Lehrkraft: {teachers}")
            code = getattr(l, "code", None)
            if code:
                notes.append(f"Code: {code}")
            if getattr(l, "substText", None):
                notes.append(f"Hinweis: {l.substText}")
            if notes:
                e.description = "\n".join(notes)

            e.uid = f"{begin.isoformat()}|{subject_name}|{room}|{teachers}"

            if getattr(l, "is_cancelled", False) or str(getattr(l, "code", "")).upper() in {"CANCELLED", "CANC"}:
                e.status = "CANCELLED"

            cal.events.add(e)

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
