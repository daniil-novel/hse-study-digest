import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from digest_config import init, merge, registry
from digest_schedule import Schedule
from local_digest import tasks_from_message
from study_report import deliver, table


def spec(**changes):
    return dict({"timezone": "UTC", "mode": "weekly", "weekdays": ["tue", "thu"], "month_days": [1,15,-1],
                 "times": ["09:00"], "catchup_hours": 24, "date_regex": ""}, **changes)


class ScheduleAndConfig(unittest.TestCase):
    def test_weekly_and_catchup_select_only_last_due_slot(self):
        s = Schedule(spec(times=["09:00","17:00"]))
        self.assertEqual(s.due(datetime.fromisoformat("2026-11-04T08:00+00:00")),
                         datetime.fromisoformat("2026-11-03T17:00+00:00"))
        self.assertIsNone(s.due(datetime.fromisoformat("2026-11-02T10:00+00:00")))

    def test_monthly_including_month_end_and_regex(self):
        s=Schedule(spec(mode="monthly",date_regex="^2028-02"))
        due=s.upcoming(datetime.fromisoformat("2028-01-01T00:00+00:00"),3)
        self.assertEqual([d.day for d in due],[1,15,29])

    def test_dst_gaps_and_overlap(self):
        s=Schedule(spec(mode="daily",timezone="America/New_York",times=["02:30","01:30"]))
        spring=datetime.fromisoformat("2026-03-08T00:00+00:00").date()
        autumn=datetime.fromisoformat("2026-11-01T00:00+00:00").date()
        self.assertEqual(len(list(s.on(spring))),1)
        self.assertEqual(len(set(s.on(autumn))),2)
        with self.assertRaises(ValueError):
            Schedule(spec(date_regex="["))

    def test_manual_delete_stays_deleted_comments_and_exclusions_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            private=Path(folder)
            init(private)
            path=private/"chats.toml"
            original=path.read_text(encoding="utf-8")
            path.write_text(original.replace("ids = []",'ids = ["-9"]')+"\n# Моя заметка\n",encoding="utf-8")
            merge(path,{"-1":"Курс А","-9":"Запрет"}, {})
            self.assertEqual(registry(path)[0],{"-1":"Курс А"})
            import tomlkit
            doc=tomlkit.parse(path.read_text(encoding="utf-8"))
            del doc["tracked"]["-1"]
            path.write_text(tomlkit.dumps(doc),encoding="utf-8")
            merge(path,{"-1":"Курс А","-2":"Курс Б"}, {})
            self.assertEqual(registry(path)[0],{"-2":"Курс Б"})
            self.assertIn("# Моя заметка",path.read_text(encoding="utf-8"))

    def test_local_labels_do_not_copy_names_or_contact_information(self):
        now=datetime.now(timezone.utc)
        msg={"text":"Иван Петров, пришлите видеоотчёт на alice@example.invalid до 04.12.2026."}
        found=tasks_from_message(msg,now)
        self.assertIn(("Подготовить видеоотчёт","2026-12-04"),found)
        self.assertNotRegex(str(found),r"Иван|Петров|@")
        rows=[dict(course="Курс",task="Подготовить отчёт",due="",source="Курс",url="",identity=key) for key in ["1/1","1/2"]]
        self.assertEqual(len(table(rows)),2)

    def test_csv_is_attached_and_daily_slots_can_send_consecutive_days(self):
        import smtplib
        with tempfile.TemporaryDirectory() as d, patch.object(smtplib,"SMTP_SSL") as connect:
            smtp=connect.return_value.__enter__.return_value
            for slot in ("2030-01-01","2030-01-01","2030-01-02"):
                deliver("<p>Demo</p>",Path(d),datetime.now(timezone.utc),slot=slot,mailbox="qa@example.test",
                        host="smtp.example.test",password="test",attachment=b"course;task\r\n")
            self.assertEqual(smtp.send_message.call_count,2)
            msg=smtp.send_message.call_args.args[0]
            self.assertEqual(msg["To"],"qa@example.test")
            attachment=next(msg.iter_attachments())
            self.assertEqual(attachment.get_filename(),"digest.csv")
            self.assertEqual(attachment.get_payload(decode=True),b"course;task\r\n")
