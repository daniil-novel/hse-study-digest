"""Local, read-only Telegram adapter and deterministic date extraction."""
import calendar
import os
import re
from datetime import date, timedelta, timezone
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3), "МСК")
ACTION = re.compile(
    r"\b(сда(ть|йте|ча|ём|ем|чи)|сда[вёе]м|сдавать|сдаётся|"
    r"пришлите|принима[юе]|отправ(ить|ьте)|загруз(ить|ите)|"
    r"представ(ить|ьте)|подготов(ить|ьте)|выполн(ить|ите)|"
    r"написать|дедлайн\w*|срок\s+сдачи|вылож(?:ить|ите)|прикреп(?:ить|ите)|защит[ау]|"
    r"присылайте|deadline|submit|due|домашн\w*\s+задани\w*|дз|контрольн\w*|экзамен\w*)\b", re.I)
DATE = re.compile(r"(?<!\d)(?:(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})\.(\d{1,2})\.(\d{4}))(?!\d)(?:[, ]+(?:в\s+)?([01]?\d|2[0-3]):([0-5]\d))?")
UNCERTAIN = re.compile(r"перен[её]с|перенос|отмен|продл|вместо|уточн|возможно|предварительн|ориентировочн|под вопросом", re.I)


def two_months_before(now):
    """Subtract two calendar months, clipping e.g. April 30 to February 28."""
    index = now.year * 12 + now.month - 3
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def exact_due(text):
    matches = list(DATE.finditer(text))
    if len(matches) != 1 or UNCERTAIN.search(text):
        return ""
    hit = matches[0]
    if not re.search(r"(?:\bдо|\bк|\bсрок(?:\s+сдачи)?|\bдедлайн|\bdeadline|\bdue(?:\s+by)?)\s*[:—–-]?\s*$",
                     text[:hit.start()], re.I):
        return ""
    if re.search(r"\b(?:UTC|GMT|CET|EST|по местному|петербургскому|пермскому)\b", text, re.I):
        return ""
    if re.match(r"[, ]+(?:в\s+)?\d{1,2}:\d|:\d", text[hit.end():]):
        return ""
    y, m, d, d2, m2, y2, hh, mm = matches[0].groups()
    try:
        result = date(int(y or y2), int(m or m2), int(d or d2)).isoformat()
    except ValueError:
        return ""
    return result + (f" {int(hh):02}:{mm}" if hh else "")


def from_text(course, text, source, url):
    """Take each action-bearing line as a candidate. Ambiguity is visible."""
    rows = []
    for line in text.splitlines():
        if ACTION.search(line) or UNCERTAIN.search(line):
            rows.append(dict(course=course, task=line.strip(), due=exact_due(line),
                             source=source, url=url))
    return rows


def aliases_for(title, courses):
    normalized = " ".join(title.casefold().split())
    hits = []
    for course, aliases in courses.items():
        for phrase in [course, *aliases]:
            if re.search(r"(?<!\w)" + re.escape(" ".join(phrase.casefold().split())) + r"(?!\w)", normalized):
                hits.append(course)
                break
    return hits


async def telegram(command, private: Path, courses, chats, now):
    try:
        from telethon import TelegramClient
    except ImportError:
        raise ValueError("Установите Telegram-модуль: python -m pip install -r requirements.txt") from None
    if not os.getenv("TG_API_ID", "").isdigit() or not os.getenv("TG_API_HASH"):
        raise ValueError("Задайте TG_API_ID и TG_API_HASH. См. README.")
    if command != "login" and not (private / "student.session").is_file():
        raise ValueError("Сначала выполните python study.py login.")
    client = TelegramClient(str(private / "student"), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    cutoff = two_months_before(now)
    try:
        if command == "login":
            await client.start()
            print("Вход в Telegram подтверждён.")
            return []
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Сессия истекла. Выполните python study.py login.")
        if command == "discover":
            candidates = []
            async for dialog in client.iter_dialogs(limit=300, ignore_migrated=True):
                if dialog.is_user or not (dialog.is_group or dialog.is_channel):
                    continue
                if dialog.date and cutoff <= dialog.date <= now:
                    matches = aliases_for(dialog.name, courses)
                    candidates.append([str(dialog.id), dialog.name,
                                       dialog.date.astimezone(MOSCOW).date().isoformat(), " / ".join(matches)])
            return candidates

        rows = []
        for chat_id, course in chats.items():
            if not re.fullmatch(r"-[1-9]\d*", chat_id) or course not in courses:
                raise ValueError("В chats нужен отрицательный ID группы и точное название курса из courses.json.")
            group = await client.get_entity(int(chat_id))
            async for message in client.iter_messages(group):
                if message.date < cutoff:
                    break
                if message.date > now or not message.raw_text:
                    continue
                username = getattr(group, "username", None)
                if username:
                    url = f"https://t.me/{username}/{message.id}"
                elif chat_id.startswith("-100"):
                    url = f"https://t.me/c/{chat_id[4:]}/{message.id}"
                else:
                    url = ""
                source = f"Telegram · {getattr(group, 'title', chat_id)} · №{message.id}"
                rows.extend(from_text(course, message.raw_text, source, url))
        return rows
    finally:
        await client.disconnect()
