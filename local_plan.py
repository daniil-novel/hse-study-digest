"""Build local extraction rules from this public synthetic curriculum only."""

import json
import re
import tomllib

from study_agent import model_json
from study_report import write

# Labels are the only output strings a matched rule can add to a student's digest.
LESSONS = {
    "references": (
        "Подготовить список источников",
        ["К пятнице оформите список источников.", "В приложение включить библиографию."],
    ),
    "diagram": (
        "Приложить схему решения",
        ["К отчёту приложите схему решения.", "До защиты нарисовать блок-схему алгоритма."],
    ),
    "demo": (
        "Записать демонстрацию",
        ["Для семинара запишите демонстрацию.", "К среде снять демонстрационное видео."],
    ),
    "defence": (
        "Подать заявку на защиту",
        ["В этом модуле подайте заявку на защиту.", "К четвергу зарегистрироваться на защиту."],
    ),
}
NEGATIVES = [
    "На семинаре обсудили список источников и схему решения.",
    "Сегодня посмотрели демонстрационное видео.",
    "Вчера на паре говорили про защиту.",
    "Где найти список источников?",
    "Подайте заявку на проживание в общежитии.",
    "Оформите список участников встречи.",
    "Приложите фотографию к анкете.",
    "Запишите номер кабинета.",
]
PLAN_REQUEST = """Выбери короткие поисковые правила для русского учебного текста. Все примеры вымышлены.
Выдай только JSON {"rules":[{"type":"код типа","phrases":["глагол объект","другой глагол объект"]}]}.
Для каждого типа покрой оба положительных примера. Каждая фраза должна совпадать в соответствующем
примере с НЕПРЕРЫВНЫМ фрагментом из 2–6 слов и содержать глагол поручения и его объект.
Сохраняй дефисы внутри слов. Пиши строчными русскими буквами. Допустим один знак * в конце слова:
он обозначает до десяти букв окончания после основы длиной не менее 4 букв. Можно оставлять слова целыми.
Фраза не должна срабатывать на другие типы и нейтральные примеры. Не возвращай имён, текстов строк отчёта,
объяснений, команд и регулярных выражений.\n"""
WORD = re.compile(r"[а-яё]+(?:-[а-яё]+)?\*?")
NO_TASK = r"^(?![^\n]*\?\s*$)(?![^\n]*\b(?:не (?:нужно|надо|требуется|просим)|отмен\w*)\b)"


def compile_phrase(value):
    if not isinstance(value, str) or not 3 <= len(value) <= 100:
        raise ValueError("В правиле нужна короткая русская фраза.")
    tokens = value.split(" ")
    if not 2 <= len(tokens) <= 6 or not all(WORD.fullmatch(t) for t in tokens):
        raise ValueError("В поисковой фразе допустимы 2–6 слов, дефис и конечная звёздочка.")
    pieces = []
    for token in tokens:
        stem = token.removesuffix("*")
        if token.endswith("*") and len(stem) < 4:
            raise ValueError("Для звёздочки нужна основа длиной от четырёх букв.")
        literal = re.escape(stem).replace("ё", "[её]")
        pieces.append(literal + (r"[а-яё]{0,10}" if token.endswith("*") else ""))
    return re.compile(NO_TASK + r"[^\n]*?(?<!\w)" + r"\s+".join(pieces) + r"(?!\w)", re.I)


def validated(items):
    if not isinstance(items, list) or len(items) != len(LESSONS):
        raise ValueError("План должен покрывать все четыре учебных типа.")
    found, result = set(), []
    for entry in items:
        if not isinstance(entry, dict) or set(entry) != {"type", "phrases"}:
            raise ValueError("У правила должны быть только поля type и phrases.")
        key, phrases = entry["type"], entry["phrases"]
        if (
            not isinstance(key, str)
            or key not in LESSONS
            or key in found
            or not isinstance(phrases, list)
            or not 1 <= len(phrases) <= 4
        ):
            raise ValueError("Неизвестный тип, повтор типа или неверное число фраз.")
        found.add(key)
        label, positives = LESSONS[key]
        others = [s for k, (_, examples) in LESSONS.items() if k != key for s in examples] + NEGATIVES
        matched = set()
        for phrase in phrases:
            regex = compile_phrase(phrase)
            # A rule must match an actual assignment clause including its action.
            hits = {i for i, line in enumerate(positives) if regex.search(line)}
            if not hits or any(regex.search(line) for line in others):
                raise ValueError("Фраза не прошла проверку на учебных примерах. Повторите plan-local.")
            # The public examples share an explicit verb immediately before the object.
            if not re.match(
                r"(?:оформ|включ|прилож|нарис|запиш|запис|снять|подай|подат|зарег)", phrase
            ):
                raise ValueError("Начните каждую фразу с глагола поручения.")
            matched.update(hits)
            result.append((regex, label))
        if len(matched) != len(positives):
            raise ValueError("Правила пропускают учебный пример. Повторите plan-local.")
    return result


def make_plan(provider, private):
    # Only these source-code constants are supplied to the CLI. No feedback from local runs.
    seed = {
        "types": {
            k: {"label": label, "positive": examples} for k, (label, examples) in LESSONS.items()
        },
        "negative": NEGATIVES,
    }
    answer = model_json(provider, private, PLAN_REQUEST, seed)
    if not isinstance(answer, dict) or set(answer) != {"rules"}:
        raise ValueError("Модель вернула неверный формат локального плана.")
    rules = answer["rules"]
    compiled = validated(rules)
    lines = [
        "# Проверено на встроенных вымышленных примерах. Исходных сообщений здесь нет.",
        "version = 1",
    ]
    for entry in rules:
        lines.extend(
            [
                "",
                "[[rules]]",
                "type = " + json.dumps(entry["type"]),
                "phrases = " + json.dumps(entry["phrases"], ensure_ascii=False),
            ]
        )
    write(private / "plan-local.toml", "\n".join(lines) + "\n")
    print(f"Сохранён plan-local.toml: проверенных поисковых фраз — {len(compiled)}.")


def load_rules(private):
    path = private / "plan-local.toml"
    if not path.is_file():
        return []
    if path.stat().st_size > 65536:
        raise ValueError("Слишком большой plan-local.toml. Выполните plan-local заново.")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("Не удалось прочитать plan-local.toml. Выполните plan-local заново.") from None
    if set(data) != {"version", "rules"} or type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Неизвестная версия или структура plan-local.toml.")
    return validated(data["rules"])
