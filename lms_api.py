"""Optional read-only Moodle REST adapter for a token issued by HSE."""
import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from lms_calendar import NoRedirect
from study_sources import MOSCOW

ENDPOINT = "https://edu.hse.ru/webservice/rest/server.php"
METHOD = "core_calendar_get_action_events_by_course"
PAGE_SIZE = 50
INVALID = "Smart LMS вернула неполные или несовместимые данные. Проверьте курс и доступ к методу API."


def fetch_page(token, course_id, start, end, cursor):
    form = urlencode({"wstoken": token, "wsfunction": METHOD, "moodlewsrestformat": "json",
                      "courseid": course_id, "timesortfrom": start, "timesortto": end,
                      "aftereventid": cursor, "limitnum": PAGE_SIZE}).encode("ascii")
    request = Request(ENDPOINT, data=form, method="POST", headers={"Accept": "application/json",
                      "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            if response.geturl() != ENDPOINT or response.status != 200:
                raise ValueError
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError
        page = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("Ошибка ответа Smart LMS. Проверьте сеть, токен и разрешение на чтение календаря.") from None
    if not isinstance(page, dict) or "exception" in page or "errorcode" in page or page.get("warnings"):
        # Do not expose the server's error string. It may echo request data.
        raise ValueError("Smart LMS не подтвердила доступ к курсу или методу календаря. Свяжитесь с администратором.")
    return page


def course_events(token, course_id, start, end):
    cursor = 0
    seen = set()
    for _ in range(100):
        page = fetch_page(token, course_id, start, end, cursor)
        events = page.get("events")
        if not isinstance(events, list) or len(events) > PAGE_SIZE:
            raise ValueError(INVALID)
        if not events:
            if page.get("lastid") not in (None, 0):
                raise ValueError(INVALID)
            return
        for event in events:
            if not isinstance(event, dict):
                raise ValueError(INVALID)
            identifier = event.get("id")
            if type(identifier) is not int or identifier < 1 or identifier in seen:
                raise ValueError(INVALID)
            seen.add(identifier)
            yield event
        cursor = page.get("lastid")
        if type(cursor) is not int or cursor != events[-1]["id"]:
            raise ValueError(INVALID)
    raise ValueError("Слишком много страниц ответа Smart LMS. Частичная сводка не сформирована.")


def read_tasks(course_ids: dict, courses: dict, now: datetime) -> list[dict]:
    if not isinstance(course_ids, dict) or any(
        not re.fullmatch(r"[1-9][0-9]*", key) or name not in courses for key, name in course_ids.items()
    ):
        raise ValueError("В lms_api_courses укажите числовые ID курсов и точные названия из courses.json.")
    if not course_ids:
        return []
    token = os.getenv("HSE_LMS_TOKEN", "").strip()
    if not token:
        raise ValueError("Для lms_api_courses нужен выданный ВШЭ HSE_LMS_TOKEN. Без доступа оставьте словарь пустым.")
    start, end = int(now.timestamp()), int((now + timedelta(days=90)).timestamp())
    tasks = []
    for course_id, name in course_ids.items():
        for event in course_events(token, int(course_id), start, end):
            if type(event.get("visible")) is not int or event["visible"] not in (0, 1):
                raise ValueError(INVALID)
            if not event["visible"]:
                continue
            if not isinstance(event.get("eventtype"), str):
                raise ValueError(INVALID)
            if event.get("modulename") != "assign" or event["eventtype"] != "due":
                continue
            course = event.get("course")
            if not isinstance(course, dict) or type(course.get("id")) is not int or course["id"] != int(course_id):
                raise ValueError(INVALID)
            module_id, stamp = event.get("instance"), event.get("timestart")
            title = event.get("activityname")
            if (type(stamp) is not int or type(module_id) is not int or module_id < 1
                or not start <= stamp <= end or not isinstance(title, str) or not title.strip()):
                raise ValueError(INVALID)
            tasks.append(dict(course=name, task=f"Сдать задание «{title.strip()}».",
                              identity=f"lms/{course_id}/{module_id}",
                              due=datetime.fromtimestamp(stamp, MOSCOW).strftime("%Y-%m-%d %H:%M"),
                              source=f"Smart LMS · API · №{event['id']}",
                              url=f"https://edu.hse.ru/mod/assign/view.php?id={module_id}"))
    return tasks
