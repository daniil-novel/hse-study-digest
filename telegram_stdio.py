"""Narrow stdio client for daniil-novel/telegram-mcp 0.2.3. No write actions."""

import json
import os
import shutil
import subprocess
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
    located = None
    if value == "telegram-mcp":
        # Обновление пакета в .venv проекта должно обгонять старый глобальный CLI из PATH.
        suffix = ".exe" if os.name == "nt" else ""
        adjacent = Path(sys.executable).parent / ("telegram-mcp" + suffix)
        if adjacent.is_file():
            located = str(adjacent)
    if not located:
        located = shutil.which(value)  # Явный путь пользователя не переопределяем.
    if not located:
        raise ValueError(
            "Не найден telegram-mcp. Укажите в [mcp].executable полный путь к установленной программе."
        )
    return str(Path(located).resolve())


def configured_env(config, private):
    """Выбрать именно настроенный файл, включая абсолютный путь вне проекта."""
    spec = Path(config["mcp"].get("env_file", "telegram.env")).expanduser()
    path = spec if spec.is_absolute() else private_path(private, spec)
    if not path.is_file():
        raise ValueError("Не найден настроенный файл .env для Telegram. Проверьте [mcp].env_file.")
    return path


def environment(values, *, authentication=False):
    """Дать MCP только настройки его задачи; реальные переменные ОС важнее значений файла."""
    # Базовые пути нужны штатным CLI и хранилищу сессии. Секреты других сервисов исключены.
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
            "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE", "XDG_DATA_HOME", "LANG", "LC_ALL", "TERM"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    names = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_DIR",
             "TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_DENIED_CHAT_IDS"]
    if authentication:
        names.extend(["TELEGRAM_PHONE", "TELEGRAM_2FA_PASSWORD"])
    for name in names:
        value = os.environ.get(name, values.get(name))  # Даже явно пустое значение ОС имеет приоритет.
        if value is not None:
            env[name] = value
    env.update(TELEGRAM_WRITE_ENABLED="false", TELEGRAM_WRITE_ALLOWED_CHAT_IDS="",
               TELEGRAM_SEND_TO_ANY_CHAT="false", PYTHONUTF8="1")
    return env


def constrain_sources(env, values, allowed):
    """В отличие от учётных данных, ACL объединяются только в сторону сужения доступа."""
    ids = None if allowed is None else {str(x) for x in allowed}
    for raw in (values.get("TELEGRAM_ALLOWED_CHAT_IDS", "*"), env.get("TELEGRAM_ALLOWED_CHAT_IDS", "*")):
        raw = str(raw or "").strip()
        if raw != "*":
            selected = {x.strip() for x in raw.split(",") if x.strip()}
            ids = selected if ids is None else ids & selected
    env["TELEGRAM_ALLOWED_CHAT_IDS"] = "*" if ids is None else ",".join(sorted(ids))
    denied = {x.strip() for raw in (values.get("TELEGRAM_DENIED_CHAT_IDS", ""),
                                  env.get("TELEGRAM_DENIED_CHAT_IDS", ""))
              for x in str(raw or "").split(",") if x.strip()}
    env["TELEGRAM_DENIED_CHAT_IDS"] = ",".join(sorted(denied))


def authorize(config, private):
    """Защитить выбранный .env и открыть штатный вход; коды вводит владелец в своём терминале."""
    from dotenv import dotenv_values

    if config["mcp"].get("demo", False):
        raise ValueError("Для учебного источника авторизация Telegram не выполняется.")
    env_file = configured_env(config, private)
    binary = executable(config["mcp"].get("executable", "telegram-mcp"))
    values = dotenv_values(env_file, interpolate=False, encoding="utf-8-sig")
    env = environment(values, authentication=True)
    args = [binary, "--env-file", str(env_file)]  # Телефон и пароль никогда не оказываются в argv.
    try:
        protected = subprocess.run(
            [*args, "protect-env"], env=environment({}), stdin=subprocess.DEVNULL,
            capture_output=True, timeout=30, check=False,
        )
        if protected.returncode != 0:
            raise ValueError("MCP не подтвердил защиту файла .env. Проверьте доступ к файлу и повторите вход.")
        # Никакого раннего isatty: upstream сначала проверит сохранённую сессию без вопросов.
        result = subprocess.run([*args, "auth"], env=env, timeout=600, check=False)
        if result.returncode == 130:
            raise SystemExit(130)  # Сохраняем стандартный код прерванного пользователем входа.
        if result.returncode != 0:
            raise ValueError("Вход в Telegram не завершён. Проверьте локальные API-данные, телефон, код и пароль 2FA.")
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except subprocess.TimeoutExpired:
        raise ValueError("Истекло время входа в Telegram. Повторите telegram-login в своём терминале.") from None
    except OSError:
        raise ValueError("Не удалось запустить Telegram MCP. Проверьте установленный CLI и права доступа.") from None


@asynccontextmanager
async def server(config, private, allowed=None):
    """Запустить настоящий процесс MCP, согласовать протокол и гарантированно закрыть соединение."""
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
        values = {} if demo else dotenv_values(configured_env(config, private), interpolate=False, encoding="utf-8-sig")
        env = environment(values)
        if demo:
            # Учебный backend не наследует действительные ключи или ограничения аккаунта.
            env = {key: value for key, value in env.items() if not key.startswith("TELEGRAM_")}
            env.update(TELEGRAM_WRITE_ENABLED="false", TELEGRAM_WRITE_ALLOWED_CHAT_IDS="")
        constrain_sources(env, values, allowed)  # Переменная ОС не отменяет запрет, заданный файлом.
        # Serve не открывает исходный .env с телефоном, паролем 2FA, SMTP или LMS-токеном.
        env_file = Path(temporary) / "reader.env"
        env_file.write_text("", encoding="utf-8")
        env_file.chmod(0o600)
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
    """Запросить одну страницу и проверить источник, формат и признаки полноты ответа."""
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
