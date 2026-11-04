"""Локальный сервис: регулярный сбор, отдельная проверка доставки и пользовательский автозапуск."""

import argparse  # Поддерживаем также самостоятельный запуск python -m agent_code.service.
import csv  # whoami возвращает SID текущего пользователя в машиночитаемом CSV.
import hashlib  # Различаем автозапуск двух копий проекта без публикации пути в имени.
import io  # Разбираем CSV из памяти, не записывая сведения пользователя в журнал.
import json  # Статус хранится в небольшом понятном файле.
import os  # Управляем группой дочернего процесса и правами локальных файлов.
import plistlib  # Формируем штатный формат LaunchAgent средствами Python.
import re  # Проверяем SID, прежде чем включать его в XML Windows.
import shutil  # Фиксируем пути установленных CLI для запуска после входа в macOS.
import signal  # Ctrl-C и SIGTERM превращаются в запрос корректной остановки.
import subprocess  # Сбор выполняется отдельным процессом с ограничением времени.
import sys  # Выбираем поддерживаемую ОС без изменения настроек компьютера при импорте.
import tempfile  # Атомарная замена состояния не оставляет наполовину записанный JSON.
import threading  # Event позволяет прервать ожидание сразу после команды остановки.
import time  # Монотонные часы не зависят от ручного перевода календарного времени.
import tomllib  # Интервалы берём из существующего private/study.toml.
import xml.etree.ElementTree as ET  # XML-экранирование защищает пути с пробелами и амперсандом.
from collections.abc import Callable  # Описываем функцию обновления пульса сервиса.
from contextlib import (
    contextmanager,
)  # Всегда возвращаем исходные обработчики сигналов.
from dataclasses import dataclass  # Интервалы после проверки становятся неизменяемыми.
from datetime import (
    datetime,
    timezone,
)  # В состоянии используем однозначные отметки UTC.
from pathlib import Path  # Все пути принадлежат явно указанному экземпляру проекта.
from typing import (
    Any,
    Iterator,
    cast,
)  # JSON-состояние содержит строки, числа и вложенные записи.

from filelock import (
    FileLock,
    Timeout,
)  # Блокировка работает между процессами на Windows и macOS.

STATUS_FILE = "service-status.json"  # Стабильное имя для команды диагностики.
TASK_NS = (
    "http://schemas.microsoft.com/windows/2004/02/mit/task"  # Схема Task Scheduler.
)
OUTCOMES = {"ok", "failed", "timeout", "stopped", "start_failed", "running"}


@dataclass(frozen=True)
class Options:
    """Частоты независимы от дней и часов отправки почты."""

    scan_interval_seconds: int = (
        900  # Обновлять локальный отчёт раз в пятнадцать минут.
    )
    tick_interval_seconds: int = 60  # Проверять календарный слот отправки раз в минуту.
    command_timeout_seconds: int = (
        600  # Один сбор или tick не занимает сервис навсегда.
    )


def _options(root: Path) -> Options:
    """Проверить настройки и запретить подмену рабочего сервиса учебным backend."""
    try:
        cfg = tomllib.loads(
            (root / "private/study.toml").read_text(encoding="utf-8-sig")
        )
        if (
            cfg["mcp"].get("demo", False) is not False
        ):  # --once проверяет реальную конфигурацию.
            raise ValueError
        spec = cfg.get(
            "service", {}
        )  # Старые конфигурации используют опубликованные defaults.
        limits = {
            "scan_interval_seconds": (30, 604800),
            "tick_interval_seconds": (10, 3600),
            "command_timeout_seconds": (10, 7200),
        }
        values = {}
        for name, (low, high) in limits.items():
            value = spec.get(name, getattr(Options(), name))
            if type(value) is not int or not low <= value <= high:
                raise ValueError  # bool не считаем числом секунд.
            values[name] = value
        return Options(**values)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError(
            "Проверьте private/study.toml и интервалы [service]; учебный backend недопустим."
        ) from None


