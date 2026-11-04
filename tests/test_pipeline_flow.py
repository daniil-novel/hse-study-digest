"""Проверки реальной связки CLI → память → Wiki → таблица без доступа к внешним чатам."""
import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import digest
import digest_config
from agent_code import knowledge
from agent_code.skills import instructions


class PipelineFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        # Создаём штатные настройки в изолированном экземпляре проекта.
        with redirect_stdout(io.StringIO()):
            digest_config.init(self.private)
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(digest, "ROOT", self.root))
        self.patches.enter_context(patch.object(digest, "PRIVATE", self.private))
        self.patches.enter_context(patch.dict(os.environ, {}, clear=True))
        self.rows = [dict(course="Анализ данных", task="Выполнить лабораторную работу",
                          due="2026-12-01", source="Фикстура учебного сообщения",
                          url="https://t.me/c/321/5", identity="test-chat/5")]

    def tearDown(self):
        self.patches.close()
        self.temp.cleanup()

    def command(self, *arguments):
        # Запускается публичная точка входа с настоящей проверкой конфигурации.
        output = io.StringIO()
        with patch("sys.argv", ["digest.py", *arguments]), redirect_stdout(output):
            digest.main()
        return output.getvalue()

    def test_run_wires_database_wiki_and_reports_and_done_survives_next_run(self):
        with patch.object(digest, "telegram_rows", new=AsyncMock(return_value=self.rows)):
            self.command("run")
        stored = knowledge.current_rows(self.private)
        self.assertEqual(len(stored), 1)
        task_id = stored[0]["task_id"]
        self.assertTrue((self.private / "knowledge.sqlite").is_file())
        wiki = self.private / "wiki" / "index.md"
        self.assertIn("Анализ данных", wiki.read_text(encoding="utf-8"))
        html = self.root / "out" / "live" / "digest.html"
        self.assertIn("лабораторную", html.read_text(encoding="utf-8"))
        csv_path = html.with_suffix(".csv")
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            table = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(len(table), 1)
        self.command("done", task_id)
        with patch.object(digest, "telegram_rows", new=AsyncMock(return_value=self.rows)):
            self.command("run")
        self.assertEqual(knowledge.current_rows(self.private)[0]["user_status"], "done")
        self.assertIn("Выполнено", html.read_text(encoding="utf-8"))
        self.assertEqual(knowledge.current_rows(self.private)[0]["version"], 1)

    def test_failure_before_extraction_preserves_database_and_generated_files(self):
        with patch.object(digest, "telegram_rows", new=AsyncMock(return_value=self.rows)):
            self.command("run")
        protected = [self.private / "knowledge.sqlite", self.private / "wiki" / "index.md",
                     self.root / "out" / "live" / "digest.html", self.root / "out" / "live" / "digest.csv"]
        before = {p: p.read_bytes() for p in protected}
        with patch.object(digest, "telegram_rows", new=AsyncMock(side_effect=ValueError("Fixture read failed"))):
            with self.assertRaisesRegex(ValueError, "Fixture read failed"):
                self.command("run")
        self.assertEqual({p: p.read_bytes() for p in protected}, before)

    def test_empty_base_skill_route_search_and_ask_make_no_cloud_call_or_fabrication(self):
        # Проверяем команды, на которые направляют реально загружаемые инструкции скиллов.
        self.assertIn('python digest.py search', instructions("study-search"))
        self.assertIn('python digest.py run', instructions("study-collector"))
        with patch.object(knowledge, "model_json") as model:
            self.assertEqual(json.loads(self.command("search", "проверь какие лабораторные мне сдавать")), [])
            answer = json.loads(self.command("ask", "проверь какие лабораторные мне сдавать", "--provider", "codex"))
            self.assertEqual(answer["evidence"], [])
            self.assertIn("не найдены", answer["answer"])
            model.assert_not_called()
        self.assertFalse((self.private / "knowledge.sqlite").exists())

    def test_busy_collection_lock_preserves_user_status_and_report_files(self):
        from filelock import FileLock

        with patch.object(digest, "telegram_rows", new=AsyncMock(return_value=self.rows)):
            self.command("run")
        task_id = knowledge.current_rows(self.private)[0]["task_id"]
        protected = [self.private / "knowledge.sqlite", *self.private.joinpath("wiki").glob("*.md"),
                     self.root / "out" / "live" / "digest.html", self.root / "out" / "live" / "digest.csv"]
        before = {path: path.read_bytes() for path in protected}
        # Другой экземпляр FileLock воспроизводит захваченный работающим сборщиком замок.
        with FileLock(str(self.private / "digest.lock"), timeout=0):
            for arguments in (("done", task_id), ("reopen", task_id), ("wiki",)):
                with self.assertRaisesRegex(ValueError, "Сбор ещё работает"):
                    self.command(*arguments)
            # Локальное чтение по-прежнему доступно, пока сборщик держит замок.
            self.assertEqual(len(json.loads(self.command("search", "лаборатор"))), 1)
        self.assertEqual(knowledge.current_rows(self.private)[0]["user_status"], "open")
        self.assertEqual({path: path.read_bytes() for path in protected}, before)


if __name__ == "__main__":
    unittest.main()
