"""Учебная Wiki: Telegram MCP, версии заданий, RAG и доставка. Начните с init."""

import argparse  # Превращает команды терминала в проверенные параметры.
import asyncio  # Запускает асинхронный клиент MCP из обычной команды Python.
import csv  # Читает сохранённые основания выбора учебных групп.
import os  # Даёт доступ к переменным, загруженным из локального .env.
import sqlite3  # Журнал доставки хранится отдельно от базы учебных задач.
import subprocess  # Запускает штатную авторизацию в терминале владельца аккаунта.
import sys  # Возвращает ОС понятный код успеха или ошибки.
import json  # Машиночитаемый ответ поиска пригоден и человеку, и скиллу.
from contextlib import closing
from datetime import datetime

from digest_config import ROOT, init, merge, registry, registry_path, settings
from digest_schedule import Schedule, launch_agent
from study_agent import authenticate, extract
from study_report import deliver, render
from study_sources import MOSCOW, from_text

PRIVATE = ROOT / "private"  # Закрытая папка исключена из Git и публикации.


def smtp_password(address):
    """Получить секрет, не передавая его модели и не записывая в отчёт."""
    if password := os.environ.get("DIGEST_SMTP_PASSWORD"):
        return password
    if sys.platform == "darwin":
        item = subprocess.run(
            ["security", "find-generic-password", "-s", "hse-study-digest.smtp", "-a", address, "-w"],
            capture_output=True,
            text=True,
        )
        if not item.returncode:
            return item.stdout.rstrip("\r\n")
    raise ValueError(
        "Заполните DIGEST_SMTP_PASSWORD в локальном .env."
    )