def _python(root: Path, *, background: bool = False) -> Path:
    """Выбрать Python именно этого .venv, сохранив путь символической ссылки macOS."""
    folder = root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    python = folder / ("python.exe" if sys.platform == "win32" else "python")
    hidden = (
        folder / "pythonw.exe"
    )  # Фоновый процесс Windows не открывает консольное окно.
    if background and sys.platform == "win32" and hidden.is_file():
        python = hidden
    if not python.is_file():
        raise ValueError(
            "Не найден Python проекта. Создайте .venv и установите зависимости."
        )
    return python  # resolve() здесь раскрыл бы symlink и мог бы потерять virtualenv.


def _stamp() -> str:
    """Записать время вместе с часовым поясом."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic(path: Path, data: bytes) -> None:
    """Сохранить собственный файл целиком; временный файл принадлежит тому же каталогу."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            name = (
                stream.name
            )  # Удаляем только тот временный файл, который создали сами.
            stream.write(data)
        os.chmod(
            name, 0o600
        )  # POSIX-права; на Windows действуют ACL папки пользователя.
        os.replace(name, path)  # Читатель увидит старую или новую полную версию.
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def _read_health(root: Path) -> dict[str, Any]:
    """Читать только известные поля; повреждённый JSON не печатается пользователю."""
    try:
        path = root / "private" / STATUS_FILE
        if path.stat().st_size > 65_536:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        result = {}
        for key in (
            "started_at",
            "heartbeat",
            "last_success_scan",
            "last_success_tick",
        ):
            if isinstance(data.get(key), str):
                datetime.fromisoformat(
                    data[key]
                )  # Не выводим произвольный текст вместо времени.
                result[key] = data[key]
        if data.get("state") in {"starting", "running", "stopped", "failed"}:
            result["state"] = data["state"]
        if type(data.get("pid")) is int:
            result["pid"] = data["pid"]
        for key in ("last_scan", "last_tick"):
            record = data.get(key, {})
            if isinstance(record, dict) and record.get("result") in OUTCOMES:
                clean = {"result": record["result"]}
                for field in ("started_at", "finished_at"):
                    if isinstance(record.get(field), str):
                        datetime.fromisoformat(record[field])
                        clean[field] = record[field]
                if type(record.get("return_code")) is int:
                    clean["return_code"] = record["return_code"]
                result[key] = clean
        return result
    except (OSError, ValueError, TypeError):
        return {}  # Отсутствие состояния отличается от свидетельства успешной работы.


@contextmanager
def _signals(stop: threading.Event) -> Iterator[None]:
    """Перехватить завершение только в главном потоке; затем восстановить окружение CLI."""
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.signal(kind, lambda _signum, _frame: stop.set())
    try:
        yield
    finally:
        for restore_kind, handler in previous.items():
            signal.signal(restore_kind, handler)


