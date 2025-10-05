import os
import sys
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import pytz
import webuntis
from ics import Event


def get_env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and not value:
        print(f"Missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return value


def login_session():
    session = webuntis.Session(
        server=get_env("WEBUNTIS_SERVER", required=True),
        school=get_env("WEBUNTIS_SCHOOL", required=True),
        username=get_env("WEBUNTIS_USERNAME", required=True),
        password=get_env("WEBUNTIS_PASSWORD", required=True),
        useragent=get_env("WEBUNTIS_CLIENT", "untis-cal-sync"),
    )
    session.login()
    return session


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

    raise RuntimeError("Konnte keinen Scope bestimmen (kein get_current_user und keine IDs in .env).")


def determine_timerange(tz: pytz.BaseTzInfo):
    """Determine the timetable window based on environment overrides."""

    def _parse_days(var_name: str, default: Optional[int]) -> Optional[int]:
        raw = get_env(var_name, None)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Ungültiger Wert für {var_name}: {raw}") from exc
        if value < 0:
            raise ValueError(f"{var_name} darf nicht negativ sein: {raw}")
        return value

    days_back = _parse_days("UNTIS_DAYS_BACK", 0) or 0
    days_forward = _parse_days("UNTIS_DAYS_FORWARD", None)

    if days_forward is None:
        days_forward = _parse_days("UNTIS_LOOKAHEAD_DAYS", 14)

    today = datetime.now(tz).date()
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=days_forward)

    if start > end:
        raise ValueError("Startdatum liegt nach dem Enddatum. Prüfe deine UNTIS_* Konfiguration.")

    return start, end


def timerange_to_datetimes(
    start_date: date, end_date: date, tz: pytz.BaseTzInfo
) -> Tuple[datetime, datetime]:
    """Return timezone-aware datetimes covering the inclusive date range."""

    start_dt = tz.localize(datetime.combine(start_date, time.min))
    end_dt = tz.localize(datetime.combine(end_date, time.max))
    return start_dt, end_dt


def fetch_timetable(session, scope, start, end):
    kwargs = {"start": start, "end": end}
    if "studentId" in scope:
        kwargs["studentId"] = scope["studentId"]
    elif "teacherId" in scope:
        kwargs["teacherId"] = scope["teacherId"]
    elif "classId" in scope:
        kwargs["klasseId"] = scope["classId"]
    elif "personType" in scope and "personId" in scope:
        kwargs["personType"] = scope["personType"]
        kwargs["personId"] = scope["personId"]

    return session.timetable(**kwargs)


def _localize(dt: Optional[datetime], tz: pytz.BaseTzInfo) -> Optional[datetime]:
    if dt is None:
        return None

    if dt.tzinfo:
        return dt.astimezone(tz)

    return tz.localize(dt)


def extract_lesson_details(lesson, tz: pytz.BaseTzInfo) -> Dict[str, object]:
    """Return normalized data for a WebUntis lesson entry."""

    begin = _localize(getattr(lesson, "start", None), tz)
    end = _localize(getattr(lesson, "end", None), tz)

    if not begin or not end:
        raise ValueError("Unterrichtseinheit ohne Start/Ende erhalten.")

    subject = getattr(lesson, "subject", None)
    subject_name = (
        getattr(subject, "long_name", None)
        or getattr(subject, "name", None)
        or "Unterricht"
    )

    rooms = []
    for room in getattr(lesson, "rooms", []) or []:
        room_name = (
            getattr(room, "long_name", None)
            or getattr(room, "name", None)
        )
        if room_name:
            rooms.append(room_name)
    room_str = ", ".join(rooms)

    teacher_names: List[str] = []
    for teacher in getattr(lesson, "teachers", []) or []:
        name = (
            getattr(teacher, "long_name", None)
            or getattr(teacher, "name", None)
        )
        if name:
            teacher_names.append(name)
    teachers_str = ", ".join(teacher_names)

    notes: List[str] = []
    if teachers_str:
        notes.append(f"Lehrkraft: {teachers_str}")

    code = getattr(lesson, "code", None)
    if code:
        notes.append(f"Code: {code}")

    subst_text = getattr(lesson, "substText", None)
    if subst_text:
        notes.append(f"Hinweis: {subst_text}")

    cancelled = bool(
        getattr(lesson, "is_cancelled", False)
        or str(getattr(lesson, "code", "")).upper() in {"CANCELLED", "CANC"}
    )

    return {
        "begin": begin,
        "end": end,
        "subject": subject_name,
        "room": room_str,
        "teachers": teachers_str,
        "notes": notes,
        "cancelled": cancelled,
    }


def normalize_lessons(
    lessons: Iterable, tz: pytz.BaseTzInfo
) -> List[Dict[str, object]]:
    """Return sorted lesson detail dicts for deterministic processing."""

    details = [extract_lesson_details(lesson, tz) for lesson in lessons]
    details.sort(key=lambda d: (d["begin"], d["end"], d["subject"], d["room"]))
    return details


def _compose_title(details: Dict[str, object]) -> str:
    title = str(details["subject"])
    room = details.get("room")
    if room:
        title += f" · {room}"
    return title


def lesson_uid(details: Dict[str, object]) -> str:
    """Stable UID for a lesson based on time, subject, room and teacher."""

    return "|".join(
        [
            details["begin"].isoformat(),
            str(details["subject"]),
            str(details.get("room", "")),
            str(details.get("teachers", "")),
        ]
    )


def build_event(details: Dict[str, object]) -> Event:
    """Create an ICS event from normalized lesson details."""

    event = Event()
    event.name = _compose_title(details)
    event.begin = details["begin"]
    event.end = details["end"]
    event.location = details.get("room") or None

    notes = details.get("notes") or []
    if notes:
        event.description = "\n".join(notes)

    event.uid = lesson_uid(details)

    if details.get("cancelled"):
        event.status = "CANCELLED"

    return event
