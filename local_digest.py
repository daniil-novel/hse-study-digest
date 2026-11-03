"""Read real MCP data locally. Only fixed vocabulary, dates and source references leave this module."""
import re
from datetime import datetime, timedelta

from digest_config import registry, registry_path
from study_sources import MOSCOW, aliases_for, exact_due, two_months_before
from telegram_stdio import catalog, page, server, stamp

# Output never substitutes captured message text into a task title. Even a message containing
# a phone, URL, student name or e-mail can only produce one of these generic action labels.
OBJECTS = [
    (r"видео|ролик", "Подготовить видеоотчёт"),
    (r"репозитор|гитхаб|github", "Подготовить репозиторий проекта"),
    (r"аннотац", "Подготовить аннотацию"),
    (r"таблиц|строк[ауие]", "Заполнить учебную таблицу"),
    (r"выбрать тем|выберите тем|определиться с тем|топик|тем[уы] работ", "Выбрать или уточнить тему работы"),
    (r"презентац|слайд", "Подготовить презентацию"),
    (r"тест|опрос", "Пройти тест или опрос"),
    (r"лабораторн", "Выполнить лабораторную работу"),
    (r"эссе", "Подготовить эссе"),
    (r"отч[её]т", "Подготовить отчёт"),
    (r"задани|домашн|\bдз\b|\bдр\b", "Выполнить учебное задание"),
    (r"проект|работ[уы]", "Подготовить проектную работу"),
]
ACTION = re.compile(r"\b(?:нужно|необходимо|просьба|прошу|просим|следует|должн\w*|предстоит|требуется|"
                    r"сда(?:ть|йте|чи)|срок|дедлайн\w*|заполн\w*|загруз\w*|подготов\w*|"
                    r"пришл\w*|отправ\w*|представ\w*|выб(?:рать|ерите)|выполн\w*|пройдите|пройти|сдела(?:ть|йте))\b",re.I)
NEGATED = re.compile(r"\b(?:не (?:надо|нужно|требуется|присылайте|сдавайте|отправляйте)|"
                     r"отмен(?:ён|ен|или|яем)\w*)\b", re.I)
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
LONG_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})(?:\s+года)?\b", re.I)


def tasks_from_message(message, now, extra_rules=()):
    text = message.get("text") or ""
    found = set()
    for line in text.splitlines():
        if line.rstrip().endswith("?") or NEGATED.search(line):
            continue
        names = [name for rule,name in OBJECTS if ACTION.search(line) and re.search(rule,line,re.I)]
        names.extend(name for pattern,name in extra_rules if pattern.search(line))
        # Use a general category only when no more specific object was mentioned.
        if len(names) > 1:
            names = [n for n in names if n not in {"Выполнить учебное задание", "Подготовить проектную работу"}] or names[:1]
        if "Подготовить видеоотчёт" in names and "Подготовить отчёт" in names:
            names.remove("Подготовить отчёт")
        normalized = LONG_DATE.sub(lambda m: f"{m[1]}.{MONTHS.index(m[2].lower())+1:02}.{m[3]}", line)
        due = exact_due(normalized)
        found.update((name,due) for name in names)
    return sorted(found)


async def harvest(cfg, private, *, discover_only=False, now=None):
    from local_plan import load_rules
    rules = load_rules(private)
    allowed_labels = {name for _,name in OBJECTS}
    for name, terms in cfg["local"].get("task_regex", {}).items():
        if name not in allowed_labels or not isinstance(terms,list) or not all(isinstance(t,str) and t for t in terms):
            raise ValueError("В [local.task_regex] нужны стандартный тип задания и список выражений.")
        try:
            rules.extend((re.compile(t,re.I),name) for t in terms)
        except re.error:
            raise ValueError("Проверьте регулярное выражение в [local.task_regex].") from None
    now = now or datetime.now(MOSCOW)
    demo = cfg["mcp"]["demo"]
    if demo:
        raise ValueError("Локальный режим предназначен для действующего MCP. Для учебного MCP выберите agent.mode=cloud.")
    cutoff = two_months_before(now)
    path = registry_path(cfg,private)
    tracked, denied, patterns, seen = registry(path)
    exact_titles, exact_ids = cfg["local"]["chat_titles"], cfg["local"]["chat_ids"]
    size = cfg["scan"]["page_size"]
    # The upstream catalog may contain previews. catalog() projects only ID, type, title, time.
    async with server(cfg,private) as (client, _, __):
        dialogs = await catalog(client,False,size,cfg["scan"]["max_dialogs"],lambda:None)
    selected, proposals = {}, {}
    for cid,(title,kind,latest) in dialogs.items():
        if kind not in {"group","channel"} or not latest or not cutoff <= latest <= now:
            continue
        if cid in denied or any(re.search(p,title) for p in patterns):
            continue
        candidates = aliases_for(title,cfg["courses"]) if cfg["local"].get("auto_match", True) else []
        course = tracked.get(cid) or exact_titles.get(title) or exact_ids.get(cid) or (candidates[0] if len(candidates)==1 else "")
        if course not in cfg["courses"] or (cid in seen and cid not in tracked):
            continue
        selected[cid] = (title,course)
        proposals[cid] = course
    if discover_only or not selected:
        return proposals,[]
    # Reopen with a server-side allowlist so history requests cannot reach another chat.
    result=[]
    async with server(cfg,private,allowed=selected) as (client,_,__):
        for cid,(title,course) in selected.items():
            cursor=0
            rows=[]
            date_end = now + timedelta(microseconds=1)
            while True:
                active, excluded, title_patterns, remembered = registry(path)
                if cid in excluded or (cid in remembered and cid not in active) or any(re.search(p,title) for p in title_patterns):
                    rows.clear()
                    break
                data = await page(client,"messages_between",{"chat_id":int(cid),"start":cutoff.isoformat(),
                                  "end":date_end.isoformat(),"limit":size,"before_id":cursor},False)
                for msg in data["items"]:
                    if str(msg.get("chat_id")) != cid or not cutoff <= stamp(msg["date"]) < date_end:
                        continue
                    msg_id=msg["id"]
                    if type(msg_id) is not int or msg_id < 1:
                        raise ValueError("MCP вернул некорректный номер сообщения.")
                    url=f"https://t.me/c/{cid[4:]}/{msg_id}" if cid.startswith("-100") else ""
                    for task,due in tasks_from_message(msg,now,rules):
                        label = title if title in exact_titles else course
                        rows.append(dict(course=course,task=task,due=due,source=f"{label} · №{msg_id}",url=url,
                                         identity=f"{cid}/{msg_id}"))
                if not data["has_more"]:
                    break
                nxt=data.get("next_before_id")
                if type(nxt) is not int or nxt <= 0 or (cursor and nxt >= cursor):
                    raise ValueError("Нарушена пагинация истории Telegram.")
                cursor=nxt
            result.extend(rows)
    return proposals,result
