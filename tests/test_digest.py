import asyncio
import io
import json
import os
import smtplib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import study
import study_agent
import study_report
from study_sources import MOSCOW, aliases_for, exact_due, telegram, two_months_before

NOW = datetime(2026, 11, 4, 10, tzinfo=MOSCOW)


class DigestTests(unittest.TestCase):

    def test_calendar_boundaries_and_dates(self):
        for current, expected in [("2024-04-30", "2024-02-29"), ("2025-01-31", "2024-11-30")]:
            self.assertEqual(str(two_months_before(datetime.fromisoformat(current)).date()), expected)
        self.assertEqual(exact_due("Загрузить отчёт до 29.02.2024 в 09:35"), "2024-02-29 09:35")
        for text in ("Сдать завтра", "Сдать до 31.02.2026", "Занятие 04.11.2026. Сдать завтра",
                     "Дедлайн перенесли с 04.11.2026 на 06.11.2026", "Сдать до 04.11.2026 в 26:33",
                     "Сдать до 04.11.2026 12:30 UTC"):
            self.assertEqual(exact_due(text), "", text)

    def test_discovery_reads_metadata_and_reports_ambiguous_courses(self):
        courses = {"Математика": ["матан"], "Алгебра": ["алг"]}
        self.assertEqual(aliases_for("МАТАН и АЛГ", courses), list(courses))
        self.assertFalse(aliases_for("алгебраический", courses))
        dialogs = [
            SimpleNamespace(id=-1, is_user=False, is_group=True, is_channel=False, date=NOW, name="Матан"),
            SimpleNamespace(id=2, is_user=True, is_group=False, is_channel=False, date=NOW, name="Private"),
            SimpleNamespace(id=-3, is_user=False, is_group=True, is_channel=False, date=NOW.replace(month=8), name="Old"),
        ]
        seen = []

        class Client:
            def __init__(self, *args): pass
            async def connect(self): pass
            async def disconnect(self): pass
            async def is_user_authorized(self): return True
            async def iter_dialogs(self, limit, **kwargs):
                seen.append(limit)
                for dialog in dialogs:
                    yield dialog

        with tempfile.TemporaryDirectory() as d, patch.dict(sys.modules, {"telethon": SimpleNamespace(TelegramClient=Client)}), \
                patch.dict(os.environ, {"TG_API_ID": "42", "TG_API_HASH": "synthetic"}):
            root = Path(d)
            (root / "student.session").touch()
            result = asyncio.run(telegram("discover", root, courses, {}, NOW))
        self.assertEqual(seen, [300])
        self.assertEqual(result, [["-1", "Матан", "2026-11-04", "Математика"]])

    def test_provider_gets_approved_text_only_and_verifies_exact_quote(self):
        note = dict(id="local-id", course="Индивидуальный предмет", text="Сдать работу до 06.11.2026.", url="https://edu.hse.ru/1")
        answer = json.dumps({"tasks": [{"index": 0, "quote": note["text"]}]}).encode()
        status = subprocess.CompletedProcess([], 0, b"", b"Logged in using ChatGPT")
        output = subprocess.CompletedProcess([], 0, answer, b"")
        with tempfile.TemporaryDirectory() as d, patch.object(study_agent.shutil, "which", return_value="codex"), \
                patch.dict(os.environ, {"TG_API_HASH": "sentinel", "OPENAI_API_KEY": "sentinel"}), \
                patch.object(subprocess, "run", side_effect=[status, output]) as cli:
            rows = study_agent.extract("codex", Path(d), [note])
        args, kw = cli.call_args
        self.assertEqual(rows[0]["due"], "2026-11-06")
        self.assertNotIn("TG_API_HASH", kw["env"])
        self.assertNotIn("OPENAI_API_KEY", kw["env"])
        self.assertNotIn(note["id"].encode(), kw["input"])
        self.assertNotIn(note["course"].encode(), kw["input"])
        self.assertNotIn(note["url"].encode(), kw["input"])
        self.assertNotEqual(kw["cwd"], str(study.ROOT))
        self.assertIn("--ignore-user-config", args[0])
        fabricated = subprocess.CompletedProcess([], 0, b'{"tasks":[{"index":0,"quote":"invented"}]}', b"")
        with tempfile.TemporaryDirectory() as d, patch.object(study_agent.shutil, "which", return_value="codex"), \
                patch.object(subprocess, "run", side_effect=[status, fabricated]), self.assertRaises(ValueError):
            study_agent.extract("codex", Path(d), [note])

    def test_live_reader_requests_only_explicit_group_and_honors_cutoff(self):
        accessed = []

        class Client:
            def __init__(self, *args): pass
            async def connect(self): pass
            async def disconnect(self): pass
            async def is_user_authorized(self): return True
            async def get_entity(self, identifier):
                accessed.append(identifier)
                return SimpleNamespace(title="Вымышленная группа", username=None)
            async def iter_messages(self, group):
                for idx, when in [(5, NOW), (4, two_months_before(NOW)), (3, NOW.replace(month=8))]:
                    yield SimpleNamespace(id=idx, date=when, raw_text=f"Сдать отчёт №{idx} до 17.11.2026.")

        with tempfile.TemporaryDirectory() as d, patch.dict(sys.modules, {"telethon": SimpleNamespace(TelegramClient=Client)}), \
                patch.dict(os.environ, {"TG_API_ID": "42", "TG_API_HASH": "synthetic"}):
            p = Path(d)
            (p / "student.session").touch()
            rows = asyncio.run(telegram("run", p, {"Курс": []}, {"-1001234": "Курс"}, NOW))
            with self.assertRaises(ValueError):
                asyncio.run(telegram("run", p, {"Курс": []}, {"7": "Курс"}, NOW))
        self.assertEqual(accessed, [-1001234])
        self.assertEqual([r["url"] for r in rows], ["https://t.me/c/1234/5", "https://t.me/c/1234/4"])

    def test_mail_is_self_only_and_ambiguous_sends_do_not_retry(self):
        env = {"DIGEST_EMAIL": "student@example.org", "DIGEST_SMTP_HOST": "example.org", "DIGEST_SMTP_PASSWORD": "test"}
        for interrupt in [False, True]:
            with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, env), \
                    patch.object(smtplib, "SMTP_SSL") as connect, redirect_stdout(io.StringIO()):
                smtp = connect.return_value.__enter__.return_value
                if interrupt:
                    smtp.send_message.side_effect = smtplib.SMTPServerDisconnected("test")
                    with self.assertRaises(smtplib.SMTPServerDisconnected):
                        study_report.deliver("test", Path(d), NOW)
                    with self.assertRaises(ValueError):
                        study_report.deliver("test", Path(d), NOW)
                else:
                    study_report.deliver("test", Path(d), NOW)
                    study_report.deliver("test", Path(d), NOW)
                self.assertEqual(smtp.send_message.call_count, 1)
                self.assertEqual(smtp.send_message.call_args.kwargs["to_addrs"], [env["DIGEST_EMAIL"]])


    def test_expired_claude_login_explained_without_exposing_stdout(self):
        status = subprocess.CompletedProcess([], 0, b'{"authMethod":"claude.ai"}', b"")
        error = subprocess.CompletedProcess([], 1, b"OAuth session expired and could not be refreshed; secret", b"")
        with tempfile.TemporaryDirectory() as d, patch.object(study_agent.shutil, "which", return_value="claude"), \
                patch.object(subprocess, "run", side_effect=[status, error]), self.assertRaises(ValueError) as caught:
            study_agent.request("claude", Path(d), ["test"])
        self.assertIn("python digest.py auth claude", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))


    def test_spreadsheet_html_and_feed_url_protection(self):
        self.assertIn("'=1+1", study_report.csv_text([["=1+1"]]))
        self.assertEqual(study_report.link("https://edu.hse.ru/calendar?token=secret"), "")
        self.assertEqual(study_report.link("javascript:alert(1)"), "")
        self.assertEqual(study_report.link("https://t.me/c/123/45"), "https://t.me/c/123/45")


if __name__ == "__main__":
    unittest.main()
