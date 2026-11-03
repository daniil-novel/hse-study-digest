"""Calendar slots for a personal digest; scheduling is independent of collection."""

import calendar
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LABEL = "local.hse-study-digest"
DAYS = "mon tue wed thu fri sat sun".split()
UTC = timezone.utc


class Schedule:
    def __init__(self, spec):
        try:
            self.zone = ZoneInfo(spec["timezone"])
            self.mode = spec["mode"]
            self.weekdays = {DAYS.index(s) for s in spec["weekdays"]}
            self.month_days = set(spec["month_days"])
            self.catchup = spec["catchup_hours"]
            self.pattern = re.compile(spec.get("date_regex", ""))
            clocks = spec["times"]
            if (
                not isinstance(clocks, list)
                or not clocks
                or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in clocks)
            ):
                raise ValueError
            self.times = sorted({time.fromisoformat(t) for t in clocks})
            if (
                self.mode not in {"daily", "weekly", "monthly"}
                or (self.mode == "weekly" and not self.weekdays)
                or (self.mode == "monthly" and not self.month_days)
                or any(type(n) is not int or n == 0 or abs(n) > 31 for n in self.month_days)
                or type(self.catchup) is not int
                or not 1 <= self.catchup <= 744
            ):
                raise ValueError
        except (TypeError, KeyError, ValueError, ZoneInfoNotFoundError, re.error):
            raise ValueError(
                "Проверьте [schedule]: режим, часовой пояс, дни, часы, regex и окно 1–744 часа."
            ) from None

    def on(self, day: date):
        if self.mode == "weekly" and day.weekday() not in self.weekdays:
            return
        if self.mode == "monthly":
            last = calendar.monthrange(day.year, day.month)[1]
            if day.day not in self.month_days and day.day - last - 1 not in self.month_days:
                return
        if not self.pattern.search(day.isoformat()):
            return
        for clock in self.times:
            local = datetime.combine(day, clock, self.zone)
            # Local times skipped by daylight saving do not create a slot. A repeated time runs once.
            utc = local.astimezone(UTC)
            if utc.astimezone(self.zone).replace(tzinfo=None) == local.replace(tzinfo=None):
                yield utc

    def due(self, now):
        now = now.astimezone(UTC)
        after = now - timedelta(hours=self.catchup)
        day, end = after.astimezone(self.zone).date(), now.astimezone(self.zone).date()
        latest = None
        while day <= end:
            for instant in self.on(day):
                if after <= instant <= now:
                    latest = instant
            day += timedelta(days=1)
        return latest

    def upcoming(self, now, count=6):
        start = now.astimezone(self.zone).date()
        result = []
        for offset in range(731):
            result.extend(
                t.astimezone(self.zone) for t in self.on(start + timedelta(days=offset)) if t > now
            )
            if len(result) >= count:
                break
        return result[:count]


def launch_agent(root: Path, remove=False):
    if sys.platform != "darwin":
        raise ValueError("Установка LaunchAgent доступна на macOS в сеансе пользователя.")
    destination = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = f"gui/{os.getuid()}"
    service = domain + "/" + LABEL
    if destination.exists():
        current = plistlib.loads(destination.read_bytes())
        if str(root / "digest.py") not in current.get("ProgramArguments", []):
            raise ValueError("Уже зарегистрирована задача другого экземпляра практикума.")
    known = subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0
    if known:
        if not destination.exists():
            raise ValueError("Имя LaunchAgent уже занято другой задачей.")
        subprocess.run(["launchctl", "bootout", service], check=True)
    if remove:
        destination.unlink(missing_ok=True)
        print("Локальное расписание удалено.")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = [str(Path(sys.executable).parent)]
    paths.extend(
        str(Path(p).parent) for name in ("telegram-mcp", "codex", "claude") if (p := shutil.which(name))
    )
    paths.extend(os.environ.get("PATH", os.defpath).split(os.pathsep))
    content = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(root / "digest.py"), "tick"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": os.pathsep.join(dict.fromkeys(paths))},
        "StartInterval": 60,
        "RunAtLoad": True,
        "StandardOutPath": str(root / "private/schedule.log"),
        "StandardErrorPath": str(root / "private/schedule-errors.log"),
        "Umask": 0o077,
    }
    temp = destination.with_suffix(".tmp")
    temp.write_bytes(plistlib.dumps(content))
    temp.chmod(0o600)
    temp.replace(destination)
    subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
    print("Расписание зарегистрировано. После входа в macOS launchd проверяет время раз в минуту.")