def already_sent(slot, mailbox):
    """Не повторять уже отправленный выпуск и остановиться при неизвестном исходе SMTP."""
    dbpath = PRIVATE / "mail.sqlite"
    if dbpath.is_file():
        with closing(sqlite3.connect(dbpath)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='mail' AND type='table'").fetchone():
                row = db.execute(
                    "SELECT state FROM mail WHERE slot=? AND mailbox=?", (slot, mailbox)
                ).fetchone()
                if row:
                    if row[0] == "sent":
                        return True
                    raise ValueError(
                        "Предыдущая попытка отправки не подтверждена. Проверьте свой ящик."
                    )
    return False


async def telegram_rows(cfg, mode, now):
    """Сначала обновить реестр источников, затем собрать задания разрешённым способом."""
    from telegram_agent import collect

    path = registry_path(cfg, PRIVATE)
    # Помимо выбранных ID читаем запреты и историю удалений: ручное решение имеет приоритет.
    tracked, ids, patterns, seen = registry(path)
    if cfg["agent"]["mode"] == "local":
        from local_digest import harvest

        titles = {}
        proposals, rows = await harvest(
            cfg, PRIVATE, discover_only=(mode == "discover"), now=now, titles=titles
        )
        merge(path, proposals, tracked, titles)
    else:
        # Группы, которые человек удалил из tracked, повторно не предлагаем.
        blocked = ids | (seen - tracked.keys())
        proposals, _ = await collect(
            "discover", cfg, PRIVATE, cfg["courses"], tracked, blocked, patterns
        )
        evidence = PRIVATE / (
            "discovery-evidence-demo.csv" if cfg["mcp"]["demo"] else "discovery-evidence.csv"
        )
        titles = {}
        if evidence.is_file():
            with evidence.open(encoding="utf-8-sig", newline="") as f:
                titles = {
                    entry["chat_id"]: entry["title"]
                    for entry in csv.DictReader(f, delimiter=";")
                    if entry.get("title")
                }
        merge(path, proposals, tracked, titles)
        tracked, ids, patterns, seen = registry(path)  # Учитываем реестр после текущего обнаружения.
        if mode == "discover":
            rows = []
        else:
            _, rows = await collect(
                "run", cfg, PRIVATE, cfg["courses"], tracked, ids | (seen - tracked.keys()), patterns
            )
    print(f"Реестр {path.name}: отслеживаемых чатов — {len(registry(path)[0])}.")
    return rows


def other_sources(cfg, now):
    """Объединить доступный календарь, REST Smart LMS и собственные записи студента."""
    import lms_api
    from study import calendar_rows, own_notes

    lms, courses = cfg["lms"], cfg["courses"]
    # Пустые настройки дополнительных источников означают, что запрос к ним не нужен.
    rows = calendar_rows(lms["calendar"], courses) if lms["calendar"] else []
    rows.extend(lms_api.read_tasks(lms["api_courses"], courses, now))
    if lms["use_notes"]:
        notes = own_notes(courses)
        if cfg["agent"]["mode"] == "cloud":
            rows.extend(extract(cfg["agent"]["provider"], PRIVATE, notes))
        else:
            for n in notes:
                rows.extend(from_text(n["course"], n["text"], f"Личная заметка · {n['id']}", n["url"]))
    return rows


def main():
    """Разобрать одну команду, выполнить её и завершить короткий рабочий процесс."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in (
        "init",
        "telegram-login",
        "doctor",
        "inventory",
        "discover",
        "run",
        "local",
        "send",
        "tick",
        "schedule",
        "install-schedule",
        "remove-schedule",
        "wiki",
        "install-service",
        "remove-service",
        "status",
    ):
        sub.add_parser(cmd)
    sub.add_parser("auth").add_argument("provider", choices=["codex", "claude"])
    # Поиск читает локальную базу; ask дополнительно вызывает выбранную модель.
    sub.add_parser("search").add_argument("query")
    ask_parser = sub.add_parser("ask", help="Отправить вопрос и найденные карточки провайдеру подписки")
    ask_parser.add_argument("query")
    ask_parser.add_argument("--provider", choices=["codex", "claude"])
    sub.add_parser("install-skills").add_argument("provider", choices=["codex", "claude"])
    sub.add_parser("service").add_argument("--once", action="store_true")
    for name in ("done", "reopen"):
        sub.add_parser(name).add_argument("task_id")
    args = parser.parse_args()  # Неизвестные команды останавливаются до доступа к аккаунтам.
    cmd = args.cmd
    if cmd == "init":
        return init(PRIVATE)
    from agent_code.runtime import load_environment

    if cmd == "install-skills":
        from agent_code.skills import install

        return install(args.provider)
    if cmd in {"service", "install-service", "remove-service", "status"}:
        from agent_code import service

        if cmd == "service":
            result = service.run(ROOT, once=args.once)
            if args.once and result.get("last_scan", {}).get("result") != "ok":
                raise ValueError("Сбор не завершён. Проверьте status и выполните run для диагностики.")
            return
        result = {"install-service": service.install, "remove-service": service.remove,
                  "status": service.status}[cmd](ROOT)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    # Telegram-поля читает шлюз из выбранного файла; ROOT/.env не должен изображать переменные ОС.
    load_environment(ROOT, include_telegram=False)
    if cmd in {"search", "wiki", "done", "reopen"}:
        from agent_code import knowledge

        if cmd == "search":
            print(json.dumps(knowledge.search(PRIVATE, args.query), ensure_ascii=False, indent=2))
        else:
            from filelock import FileLock, Timeout

            PRIVATE.mkdir(parents=True, exist_ok=True)
            try:
                # Один замок для сбора и ручной отметки не даёт затереть свежее представление.
                with FileLock(str(PRIVATE / "digest.lock"), timeout=0):
                    if cmd == "wiki":
                        for path in knowledge.export_wiki(PRIVATE):
                            print(path)
                    else:
                        knowledge.set_done(PRIVATE, args.task_id, done=cmd == "done")
                        knowledge.export_wiki(PRIVATE)
                        # Отметку сразу видно в HTML/CSV, без повторного обращения к Telegram.
                        render(knowledge.current_rows(PRIVATE), ROOT / "out/live", False, datetime.now(MOSCOW))
            except Timeout:
                raise ValueError("Сбор ещё работает. Повторите изменение отметки после его завершения.") from None
        return
    if cmd == "remove-schedule":
        return launch_agent(ROOT, remove=True)
    if cmd == "auth":
        return authenticate(args.provider, PRIVATE)
    cfg = settings(PRIVATE)  # Проверяем TOML и подставляем почтовые параметры из окружения.
    if cmd == "ask":
        from agent_code.knowledge import answer

        result = answer(PRIVATE, args.query, args.provider or cfg["agent"]["provider"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if cmd == "telegram-login":
        from telegram_stdio import authorize

        return authorize(cfg, PRIVATE)  # Штатная сессия повторно используется, новый код вводится только владельцем.
    if cmd == "local":
        cfg["agent"]["mode"] = "local"
    now = datetime.now(MOSCOW)  # Один момент времени связывает сбор, отчёт и почтовый выпуск.
    schedule = Schedule(cfg["schedule"])
    if cmd == "schedule":
        dates = schedule.upcoming(now)
        for dt in dates:
            print(dt.strftime("%Y-%m-%d %H:%M %Z"))
        if not dates:
            print("Нет запусков на ближайшие два года. Проверьте расписание.")
        return
    mail = cfg["mail"]
    demo = cfg["mcp"]["demo"]
    if cmd in {"tick", "send", "install-schedule"} and (demo or not mail["enabled"]):
        if cmd == "tick":
            return
        raise ValueError("Для доставки нужны mcp.demo=false и mail.enabled=true.")
    if cmd == "install-schedule":
        if not mail["address"] or not mail["host"]:
            raise ValueError("Укажите почтовый адрес и SMTP-сервер в [mail].")
        return launch_agent(ROOT)
    slot = None  # Без почтового слота команда только обновляет локальные результаты.
    if cmd in {"send", "tick"}:
        instant = schedule.due(now)
        if cmd == "tick" and not instant:
            return
        # An intentional manual send outside a scheduled slot has its own once-per-day key.
        slot = (
            instant.isoformat()
            if instant
            else "manual:" + now.astimezone(schedule.zone).date().isoformat()
        )
        if already_sent(slot, mail["address"]):
            if cmd == "send":
                print("Этот выпуск уже принят сервером почты.")
            return
    from filelock import FileLock, Timeout

    try:
        # Serialize two collections and their report files. launchd retries a busy tick later.
        with FileLock(str(PRIVATE / "digest.lock"), timeout=0):
            from telegram_agent import doctor, inventory

            if cmd in {"doctor", "inventory", "discover", "local"} and not cfg["mcp"]["enabled"]:
                raise ValueError("Telegram отключён в [mcp].enabled.")
            if cmd == "doctor":
                return asyncio.run(doctor(cfg, PRIVATE))
            if cmd == "inventory":
                asyncio.run(inventory(cfg, PRIVATE))
                return
            if slot and already_sent(slot, mail["address"]):
                return
            pw = smtp_password(mail["address"]) if slot else None  # Проверяем секрет до дорогого сбора.
            rows = asyncio.run(telegram_rows(cfg, cmd, now)) if cfg["mcp"]["enabled"] else []
            if cmd == "discover":
                return
            if not demo:
                if not cfg["mcp"]["enabled"] and not any(cfg["lms"].values()):
                    raise ValueError("Включите Telegram или выберите источник Smart LMS.")
                rows.extend(other_sources(cfg, now))
            output = ROOT / "out" / ("mcp-demo" if demo else "live")
            # Память хранится отдельно от процесса и переживает его завершение.
            from agent_code.knowledge import update, export_wiki, current_rows

            stats = update(rows, PRIVATE, now)
            export_wiki(PRIVATE)  # Markdown-страницы строятся из проверенных записей базы.
            print("База знаний:", json.dumps(stats, ensure_ascii=False))
            rows = current_rows(PRIVATE)  # Сохраняем известные задания даже вне текущего окна чтения.
            html = render(rows, output, demo, now)
            if slot:
                deliver(
                    html,
                    PRIVATE,
                    now,
                    slot=slot,
                    mailbox=mail["address"],
                    host=mail["host"],
                    port=mail.get("port", 465),
                    password=pw,
                    attachment=(output / "digest.csv").read_bytes(),
                )
    except Timeout:
        if cmd != "tick":
            raise ValueError("Другой запуск уже собирает сводку.") from None


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        sys.exit(str(error))
    except ModuleNotFoundError:
        sys.exit("Не установлены компоненты. Выполните python -m pip install -r requirements-mcp.txt.")
    except ExceptionGroup as error:
        # anyio wraps errors from a stdio session in task groups. Expose only our fixed instructions.
        auth_hint = "Вход в Telegram истёк или отсутствует. Запустите python digest.py telegram-login в своём терминале."
        pending = list(error.exceptions)
        while pending:
            item = pending.pop()
            if isinstance(item, BaseExceptionGroup):
                pending.extend(item.exceptions)
            elif isinstance(item, ValueError) and str(item) == auth_hint:
                sys.exit(auth_hint)
        sys.exit(
            "Сбой чтения Telegram MCP. Проверьте сессию, ограничения доступа и настройки. Отчёт не отправлен."
        )
    except Exception as error:
        # MCP/SMTP exceptions may contain source bodies, secrets or addresses. Keep errors local and short.
        sys.exit(f"Сбор остановлен ({type(error).__name__}). Проверьте MCP, настройки и авторизацию.")
