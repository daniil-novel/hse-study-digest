"""Read explicit deadlines from an optional Moodle calendar, entirely outside the LLM."""

import hashlib
import re
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


ROOT = Path(__file__).resolve().parent
MOSCOW = timezone(timedelta(hours=3))
MAX_SIZE = 5_000_000
DUE = re.compile(r"\b(deadline|is due|due date|дедлайн|срок сдачи|крайний срок|последний срок|завершение при[её]ма)\b", re.I)
NOT_DUE = re.compile(r"\b(no deadline|no due date|дедлайна нет|без срока|обсуждени\w*|напоминани\w*|reminder|opens?)\b", re.I)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def calendar_bytes(location):
    """Fetch only the HSE export endpoint. OAuth or login redirects require manual export."""
    remote = "://" in location
    if remote:
        url = urlsplit(location)
        if (url.scheme != "https" or url.netloc.lower() != "edu.hse.ru"
                or url.path != "/calendar/export_execute.php"):
            raise ValueError("Нужна HTTPS-ссылка экспорта календаря с edu.hse.ru или локальный файл .ics.")
    try:
        if remote:
            stream = build_opener(NoRedirect()).open(location, timeout=20)
        else:
            stream = (ROOT / location).open("rb")
        with stream:
            data = stream.read(MAX_SIZE + 1)
    except (OSError, ValueError):
        # urllib errors can contain the full secret subscription URL.
        raise ValueError("Не удалось прочитать календарь. Проверьте файл или заново получите ссылку в Smart LMS.") from None
    data = data.removeprefix(b"\xef\xbb\xbf").lstrip()
    if len(data) > MAX_SIZE or not data.startswith(b"BEGIN:VCALENDAR"):
        raise ValueError("Ожидался файл iCalendar не более 5 МБ. Возможно, Smart LMS показала страницу входа.")
    return data


def card_url(raw):
    """Publish only the non-secret Moodle activity ID."""
    parts = urlsplit(str(raw))
    activity = re.fullmatch(r"/mod/(assign|quiz|workshop)/view\.php", parts.path)
    identifier = parse_qs(parts.query).get("id", [""])[0]
    if (parts.scheme == "https" and parts.netloc.lower() == "edu.hse.ru"
            and activity and identifier.isascii() and identifier.isdigit()):
        return f"https://edu.hse.ru{parts.path}?id={identifier}"
    return "https://edu.hse.ru/calendar/view.php"


def read_tasks(location: str, courses: dict[str, list[str]]) -> list[dict]:
    try:
        from icalendar import Calendar
    except ImportError:
        raise ValueError("Для календаря установите пакеты: python -m pip install -r requirements-calendar.txt") from None
    data = calendar_bytes(location)
    try:
        calendar = Calendar.from_ical(data)
    except ValueError:
        raise ValueError("Не удалось разобрать файл календаря .ics.") from None
    tasks = []
    skipped = 0
    for event in calendar.walk():
        if event.name not in {"VEVENT", "VTODO"} or str(event.get("STATUS", "")).upper() in {"CANCELLED", "COMPLETED"}:
            continue
        title = str(event.get("SUMMARY", "")).strip()
        field = "DUE" if event.name == "VTODO" else "DTSTART"
        if not title or (event.name == "VEVENT" and (not DUE.search(title) or NOT_DUE.search(title))):
            continue
        text = " ".join(str(event.get(k, "")) for k in ("SUMMARY", "DESCRIPTION"))
        categories = event.get("CATEGORIES", [])
        if not isinstance(categories, list):
            categories = [categories]
        for group in categories:
            text += " " + " ".join(str(token) for token in getattr(group, "cats", []))
        matches = [name for name, aliases in courses.items()
                   if any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.I)
                          for alias in [name, *aliases] if alias)]
        if len(matches) != 1 or "RRULE" in event:
            skipped += 1
            continue
        value = getattr(event.get(field), "dt", None)
        due = ""
        if isinstance(value, datetime) and value.tzinfo is not None:
            due = value.astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M")
        elif isinstance(value, date) and not isinstance(value, datetime):
            due = value.isoformat()
        uid = str(event.get("UID", "") or f"{matches[0]} {title} {due}")
        short_id = hashlib.sha256(uid.encode()).hexdigest()[:10]
        tasks.append(dict(course=matches[0], task=title, due=due, identity=f"ics/{uid}", source=f"Smart LMS · ICS {short_id}", url=card_url(event.get("URL", ""))))
    if skipped:
        warnings.warn(f"Календарь: пропущено событий со сроком: {skipped}. Проверьте псевдонимы курсов и повторяющиеся события.")
    return tasks
