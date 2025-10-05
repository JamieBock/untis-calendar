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

def _safe_join(items):
    return ", ".join([x for x in items if x]) if items else ""

def resolve_subject_names(session, lesson):
    # 0.1.x: lesson._data["su"] ist Liste mit Subject-IDs
    try:
        su = getattr(lesson, "_data", {}).get("su", [])
        if not su:
            # fallback: einzelne subject-Attr
            s = getattr(lesson, "subject", None)
            name = getattr(s, "long_name", None) or getattr(s, "name", None)
            return [name] if name else []
        ids = [x.get("id") for x in su if isinstance(x, dict)]
        names = []
        if ids:
            # session.subjects().filter(id=ID) → 1 Element
            for sid in ids:
                try:
                    subj = session.subjects().filter(id=sid)[0]
                    nm = getattr(subj, "long_name", None) or getattr(subj, "name", None)
                    if nm:
                        names.append(nm)
                except Exception:
                    continue
        return names
    except Exception:
        return []

def resolve_teacher_names(session, lesson):
    try:
        te = getattr(lesson, "_data", {}).get("te", [])
        ids = [x.get("id") for x in te if isinstance(x, dict)]
        tnames = []
        if ids:
            for tid in ids:
                try:
                    t = session.teachers().filter(id=tid)[0]
                    nm = getattr(t, "long_name", None) or getattr(t, "name", None)
                    if nm:
                        tnames.append(nm)
                except Exception:
                    continue
        # Fallback: high-level API
        if not tnames:
            for t in getattr(lesson, "teachers", []) or []:
                try:
                    nm = getattr(t, "long_name", None) or getattr(t, "name", None)
                    if nm:
                        tnames.append(nm)
                except Exception:
                    continue
        return tnames
    except Exception:
        return []

def resolve_room_names(session, lesson):
    try:
        ro = getattr(lesson, "_data", {}).get("ro", [])
        ids = [x.get("id") for x in ro if isinstance(x, dict)]
        rnames = []
        if ids:
            for rid in ids:
                try:
                    r = session.rooms().filter(id=rid)[0]
                    nm = getattr(r, "name", None) or getattr(r, "long_name", None)
                    if nm:
                        rnames.append(nm)
                except Exception:
                    continue
        if not rnames:
            for r in getattr(lesson, "rooms", []) or []:
                try:
                    nm = getattr(r, "name", None) or getattr(r, "long_name", None)
                    if nm:
                        rnames.append(nm)
                except Exception:
                    continue
        return rnames
    except Exception:
        return []

def lesson_status(lesson):
    """
    Liefert ('prefix', 'notes_extra') basierend auf Lesson-Feldern.
    """
    # 0.1.x hat oft 'code' und 'statflags' / 'lstype'
    code = (getattr(lesson, "code", None) or "").lower()
    ltype = (getattr(lesson, "_data", {}).get("lstype") or "").lower()
    info = getattr(lesson, "substText", None) or getattr(lesson, "info", None) or ""
    notes = []
    prefix = ""

    # Entfall
    if getattr(lesson, "is_cancelled", False) or code in {"cancelled", "canc", "absent"}:
        prefix = "Entfall"
    # Vertretung
    elif code in {"irregular", "subst", "assigned"}:
        prefix = "Vertretung"
    # Prüfung
    if "exam" in ltype or "prüfung" in info.lower() or "klausur" in info.lower() or "exam" in info.lower():
        prefix = "Prüfung" if not prefix else prefix  # Prüfung hat Priorität nur wenn kein Entfall

    if info:
        notes.append(info)

    return prefix, _safe_join(notes)


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

            # Fach / Raum / Lehrer robust auflösen
subjects = resolve_subject_names(session, l)
subject_name = subjects[0] if subjects else "Unterricht"

rooms = resolve_room_names(session, l)
room = _safe_join(rooms)

teachers_list = resolve_teacher_names(session, l)
teachers = _safe_join(teachers_list)

# Status bestimmen (Prüfung, Vertretung, Entfall)
prefix, extra_note = lesson_status(l)

# Titel
title_core = subject_name + (f" · {room}" if room else "")
title = f"{prefix}: {title_core}" if prefix else title_core

# 🧱 Ereignis erstellen
e = Event()
e.name = title
e.begin = begin
e.end = finish
e.location = room or None

# Beschreibung (Description im Kalender)
notes = []
if teachers:
    notes.append(f"Lehrkraft: {teachers}")
if extra_note:
    notes.append(extra_note)
if getattr(l, "substText", None) and getattr(l, "substText") not in notes:
    notes.append(f"Hinweis: {l.substText}")
code_val = getattr(l, "code", None)
if code_val:
    notes.append(f"Code: {code_val}")
if notes:
    e.description = "\n".join(notes)

# stabile UID (damit sich Termine korrekt aktualisieren)
e.uid = f"{begin.isoformat()}|{subject_name}|{room}|{teachers}"

# Entfall explizit kennzeichnen
if prefix == "Entfall":
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
