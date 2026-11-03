"""Personal study digest on Telegram MCP. Run python digest.py init first."""

import argparse
import asyncio
import csv
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime

from digest_config import ROOT, init, merge, registry, registry_path, settings
from digest_schedule import Schedule, launch_agent
from study_agent import authenticate, extract
from study_report import deliver, render
from study_sources import MOSCOW, from_text

PRIVATE = ROOT / "private"


def smtp_password(address):
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
        "Добавьте пароль приложения в Связку ключей macOS или задайте DIGEST_SMTP_PASSWORD."
    )


def already_sent(slot, mailbox):
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
    from telegram_agent import collect

    path = registry_path(cfg, PRIVATE)
    tracked, ids, patterns, seen = registry(path)
    if cfg["agent"]["mode"] == "local":
        from local_digest import harvest

        titles = {}
        proposals, rows = await harvest(
            cfg, PRIVATE, discover_only=(mode == "discover"), now=now, titles=titles
        )
        merge(path, proposals, tracked, titles)
    else:
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
        tracked, ids, patterns, seen = registry(path)
        if mode == "discover":
            rows = []
        else:
            _, rows = await collect(
                "run", cfg, PRIVATE, cfg["courses"], tracked, ids | (seen - tracked.keys()), patterns
            )
    print(f"Реестр {path.name}: отслеживаемых чатов — {len(registry(path)[0])}.")
    return rows


def other_sources(cfg, now):
    import lms_api
    from study import calendar_rows, own_notes

    lms, courses = cfg["lms"], cfg["courses"]
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
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in (
        "init",
        "telegram-login",
        "plan-local",
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
    ):
        sub.add_parser(cmd)
    sub.add_parser("auth").add_argument("provider", choices=["codex", "claude"])
    args = parser.parse_args()
    cmd = args.cmd
    if cmd == "init":
        return init(PRIVATE)
    if cmd == "remove-schedule":
        return launch_agent(ROOT, remove=True)
    if cmd == "auth":
        return authenticate(args.provider, PRIVATE)
    cfg = settings(PRIVATE)
    if cmd == "plan-local":
        from local_plan import make_plan

        return make_plan(cfg["agent"]["provider"], PRIVATE)
    if cmd == "telegram-login":
        from pathlib import Path
        from telegram_stdio import executable, private_path

        if cfg["mcp"]["demo"]:
            raise ValueError("В учебном режиме вход в Telegram не нужен.")
        spec = Path(cfg["mcp"]["env_file"]).expanduser()
        env_file = spec if spec.is_absolute() else private_path(PRIVATE, spec)
        # Interactive terminal belongs to the student: Telegram code and 2FA never enter a prompt.
        subprocess.run(
            [executable(cfg["mcp"]["executable"]), "--env-file", str(env_file), "auth"], check=True
        )
        return
    if cmd == "local":
        cfg["agent"]["mode"] = "local"
    now = datetime.now(MOSCOW)
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
    slot = None
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
            pw = smtp_password(mail["address"]) if slot else None
            rows = asyncio.run(telegram_rows(cfg, cmd, now)) if cfg["mcp"]["enabled"] else []
            if cmd == "discover":
                return
            if not demo:
                if not cfg["mcp"]["enabled"] and not any(cfg["lms"].values()):
                    raise ValueError("Включите Telegram или выберите источник Smart LMS.")
                rows.extend(other_sources(cfg, now))
            output = ROOT / "out" / ("mcp-demo" if demo else "live")
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
