"""Narrow stdio client for daniil-novel/telegram-mcp 0.2.2. No write actions."""

import json
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


def private_path(private, value):
    p = (private / value).resolve()
    if not p.is_relative_to(private.resolve()):
        raise ValueError("Путь к закрытой настройке должен находиться внутри private.")
    return p


def stamp(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("Нужна дата и время с часовым поясом.")
    d = datetime.fromisoformat(value) if isinstance(value, str) else value
    if d.tzinfo is None or d.utcoffset() is None:
        raise ValueError("Нужен часовой пояс у даты.")
    return d


def executable(value):
    located = shutil.which(value)
    if not located and value == "telegram-mcp":
        suffix = ".exe" if os.name == "nt" else ""
        adjacent = Path(sys.executable).parent / ("telegram-mcp" + suffix)
        if adjacent.is_file():
            located = str(adjacent)
    if not located:
        raise ValueError(
            "Не найден telegram-mcp. Укажите в [mcp].executable полный путь к установленной программе."
        )
    return str(Path(located).resolve())


@asynccontextmanager
async def server(config, private, allowed=None):
    try:
        from dotenv import dotenv_values
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        raise ValueError("Для MCP установите зависимости из requirements-mcp.txt.") from None
    settings = config["mcp"]
    demo = settings.get("demo", False)
    if type(demo) is not bool:
        raise ValueError("mcp.demo принимает только true или false.")
    exe = executable(settings.get("executable", "telegram-mcp"))
    with tempfile.TemporaryDirectory() as temporary, open(os.devnull, "w") as errlog:
        configured = Path(settings.get("env_file", "telegram.env")).expanduser()
        env_file = (
            Path(temporary) / "demo.env"
            if demo
            else (
                configured.resolve() if configured.is_absolute() else private_path(private, configured)
            )
        )
        if demo:
            env_file.write_text("", encoding="utf-8")
        if not env_file.is_file():
            raise ValueError("Не найден private/telegram.env. Сначала настройте Telegram MCP.")
        values = dotenv_values(env_file, interpolate=False)
        env = {
            "TELEGRAM_WRITE_ENABLED": "false",
            "TELEGRAM_WRITE_ALLOWED_CHAT_IDS": "",
            "TELEGRAM_SEND_TO_ANY_CHAT": "false",
            "PYTHONUTF8": "1",
        }
        if allowed is not None or (not demo and "TELEGRAM_ALLOWED_CHAT_IDS" in os.environ):
            # Constrain the upstream allowlist, never enlarge the owner's original ACL.
            ids = None if allowed is None else {str(x) for x in allowed}
            for raw in (
                values.get("TELEGRAM_ALLOWED_CHAT_IDS", "*"),
                "*" if demo else os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "*"),
            ):
                raw = str(raw or "").strip()
                if raw != "*":
                    rule = {x.strip() for x in raw.split(",") if x.strip()}
                    ids = rule if ids is None else ids & rule
            env["TELEGRAM_ALLOWED_CHAT_IDS"] = "*" if ids is None else ",".join(sorted(ids))
        if not demo:
            blocked = {
                x.strip()
                for src in (
                    values.get("TELEGRAM_DENIED_CHAT_IDS", ""),
                    os.getenv("TELEGRAM_DENIED_CHAT_IDS", ""),
                )
                for x in str(src or "").split(",")
                if x.strip()
            }
            env["TELEGRAM_DENIED_CHAT_IDS"] = ",".join(sorted(blocked))
        args = ["--env-file", str(env_file), "serve", "--transport", "stdio"]
        if demo:
            args.append("--demo")
        params = StdioServerParameters(command=exe, args=args, env=env, cwd=temporary)
        async with stdio_client(params, errlog=errlog) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=90)
            ) as session:
                hello = await session.initialize()
                catalog = await session.list_tools()
                names = {t.name for t in catalog.tools}
                if not {"list_dialogs", "messages_between"} <= names or any(
                    t.annotations is None or t.annotations.readOnlyHint is not True
                    for t in catalog.tools
                ):
                    raise ValueError(
                        "MCP не подтвердил обязательные инструменты и режим только чтения."
                    )
                yield session, hello.serverInfo, len(names)


async def page(session, name, params, demo):
    if name not in {"list_dialogs", "messages_between"}:
        raise ValueError("Шлюз разрешает только чтение списка или заданного периода.")
    response = await session.call_tool(name, arguments={"params": params})
    if response.isError:
        for content in response.content:
            if content.type == "text" and any(
                x in content.text.casefold()
                for x in (
                    "session expired",
                    "session revoked",
                    "no local telegram session",
                    "session was revoked",
                )
            ):
                raise ValueError(
                    "Вход в Telegram истёк или отсутствует. Запустите python digest.py telegram-login в своём терминале."
                )
        raise ValueError(
            "Telegram MCP вернул ошибку. Проверьте вход, доступы и период. Частичная сводка отклонена."
        )
    data = response.structuredContent
    if data is None and len(response.content) == 1 and response.content[0].type == "text":
        data = json.loads(response.content[0].text)
    if (
        not isinstance(data, dict)
        or data.get("source") != ("demo" if demo else "telegram")
        or not isinstance(data.get("items"), list)
        or type(data.get("has_more")) is not bool
    ):
        raise ValueError("Источник или формат страницы MCP не совпал с ожидаемым.")
    return data


async def catalog(session, demo, page_size, maximum, before_read):
    """Upstream sorts by ID; inspect timestamps before taking the newest N."""
    seen = set()
    result = {}
    args = {"limit": page_size}
    while True:
        before_read()
        data = await page(session, "list_dialogs", args, demo)
        for d in data["items"]:
            chat_id, kind, title = d["id"], d["kind"], d["title"]
            if (
                type(chat_id) is not int
                or kind not in {"group", "channel", "private"}
                or not isinstance(title, str)
            ):
                raise ValueError("Неизвестный формат каталога Telegram MCP.")
            latest = d.get("latest")
            if latest and latest.get("chat_id") != chat_id:
                raise ValueError("Предпросмотр относится к другому чату.")
            result[str(chat_id)] = (title, kind, stamp(latest["date"]) if latest else None)
        if not data["has_more"]:
            break
        cursor = data.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError("Каталог MCP не вернул новый курсор. Частичный список отклонён.")
        seen.add(cursor)
        args["cursor"] = cursor
    return dict(
        sorted(
            result.items(),
            key=lambda item: item[1][2].timestamp() if item[1][2] else float("-inf"),
            reverse=True,
        )[:maximum]
    )
