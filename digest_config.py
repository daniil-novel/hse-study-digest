"""Human-owned settings and a comment-preserving, persistent chat registry."""
import os
import re
import tomllib
from pathlib import Path

from study_report import write

ROOT = Path(__file__).resolve().parent


def init(private):
    from telegram_agent import EMPTY_SCOPES_TOML
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    templates = ROOT / "templates"
    files = {"study.toml": (templates/"study.toml").read_text(encoding="utf-8"),
             "chats.toml": (templates/"chats.toml").read_text(encoding="utf-8"),
             "chats-demo.toml": (templates/"chats.toml").read_text(encoding="utf-8"),
             "telegram-scopes.toml": EMPTY_SCOPES_TOML, "lms.json": "[]\n",
             "telegram.env": "# TODO: значения из my.telegram.org, только локально.\nTELEGRAM_API_ID=\nTELEGRAM_API_HASH=\nTELEGRAM_WRITE_ENABLED=false\n"}
    for name, content in files.items():
        try:
            fd = os.open(private / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    print("Настройки готовы в", private)


def read_toml(path):
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError(f"Проверьте {path.name}: файл должен существовать и содержать корректный TOML.") from None


def settings(private):
    cfg = read_toml(private / "study.toml")
    try:
        if cfg["agent"]["mode"] not in {"cloud", "local"} or cfg["agent"]["provider"] not in {"codex", "claude"}:
            raise ValueError
        if not all(type(v) is bool for v in (cfg["mcp"]["enabled"], cfg["mcp"]["demo"], cfg["mail"]["enabled"], cfg["lms"]["use_notes"])):
            raise ValueError
        if not cfg["courses"] or any(not k.strip() or not isinstance(v, list) or any(
                not isinstance(alias, str) or not alias.strip() for alias in v) for k,v in cfg["courses"].items()):
            raise ValueError
        for group in ("chat_titles", "chat_ids"):
            if any(not k.strip() or name not in cfg["courses"] or
                   (group == "chat_ids" and not re.fullmatch(r"-[1-9]\d*", k))
                   for k,name in cfg["local"][group].items()):
                raise ValueError
        if not all(type(cfg["scan"][k]) is int and 1 <= cfg["scan"][k] <= limit
                   for k,limit in (("max_dialogs",10000),("page_size",200),("sample_size",8))):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("Некорректный private/study.toml. Сверьте секции, пределы и названия курсов с шаблоном.") from None
    return cfg


def registry_path(cfg, private):
    return private / ("chats-demo.toml" if cfg["mcp"]["demo"] else "chats.toml")


def registry(path):
    values = read_toml(path)
    try:
        tracked, ids, patterns = values["tracked"], values["exclude"]["ids"], values["exclude"]["title_regex"]
        seen = values["history"]["seen"]
        if not isinstance(tracked,dict) or not all(isinstance(v,list) for v in (ids,patterns,seen)):
            raise ValueError
        if any(not isinstance(i,str) or not re.fullmatch(r"-?[1-9]\d*",i) for i in [*ids,*seen,*tracked]):
            raise ValueError
        if any(not isinstance(k,str) or not k.strip() for k in tracked.values()) or any(not isinstance(p,str) for p in patterns):
            raise ValueError
        for p in patterns:
            re.compile(p)
        return dict(tracked), set(ids), list(patterns), set(seen)
    except (KeyError, AttributeError, TypeError, ValueError, re.error):
        raise ValueError(f"Проверьте [tracked], [exclude] и [history] в {path.name}.") from None


def merge(path, proposals, initial_tracked):
    import tomlkit
    tracked, denied, _, seen = registry(path)
    doc = tomlkit.parse(path.read_text(encoding="utf-8-sig"))
    # Re-read decisions made while the model was running. Forgetting a row is a decision.
    seen.update(initial_tracked)
    seen.update(tracked)
    for chat_id, course in proposals.items():
        if chat_id not in seen and chat_id not in denied:
            doc["tracked"][chat_id] = course
            seen.add(chat_id)
    doc["history"]["seen"] = sorted(seen)
    write(path, tomlkit.dumps(doc))
