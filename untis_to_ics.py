import re
import os
import sys
import pytz
import requests
import webuntis
from datetime import datetime, timedelta
from ics import Calendar, Event
from dotenv import load_dotenv

# -------------------- ENV helpers --------------------
def get_env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        print(f"Missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return v

# -------------------- WebUntis Session --------------------
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
    sid = os.getenv("UNTIS_STUDENT_ID")
    tid = os.getenv("UNTIS_TEACHER_ID")
    cid = os.getenv("UNTIS_CLASS_ID")
    if sid:
        return {"studentId": int(sid)}
    if tid:
        return {"teacherId": int(tid)}
    if cid:
        return {"classId": int(cid)}
    try:
        me = session.get_current_user()
        if me and hasattr(me, "personType") and me.personType and me.personId:
            return {"personType": me.personType, "personId": me.personId}
    except Exception:
        pass
    raise RuntimeError("Konnte keinen Scope bestimmen (ENV-IDs setzen).")

def fetch_timetable(session, scope, start, end):
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

# -------------------- Resolver --------------------
def _safe_join(items):
    return ", ".join([x for x in items if x]) if items else ""

def resolve_subject_names(session, lesson):
    try:
        data = getattr(lesson, "_data", {}) or {}
        su = data.get("su", [])
        names = []
        for x in su:
            try:
                sid = x.get("id")
                if sid is None:
                    continue
                subj = session.subjects().filter(id=sid)[0]
                nm = getattr(subj, "long_name", None) or getattr(subj, "name", None)
                if nm:
                    names.append(nm)
            except Exception:
                continue
        if not names:
            s = getattr(lesson, "subject", None)
            nm = getattr(s, "long_name", None) or getattr(s, "name", None)
            if nm:
                names.append(nm)
        return names
    except Exception:
        return []

def resolve_teacher_names(session, lesson):
    try:
        data = getattr(lesson, "_data", {}) or {}
        te = data.get("te", [])
        names = []
        for x in te:
            try:
                tid = x.get("id")
                if tid is None:
                    continue
                t = session.teachers().filter(id=tid)[0]
                nm = getattr(t, "long_name", None) or getattr(t, "name", None)
                if nm:
                    names.append(nm)
            except Exception:
                continue
        if not names:
            for t in getattr(lesson, "teachers", []) or []:
                try:
                    nm = getattr(t, "long_name", None) or getattr(t, "name", None)
                    if nm:
                        names.append(nm)
                except Exception:
                    continue
        return names
    except Exception:
        return []

def resolve_room_names(session, lesson):
    try:
        data = getattr(lesson, "_data", {}) or {}
        ro = data.get("ro", [])
        names = []
        for x in ro:
            try:
                rid = x.get("id")
                if rid is None:
                    continue
                r = session.rooms().filter(id=rid)[0]
                nm = getattr(r, "name", None) or getattr(r, "long_name", None)
                if nm:
                    names.append(nm)
            except Exception:
                continue
        if not names:
            for r in getattr(lesson, "rooms", []) or []:
                try:
                    nm = getattr(r, "name", None) or getattr(r, "long_name", None)
                    if nm:
                        names.append(nm)
                except Exception:
                    continue
        return names
    except Exception:
        return []

# -------------------- Status / Prüfungen --------------------
def lesson_status(lesson):
    code = (getattr(lesson, "code", None) or "").lower()
    ltype = (getattr(lesson, "_data", {}).get("lstype") or "").lower()
    info = (getattr(lesson, "substText", None) or getattr(lesson, "info", None) or "").strip()

    is_cancel = getattr(lesson, "is_cancelled", False) or code in {"cancelled", "canc", "absent"}
    is_subst = code in {"irregular", "subst", "assigned"}
    is_exam = ("exam" in ltype) or any(k in info.lower() for k in ["prüfung", "klausur", "exam", "arbeit"])

    if is_cancel:
        return "Entfall", info
    if is_exam:
        return "Prüfung", info
    if is_subst:
        return "Vertretung", info
    return "", info

# -------------------- Hausaufgaben via REST --------------------
def fetch_homeworks(session, student_id, start, end):
    try:
        base = f"https://{get_env('WEBUNTIS_SERVER', required=True)}/WebUntis/api/rest/view/v1/homeworks"
        params = {
            "resourceType": "STUDENT",
            "resourceId": int(student_id),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        rs = getattr(session, "_session", None) or requests.Session()
        resp = rs.get(base, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json() if resp.content else []
    except Exception:
        return {}

    hw = {}
    for item in data or []:
        try:
            due = item.get("dueDate")
            subj = item.get("subject", {}).get("id")
            txt = (item.get("text") or "").strip()
            if due and subj and txt:
                key = (due, int(subj))
                hw.setdefault(key, []).append(txt)
        except Exception:
            continue
    return hw

# -------------------- MAIN --------------------
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

        student_id = os.getenv("UNTIS_STUDENT_ID")
        homeworks = {}
        if student_id:
            homeworks = fetch_homeworks(session, int(student_id), start, end)

        cal = Calendar()
        seen_uids = set()

        for l in lessons:
            try:
                begin = tz.localize(l.start)
                finish = tz.localize(l.end)
            except Exception:
                begin = getattr(l, "start", None)
                finish = getattr(l, "end", None)
            if not begin or not finish:
                continue

            subjects = resolve_subject_names(session, l)
            subject_name = subjects[0] if subjects else "Unterricht"

            rooms = resolve_room_names(session, l)
            room = _safe_join(rooms)

            teachers_list = resolve_teacher_names(session, l)
            teachers = _safe_join(teachers_list)

            prefix, extra_note = lesson_status(l)
            title_core = subject_name + (f" · {room}" if room else "")
            title = f"{prefix}: {title_core}" if prefix else title_core

            e = Event()
            e.name = title
            e.begin = begin
            e.end = finish
            e.location = room or None

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

            # Hausaufgaben für Fach + Datum hinzufügen
            hw_notes = []
            try:
                raw_su = (getattr(l, "_data", {}) or {}).get("su", []) or []
                subj_ids = [int(x.get("id")) for x in raw_su if isinstance(x, dict) and x.get("id") is not None]
                due_dates = [begin.date().isoformat(), finish.date().isoformat()]
                for due in due_dates:
                    for sid in subj_ids:
                        for hw in homeworks.get((due, sid), []):
                            hw_notes.append(f"Hausaufgabe (fällig {due}): {hw}")
            except Exception:
                pass
            if hw_notes:
                notes.extend(hw_notes)

            if notes:
                e.description = "\n".join(notes)

            e.uid = f"{begin.isoformat()}|{subject_name}|{room}|{teachers}"
            if prefix == "Entfall":
                e.status = "CANCELLED"

            if e.uid in seen_uids:
                continue
            seen_uids.add(e.uid)
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
