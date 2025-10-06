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
    # Fallback (klassischer Login)
    try:
        me = session.get_current_user()
        if me and hasattr(me, "personType") and me.personType and me.personId:
            return {"personType": me.personType, "personId": me.personId}
    except Exception:
        pass
    raise RuntimeError("Konnte keinen Scope bestimmen (ENV-IDs setzen).")

def fetch_timetable(session, scope, start, end):
    # webuntis 0.1.x erwartet student/klasse/teacher als Keyword
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

# ==================== RESOLVER ====================
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

# ==================== HA/PRÜFUNG AUS TEXT ====================
EXAM_KEYWORDS = ["prüfung", "klausur", "ex", "exam", "arbeit", "test", "leistungskontrolle"]
HOMEWORK_LINE = re.compile(r'\b(hausaufgabe|ha\b|aufgabe|vokabeln|übung|uebung)\b[:\-]?\s*(.*)', re.IGNORECASE)

# deutsche Wochentage → 0=Montag ... 6=Sonntag
WEEKDAYS_DE = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6
}

DATE_DDMMYYYY = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DATE_DDMM = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")  # ohne Jahr

def parse_due_date(text, base_day: date):
    """Versucht, ein Fälligkeitsdatum aus Text zu erkennen.
       Unterstützt: dd.mm.yyyy, dd.mm., 'heute', 'morgen', Wochentage."""
    if not text:
        return None
    t = text.lower()

    # heute / morgen
    if "heute" in t:
        return base_day
    if "morgen" in t:
        return base_day + timedelta(days=1)

    # Wochentage (nächster Vorkommnis)
    for w, idx in WEEKDAYS_DE.items():
        if w in t:
            delta = (idx - base_day.weekday()) % 7
            delta = 7 if delta == 0 else delta
            return base_day + timedelta(days=delta)

    # dd.mm.yyyy
    m = DATE_DDMMYYYY.search(t)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mth, d)
        except Exception:
            pass

    # dd.mm. (Jahr = dieses oder nächstes bei zurückliegend)
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
    """Liest HA-Zeilen; gibt Liste von (text, due_date|None) zurück."""
    results = []
    if not info_text:
        return results
    lines = info_text.splitlines()
    for line in lines:
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

# ==================== BLOXER: SCHULZEITEN ====================
def merge_into_blocks(intervals):
    """intervals: Liste (begin, end) sortiert. Merged in zusammenhängende Blöcke."""
    if not intervals:
        return []
    intervals.sort(key=lambda x: x[0])
    blocks = [list(intervals[0])]
    for b, e in intervals[1:]:
        last_b, last_e = blocks[-1]
        # zusammenführen nur, wenn direkt aneinander (keine Lücke)
        if b == last_e:
            blocks[-1][1] = e
        else:
            blocks.append([b, e])
    return [(b, e) for b, e in blocks]

