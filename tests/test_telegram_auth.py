"""Границы авторизации проверяются на вымышленных .env без сети и реальной сессии."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import digest
import study_agent
import telegram_stdio
from agent_code.runtime import load_environment


class AuthEnvironmentTests(unittest.TestCase):
    def test_os_cannot_widen_file_allowlist_or_clear_file_denials(self):
        values = {
            "TELEGRAM_ALLOWED_CHAT_IDS": "-1,-2",
            "TELEGRAM_DENIED_CHAT_IDS": "-2",
        }
        with patch.dict(
            os.environ,
            {"TELEGRAM_ALLOWED_CHAT_IDS": "*", "TELEGRAM_DENIED_CHAT_IDS": ""},
            clear=True,
        ):
            env = telegram_stdio.environment(values)
        telegram_stdio.constrain_sources(env, values, {-1, -2, -3})
        self.assertEqual(env["TELEGRAM_ALLOWED_CHAT_IDS"], "-1,-2")
        self.assertEqual(env["TELEGRAM_DENIED_CHAT_IDS"], "-2")
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": ""}, clear=True):
            env = telegram_stdio.environment(values)
        telegram_stdio.constrain_sources(env, values, None)
        self.assertEqual(env["TELEGRAM_ALLOWED_CHAT_IDS"], "")

    def test_literal_2fa_password_and_os_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Пробелы внутри кавычек и ${...} — часть пароля, не ссылка на окружение.
            (root / ".env").write_text(
                "TELEGRAM_PHONE=+79990000000\nTELEGRAM_2FA_PASSWORD='  literal${HOME} # password  '\n",
                encoding="utf-8-sig",
            )
            with patch.dict(os.environ, {"HOME": "unexpected-expansion"}, clear=True):
                added = load_environment(root)
                self.assertEqual(
                    os.environ["TELEGRAM_2FA_PASSWORD"], "  literal${HOME} # password  "
                )
                self.assertEqual(os.environ["TELEGRAM_PHONE"], "+79990000000")
                self.assertNotIn("literal", repr(added))
            with patch.dict(
                os.environ, {"TELEGRAM_2FA_PASSWORD": "  OS${VALUE}  "}, clear=True
            ):
                load_environment(root)
                self.assertEqual(os.environ["TELEGRAM_2FA_PASSWORD"], "  OS${VALUE}  ")

    def test_model_never_receives_telegram_auth_secrets_for_both_providers(self):
        secrets = {
            "TELEGRAM_PHONE": "+79990000000",
            "TELEGRAM_2FA_PASSWORD": "literal${HOME} 2FA-SENTINEL",
            "TELEGRAM_API_HASH": "API-SENTINEL",
            "DIGEST_SMTP_PASSWORD": "SMTP-SENTINEL",
        }
        for provider in ("codex", "claude"):
            auth = (
                b"Logged in using ChatGPT"
                if provider == "codex"
                else b'{"authMethod":"claude.ai"}'
            )
            outputs = [
                subprocess.CompletedProcess([], 0, auth, b""),
                subprocess.CompletedProcess([], 0, b'{"ok":true}', b""),
            ]
            with self.subTest(
                provider=provider
            ), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with patch.dict(os.environ, secrets), patch.object(
                    study_agent.shutil, "which", return_value=provider
                ), patch.object(
                    study_agent.subprocess, "run", side_effect=outputs
                ) as calls:
                    result = study_agent.model_json(
                        provider,
                        root / "private",
                        "Read this public example.",
                        {"example": "synthetic"},
                    )
                self.assertEqual(result, {"ok": True})
                for call in calls.call_args_list:
                    self.assertTrue(set(secrets).isdisjoint(call.kwargs["env"]))
                    for value in secrets.values():
                        self.assertNotIn(value, repr(call.args) + repr(call.kwargs))
                    self.assertFalse(Path(call.kwargs["cwd"]).is_relative_to(root))

    def test_executable_prefers_current_venv_without_overriding_explicit_path(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            suffix = ".exe" if os.name == "nt" else ""
            adjacent = directory / ("telegram-mcp" + suffix)
            adjacent.touch()
            with patch.object(
                telegram_stdio.sys, "executable", str(directory / ("python" + suffix))
            ), patch.object(
                telegram_stdio.shutil,
                "which",
                return_value=str(directory / "explicit-mcp"),
            ) as which:
                self.assertEqual(
                    telegram_stdio.executable("telegram-mcp"), str(adjacent.resolve())
                )
                which.assert_not_called()
                self.assertEqual(
                    telegram_stdio.executable("chosen-mcp"),
                    str((directory / "explicit-mcp").resolve()),
                )
                which.assert_called_once_with("chosen-mcp")


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / "external.env"
        self.env_file.write_text(
            "TELEGRAM_API_ID=42\nTELEGRAM_API_HASH=synthetic\nTELEGRAM_PHONE=+79990000000\nTELEGRAM_2FA_PASSWORD='literal${HOME} 2FA-SENTINEL'\nTELEGRAM_SESSION_DIR='/private/synthetic session'\n",
            encoding="utf-8-sig",
        )
        self.cfg = {
            "mcp": {"env_file": str(self.env_file), "executable": "telegram-mcp"}
        }

    def invoke(self, results):
        output = io.StringIO()
        with patch.dict(
            os.environ, {"DIGEST_SMTP_PASSWORD": "SMTP-SENTINEL"}, clear=True
        ), patch.object(
            telegram_stdio, "executable", return_value="synthetic-mcp"
        ), patch.object(
            telegram_stdio.subprocess, "run", side_effect=results
        ) as call, contextlib.redirect_stdout(
            output
        ), contextlib.redirect_stderr(
            output
        ):
            telegram_stdio.authorize(self.cfg, self.root / "private")
        return call, output.getvalue()

    def test_protect_then_auth_use_chosen_file_and_no_secrets_in_arguments(self):
        ok = subprocess.CompletedProcess([], 0, b"", b"")
        call, output = self.invoke([ok, ok])
        self.assertEqual(call.call_count, 2)
        protect, auth = call.call_args_list
        self.assertEqual(
            protect.args[0],
            ["synthetic-mcp", "--env-file", str(self.env_file), "protect-env"],
        )
        self.assertEqual(auth.args[0][-1], "auth")
        self.assertEqual(
            auth.kwargs["env"]["TELEGRAM_2FA_PASSWORD"], "literal${HOME} 2FA-SENTINEL"
        )
        self.assertEqual(
            auth.kwargs["env"]["TELEGRAM_SESSION_DIR"], "/private/synthetic session"
        )
        self.assertNotIn("TELEGRAM_2FA_PASSWORD", protect.kwargs["env"])
        self.assertNotIn("DIGEST_SMTP_PASSWORD", auth.kwargs["env"])
        self.assertNotIn("2FA-SENTINEL", repr(auth.args) + output)
        self.assertNotIn(
            "stdin", auth.kwargs
        )  # Владелец вводит OTP в настоящем терминале.
        self.assertNotIn("input", auth.kwargs)

    def test_valid_session_check_is_delegated_even_without_tty(self):
        ok = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(sys.stdin, "isatty", return_value=False):
            call, _ = self.invoke([ok, ok])
        self.assertEqual(call.call_args.args[0][-1], "auth")

    def test_protect_failure_does_not_attempt_login_or_echo_output(self):
        failure = subprocess.CompletedProcess([], 1, b"2FA-SENTINEL", b"SMTP-SENTINEL")
        with patch.object(
            telegram_stdio, "executable", return_value="synthetic-mcp"
        ), patch.object(telegram_stdio.subprocess, "run", return_value=failure) as call:
            with self.assertRaises(ValueError) as caught:
                telegram_stdio.authorize(self.cfg, self.root)
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("SENTINEL", str(caught.exception))

    def test_failure_and_timeout_are_generic_cancel_preserves_130(self):
        ok = subprocess.CompletedProcess([], 0, b"", b"")
        failure = subprocess.CompletedProcess([], 1, b"2FA-SENTINEL", b"2FA-SENTINEL")
        for result in (failure, subprocess.TimeoutExpired("hidden-2FA-SENTINEL", 600)):
            with self.subTest(result=type(result).__name__), self.assertRaises(
                ValueError
            ) as caught:
                self.invoke([ok, result])
            self.assertNotIn("SENTINEL", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.invoke([ok, subprocess.CompletedProcess([], 130)])
        self.assertEqual(caught.exception.code, 130)
        with self.assertRaises(SystemExit) as caught:
            self.invoke([ok, KeyboardInterrupt()])
        self.assertEqual(caught.exception.code, 130)

    def test_missing_configured_file_stops_before_subprocess(self):
        self.cfg["mcp"]["env_file"] = str(self.root / "missing.env")
        with patch.object(telegram_stdio.subprocess, "run") as call, self.assertRaises(
            ValueError
        ):
            telegram_stdio.authorize(self.cfg, self.root)
        call.assert_not_called()

    def test_digest_routes_login_to_helper_without_reading_a_real_env(self):
        with patch.object(sys, "argv", ["digest.py", "telegram-login"]), patch(
            "agent_code.runtime.load_environment"
        ) as load, patch.object(
            digest, "settings", return_value=self.cfg
        ), patch.object(
            telegram_stdio, "authorize"
        ) as login:
            digest.main()
        login.assert_called_once_with(self.cfg, digest.PRIVATE)
        load.assert_called_once_with(digest.ROOT, include_telegram=False)

    def test_explicit_external_file_wins_over_root_file_but_true_os_values_win(self):
        # Ни A, ни B не содержат настоящих данных; main читает только временный ROOT.
        (self.root / ".env").write_text(
            "TELEGRAM_API_HASH=root-A\nTELEGRAM_PHONE=root-phone-A\nTELEGRAM_SESSION_DIR='/root/session-A'\nTELEGRAM_ALLOWED_CHAT_IDS=\nDIGEST_SMTP_PASSWORD=root-mail\n",
            encoding="utf-8",
        )
        cases = [
            ({}, ("synthetic", "+79990000000", "/private/synthetic session")),
            (
                {
                    "TELEGRAM_API_HASH": "OS-C",
                    "TELEGRAM_PHONE": "OS-phone-C",
                    "TELEGRAM_SESSION_DIR": "/os/session-C",
                },
                ("OS-C", "OS-phone-C", "/os/session-C"),
            ),
        ]
        for original, expected in cases:
            with self.subTest(original=bool(original)), patch.dict(
                os.environ, original, clear=True
            ), patch.object(digest, "ROOT", self.root), patch.object(
                digest, "PRIVATE", self.root / "private"
            ), patch.object(
                sys, "argv", ["digest.py", "telegram-login"]
            ), patch.object(
                digest, "settings", return_value=self.cfg
            ), patch.object(
                telegram_stdio, "executable", return_value="synthetic-mcp"
            ), patch.object(
                telegram_stdio.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as calls:
                digest.main()
                env = calls.call_args.kwargs["env"]
                self.assertEqual(
                    tuple(
                        env[key]
                        for key in (
                            "TELEGRAM_API_HASH",
                            "TELEGRAM_PHONE",
                            "TELEGRAM_SESSION_DIR",
                        )
                    ),
                    expected,
                )
                self.assertEqual(os.environ["DIGEST_SMTP_PASSWORD"], "root-mail")
                self.assertNotIn("TELEGRAM_ALLOWED_CHAT_IDS", os.environ)


class ReaderEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_root_file_keeps_empty_allowlist_deny_all(self):
        captured = {}

        @contextlib.asynccontextmanager
        async def stdio(params, errlog):
            captured.update(params.env)
            yield None, None

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.initialize.return_value = SimpleNamespace(serverInfo="synthetic")
        session.list_tools.return_value = SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=name, annotations=SimpleNamespace(readOnlyHint=True)
                )
                for name in ("list_dialogs", "messages_between")
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / ".env"
            source.write_text(
                "TELEGRAM_API_ID=42\nTELEGRAM_API_HASH=root-A\nTELEGRAM_ALLOWED_CHAT_IDS=\n",
                encoding="utf-8",
            )
            cfg = {"mcp": {"env_file": str(source), "executable": "telegram-mcp"}}
            with patch.dict(
                os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": "*"}, clear=True
            ), patch.object(
                telegram_stdio, "executable", return_value="synthetic-mcp"
            ), patch(
                "mcp.client.stdio.stdio_client", stdio
            ), patch(
                "mcp.ClientSession", return_value=session
            ):
                load_environment(root, include_telegram=False)
                self.assertNotIn("TELEGRAM_API_HASH", os.environ)
                async with telegram_stdio.server(cfg, root, allowed={-1, -2}):
                    self.assertEqual(captured["TELEGRAM_API_HASH"], "root-A")
                    self.assertEqual(captured["TELEGRAM_ALLOWED_CHAT_IDS"], "")

    async def test_reader_has_effective_session_and_acl_but_no_login_or_other_secrets(
        self,
    ):
        captured = {}

        @contextlib.asynccontextmanager
        async def stdio(params, errlog):
            captured["env"] = params.env
            captured["args"] = params.args
            captured["file"] = Path(params.args[1])
            captured["file_bytes"] = captured["file"].read_bytes()
            yield None, None

        class Session:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def initialize(self):
                return SimpleNamespace(serverInfo="synthetic")

            async def list_tools(self):
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name=name, annotations=SimpleNamespace(readOnlyHint=True)
                        )
                        for name in ("list_dialogs", "messages_between")
                    ]
                )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "configured.env"
            source.write_text(
                "TELEGRAM_API_ID=42\nTELEGRAM_API_HASH=file-hash\nTELEGRAM_PHONE=FILE-PHONE\nTELEGRAM_2FA_PASSWORD=FILE-2FA\nDIGEST_SMTP_PASSWORD=SMTP-SENTINEL\nTELEGRAM_ALLOWED_CHAT_IDS=-1,-2\nTELEGRAM_DENIED_CHAT_IDS=-9\nTELEGRAM_SESSION_DIR='/private/synthetic session'\n",
                encoding="utf-8-sig",
            )
            cfg = {"mcp": {"env_file": str(source), "executable": "telegram-mcp"}}
            original_env = {
                "TELEGRAM_API_HASH": "os-hash",
                "TELEGRAM_ALLOWED_CHAT_IDS": "-2,-3",
                "TELEGRAM_DENIED_CHAT_IDS": "-8",
                "TELEGRAM_PHONE": "OS-PHONE",
                "TELEGRAM_2FA_PASSWORD": "OS-2FA",
                "DIGEST_SMTP_PASSWORD": "OS-SMTP",
            }
            with patch.dict(os.environ, original_env, clear=True), patch.object(
                telegram_stdio, "executable", return_value="synthetic-mcp"
            ), patch("mcp.client.stdio.stdio_client", stdio), patch(
                "mcp.ClientSession", Session
            ):
                async with telegram_stdio.server(cfg, root, allowed={-2, -4}):
                    self.assertEqual(captured["env"]["TELEGRAM_API_HASH"], "os-hash")
                    self.assertEqual(captured["env"]["TELEGRAM_API_ID"], "42")
                    self.assertEqual(
                        captured["env"]["TELEGRAM_SESSION_DIR"],
                        "/private/synthetic session",
                    )
                    self.assertEqual(captured["env"]["TELEGRAM_ALLOWED_CHAT_IDS"], "-2")
                    self.assertEqual(
                        captured["env"]["TELEGRAM_DENIED_CHAT_IDS"], "-8,-9"
                    )
                    for key in (
                        "TELEGRAM_PHONE",
                        "TELEGRAM_2FA_PASSWORD",
                        "DIGEST_SMTP_PASSWORD",
                    ):
                        self.assertNotIn(key, captured["env"])
                    self.assertNotEqual(captured["file"], source)
                    self.assertEqual(captured["file_bytes"], b"")
                    self.assertNotIn("SENTINEL", repr(captured))
            self.assertFalse(
                captured["file"].exists()
            )  # Временный файл не оставляется на диске.


if __name__ == "__main__":
    unittest.main()
