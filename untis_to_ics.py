import os
import sys
import re
from datetime import datetime, timedelta, time, date

import pytz
import webuntis
from ics import Calendar, Event
from dotenv import load_dotenv

# ==================== ENV ====================
def get_env(name: str, default=None, required: bool = False):
    v = os.getenv(name, default)
    if required and not v:
        print(f"Missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return v

# ==================== Login & Timetable ====================
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
    if sid: return {"studentId": int(sid)}
    if tid: return {"teacherId": int(tid)}
    if cid: return {"classId": int(cid)}
    try:
        me = session.get_current_user()
        if me and getattr(me, "personType", None) and getattr(me, "personId", None):
            return {"personType": me.personType, "personId": me.personId}
    except Exception:
        pass
    raise RuntimeError("Konnte keinen Scope bestimmen (ENV-IDs setzen).")

def fetch_timetable(session, scope, start, end):
    kw = {"start": start, "end": end}
    if "studentId" in scope: kw["student"] = int(scope["studentId"])
    elif "teacherId" in scope: kw["teacher"] = int(scope["teacherId"])
    elif "classId" in scope: kw["klasse"] = int(scope["classId"])
    elif "personType" in scope and "personId" in scope:
        pt, pid = int(scope["personType"]), int(scope["personId"])
        if pt == 5: kw["student"] = pid
        elif pt == 2: kw["teacher"] = pid
        elif pt == 1: kw["klasse"] = pid
    else:
        raise RuntimeError("Unbekannter Scope für timetable().")
    return session.timetable(**kw)

# ==================== HA-/Prüfungs-Erkennung (Text) ====================
EXAM_KEYWORDS = ["prüfung", "klausur", "arbeit", "test", "leistungskontrolle", "ex", "exam", "ka", "lk"]
HOMEWORK_HINTS = ["hausaufgabe", "ha", "aufgabe", "vokabel", "übung", "uebung", "arbeitsblatt", "abgabe"]

WEEKDAYS_DE = {"montag":0,"dienstag":1,"mittwoch":2,"donnerstag":3,"freitag":4,"samstag":5,"sonntag":6}
DATE_DDMMYYYY = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DATE_DDMM      = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")

def extract_info_text(lesson) -> str:
    parts = []
    for key in ("substText","info"):
        v = getattr(lesson, key, None)
        if isinstance(v, str) and v.strip(): parts.append(v.strip())
    raw = getattr(lesson, "_data", {}) or {}
    for key in ("txt","lsnote","notice"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip(): parts.append(v.strip())
    return "\n".join(parts)

def parse_due_date(text: str, base_day: date):
    if not text: return None
    t = text.lower()
    if "heute" in t:  return base_day
    if "morgen" in t: return base_day + timedelta(days=1)
    for w, idx in WEEKDAYS_DE.items():
        if w in t:
            d = (idx - base_day.weekday()) % 7
            d = 7 if d == 0 else d
            return base_day + timedelta(days=d)
    m = DATE_DDMMYYYY.search(t)
    if m:
        d, mth, y = map(int, m.groups())
        try: return date(y, mth, d)
        except Exception: pass
    m = DATE_DDMM.search(t)
    if m:
        d, mth = map(int, m.groups())
        try:
            cand = date(base_day.year, mth, d)
            if cand < base_day: cand = date(base_day.year+1, mth, d)
            return cand
        except Exception: pass
    return None

def contains_homework(text: str) -> bool:
    if not text: return False
    low = text.lower()
    return any(h in low for h in HOMEWORK_HINTS) or "bis " in low

def detect_exam(text: str) -> bool:
    if not text: return False
    low = text.lower()
    return any(k in low for k in EXAM_KEYWORDS)

# ==================== Fachnamen ====================
def get_subject_names(session, lesson):
    names = []
    try:
        data = getattr(lesson, "_data", {}) or {}
        for x in data.get("su", []):
            sid = x.get("id")
            if not sid: continue
            try:
                subj = session.subjects().filter(id=sid)[0]
                nm = getattr(subj, "long_name", None) or getattr(subj, "name", None)
                if nm: names.append(nm)
            except Exception:
                continue
    except Exception:
        pass
    if not names:
        s = getattr(lesson, "subject", None)
        nm = getattr(s, "long_name", None) or getattr(s, "name", None)
        if nm: names.append(nm)
    return names

def next_subject_day(subject: str, lessons, session, tz, base_day: date):
    subj_low = subject.lower()
    best = None
    horizon = base_day + timedelta(days=21)
    for l in lessons:
        try: b = tz.localize(l.start)
        except Exception: b = getattr(l, "start", None)
        if not b: continue
        d = b.date()
        if not (base_day < d <= horizon): continue
        names = get_subject_names(session, l)
        if any(subj_low == n.lower() for n in names):
            if best is None or d < best: best = d
    return best or (base_day + timedelta(days=1))

# ==================== Hausaufgaben via Modul (wenn vorhanden) ====================
def fetch_homeworks(session, scope, start, end):
    """
    Versucht mehrere Varianten, je nach WebUntis/Library-Version:
    - session.homeworks(...)
    - session.get_homeworks(...)        (manche Versionen)
    - Fällt still zurück, wenn nicht verfügbar.
    Rückgabe: Liste Dicts mit keys: text, due(date), subjectIds
    """
    try:
        kw = {"start": start, "end": end}
        if "studentId" in scope: kw["student"] = int(scope["studentId"])
        elif "personType" in scope and int(scope["personType"]) == 5:
            kw["student"] = int(scope["personId"])

        # Variante A
        if hasattr(session, "homeworks"):
            res = session.homeworks(**kw)
            if res: return res
        # Variante B
        if hasattr(session, "get_homeworks"):
            res = session.get_homeworks(**kw)
            if res: return res
    except Exception:
        pass
    return []

def subjects_lookup(session):
    d = {}
    try:
        for s in session.subjects():
            sid = getattr(s, "id", None)
            if sid:
                d[int(sid)] = getattr(s, "long_name", None) or getattr(s, "name", None)
    except Exception:
        pass
    return d

# ==================== Blöcke (Pausen ≤ 20 zusammenfassen) ====================
def merge_into_blocks(intervals, max_gap_min=20):
    if not intervals: return []
    intervals = sorted(intervals, key=lambda x: x[0])
    max_gap = timedelta(minutes=max_gap_min)
    merged = [list(intervals[0])]
    for b, e in intervals[1:]:
        last_b, last_e = merged[-1]
        if b - last_e <= max_gap:
            if e > last_e: merged[-1][1] = e
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
        end   = (datetime.now(tz) + timedelta(days=21)).date()  # 3 Wochen -> HA-Fallback möglich
        scope = pick_scope(session)
        lessons = fetch_timetable(session, scope, start, end)

        cal = Calendar()
        seen_uids = set()

        # ---------- 1) Unterricht einsammeln (nicht-cancelled) ----------
        by_day = {}
        for l in lessons:
            try:
                begin = tz.localize(l.start); finish = tz.localize(l.end)
            except Exception:
                begin = getattr(l, "start", None); finish = getattr(l, "end", None)
            if not begin or not finish: continue
            code = (getattr(l, "code", None) or "").lower()
            is_cancel = getattr(l, "is_cancelled", False) or code in {"cancelled","canc","absent"}
            if is_cancel:  # Entfall erzeugt Lücke -> NICHT in Blöcke aufnehmen
                continue
            by_day.setdefault(begin.date(), []).append((begin, finish))

        # ---------- 2) Schulblöcke (Pausen ≤ 20 Min zusammenfassen) ----------
        for day, intervals in sorted(by_day.items()):
            blocks = merge_into_blocks(intervals, max_gap_min=20)
            for b, e in blocks:
                ev = Event()
                ev.name  = f"Schule {b.strftime('%H:%M')}–{e.strftime('%H:%M')}"
                ev.begin = b
                ev.end   = e
                b_utc = b.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
                e_utc = e.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
                uid = f"{b_utc}-{e_utc}@untis-merged"
                ev.uid = uid
                if uid in seen_uids: continue
                seen_uids.add(uid)
                cal.events.add(ev)

        # ---------- 3a) Hausaufgaben aus Modul (falls vorhanden) ----------
        subj_map = subjects_lookup(session)
        hw_entries = fetch_homeworks(session, scope, start, end)
        for hw in hw_entries:
            # Versuche Felder robust zu lesen
            txt = (hw.get("text") or hw.get("description") or "").strip()
            if not txt:
                continue
            # due / dueDate / date (verschiedene Varianten)
            due = None
            for k in ("due", "dueDate", "date"):
                v = hw.get(k)
                if isinstance(v, (date, datetime)):
                    due = v.date() if isinstance(v, datetime) else v
                    break
                if isinstance(v, str):
                    # 2025-10-08 oder 08.10.2025
                    try:
                        if "-" in v:
                            due = datetime.fromisoformat(v).date()
                        else:
                            m = DATE_DDMMYYYY.search(v)
                            if m:
                                d, mth, y = map(int, m.groups())
                                due = date(y, mth, d)
                    except Exception:
                        pass
            # Fachname
            subj = None
            for k in ("subjectIds", "subjectId", "subject"):
                if k in hw:
                    val = hw[k]
                    if isinstance(val, list) and val:
                        subj = subj_map.get(int(val[0])) or subj
                    elif isinstance(val, int):
                        subj = subj_map.get(int(val)) or subj
                    elif isinstance(val, str) and not subj:
                        subj = val
            if not subj: subj = "Fach"

            # Fallback Datum, falls nicht geliefert: nächster Unterrichtstag
            if not due:
                base = start
                due = next_subject_day(subj, lessons, session, tz, base)

            hw_begin = tz.localize(datetime.combine(due, HW_TIME))
            hw_end   = hw_begin + timedelta(minutes=30)
            ev = Event()
            ev.name = f"{subj} – Hausaufgabe"
            ev.begin = hw_begin
            ev.end   = hw_end
            ev.description = txt
            ev.uid = f"HW|{due.isoformat()}|{subj}|{abs(hash(txt))}"
            cal.events.add(ev)

        # ---------- 3b) HA/Prüfungen aus Stunden-Notizen (Fallback) ----------
        created_hw, created_exam = set(), set()
        for l in lessons:
            try: begin = tz.localize(l.start)
            except Exception: begin = getattr(l, "start", None)
            if not begin: continue
            base_day = begin.date()
            info = extract_info_text(l).strip()
            subjects = get_subject_names(session, l)
            subject = subjects[0] if subjects else "Fach"

            # Hausaufgabe
            if contains_homework(info):
                due = parse_due_date(info, base_day) or next_subject_day(subject, lessons, session, tz, base_day)
                key = (due.isoformat(), subject, info)
                if key not in created_hw:
                    created_hw.add(key)
                    hw_begin = tz.localize(datetime.combine(due, HW_TIME))
                    hw_end   = hw_begin + timedelta(minutes=30)
                    ev = Event()
                    ev.name = f"{subject} – Hausaufgabe"
                    ev.begin = hw_begin
                    ev.end   = hw_end
                    ev.description = info
                    ev.uid = f"HW|{due.isoformat()}|{subject}|{abs(hash(info))}"
                    cal.events.add(ev)

            # Prüfung
            if detect_exam(info):
                due = parse_due_date(info, base_day) or base_day
                key = (due.isoformat(), subject, "exam")
                if key not in created_exam:
                    created_exam.add(key)
                    ex_begin = tz.localize(datetime.combine(due, time(8, 0)))
                    ex_end   = ex_begin + timedelta(hours=2)
                    ev = Event()
                    ev.name = f"Prüfung: {subject}"
                    ev.begin = ex_begin
                    ev.end   = ex_end
                    ev.description = info
                    ev.uid = f"EXAM|{due.isoformat()}|{subject}"
                    cal.events.add(ev)

        # ---------- 4) Schreiben ----------
        with open(out_path, "w", encoding="utf-8") as f:
            f.writelines(cal.serialize_iter())
        print(f"ICS geschrieben: {out_path}")

    finally:
        try: session.logout()
        except Exception: pass

if __name__ == "__main__":
    main()
