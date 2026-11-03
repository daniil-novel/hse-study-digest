import io
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs

import lms_api
from study_sources import MOSCOW

NOW = datetime(2026, 11, 4, 10, tzinfo=MOSCOW)
EVENT = {"id": 27, "modulename": "assign", "eventtype": "due", "visible": 1,
         "activityname": "Лабораторная № 1", "instance": 25, "course": {"id": 105},
         "timestart": int((NOW + timedelta(days=2)).timestamp())}


class Reply(io.BytesIO):
    status = 200
    def geturl(self):
        return lms_api.ENDPOINT


class ApiTests(unittest.TestCase):
    def test_multiple_pages_visible_assignments_only(self):
        pages = [{"events": [EVENT, dict(EVENT, id=36, visible=0)], "lastid": 36},
                 {"events": [dict(EVENT, id=12, eventtype="gradingdue")], "lastid": 12},
                 {"events": [], "lastid": None}]
        with patch.dict(os.environ, {"HSE_LMS_TOKEN": "synthetic"}), patch.object(lms_api, "fetch_page", side_effect=pages) as fetch:
            rows = lms_api.read_tasks({"105": "Курс"}, {"Курс": []}, NOW)
        self.assertEqual([c.args[-1] for c in fetch.call_args_list], [0, 36, 12])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://edu.hse.ru/mod/assign/view.php?id=25")
        self.assertEqual(rows[0]["due"], "2026-11-06 10:00")

    def test_invalid_page_or_course_aborts_whole_read(self):
        cases = [
            [{"events": [dict(EVENT, course={"id": 638})], "lastid": 27}],
            [{"events": [EVENT], "lastid": 27}, {"events": [EVENT], "lastid": 27}],
            [{"events": [EVENT], "lastid": 99}],
            [{"events": [dict(EVENT, visible=None)], "lastid": 27}],
            [{"events": [EVENT], "lastid": 27}, {}],
        ]
        for pages in cases:
            with self.subTest(pages=pages), patch.dict(os.environ, {"HSE_LMS_TOKEN": "test"}), \
                    patch.object(lms_api, "fetch_page", side_effect=pages), self.assertRaises(ValueError):
                lms_api.read_tasks({"105": "Курс"}, {"Курс": []}, NOW)

    def test_token_only_in_post_body_and_redacted_on_denial(self):
        secret = "test+token/="
        reply = Reply(json.dumps({"exception": "denied", "message": secret}).encode())
        with patch.object(lms_api, "build_opener") as opener, self.assertRaises(ValueError) as err:
            opener.return_value.open.return_value = reply
            lms_api.fetch_page(secret, 105, 1, 2, 0)
        req = opener.return_value.open.call_args.args[0]
        self.assertIsInstance(opener.call_args.args[0], lms_api.NoRedirect)
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "https://edu.hse.ru/webservice/rest/server.php")
        self.assertNotIn(secret, str(req.headers))
        self.assertNotIn(secret, str(err.exception))
        fields = parse_qs(req.data.decode("ascii"))
        self.assertEqual(fields["wstoken"], [secret])
        self.assertEqual(fields["wsfunction"], ["core_calendar_get_action_events_by_course"])

    def test_without_access_no_requests(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(lms_api, "fetch_page") as fetch:
            self.assertEqual(lms_api.read_tasks({}, {}, NOW), [])
            with self.assertRaisesRegex(ValueError, "HSE_LMS_TOKEN"):
                lms_api.read_tasks({"105": "Курс"}, {"Курс": []}, NOW)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
