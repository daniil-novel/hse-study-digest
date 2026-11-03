"""Build a local HTML/CSV digest and optionally mail it to the signed-in mailbox."""
import csv
import io
import os
import re
import smtplib
import sqlite3
import ssl
from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from study_sources import DATE

HEADERS = ["Предмет", "Что сделать", "Срок (МСК)", "Статус", "Источник", "Ссылка"]


def link(value):
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return ""
    if p.hostname == "t.me" and re.fullmatch(r"/(?:c/\d+|[\w]+)/\d+", p.path):
        return urlunsplit(("https", "t.me", p.path, "", ""))
    if p.hostname != "hse.ru" and not p.hostname.endswith(".hse.ru"):
        return ""
    if p.path.endswith("export_execute.php") or p.fragment:
        return ""
    query = parse_qs(p.query, keep_blank_values=True)
    if query and (query.keys() != {"id"} or len(query["id"]) != 1 or not re.fullmatch(r"[0-9]+", query["id"][0])):
        return ""
    return urlunsplit(("https", p.hostname, p.path, urlencode(query, doseq=True), ""))


def table(rows):
    grouped = {}
    deadlines = defaultdict(set)
    for row in rows:
        task = " ".join(row["task"].split())
        course, due = row["course"], row["due"]
        base = (course, " ".join((DATE.sub("", task) if due else task).casefold().split()), row.get("identity", ""))
        key = (base, due)
        if key not in grouped:
            grouped[key] = [task, []]
        pair = (row["source"], link(row["url"]))
        if pair not in grouped[key][1]:
            grouped[key][1].append(pair)
        if due:
            deadlines[base].add(due)
    output = []
    for (base, due), (task, sources) in grouped.items():
        if len(deadlines[base]) > 1:
            status = "Сверить разные сроки"
        elif not due:
            status = "Уточнить по источнику"
        else:
            status = "Срок указан"
        output.append((base[0], task, due or "Не уточнён", status, sorted(sources)))
    return sorted(output, key=lambda row: (row[2] == "Не уточнён", row[2], row[0], row[1]))


def write(path, content, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, encoding=encoding, newline="", delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(content)
        except BaseException:
            f.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def csv_text(rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";")
    for row in rows:
        # Excel and LibreOffice may treat untrusted text as a formula.
        writer.writerow(["'" + cell if cell.lstrip().startswith(("=", "+", "-", "@"))
                         and not re.fullmatch(r"-\d+", cell) else cell for cell in row])
    return out.getvalue()


def render(rows, folder, demo, now):
    result = table(rows)
    grid = [HEADERS]
    body = []
    for course, task, due, status, sources in result:
        cells = [course, task, due, status]
        grid.append([*cells, "\n".join(s for s, _ in sources), "\n".join(u for _, u in sources if u)])
        refs = [f'<a href="{escape(u, quote=True)}">{escape(s)}</a>' if u else escape(s) for s, u in sources]
        body.append("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in cells)
                    + "<td>" + "<br>".join(refs) + "</td></tr>")
    if not result:
        body.append('<tr><td colspan="5">В подключённых источниках кандидатов не найдено.</td></tr>')
    mark = "Учебный пример · вымышленные записи" if demo else "Личная сводка"
    html = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Учебный дайджест</title>
<style>html{{background:#f4f6f8;color:#162a3b;font:16px/1.5 system-ui,sans-serif}}main{{max-width:1100px;margin:4vh auto;padding:2em;background:white;border-top:5px solid #23626a}}h1{{line-height:1.1}}p{{color:#445966}}section{{overflow:auto}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:.65em;vertical-align:top;border-bottom:1px solid #dde4e9}}th{{background:#edf4f4}}td:nth-child(3){{white-space:nowrap}}a{{color:#075970}}@media(max-width:600px){{main{{padding:.8em}}}}</style>
<main><p>{mark} · {now:%d.%m.%Y} · Москва</p><h1>Учебные задания</h1>
<p>Строк в сводке — {len(result)}. Откройте источник, чтобы проверить формулировку и изменения срока.</p>
<section><table><thead><tr>{''.join('<th>'+h+'</th>' for h in HEADERS[:5])}</tr></thead>
<tbody>{''.join(body)}</tbody></table></section></main></html>"""
    write(folder / "digest.csv", csv_text(grid), "utf-8-sig")
    write(folder / "digest.html", html)
    print(f"Строк: {len(result)}. Таблица: {folder / 'digest.html'}")
    return html


def deliver(html, folder, now, *, slot=None, mailbox=None, host=None, password=None, attachment=None):
    mailbox = (mailbox if mailbox is not None else os.getenv("DIGEST_EMAIL", "")).strip()
    host = (host if host is not None else os.getenv("DIGEST_SMTP_HOST", "")).strip()
    password = password or os.getenv("DIGEST_SMTP_PASSWORD")
    if not re.fullmatch(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", mailbox) or not host or not password:
        raise ValueError("Нужны DIGEST_EMAIL (один адрес), DIGEST_SMTP_HOST и DIGEST_SMTP_PASSWORD.")
    day = now.date()
    if slot is None:  # Compatibility with the original Windows exercise.
        slot = day - timedelta(days=day.weekday() if day.weekday() < 3 else day.weekday() - 3)
    message = EmailMessage()
    message["From"] = message["To"] = mailbox
    message["Subject"] = f"Учебные задания · {day:%d.%m.%Y}"
    message.set_content("Учебный дайджест доступен в HTML-версии письма.")
    message.add_alternative(html, subtype="html")
    if attachment is not None:
        message.add_attachment(attachment, maintype="text", subtype="csv", filename="digest.csv")
    with closing(sqlite3.connect(folder / "mail.sqlite")) as db:
        db.execute("CREATE TABLE IF NOT EXISTS mail(slot TEXT, mailbox TEXT, state TEXT, PRIMARY KEY(slot,mailbox))")
        def previous():
            return db.execute("SELECT state FROM mail WHERE slot=? AND mailbox=?", (str(slot), mailbox)).fetchone()

        def report_previous(record):
            if record[0] == "sent":
                print("В этот период сводка уже отправлена.")
                return
            raise ValueError("Статус предыдущей отправки неясен. Проверьте почту. Автоповтор остановлен.")

        record = previous()
        if record:
            return report_previous(record)
        with smtplib.SMTP_SSL(host, 465, timeout=30, context=ssl.create_default_context()) as smtp:
            smtp.login(mailbox, password)
            try:
                db.execute("INSERT INTO mail VALUES (?,?,?)", (str(slot), mailbox, "pending"))
                db.commit()
            except sqlite3.IntegrityError:
                return report_previous(previous())
            # A lost SMTP acknowledgement is ambiguous. Do not auto-send again.
            smtp.send_message(message, from_addr=mailbox, to_addrs=[mailbox])
            db.execute("UPDATE mail SET state='sent' WHERE slot=? AND mailbox=?", (str(slot), mailbox))
            db.commit()
    print("Почтовый сервер принял письмо для вашего адреса.")
