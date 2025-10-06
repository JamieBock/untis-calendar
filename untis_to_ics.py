import os
import sys
import pytz
import re
import webuntis
from datetime import datetime, timedelta, time, date
from ics import Calendar, Event
from dotenv import load_dotenv

# ==================== ENV HELPERS ====================
def get_env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        print(f"Missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return v

# ==================== UNTIS LOGIN ====================
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

# ==================== TEXT-/HA-ERKENNUNG ====================
EXAM_KEYWORDS = ["prüfung", "klausur", "ex", "exam", "arbeit", "test", "leistungskontrolle", "ka", "lk"]
HOMEWORK_LINE = re.compile(r'\b(hausaufgabe|ha\b|aufgabe|vokabeln|übung|uebung)\b[:\-]?\s*(.*)', re.IGNORECASE)

WEEKDAYS_DE = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6
}

DATE_DDMMYYYY = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DATE_DDMM = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")

def extract_info_text(lesson):
    parts = []
    for key in ("substText", "info"):
        val = getattr(lesson, key, None)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    raw = getattr(lesson, "_data", {}) or {}
    for key in ("txt", "lsnote", "notice"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return "\n".join(parts)

def parse_due_date(text, base_day: date):
    if not text:
        return None
    t = text.lower()
    if "heute" in t:
        return base_day
    if "morgen" in t:
        return base_day + timedelta(days=1)
    for w, idx in WEEKDAYS_DE.items():
        if w in t:
            delta = (idx - base_day.weekday()) % 7
            delta = 7 if delta == 0 else delta
            return base_day + timedelta(days=delta)
    m = DATE_DDMMYYYY.search(t)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mth, d)
        except Exception:
            pass
    m = DATE_DDMM.search(t)
    if m:
        d, mth = int(m.group(1)), int(m.group(2))
        try:
            cand = date(base_day.year, mth, d)
            if cand < base_day:
                cand = date(base_day.year + 1, mth, d)
            return cand
        except Exception:
            pass
    return None

def find_homework_items(info_text):
    results = []
    if not info_text:
        return results
    for line in info_text.splitlines():
        m = HOMEWORK_LINE.search(line)
        if m:
            text_part = (m.group(2) or "").strip() or line.strip()
            results.append(text_part)
    return results

def detect_exam_from_text(info_text):
    if not info_text:
        return False
    low = info_text.lower()
    return any(k in low for k in EXAM_KEYWORDS)

# ==================== BLOCK-MERGE ====================
def merge_into_blocks(intervals, max_gap_min=15):
    """Fasst Unterrichtsblöcke zusammen, wenn Lücke <= max_gap_min."""
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    max_gap = timedelta(minutes=max_gap_min)
    merged = [list(intervals[0])]
    for b, e in intervals[1:]:
        last_b, last_e = merged[-1]
        if b - last_e <= max_gap:
            if e > last_e:
                merged[-1][1] = e
        else:
            merged.append([b, e])
    return [(b, e) for b, e in merged]

# ==================== MAIN ====================
def main():
    load_dotenv()
    tzname = get_env("TIMEZONE", "Europe/Berlin")
    tz = pytz.timezone(tzname)
    out_path = get_env("ICS_OUTPUT_PATH", "./docs/untis.ics")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    HW_TIME = time(17, 0)
    session = login_session()

        try:
        start = datetime.now(tz).date()
        end = (datetime.now(tz) + timedelta(days=14)).date()
        scope = pick_scope(session)
        lessons = fetch_timetable(session, scope, start, end)

        cal = Calendar()
        seen_uids = set()

        # Gruppiere Unterricht pro Tag (nur nicht-cancelled)
        by_day = {}
        for l in lessons:
            ...
            by_day.setdefault(begin.date(), []).append((begin, finish))

        # Erstelle zusammengefasste Schulblöcke (Titel inkl. Uhrzeit, stabile UID)
        for day, intervals in sorted(by_day.items()):
            blocks = merge_into_blocks(intervals, max_gap_min=15)
            for b, e in blocks:
                ev = Event()
                ev.name = f"Schule {b.strftime('%H:%M')}–{e.strftime('%H:%M')}"
                ev.begin = b
                ev.end = e
                b_utc = b.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
                e_utc = e.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
                uid = f"{b_utc}-{e_utc}@untis-jamie"
                ev.uid = uid
                if uid not in seen_uids:
                    seen_uids.add(uid)
                    cal.events.add(ev)

        # Hausaufgaben & Prüfungen als eigene Termine
        created_hw, created_exam = set(), set()
        for l in lessons:
            try:
                begin = tz.localize(l.start)
            except Exception:
                begin = getattr(l, "start", None)
            if not begin:
                continue
            base_day = begin.date()
            info_text = extract_info_text(l)

            # Fachname (für Titel)
            subjects = []
            try:
                data = getattr(l, "_data", {}) or {}
                su = data.get("su", [])
                for x in su:
                    sid = x.get("id")
                    if sid:
                        subj = session.subjects().filter(id=sid)[0]
                        nm = getattr(subj, "long_name", None) or getattr(subj, "name", None)
                        if nm:
                            subjects.append(nm)
            except Exception:
                pass
            subject = subjects[0] if subjects else "Fach"

            # Hausaufgaben
            for txt in find_homework_items(info_text):
                due = parse_due_date(txt, base_day) or parse_due_date(info_text, base_day)
                if not due:
                    continue
                key = (due.isoformat(), subject, txt)
                if key in created_hw:
                    continue
                created_hw.add(key)
                hw_begin = tz.localize(datetime.combine(due, HW_TIME))
                hw_end = hw_begin + timedelta(minutes=30)
                ev = Event()
                ev.name = f"{subject} – Hausaufgabe"
                ev.begin = hw_begin
                ev.end = hw_end
                ev.description = txt
                ev.uid = f"HW|{due.isoformat()}|{subject}|{hash(txt)}"
                cal.events.add(ev)

            # Prüfungen
            if detect_exam_from_text(info_text):
                due = parse_due_date(info_text, base_day) or base_day
                key = (due.isoformat(), subject, "exam")
                if key in created_exam:
                    continue
                created_exam.add(key)
                ex_begin = tz.localize(datetime.combine(due, time(8, 0)))
                ex_end = ex_begin + timedelta(hours=2)
                ev = Event()
                ev.name = f"Prüfung: {subject}"
                ev.begin = ex_begin
                ev.end = ex_end
                ev.description = info_text
                ev.uid = f"EXAM|{due.isoformat()}|{subject}"
                cal.events.add(ev)

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
