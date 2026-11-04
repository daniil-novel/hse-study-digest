"""Загрузка локальных секретов без подмены системного окружения и сеансов входа."""

import io  # Передаём dotenv проверенный текст, не создавая дополнительный файл секретов.
import os  # Дочерние процессы получают разрешённые значения через окружение.
from pathlib import Path  # Ищем .env только в явно указанной папке проекта.
from urllib.parse import urlsplit  # Проверяем адрес LMS до использования токена.

from dotenv import (
    dotenv_values,
    load_dotenv,
)  # Библиотека читает .env без исполнения команд.

# Перечень закрыт: .env не может менять PATH, PYTHONPATH, настройки записи MCP или модели.
ENV_KEYS = frozenset(
    {
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_PHONE",
        "TELEGRAM_ALLOWED_CHAT_IDS",  # Штатный список разрешённых источников MCP.
        "TELEGRAM_DENIED_CHAT_IDS",  # Явный запрет источника сохраняет приоритет в шлюзе.
        "TELEGRAM_SESSION_DIR",  # Путь к штатному хранилищу входа, а не сама сессия.
        "TELEGRAM_WRITE_ENABLED",  # Принимаем только false: сервис читает сообщения.
        "DIGEST_SMTP_PASSWORD",
        "DIGEST_SMTP_ADDRESS",
        "DIGEST_SMTP_HOST",
        "DIGEST_SMTP_PORT",
        "LMS_URL",
        "LMS_TOKEN",
        "HSE_LMS_TOKEN",  # Совместимость с фактическим именем в lms_api.py.
    }
)


def _validate(values: dict[str, str | None]) -> None:
    """Проверить эффективные значения до изменения окружения процесса."""
    write = os.environ.get(
        "TELEGRAM_WRITE_ENABLED", values.get("TELEGRAM_WRITE_ENABLED") or "false"
    )
    if write.strip().lower() != "false":
        raise ValueError(
            "TELEGRAM_WRITE_ENABLED должен быть false для этого практикума."
        )
    address = os.environ.get("LMS_URL", values.get("LMS_URL") or "").strip()
    if address:
        url = urlsplit(address)
        if (
            url.scheme != "https"
            or url.netloc != "edu.hse.ru"
            or url.path not in {"", "/", "/webservice/rest/server.php"}
            or url.query
            or url.fragment
        ):
            raise ValueError("LMS_URL допускает только официальный https://edu.hse.ru.")


def load_environment(root: Path) -> tuple[str, ...]:
    """Добавить разрешённые непустые значения ROOT/.env, сохранив уже заданные.

    Args:
        root: Корень данного экземпляра проекта, а не текущая папка терминала.

    Returns:
        Имена добавленных переменных. Сами значения функция не возвращает и не печатает.

    Raises:
        ValueError: Файл непригоден или содержит неподдерживаемые настройки.
    """
    path = Path(root).resolve() / ".env"  # Не ищем секреты в родительских каталогах.
    before = set(os.environ)  # Нужны только имена для безопасного результата функции.
    _validate(
        {}
    )  # Ограничения чтения действуют также для переменных, заданных самой ОС.
    if path.is_file():
        try:
            if (
                path.stat().st_size > 65_536
            ):  # Ограничиваем объём случайно подставленного файла.
                raise ValueError
            raw = path.read_text(
                encoding="utf-8-sig"
            )  # Допускаем BOM редактора Windows.
            values = dotenv_values(stream=io.StringIO(raw), interpolate=False)
            if (
                set(values) - ENV_KEYS
            ):  # Неизвестные ключи не попадают в окружение даже временно.
                raise ValueError
            _validate(
                values
            )  # Не разрешаем перенаправление LMS или запись даже в штатном .env MCP.
            lines = []
            for key, value in values.items():
                if (
                    not value
                ):  # Пустой публичный шаблон не перекрывает штатные настройки.
                    continue
                if any(c in value for c in ("\n", "\r", "\x00")):
                    raise ValueError  # Секреты и адреса здесь должны занимать одну строку.
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                lines.append(
                    f"{key}='{escaped}'"
                )  # Цитирование относится к dotenv, не к shell.
            load_dotenv(
                stream=io.StringIO("\n".join(lines)),
                override=False,  # Значения, заданные ОС, имеют приоритет над файлом.
                interpolate=False,  # ${...} в пароле остаётся буквальной частью пароля.
            )
        except (OSError, UnicodeError, ValueError):
            raise ValueError(
                "Проверьте .env по templates/environment.env; значения не выводятся."
            ) from None

    if token := os.environ.get("LMS_TOKEN"):
        os.environ.setdefault(
            "HSE_LMS_TOKEN", token
        )  # Старое явно заданное имя сохраняет приоритет.
    return tuple(
        sorted(set(os.environ) - before)
    )  # Диагностика получает только имена ключей.
