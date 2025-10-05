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
        jsonrpc_endpoint="/WebUntis/jsonrpc.do",
    )
    return s.login()

def pick_scope(session):
    # Versuch 1: Eigene Person ermitteln (funktioniert i.d.R. für Schüler-Accounts)
    try:
        me = session.get_current_user()
        if me and hasattr(me, "personType") and me.personType and me.personId:
            return {"personType": me.personType, "personId": me.personId}
    except Exception:
        pass

    # Fallback: IDs aus .env
    sid = os.getenv("UNTIS_STUDENT_ID")
    tid = os.getenv("UNTIS_TEACHER_ID")
    cid = os.getenv("UNTIS_CLASS_ID")
    if sid:
        return {"studentId": int(sid)}
    if tid:
        return {"teacherId": int(tid)}
    if cid:
        return {"classId": int(cid)}

    raise RuntimeError("Konnte keinen Scope bestimmen (kein get_current_user und keine IDs in .env).")

def fetch_timetable(session, scope, start, end):
    # webuntis-lib akzeptiert keyword je nach Scope
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
            # l enthält u. a.: start, end (datetime naive, Europe/Berlin), subject, room, teacher, code (=cancelled? substitution?)
            # Library liefert Felder je nach Version. Wir versuchen defensiv zu lesen.
            try:
                begin = tz.localize(l.start)
                finish = tz.localize(l.end)
            except Exception:
                # Falls schon aware:
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

            # UID stabilisieren
            uid_key = f"{begin.isoformat()}|{subject_name}|{room}|{teachers}"
            e.uid = uid_key

            # Entfall markieren
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
