"""Build a personal HSE study digest. Start with: python study.py demo"""
import argparse
import asyncio
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import lms_api
from study_agent import authenticate, classify_demo, extract
from study_report import csv_text, deliver, render, write
from study_sources import MOSCOW, from_text, telegram, two_months_before

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"
OUT = ROOT / "out"
SETTINGS = {"provider": "codex", "allow_cloud": False, "email_enabled": False,
            "chats": {}, "lms_calendar": "", "lms_api_courses": {}}
COURSES = {"Анализ данных": ["анализ данных", "data analysis"]}


def read(path):
    if not path.is_file():
        raise ValueError(f"Нет файла {path.name}. Сначала выполните python study.py setup.")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Проверьте UTF-8 и синтаксис JSON в {path.name}.") from e


def setup():
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in [("settings", SETTINGS), ("courses", COURSES), ("lms", [])]:
        path = PRIVATE / (name + ".json")
        try:
            with path.open("x", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except FileExistsError:
            pass
    print(f"Настройки и личные записи: {PRIVATE}")


def demo():
    sample = read(ROOT / "example.json")
    now = datetime.fromisoformat(sample["date"])
    rows = []
    for note in sample["notes"]:
        rows.extend(from_text(note["course"], note["text"], f"Демо · заметка {note['id']}", ""))
    for group in sample["dialogs"][:300]:
        if group["kind"] != "group" or not group["approved"]:
            continue
        for msg in group["messages"]:
            if two_months_before(now) <= datetime.fromisoformat(msg["date"]) <= now:
                source = f"Демо · группа {group['id']} · №{msg['id']}"
                rows.extend(from_text(group["course"], msg["text"], source, ""))
    render(rows, OUT / "demo", True, now)


def config():
    settings = read(PRIVATE / "settings.json")
    courses = read(PRIVATE / "courses.json")
    if not isinstance(settings, dict) or not isinstance(courses, dict) or not courses:
        raise ValueError("Проверьте settings.json и заполните courses.json по образцу.")
    for course, aliases in courses.items():
        if (not isinstance(course, str) or not course.strip() or not isinstance(aliases, list)
            or any(not isinstance(x, str) or not x.strip() for x in aliases)):
            raise ValueError("Для каждого предмета укажите список сокращений или [].")
    if (settings.get("provider") not in {"codex", "claude"} or not isinstance(settings.get("chats"), dict)
        or type(settings.get("allow_cloud")) is not bool or type(settings.get("email_enabled")) is not bool):
        raise ValueError("Неверные поля settings.json. Сверьтесь с примером в README.")
    if not isinstance(settings.get("lms_calendar", ""), str):
        raise ValueError("В lms_calendar ожидается путь, адрес или пустая строка.")
    if not isinstance(settings.get("lms_api_courses", {}), dict):
        raise ValueError("В lms_api_courses ожидаются ID курсов и названия из ИУП, либо {}.")
    return settings, courses


def own_notes(courses):
    notes = read(PRIVATE / "lms.json")
    if not isinstance(notes, list):
        raise ValueError("lms.json должен содержать список записей.")
    ids = set()
    for note in notes:
        if not isinstance(note, dict) or any(not isinstance(note.get(k), str) for k in ("course", "text", "id", "url")):
            raise ValueError("В каждой заметке нужны текстовые поля course, text, id, url.")
        if note["course"] not in courses or not note["id"] or note["id"] in ids or not 0 < len(note["text"]) <= 8000:
            raise ValueError("Проверьте предмет, уникальный непустой id и текст заметки (1–8000 знаков).")
        ids.add(note["id"])
    return notes


def calendar_rows(location, courses):
    from lms_calendar import read_tasks
    if not re.match(r"^https?://", location, re.I):
        location = str((ROOT / location).resolve())
    rows = read_tasks(location, courses)
    for row in rows:
        due = row["due"]
        if row["course"] not in courses or not row["task"]:
            raise ValueError("В календарной записи неизвестный предмет или пустая задача.")
        if due:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", due):
                raise ValueError("Формат срока в календарной записи не распознан.")
            datetime.fromisoformat(due)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for cmd in ("demo", "setup", "login", "discover", "run", "send"):
        subs.add_parser(cmd)
    for cmd in ("auth", "probe", "classify-demo"):
        subs.add_parser(cmd).add_argument("provider", choices=["codex", "claude"])
    args = parser.parse_args()
    cmd = args.command
    if cmd == "demo":
        return demo()
    if cmd == "setup":
        return setup()
    if cmd == "auth":
        return authenticate(args.provider, PRIVATE)
    if cmd == "classify-demo":
        return classify_demo(args.provider, PRIVATE)
    if cmd == "probe":
        sample = read(ROOT / "example.json")["notes"][:1]
        rows = extract(args.provider, PRIVATE, sample)
        if not rows or not any(row["due"] == "2026-11-12 23:59" for row in rows):
            raise ValueError("Тест не пройден: модель не вернула проверяемое задание с ожидаемым сроком.")
        print("Тест пройден. Модель выделила поручение и сохранила точный срок из вымышленной записи.")
        return

    settings, courses = config()
    now = datetime.now(MOSCOW)
    if cmd in ("login", "discover"):
        result = asyncio.run(telegram(cmd, PRIVATE, courses, {}, now))
        if cmd == "discover":
            path = PRIVATE / "candidates.csv"
            write(path, csv_text([["chat_id", "Название", "Последнее сообщение (МСК)", "Возможный предмет"], *result]), "utf-8-sig")
            print(f"Активных групп и каналов: {len(result)}. Список: {path}")
        return
    if cmd == "send" and not settings["email_enabled"]:
        raise ValueError("Отправка выключена. Включите email_enabled после проверки локальной таблицы.")
    notes = own_notes(courses)
    chats = settings["chats"]
    location = settings.get("lms_calendar", "")
    course_ids = settings.get("lms_api_courses", {})
    if not (notes or chats or location or course_ids):
        raise ValueError("Источники не заданы. Добавьте свою заметку, календарь или подтверждённый чат.")
    rows = asyncio.run(telegram("run", PRIVATE, courses, chats, now)) if chats else []
    if location:
        rows.extend(calendar_rows(location, courses))
    if course_ids:
        rows.extend(lms_api.read_tasks(course_ids, courses, now))
    if settings["allow_cloud"]:
        rows.extend(extract(settings["provider"], PRIVATE, notes))
    else:
        for note in notes:
            rows.extend(from_text(note["course"], note["text"], f"Личная заметка · {note['id']}", note["url"]))
    html = render(rows, OUT / "live", False, now)
    if cmd == "send":
        deliver(html, PRIVATE, now)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        # Third-party library errors can contain URLs, account details or source text.
        message = str(error) if type(error) is ValueError else type(error).__name__ + " — проверьте настройки и доступ."
        print("Сбор остановлен. " + message, file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"Сбор остановлен ({type(error).__name__}). Проверьте доступ к источнику.", file=sys.stderr)
        sys.exit(1)
