"""Вспомогательные источники; прежнее имя CLI делегирует digest.py."""
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"


def read(path):
    if not path.is_file():
        raise ValueError(f"Нет файла {path.name}. Сначала выполните python digest.py init.")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Проверьте UTF-8 и синтаксис JSON в {path.name}.") from e

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

if __name__ == "__main__":
    from digest import main
    main()
