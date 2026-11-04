"""Установка переносимых скиллов и загрузка инструкций для подписочного CLI."""

from pathlib import Path  # Пути работают и на Windows, и на macOS.
import shutil  # Копирует папку скилла вместе с её справочными материалами.

ROOT = Path(__file__).resolve().parents[1]  # Корень проекта не зависит от текущей папки терминала.
NAMES = ("study-collector", "study-search", "study-maintainer")  # Только скиллы этого проекта.


def instructions(name):
    """Прочитать тело скилла без служебного YAML-заголовка."""
    if name not in NAMES:  # Произвольный путь нельзя подставить вместо имени скилла.
        raise ValueError("Неизвестный скилл практикума.")
    content = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    return content.split("---", 2)[2].strip()  # Первые два разделителя ограничивают метаданные.


def install(provider):
    """Установить скиллы в текущий проект, сохранив пользовательские правки."""
    if provider not in {"codex", "claude"}:  # Выбираем поддерживаемый каталог обнаружения.
        raise ValueError("Укажите codex или claude.")
    folder = ROOT / (".agents" if provider == "codex" else ".claude") / "skills"
    for name in NAMES:  # Каждый скилл переносится отдельной папкой.
        destination = folder / name
        if destination.exists():  # Повторная установка не затирает уже отредактированные инструкции.
            print(f"Сохранена существующая папка: {destination}")
            continue
        shutil.copytree(ROOT / "skills" / name, destination)
        print(f"Установлен скилл: {destination}")