def _system(
    args: list[str], *, capture: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """Вызвать штатную команду ОС без shell, паролей в argv и вывода сырых ошибок."""
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(
            "Системная команда не завершилась. Проверьте права текущего пользователя."
        ) from None


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    """Остановить запущенную нами группу, чтобы после тайм-аута не продолжался сбор MCP."""
    if sys.platform == "win32":
        try:
            _system(["taskkill", "/PID", str(child.pid), "/T", "/F"])
        except ValueError:
            child.kill()  # Резервная остановка непосредственного процесса без вывода stderr.
    else:
        try:
            os.killpg(
                child.pid, signal.SIGTERM
            )  # Дать Python время закрыть сетевые соединения.
        except ProcessLookupError:
            pass  # Процесс успел завершиться между проверкой и сигналом.
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass  # После короткого срока закрываем оставшиеся процессы группы.
        try:
            os.killpg(
                child.pid, signal.SIGKILL
            )  # Потомки могли пережить непосредственного ребёнка.
        except ProcessLookupError:
            pass  # Вся группа уже штатно завершилась.
    try:
        child.wait(
            timeout=5
        )  # Забрать код завершения; не оставлять zombie-процесс на macOS.
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _execute(
    root: Path,
    command: str,
    timeout: int,
    stop: threading.Event,
    pulse: Callable[[], None],
) -> dict[str, Any]:
    """Запустить один разрешённый шаг; его stdout/stderr никогда не становятся журналом."""
    if command not in {"run", "tick"}:
        raise ValueError("Сервис запускает только run и tick.")
    started = _stamp()
    env = (
        os.environ.copy()
    )  # Здесь только исходное окружение ОС; .env заново прочитает digest.py.
    env["PYTHONUTF8"] = (
        "1"  # Кириллица не зависит от Windows code page и фонового терминала.
    )
    env["PYTHONUNBUFFERED"] = "1"
    flags: dict[str, Any] = {"start_new_session": True}
    if sys.platform == "win32":
        flags = {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    try:
        child = subprocess.Popen(
            [str(_python(root)), "-X", "utf8", str(root / "digest.py"), command],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **flags,
        )
    except OSError:
        return {
            "result": "start_failed",
            "started_at": started,
            "finished_at": _stamp(),
        }
    deadline = time.monotonic() + timeout
    outcome = "failed"
    try:
        while child.poll() is None:
            pulse()  # Статус остаётся свежим даже во время долгого сетевого запроса.
            if stop.is_set() or time.monotonic() >= deadline:
                outcome = "stopped" if stop.is_set() else "timeout"
                _stop_child(child)
                break
            stop.wait(0.2)  # Ожидание немедленно прерывается сигналом остановки.
        else:
            outcome = "ok" if child.returncode == 0 else "failed"
    finally:
        if (
            child.poll() is None
        ):  # В том числе при сбое записи состояния или KeyboardInterrupt.
            _stop_child(child)
    return {
        "result": outcome,
        "started_at": started,
        "finished_at": _stamp(),
        "return_code": child.returncode,
    }


def run(root: Path, once: bool = False) -> dict[str, Any]:
    """Обновлять отчёт и проверять доставку; --once делает один реальный сбор без отправки."""
    root = Path(root).resolve()
    # .env загружает каждый digest.py отдельно: следующий сбор увидит обновлённый файл.
    options = _options(root)
    _python(root)  # Ошибку установки замечаем до запуска постоянного цикла.
    private = root / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = FileLock(str(private / "service.lock"), timeout=0)
    try:
        lock.acquire()  # Два сервиса одной копии проекта не могут опрашивать источники параллельно.
    except Timeout:
        raise ValueError(
            "Сервис этого проекта уже работает. Используйте python digest.py status."
        ) from None
    stop = threading.Event()
    health = _read_health(
        root
    )  # Сохраняем последние успешные моменты между перезапусками.
    health.update(state="starting", started_at=_stamp(), pid=os.getpid())
    last_pulse = -float("inf")

    def pulse(force: bool = False) -> None:
        nonlocal last_pulse
        request = private / "service-stop.txt"
        if (
            request.is_file()
            and request.stat().st_size <= 32
            and request.read_text(encoding="ascii").strip() == str(os.getpid())
        ):
            stop.set()  # remove-service в Windows сначала просит завершить дочерний процесс.
        if force or time.monotonic() - last_pulse >= 5:
            health["heartbeat"] = _stamp()
            _atomic(
                private / STATUS_FILE,
                (json.dumps(health, ensure_ascii=False, indent=2) + "\n").encode(),
            )
            last_pulse = time.monotonic()

    next_scan, next_tick = (
        0.0,
        0.0,
    )  # Первый рабочий цикл выполняется сразу после запуска.
    try:
        with _signals(stop):
            health["state"] = "running"
            pulse(True)
            while not stop.is_set():
                for command, key, due, interval in (
                    ("run", "scan", next_scan, options.scan_interval_seconds),
                    ("tick", "tick", next_tick, options.tick_interval_seconds),
                ):
                    if (
                        stop.is_set()
                        or (once and command == "tick")
                        or time.monotonic() < due
                    ):
                        continue
                    health["last_" + key] = {
                        "result": "running",
                        "started_at": _stamp(),
                    }
                    pulse(True)
                    record = _execute(
                        root, command, options.command_timeout_seconds, stop, pulse
                    )
                    health["last_" + key] = record
                    if record["result"] == "ok":
                        health["last_success_" + key] = record["finished_at"]
                    if command == "run":
                        next_scan = time.monotonic() + interval
                    else:
                        next_tick = time.monotonic() + interval
                    pulse(
                        True
                    )  # Ошибка одного шага не стирает успех другого и не останавливает цикл.
                if once:
                    break
                pulse()
                stop.wait(
                    min(1.0, max(0.0, min(next_scan, next_tick) - time.monotonic()))
                )
            health["state"] = "stopped"
    except KeyboardInterrupt:
        health["state"] = (
            "stopped"  # Защита для Ctrl-C до установки обработчика сигналов.
        )
    except Exception:
        health["state"] = "failed"
        raise ValueError(
            "Сервис остановлен из-за внутреннего сбоя. Выполните python digest.py status."
        ) from None
    finally:
        try:
            pulse(True)  # Завершение сохраняется до освобождения блокировки процесса.
        finally:
            lock.release()
    return health


def status(root: Path) -> dict[str, Any]:
    """Вернуть проверенное состояние; наличие старого PID само по себе не означает работу."""
    root = Path(root).resolve()
    health = _read_health(root)
    lock_path = root / "private/service.lock"
    running = False
    if lock_path.exists():
        try:
            with FileLock(str(lock_path), timeout=0):
                pass  # Свободная блокировка означает, что сервис уже завершился.
        except Timeout:
            running = True
    health["running"] = running
    health.setdefault("state", "not_started")
    if heartbeat := health.get("heartbeat"):
        stamp = datetime.fromisoformat(heartbeat)
        if stamp.tzinfo is not None:
            age = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
            health["heartbeat_age_seconds"] = age
            health["responsive"] = (
                running and age <= 30
            )  # Это пульс процесса, а не проверка сети.
    return health


def _label(root: Path) -> str:
    """Имя относится к конкретной папке: удаление не задевает другую копию проекта."""
    identity = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[
        :12
    ]
    return "local.hse-study-digest.service." + identity


def _windows_xml(root: Path, python: Path, sid: str) -> bytes:
    """Сформировать пользовательскую задачу без пароля Windows и административных прав."""
    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise ValueError("Не удалось определить SID текущего пользователя.")
    task = ET.Element("Task", {"version": "1.2", "xmlns": TASK_NS})
    info = ET.SubElement(task, "RegistrationInfo")
    ET.SubElement(info, "Description").text = "HSE Study Digest: local service"
    trigger = ET.SubElement(ET.SubElement(task, "Triggers"), "LogonTrigger")
    ET.SubElement(trigger, "Enabled").text = "true"
    ET.SubElement(trigger, "UserId").text = (
        sid  # Запуск только при входе владельца задачи.
    )
    principal = ET.SubElement(
        ET.SubElement(task, "Principals"), "Principal", {"id": "Owner"}
    )
    ET.SubElement(principal, "UserId").text = sid
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    for key, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
    ):
        ET.SubElement(settings, key).text = (
            value  # Фоновый цикл не ограничен тремя днями Windows.
        )
    restart = ET.SubElement(settings, "RestartOnFailure")
    ET.SubElement(restart, "Interval").text = "PT1M"
    ET.SubElement(restart, "Count").text = (
        "3"  # Не создаём бесконечный шторм перезапусков.
    )
    action = ET.SubElement(ET.SubElement(task, "Actions", {"Context": "Owner"}), "Exec")
    ET.SubElement(action, "Command").text = str(python)
    ET.SubElement(action, "Arguments").text = "-X utf8 -m agent_code.service run"
    ET.SubElement(action, "WorkingDirectory").text = str(root)
    return cast(bytes, ET.tostring(task, encoding="utf-16", xml_declaration=True))


def _windows_sid() -> str:
    """Получить SID через whoami без запроса имени, пароля или прав администратора."""
    response = _system(["whoami", "/user", "/fo", "csv", "/nh"], capture=True)
    if response.returncode == 0:
        text = response.stdout.decode("utf-8", errors="replace")
        for row in csv.reader(io.StringIO(text)):
            if len(row) == 2 and re.fullmatch(r"S-1-\d+(?:-\d+)+", row[1]):
                return row[1]  # Локализованное имя учётной записи нам не требуется.
    raise ValueError("Не удалось определить текущего пользователя Windows.")


def _owns_windows(raw: bytes, root: Path, sid: str) -> bool:
    """Перед изменением существующей задачи проверить владельца и действие."""
    try:
        task = ET.fromstring(raw)

        def find(field: str) -> str | None:
            return task.findtext(".//{" + TASK_NS + "}" + field)

        command = find("Command") or ""
        allowed = {
            str(root / ".venv/Scripts/python.exe").casefold(),
            str(root / ".venv/Scripts/pythonw.exe").casefold(),
        }
        return (
            command.casefold() in allowed
            and find("WorkingDirectory") == str(root)
            and find("Arguments") == "-X utf8 -m agent_code.service run"
            and task.findtext(
                f"./{{{TASK_NS}}}Principals/{{{TASK_NS}}}Principal/{{{TASK_NS}}}UserId"
            )
            == sid
            and find("LogonType") == "InteractiveToken"
            and find("RunLevel") == "LeastPrivilege"
            and len(task.findall(f"./{{{TASK_NS}}}Actions/*")) == 1
        )
    except (ET.ParseError, ValueError):
        return False


def _mac_plist(root: Path, python: Path) -> bytes:
    """Сформировать LaunchAgent, который читает .env только внутри Python-процесса."""
    paths = [str(python.parent)]
    paths.extend(
        str(Path(p).parent) for name in ("codex", "claude") if (p := shutil.which(name))
    )
    paths.extend(os.environ.get("PATH", os.defpath).split(os.pathsep))
    return plistlib.dumps(
        {
            "Label": _label(root),
            "ProgramArguments": [
                str(python),
                "-X",
                "utf8",
                "-m",
                "agent_code.service",
                "run",
            ],
            "WorkingDirectory": str(root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 60,
            "ExitTimeOut": 15,
            "Umask": 0o077,
            "EnvironmentVariables": {
                "PATH": os.pathsep.join(dict.fromkeys(paths)),
                "PYTHONUTF8": "1",
            },
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        }
    )  # Постоянный диагностический след — только разрешённые поля service-status.json.


def _owns_mac(path: Path, root: Path) -> bool:
    """Не выгружать чужой LaunchAgent даже при совпадении имени."""
    try:
        value = plistlib.loads(path.read_bytes())
        args = value.get("ProgramArguments", [])
        return (
            value.get("Label") == _label(root)
            and value.get("WorkingDirectory") == str(root)
            and len(args) == 6
            and args[0] == str(root / ".venv/bin/python")
            and args[1:] == ["-X", "utf8", "-m", "agent_code.service", "run"]
        )
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        return False


def install(root: Path) -> str:
    """Зарегистрировать и запустить сервис текущего пользователя; вызывается только явно."""
    root = Path(root).resolve()
    _options(root)  # При установке также нельзя незаметно включить demo-источники.
    python = _python(root, background=True)
    label = _label(root)
    if sys.platform == "win32":
        sid = _windows_sid()
        existing = _system(["schtasks", "/Query", "/TN", label, "/XML"], capture=True)
        if existing.returncode == 0 and not _owns_windows(existing.stdout, root, sid):
            raise ValueError(
                "Имя задачи занято другим действием; чужая задача не изменена."
            )
        definition = root / "private/service-task.xml"
        _atomic(definition, _windows_xml(root, python, sid))
        args = ["schtasks", "/Create", "/TN", label, "/XML", str(definition)]
        if existing.returncode == 0:
            args.append(
                "/F"
            )  # Перезаписывать разрешено только уже проверенную собственную задачу.
        if _system(args).returncode != 0:
            raise ValueError(
                "Windows не зарегистрировала задачу. Проверьте политику Task Scheduler."
            )
        if _system(["schtasks", "/Run", "/TN", label]).returncode != 0:
            raise ValueError(
                "Задача зарегистрирована, но запуск не подтверждён. Выполните python digest.py status."
            )
    elif sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        target = domain + "/" + label
        if _system(["launchctl", "print", domain]).returncode != 0:
            raise ValueError(
                "Для LaunchAgent войдите в графический сеанс macOS своим пользователем."
            )
        destination = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        existing = _system(["launchctl", "print", target]).returncode == 0
        if (destination.exists() or existing) and not _owns_mac(destination, root):
            raise ValueError(
                "Имя LaunchAgent занято другим действием; чужой агент не изменён."
            )
        if existing and _system(["launchctl", "bootout", target]).returncode != 0:
            raise ValueError("Не удалось остановить предыдущий экземпляр LaunchAgent.")
        _atomic(destination, _mac_plist(root, python))
        if _system(["plutil", "-lint", str(destination)]).returncode != 0:
            raise ValueError(
                "macOS не подтвердила корректность plist; агент не запущен."
            )
        if (
            _system(["launchctl", "bootstrap", domain, str(destination)]).returncode
            != 0
        ):
            raise ValueError(
                "LaunchAgent записан, но загрузка не подтверждена. Проверьте сеанс macOS."
            )
    else:
        raise ValueError("Автозапуск этого практикума поддерживает Windows и macOS.")
    return "Автозапуск зарегистрирован. После первого сбора выполните python digest.py status."


def remove(root: Path) -> str:
    """Отключить собственный автозапуск, сохранив конфигурацию, отчёты и сеансы входа."""
    root = Path(root).resolve()
    label = _label(root)
    if sys.platform == "win32":
        sid = _windows_sid()
        existing = _system(["schtasks", "/Query", "/TN", label, "/XML"], capture=True)
        if existing.returncode != 0:
            return "Задача не найдена или недоступна; удаление не выполнялось."
        if not _owns_windows(existing.stdout, root, sid):
            raise ValueError(
                "Задача не принадлежит этому проекту; удаление остановлено."
            )
        current = status(root)
        if current.get("running") and type(current.get("pid")) is int:
            request = root / "private/service-stop.txt"
            _atomic(request, str(current["pid"]).encode("ascii"))
            deadline = time.monotonic() + 15
            while status(root)["running"] and time.monotonic() < deadline:
                time.sleep(0.2)  # Дождаться закрытия группы MCP до остановки задачи ОС.
            if status(root)["running"]:
                raise ValueError(
                    "Сервис не подтвердил остановку; автозапуск сохранён. Проверьте status."
                )
            request.unlink(missing_ok=True)
        _system(
            ["schtasks", "/End", "/TN", label]
        )  # Уже остановленная задача может вернуть ошибку.
        if _system(["schtasks", "/Delete", "/TN", label, "/F"]).returncode != 0:
            raise ValueError("Windows не подтвердила удаление задачи.")
    elif sys.platform == "darwin":
        target = f"gui/{os.getuid()}/" + label
        destination = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        existing = _system(["launchctl", "print", target]).returncode == 0
        if not destination.exists() and not existing:
            return "LaunchAgent этого проекта не найден."
        if not _owns_mac(destination, root):
            raise ValueError(
                "LaunchAgent не принадлежит этому проекту; удаление остановлено."
            )
        if existing and _system(["launchctl", "bootout", target]).returncode != 0:
            raise ValueError("macOS не подтвердила остановку LaunchAgent.")
        destination.unlink()  # Удаляется единственный предварительно проверенный plist.
    else:
        raise ValueError("Удаление автозапуска поддерживает Windows и macOS.")
    return "Автозапуск удалён. Личные настройки и результаты сохранены."


def main() -> None:
    """Командный интерфейс пригоден для Task Scheduler и LaunchAgent без shell-обёрток."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "install", "remove", "status"])
    parser.add_argument(
        "--once", action="store_true", help="один реальный сбор без отправки"
    )
    args = parser.parse_args()
    root = (
        Path(__file__).resolve().parents[1]
    )  # Запуск не зависит от текущей папки оболочки.
    try:
        if args.command == "run":
            result = run(root, once=args.once)
            if args.once and result.get("last_scan", {}).get("result") != "ok":
                raise ValueError(
                    "Разовая проверка не завершилась успешно. Выполните python digest.py status."
                )
        elif args.command == "status":
            print(json.dumps(status(root), ensure_ascii=False, indent=2))
        else:
            print(install(root) if args.command == "install" else remove(root))
    except ValueError as error:
        sys.exit(
            str(error)
        )  # Все ValueError этого модуля содержат только фиксированные сообщения.
    except Exception:
        sys.exit(
            "Команда сервиса завершилась с ошибкой. Проверьте настройки и права пользователя."
        )


if __name__ == "__main__":
    main()  # Импорт из digest.py никогда не устанавливает автозапуск и не начинает опрос.