# ==================== MAIN ====================
def main():
    load_dotenv()
    tzname = get_env("TIMEZONE", "Europe/Berlin")
    tz = pytz.timezone(tzname)

    out_path = get_env("ICS_OUTPUT_PATH", "./docs/untis.ics")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Uhrzeit für Hausaufgaben-Termine
    HW_TIME = time(17, 0)  # 17:00

    session = login_session()
    try:
        start = datetime.now(tz).date()
        end = (datetime.now(tz) + timedelta(days=14)).date()

        scope = pick_scope(session)
        lessons = fetch_timetable(session, scope, start, end)

        cal = Calendar()
        seen_uids = set()

        # 1) Daten nach Tag einsortieren, nur nicht-cancelled Stunden
        by_day = {}  # date -> list of lessons
        for l in lessons:
            # Zeiten
            try:
                begin = tz.localize(l.start)
                finish = tz.localize(l.end)
            except Exception:
                begin = getattr(l, "start", None)
                finish = getattr(l, "end", None)
            if not begin or not finish:
                continue

            # Entfallte Stunden rausnehmen (sie sollen Lücken erzeugen)
            code = (getattr(l, "code", None) or "").lower()
            is_cancel = getattr(l, "is_cancelled", False) or code in {"cancelled", "canc", "absent"}
            if is_cancel:
                # Wir speichern sie NICHT als Unterricht, damit dadurch eine Lücke entsteht
                # ABER: Info/HA/Prüfung kann trotzdem aus Text gelesen werden (weiter unten)
                pass
            else:
                day_key = begin.date()
                by_day.setdefault(day_key, []).append(l)

        # 2) Für jeden Tag zusammenhängende "Schule"-Blöcke bilden
        for day, ls in sorted(by_day.items()):
            intervals = []
            for l in ls:
                try:
                    b = tz.localize(l.start)
                    e = tz.localize(l.end)
                except Exception:
                    b = getattr(l, "start", None)
                    e = getattr(l, "end", None)
                if not b or not e:
                    continue
                intervals.append((b, e))
            if not intervals:
                continue

            blocks = merge_into_blocks(intervals)
            # Pro Block ein Event "Schule"
            for b, e in blocks:
                eobj = Event()
                eobj.name = "Schule"
                eobj.begin = b
                eobj.end = e
                # UID: Tag + Blockzeiten
                uid = f"BLOCK|{b.isoformat()}|{e.isoformat()}"
                eobj.uid = uid
                if uid not in seen_uids:
                    seen_uids.add(uid)
                    cal.events.add(eobj)

        # 3) Hausaufgaben & Prüfungen als separate Termine
        #    - Wir iterieren alle Stunden (auch entfallene), lesen Info-Text,
        #      finden HA-Zeilen und versuchen Fälligkeitsdatum zu parsen (sonst skip).
        #    - Prüfungen: eigener Termin am Prüfungstag (wenn Datum erkennbar),
        #      ansonsten überlassen wir es dem Schul-Block (oder du gibst mir Bescheid).
        created_hw_keys = set()  # dedupe: (due_date, subject_name, text)
        created_exam_keys = set()  # (due_date, subject_name, snippet)

        for l in lessons:
            # Zeiten
            try:
                begin = tz.localize(l.start)
                finish = tz.localize(l.end)
            except Exception:
                begin = getattr(l, "start", None)
                finish = getattr(l, "end", None)
            if not begin or not finish:
                continue
            base_day = begin.date()

            # Subject-Name (für Titel)
            subjects = resolve_subject_names(session, l)
            subject_name = subjects[0] if subjects else "Fach"

            # Info-Text
            info_text = extract_info_text(l)

            # --- HA ---
            ha_items = find_homework_items(info_text)
            for txt in ha_items:
                due = parse_due_date(txt, base_day) or parse_due_date(info_text, base_day)
                if not due:
                    # Fälligkeit nicht erkennbar -> skip (kein falsches Datum erzeugen)
                    continue
                key = (due.isoformat(), subject_name, txt)
                if key in created_hw_keys:
                    continue
                created_hw_keys.add(key)

                hw_begin = tz.localize(datetime.combine(due, HW_TIME))
                hw_end = hw_begin + timedelta(minutes=30)

                ev = Event()
                ev.name = f"{subject_name} – Hausaufgabe"
                ev.begin = hw_begin
                ev.end = hw_end
                ev.description = txt
                ev.uid = f"HW|{due.isoformat()}|{subject_name}|{hash(txt)}"
                cal.events.add(ev)

            # --- Prüfung ---
            is_exam = detect_exam_from_text(info_text)
            if is_exam:
                # versuche Datum zu erkennen; wenn keines erkennbar, setze auf base_day
                due = parse_due_date(info_text, base_day) or base_day
                key = (due.isoformat(), subject_name, "exam")
                if key in created_exam_keys:
                    continue
                created_exam_keys.add(key)

                ex_begin = tz.localize(datetime.combine(due, time(8, 0)))
                ex_end = ex_begin + timedelta(hours=2)

                ev = Event()
                ev.name = f"Prüfung: {subject_name}"
                ev.begin = ex_begin
                ev.end = ex_end
                # optional: Info-Text in Beschreibung
                if info_text:
                    ev.description = info_text
                ev.uid = f"EXAM|{due.isoformat()}|{subject_name}"
                cal.events.add(ev)

        # 4) Schreiben
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
