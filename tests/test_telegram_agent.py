import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import telegram_agent as agent
import telegram_stdio
from study_sources import MOSCOW


DEMO = {"mcp": {"demo": True}, "agent": {"provider": "codex"}, "scan": {"page_size": 1}}


def model(provider, private, instructions, payload):
    if instructions == agent.PLAN:
        return {"tool": payload["available_tool"], "groups": [g["group"] for g in payload["groups"]]}
    messages = payload["messages"]
    if instructions == agent.CLASSIFY:
        return {
            "course": payload["allowed_courses"][0],
            "id": messages[0]["id"],
            "quote": messages[0]["text"],
        }
    return {"tasks": [{"id": m["id"], "quote": m["text"]} for m in messages]}


@asynccontextmanager
async def fake_server(config, private, allowed=None):
    yield object(), None, 7


def scope_toml(now, chat, status="active"):
    start, end = now - timedelta(days=20), now + timedelta(days=7)
    return f'''[scope."{chat}"]
provider="codex"
status="{status}"
start="{start.isoformat()}"
end="{end.isoformat()}"
valid_until="{end.isoformat()}"
platform_permit="test case permission"
complete_roster=true
members={{"42"="test case individual permission"}}
content="title_and_messages"
'''


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_requests_reads_before_extraction_and_follows_each_cursor(self):
        fake_key = "-1000000000303"
        listing = {
            fake_key: ("Демо: канал", "channel", agent.DEMO_NOW),
            "-202": ("Демо: исключить", "group", agent.DEMO_NOW),
        }
        calls = []

        async def page(session, scope, key, window, before, limit):
            calls.append((key, before))
            return (
                ([{"id": 4, "text": "Сдать отчёт до 10.10.2026."}], 4)
                if not before
                else ([{"id": 3, "text": "Подготовить схему."}], 0)
            )

        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(agent, "server", fake_server),
            patch.object(agent, "catalog", return_value=listing),
            patch.object(agent, "read_page", side_effect=page),
            patch.object(agent, "model_json", side_effect=model) as llm,
        ):
            p = Path(d)
            tracked, _ = await agent.collect("discover", DEMO, p, {}, {}, [], ["исключить"])
            self.assertEqual(tracked, {fake_key: agent.DEMO_COURSE})
            self.assertIn(
                "message_id", (p / "discovery-evidence-demo.csv").read_text(encoding="utf-8-sig")
            )
            _, rows = await agent.collect("run", DEMO, p, {}, tracked, [], ["исключить"])
            self.assertEqual([r["due"] for r in rows], ["2026-10-10", ""])
            self.assertTrue(all(r["url"] == "" for r in rows))
            self.assertEqual(calls, [(fake_key, 0), (fake_key, 0), (fake_key, 4)])
            self.assertEqual(llm.call_args_list[0].args[2], agent.PLAN)
            self.assertNotIn("исключить", str(llm.call_args_list))
            await agent.collect("discover", DEMO, p, {}, tracked, [], ["исключить"])
            self.assertIn(
                "Сдать отчёт", (p / "discovery-evidence-demo.csv").read_text(encoding="utf-8-sig")
            )
            await agent.collect("discover", DEMO, p, {}, tracked, [fake_key], ["исключить"])
            self.assertNotIn(
                "Сдать отчёт", (p / "discovery-evidence-demo.csv").read_text(encoding="utf-8-sig")
            )

    async def test_model_cannot_invoke_another_tool_or_invent_source(self):
        listing = {"-202": ("Демо", "group", agent.DEMO_NOW)}
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(agent, "server", fake_server),
            patch.object(agent, "catalog", return_value=listing),
            patch.object(agent, "read_page") as read,
            patch.object(agent, "model_json", return_value={"tool": "send_message", "groups": ["g1"]}),
        ):
            with self.assertRaises(ValueError):
                await agent.collect("discover", DEMO, Path(d), {}, {}, [], [])
            read.assert_not_called()
        for bad in ({"id": 4, "quote": "invented"}, {"id": 5, "quote": "actual"}):
            with self.assertRaises(ValueError):
                agent.verified(bad, [{"id": 4, "text": "actual"}])

    async def test_scope_revoked_during_model_turn_aborts_before_history(self):
        now = datetime.now(MOSCOW)
        key = "-1000000000303"
        listing = {key: ("Тест", "group", now)}
        conf = {"agent": {"provider": "codex", "mode": "cloud"}, "mcp": {"demo": False}}
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(agent, "server", fake_server),
            patch.object(agent, "catalog", return_value=listing),
            patch.object(agent, "read_page") as read,
        ):
            private = Path(d)
            consent = private / "telegram-scopes.toml"
            consent.write_text(scope_toml(now, key), encoding="utf-8")

            def revoke(*args):
                consent.write_text(scope_toml(now, key, "revoked"), encoding="utf-8")
                return {"tool": "read_samples", "groups": ["g1"]}

            with patch.object(agent, "model_json", side_effect=revoke), self.assertRaises(ValueError):
                await agent.collect("discover", conf, private, {"Тест": []}, {}, [], [])
            read.assert_not_called()

    async def test_local_only_ids_titles_and_denied_chat_are_not_sampled_or_transmitted(self):
        now = datetime.now(MOSCOW)
        ids = ["-1000000000001", "-1000000000002", "-1000000000003"]
        conf = {
            "mcp": {},
            "agent": {"provider": "codex", "mode": "cloud"},
            "local": {"chat_titles": {"Только на ноутбуке": "Курс"}, "chat_ids": {ids[1]: "Курс"}},
        }
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(agent, "server", fake_server),
            patch.object(agent, "catalog", return_value={ids[0]: ("Только на ноутбуке", "group", now)}),
            patch.object(agent, "read_page") as read,
            patch.object(agent, "model_json") as llm,
        ):
            p = Path(d)
            (p / "telegram-scopes.toml").write_text("\n".join(scope_toml(now, k) for k in ids))
            self.assertEqual(await agent.collect("discover", conf, p, {}, {}, [ids[2]], []), ({}, []))
            read.assert_not_called()
            llm.assert_not_called()

    async def test_author_filter_and_contact_redaction_before_provider(self):
        now = datetime.now(MOSCOW)
        key = "-1000000000001"
        msg = {
            "id": 4,
            "date": (now - timedelta(hours=1)).isoformat(),
            "chat_id": int(key),
            "sender_id": 42,
            "text": "Сдать отчёт до 12.11.2026. Почта: test@example.org",
        }
        response = {"items": [msg], "has_more": False}
        with tempfile.TemporaryDirectory() as d, patch.object(agent, "page", return_value=response):
            p = Path(d)
            (p / "telegram-scopes.toml").write_text(scope_toml(now, key))
            scope = agent.Scope({"mcp": {}, "agent": {"provider": "codex"}}, p)
            window = scope.permitted()[key]
            messages, _ = await agent.read_page(None, scope, key, window, 0, 3)
            self.assertEqual(messages, [{"id": 4, "text": "Сдать отчёт до 12.11.2026."}])
            msg["sender_id"] = 999
            with self.assertRaises(ValueError):
                await agent.read_page(None, scope, key, window, 0, 3)

    async def test_metadata_selects_most_recent_after_pagination_and_drops_text(self):
        d = lambda i, t: {
            "id": i,
            "kind": "group",
            "title": "Курс",
            "latest": {"chat_id": i, "date": t, "text": "private preview"},
        }
        a = {"items": [d(-30, "2026-03-01T12:00:00Z")], "has_more": True, "next_cursor": "opaque"}
        b = {"items": [d(-20, "2026-08-01T12:00:00Z")], "has_more": False}
        with patch.object(telegram_stdio, "page", side_effect=[a, b]):
            result = await telegram_stdio.catalog(None, False, 1, 1, lambda: None)
        self.assertEqual(list(result), ["-20"])
        self.assertNotIn("preview", str(result))
        self.assertEqual(agent.limits({"scan": {"max_dialogs": 350}})[1], 350)

    async def test_expired_upstream_auth_only_returns_safe_login_hint(self):
        err = SimpleNamespace(
            isError=True,
            content=[SimpleNamespace(type="text", text="Telegram session expired: private-sentinel")],
        )
        session = SimpleNamespace(call_tool=AsyncMock(return_value=err))
        with self.assertRaisesRegex(ValueError, "digest.py telegram-login") as caught:
            await telegram_stdio.page(session, "list_dialogs", {}, False)
        self.assertNotIn("private-sentinel", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
