"""Regression checks for the difference between a scheduled class and a deadline."""

import tempfile
import unittest
from pathlib import Path

from lms_calendar import ROOT, calendar_bytes, card_url, read_tasks

try:
    import icalendar
except ImportError:
    icalendar = None


class CalendarTests(unittest.TestCase):
    def test_reject_external_subscription(self):
        with self.assertRaises(ValueError):
            calendar_bytes("https://example.invalid/calendar/export_execute.php?secret=private")

    def test_url_keeps_only_card_id(self):
        url = "https://edu.hse.ru/mod/assign/view.php?id=123&authtoken=private"
        self.assertEqual(card_url(url), "https://edu.hse.ru/mod/assign/view.php?id=123")
        self.assertEqual(card_url("https://example.invalid/?key=secret"), "https://edu.hse.ru/calendar/view.php")

    @unittest.skipUnless(icalendar, "optional calendar package")
    def test_deadline_in_moscow_and_class_skipped(self):
        rows = read_tasks("examples/deadlines.ics", {"Алгоритмы": []})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["due"], "2026-11-09 20:59")
        self.assertEqual(rows[0]["task"], "Срок сдачи отчёта по практике")

    @unittest.skipUnless(icalendar, "optional calendar package")
    def test_all_day_floating_and_cancelled(self):
        base = (ROOT / "examples/deadlines.ics").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "sample.ics")
            for time, due in [(";VALUE=DATE:20261109", "2026-11-09"), (":20261109T175900", "")]:
                path.write_text(base.replace(":20261109T175900Z", time), encoding="utf-8")
                self.assertEqual(read_tasks(str(path), {"Алгоритмы": []})[0]["due"], due)
            path.write_text(base.replace("UID:homework", "STATUS:CANCELLED\nUID:homework"), encoding="utf-8")
            self.assertEqual(read_tasks(str(path), {"Алгоритмы": []}), [])
