"""Проверяем сохранность памяти и подтверждение ответов без реальных пользовательских данных."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_code import knowledge as kb

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def record(identity="chat/1", task="Подготовить отчёт", due="2026-11-12"):
    # Это исключительно локальная фикстура тестов.
    return dict(course="Анализ данных", task=task, due=due, source="Учебное объявление",
                url="https://t.me/c/123/1", identity=identity)


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def counts(self):
        # Соединение закрывается до удаления временной папки, в том числе на Windows.
        with sqlite3.connect(self.private / "knowledge.sqlite") as db:
            result = [db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                      for name in ("tasks", "versions", "observations")]
        db.close()
        return result

    def test_same_identity_versions_retry_and_user_completion(self):
        first = kb.update([record()], self.private, NOW)
        item = kb.current_rows(self.private)[0]
        kb.set_done(self.private, item["task_id"])
        changed = kb.update([record(due="2026-11-15")], self.private, NOW + timedelta(hours=1))
        self.assertEqual((first["created"], changed["changed"]), (1, 1))
        current = kb.current_rows(self.private)[0]
        self.assertEqual((current["task_id"], current["version"], current["user_status"]),
                         (item["task_id"], 2, "done"))
        kb.update([record(due="2026-11-15")], self.private, NOW + timedelta(hours=1))
        self.assertEqual(self.counts(), [1, 2, 2])
        kb.set_done(self.private, item["task_id"], False)
        self.assertEqual(kb.current_rows(self.private)[0]["user_status"], "open")

    def test_distinct_sources_and_multiple_tasks_keep_separate_identity(self):
        rows = [record("source/1"), record("source/2"), record("source/1", "Подготовить презентацию")]
        kb.update(rows, self.private, NOW)
        self.assertEqual(len(kb.current_rows(self.private)), 3)
        original = {r["task"]: r["task_id"] for r in kb.current_rows(self.private) if r["task"] == "Подготовить презентацию"}
        stats = kb.update([rows[0], record("source/1", "Подготовить презентацию", "2026-11-20")],
                          self.private, NOW + timedelta(days=1))
        self.assertEqual((stats["created"], stats["changed"]), (0, 1))
        presentation = next(r for r in kb.current_rows(self.private) if r["task"] == "Подготовить презентацию")
        self.assertEqual(presentation["task_id"], original[presentation["task"]])
        self.assertEqual([r["availability"] for r in kb.current_rows(self.private)].count("not_checked"), 1)

    def test_empty_failed_and_old_collection_do_not_erase_or_revert(self):
        kb.update([record(due="2026-12-01")], self.private, NOW)
        before = kb.current_rows(self.private)
        kb.update([], self.private, NOW + timedelta(days=1))
        self.assertEqual(kb.current_rows(self.private), before)
        with self.assertRaises(ValueError):
            kb.update([record(due="2026-12-02"), {"course": 3}], self.private, NOW + timedelta(days=1))
        self.assertEqual(kb.current_rows(self.private), before)
        stale = kb.update([record(due="2026-11-01")], self.private, NOW - timedelta(days=1))
        self.assertEqual(stale["ignored_stale"], 1)
        self.assertEqual(kb.current_rows(self.private), before)

    def test_interrupted_transaction_rolls_back_versions_and_observations(self):
        kb.update([record("one"), record("two")], self.private, NOW)
        before = kb.current_rows(self.private)
        real_revision = kb._revision
        calls = []
        def fail_second(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("simulated storage failure")
            return real_revision(*args)
        with patch.object(kb, "_revision", side_effect=fail_second), self.assertRaises(RuntimeError):
            kb.update([record("one", due="2026-12-01"), record("two", due="2026-12-02")],
                      self.private, NOW + timedelta(days=1))
        self.assertEqual(kb.current_rows(self.private), before)
        self.assertEqual(self.counts(), [2, 2, 2])

    def test_fts_indexes_only_current_versions_and_treats_query_as_text(self):
        kb.update([record(task="Подготовить черновик")], self.private, NOW)
        original = kb.search(self.private, "чернов")[0]
        kb.update([record(task="Подготовить итоговый отчёт")], self.private, NOW + timedelta(hours=1))
        self.assertEqual(kb.search(self.private, "чернов"), [])
        results = kb.search(self.private, 'отчёт" OR 1=1; DROP TABLE tasks;')
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["task_id"], results[0]["version"]), (original["task_id"], 2))
        self.assertEqual(self.counts()[:2], [1, 2])

    def test_answer_invokes_skill_and_accepts_only_exact_current_evidence(self):
        kb.update([record()], self.private, NOW)
        item = kb.search(self.private, "отчёт")[0]
        (self.private / "secret.txt").write_text("private_canary")
        response = {"sentences": [{"text": "Нужно подготовить отчёт.", "evidence": [
            {"evidence_id": item["evidence_id"], "quote": "Подготовить отчёт"}]}]}
        with patch("agent_code.skills.instructions", return_value="SKILL_RULES") as skill, \
                patch.object(kb, "model_json", return_value=response) as llm:
            answer = kb.answer(self.private, "отчёт", "codex")
            skill.assert_called_once_with("study-search")
            self.assertIn("SKILL_RULES", llm.call_args.args[2])
            self.assertNotIn("private_canary", str(llm.call_args.args[2:]))
            self.assertNotIn("Учебное объявление", str(llm.call_args.args[2:]))
            self.assertEqual(answer["evidence"][0]["url"], "https://t.me/c/123/1")
        for evidence in ({"evidence_id": "unknown", "quote": "Подготовить отчёт"},
                         {"evidence_id": item["evidence_id"], "quote": "Срок — 2035-01-01"}):
            response["sentences"][0]["evidence"] = [evidence]
            with patch("agent_code.skills.instructions", return_value=""), patch.object(kb, "model_json", return_value=response):
                with self.assertRaises(ValueError):
                    kb.answer(self.private, "отчёт", "codex")

    def test_answer_rejects_concurrent_change_and_empty_search_stays_local(self):
        with patch.object(kb, "model_json") as llm:
            self.assertFalse(kb.answer(self.private, "отчёт", "codex")["evidence"])
            llm.assert_not_called()
        kb.update([record()], self.private, NOW)
        item = kb.search(self.private, "отчёт")[0]
        def concurrent(*args):
            kb.update([record(due="2026-12-01")], self.private, NOW + timedelta(hours=1))
            return {"sentences": [{"text": "Отчёт к старой дате.", "evidence": [
                {"evidence_id": item["evidence_id"], "quote": "Подготовить отчёт"}]}]}
        with patch("agent_code.skills.instructions", return_value=""), patch.object(kb, "model_json", side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, "изменились"):
                kb.answer(self.private, "отчёт", "codex")

    def test_wiki_has_versions_safe_markdown_and_no_obsolete_course_page(self):
        row = record(task="Подготовить ![изображение](https://example.org/pixel) <script>")
        kb.update([row], self.private, NOW)
        first = kb.export_wiki(self.private)
        page = first[1].read_text(encoding="utf-8")
        self.assertNotIn("<script>", page)
        self.assertIn("\\!\\[", page)
        self.assertIn("https://t.me/c/123/1", page)
        custom = self.private / "wiki" / "personal.md"
        custom.write_text("личная заметка")
        row.update(course="Новый предмет", due="2026-11-30")
        kb.update([row], self.private, NOW + timedelta(hours=1))
        second = kb.export_wiki(self.private)
        self.assertFalse(first[1].exists())
        self.assertTrue(custom.exists())
        page = second[1].read_text(encoding="utf-8")
        self.assertIn("v1", page)
        self.assertIn("v2", page)
        self.assertIn("2026-11-12", page)
        self.assertIn("2026-11-30", page)


if __name__ == "__main__":
    unittest.main()
