"""Проверки сервиса используют временный проект и подменяют регистрацию в ОС."""

import contextlib  # Перехватываем вывод, чтобы проверить отсутствие секретов.
import io  # Тестовый вывод остаётся в памяти.
import json  # Проверяем реальные сохранённые состояния.
import os  # Изолируем окружение каждого теста.
import plistlib  # Проверяем сгенерированный plist без установки LaunchAgent.
import subprocess  # Создаём только собственные краткоживущие тестовые процессы.
import sys  # Используем интерпретатор тестов для синтетического digest.py.
import tempfile  # Ни один тест не изменяет private настоящего пользователя.
import threading  # Имитируем запрос остановки во время работающего дочернего процесса.
import unittest  # Сохраняем общий для репозитория способ запуска тестов.
import xml.etree.ElementTree as ET  # Читаем XML задачи как структурированные данные.
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from filelock import FileLock

from agent_code import runtime, service


class TemporaryProject(unittest.TestCase):
    """Общий временный каталог не содержит настоящих учётных данных или сетевых вызовов."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="учебный сервис ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "private").mkdir()
        (self.root / "private/study.toml").write_text(
            "[mcp]\nenabled=true\n", encoding="utf-8"
        )
        (self.root / "digest.py").write_text("pass\n", encoding="utf-8")


class EnvironmentTests(TemporaryProject):
    def load(self, text, original=None):
        (self.root / ".env").write_text(text, encoding="utf-8-sig")
        with patch.dict(os.environ, original or {}, clear=True):
            names = runtime.load_environment(self.root)
            return names, dict(os.environ)

    def test_existing_os_value_wins_and_empty_template_does_not_mask_it(self):
        names, env = self.load(
            "DIGEST_SMTP_PASSWORD=file-value\nTELEGRAM_API_ID=\n",
            {"DIGEST_SMTP_PASSWORD": "os-value"},
        )
        self.assertEqual(env["DIGEST_SMTP_PASSWORD"], "os-value")
        self.assertNotIn("TELEGRAM_API_ID", env)
        self.assertEqual(names, ())

    def test_values_are_literal_and_alias_preserves_legacy_environment(self):
        names, env = self.load(
            'DIGEST_SMTP_PASSWORD="a\'\\\\b${HOME}"\nLMS_TOKEN=new\n',
            {"HSE_LMS_TOKEN": "old"},
        )
        self.assertEqual(env["DIGEST_SMTP_PASSWORD"], "a'\\b${HOME}")
        self.assertEqual(env["HSE_LMS_TOKEN"], "old")
        self.assertEqual(set(names), {"DIGEST_SMTP_PASSWORD", "LMS_TOKEN"})
        _, env = self.load("LMS_TOKEN=new\nLMS_URL=https://edu.hse.ru\n")
        self.assertEqual(env["HSE_LMS_TOKEN"], "new")

    def test_unsupported_settings_are_rejected_before_any_mutation(self):
        (self.root / ".env").write_text(
            "LMS_TOKEN=hidden-value\nPATH=evil\n", encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                runtime.load_environment(self.root)
            self.assertEqual(dict(os.environ), {})
            self.assertNotIn("hidden-value", str(caught.exception))

    def test_foreign_lms_and_write_mode_are_rejected_without_loading_secret(self):
        for line in ("LMS_URL=https://example.invalid", "TELEGRAM_WRITE_ENABLED=true"):
            with self.subTest(line=line), patch.dict(os.environ, {}, clear=True):
                (self.root / ".env").write_text(
                    "LMS_TOKEN=hidden-value\n" + line, encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    runtime.load_environment(self.root)
                self.assertNotIn("LMS_TOKEN", os.environ)

    def test_mcp_acl_and_session_path_are_preserved(self):
        _, env = self.load(
            "TELEGRAM_ALLOWED_CHAT_IDS=-1,-2\nTELEGRAM_DENIED_CHAT_IDS=-2\nTELEGRAM_SESSION_DIR='/Users/test/session data'\nTELEGRAM_WRITE_ENABLED=false"
        )
        self.assertEqual(env["TELEGRAM_ALLOWED_CHAT_IDS"], "-1,-2")
        self.assertEqual(env["TELEGRAM_DENIED_CHAT_IDS"], "-2")
        self.assertEqual(env["TELEGRAM_SESSION_DIR"], "/Users/test/session data")

    def test_multiline_secret_is_rejected_without_echo(self):
        with self.assertRaises(ValueError) as caught:
            self.load('DIGEST_SMTP_PASSWORD="hidden\nvalue"')
        self.assertNotIn("hidden", str(caught.exception))

    def test_no_parent_dotenv_search(self):
        (self.root / ".env").write_text("LMS_TOKEN=parent-value", encoding="utf-8")
        child = self.root / "child"
        child.mkdir()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime.load_environment(child), ())


class ServiceTests(TemporaryProject):
    def test_interpreter_is_project_venv_and_background_windows_uses_pythonw(self):
        for platform, relative in (
            ("win32", ".venv/Scripts/python.exe"),
            ("darwin", ".venv/bin/python"),
        ):
            with self.subTest(platform=platform), patch.object(
                service.sys, "platform", platform
            ):
                with self.assertRaises(ValueError):
                    service._python(self.root)
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                self.assertEqual(service._python(self.root), path)
                if platform == "win32":
                    hidden = path.with_name("pythonw.exe")
                    hidden.touch()
                    self.assertEqual(
                        service._python(self.root, background=True), hidden
                    )

    def test_os_command_failure_is_sanitized(self):
        with patch.object(service.subprocess, "run", side_effect=OSError("SECRET")):
            with self.assertRaises(ValueError) as caught:
                service._system(["unused-test-command"])
        self.assertNotIn("SECRET", str(caught.exception))

    def test_child_launch_failure_is_sanitized(self):
        with patch.object(
            service, "_python", return_value=Path(sys.executable)
        ), patch.object(service.subprocess, "Popen", side_effect=OSError("SECRET")):
            result = service._execute(
                self.root, "run", 10, threading.Event(), lambda: None
            )
        self.assertEqual(result["result"], "start_failed")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unexpected_command_cannot_be_run_by_service(self):
        with self.assertRaises(ValueError):
            service._execute(self.root, "send", 10, threading.Event(), lambda: None)

    def test_mac_child_group_gets_term_then_kill_after_grace(self):
        child = unittest.mock.Mock(pid=43210)
        child.wait.side_effect = [subprocess.TimeoutExpired("test", 3), 0]
        with patch.object(service.sys, "platform", "darwin"), patch.object(
            service.os, "killpg", create=True
        ) as kill, patch.object(service.signal, "SIGKILL", 9, create=True):
            service._stop_child(child)
            self.assertEqual(
                [c.args[1] for c in kill.call_args_list], [service.signal.SIGTERM, 9]
            )

    def test_atomic_failure_keeps_prior_complete_file(self):
        path = self.root / "private/snapshot.json"
        path.write_text("old", encoding="utf-8")
        with patch.object(service.os, "replace", side_effect=OSError("test")):
            with self.assertRaises(OSError):
                service._atomic(path, b"new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(
            {p.name for p in path.parent.iterdir()}, {"snapshot.json", "study.toml"}
        )

    def test_cli_success_status_install_and_remove_use_callable_api(self):
        for command, target, result in (
            ("status", "status", {"running": False}),
            ("install", "install", "installed"),
            ("remove", "remove", "removed"),
            ("run", "run", {"last_scan": {"result": "ok"}}),
        ):
            with self.subTest(command=command), patch.object(
                service.sys, "argv", ["service", command, "--once"]
            ), patch.object(
                service, target, return_value=result
            ) as function, contextlib.redirect_stdout(
                io.StringIO()
            ):
                service.main()
                function.assert_called_once()

    def test_cli_reports_failed_once_as_failure_without_secret(self):
        for failure in ({"last_scan": {"result": "timeout"}}, RuntimeError("SECRET")):
            with self.subTest(failure=type(failure).__name__), patch.object(
                service.sys, "argv", ["service", "run", "--once"]
            ):
                options = (
                    {"side_effect": failure}
                    if isinstance(failure, Exception)
                    else {"return_value": failure}
                )
                with patch.object(service, "run", **options), self.assertRaises(
                    SystemExit
                ) as caught:
                    service.main()
                self.assertNotIn("SECRET", str(caught.exception))

    def test_defaults_and_explicit_live_mode(self):
        self.assertEqual(service._options(self.root), service.Options())
        (self.root / "private/study.toml").write_text(
            "[mcp]\ndemo=true", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            service.run(self.root, once=True)

    def test_intervals_reject_booleans_and_out_of_bounds(self):
        for value in ("true", "0", "99999999"):
            with self.subTest(value=value):
                (self.root / "private/study.toml").write_text(
                    "[mcp]\n[service]\nscan_interval_seconds=" + value, encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    service._options(self.root)

    def test_once_is_real_run_without_delivery_and_has_persistent_health(self):
        with patch.object(service, "_python", return_value=Path(sys.executable)):
            health = service.run(self.root, once=True)
        self.assertEqual(health["state"], "stopped")
        self.assertEqual(health["last_scan"]["result"], "ok")
        self.assertNotIn("last_tick", health)
        saved = json.loads(
            (self.root / "private/service-status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["last_success_scan"], health["last_scan"]["finished_at"])
        self.assertFalse(service.status(self.root)["running"])

    def test_single_instance_uses_process_lock(self):
        with FileLock(str(self.root / "private/service.lock"), timeout=0):
            with patch.object(service, "_python", return_value=Path(sys.executable)):
                with self.assertRaisesRegex(ValueError, "уже работает"):
                    service.run(self.root, once=True)

    def test_failure_keeps_previous_success_and_discards_child_output(self):
        old = "2026-01-02T03:04:05+00:00"
        (self.root / "private/service-status.json").write_text(
            json.dumps({"last_success_scan": old}), encoding="utf-8"
        )
        (self.root / "digest.py").write_text(
            "import sys\nprint('SENSITIVE stdout')\nprint('SENSITIVE stderr', file=sys.stderr)\nsys.exit(7)\n",
            encoding="utf-8",
        )
        buffer = io.StringIO()
        with patch.object(
            service, "_python", return_value=Path(sys.executable)
        ), contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            result = service.run(self.root, once=True)
        self.assertEqual(result["last_scan"]["result"], "failed")
        self.assertEqual(result["last_scan"]["return_code"], 7)
        self.assertEqual(result["last_success_scan"], old)
        self.assertNotIn("SENSITIVE", buffer.getvalue() + json.dumps(result))

    def test_timeout_terminates_actual_test_child(self):
        (self.root / "digest.py").write_text(
            "import time\ntime.sleep(30)\n", encoding="utf-8"
        )
        with patch.object(service, "_python", return_value=Path(sys.executable)):
            result = service._execute(
                self.root, "run", 1, threading.Event(), lambda: None
            )
        self.assertEqual(result["result"], "timeout")
        self.assertIsNotNone(result["return_code"])

    def test_stop_request_interrupts_waiting_child(self):
        (self.root / "digest.py").write_text(
            "import time\ntime.sleep(30)\n", encoding="utf-8"
        )
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        try:
            with patch.object(service, "_python", return_value=Path(sys.executable)):
                result = service._execute(self.root, "run", 10, stop, lambda: None)
        finally:
            timer.cancel()
        self.assertEqual(result["result"], "stopped")

    def test_loop_checks_delivery_independently_from_scan(self):
        calls = []
        now = [1.0]

        class ClockEvent:
            stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, seconds):
                now[0] += seconds

        event = ClockEvent()

        def execute(root, command, timeout, stop, pulse):
            calls.append(command)
            if calls.count("tick") == 3:
                stop.set()
            return {
                "result": "ok",
                "started_at": service._stamp(),
                "finished_at": service._stamp(),
                "return_code": 0,
            }

        with patch.object(
            service, "_python", return_value=Path(sys.executable)
        ), patch.object(
            service, "_options", return_value=service.Options(10, 1, 10)
        ), patch.object(
            service, "_execute", side_effect=execute
        ), patch.object(
            service.time, "monotonic", side_effect=lambda: now[0]
        ), patch.object(
            service.threading, "Event", return_value=event
        ):
            result = service.run(self.root)
        self.assertEqual(calls, ["run", "tick", "tick", "tick"])
        self.assertEqual(result["state"], "stopped")

    def test_unexpected_failure_releases_lock_and_does_not_echo_exception(self):
        with patch.object(
            service, "_python", return_value=Path(sys.executable)
        ), patch.object(service, "_execute", side_effect=RuntimeError("SECRET")):
            with self.assertRaises(ValueError) as caught:
                service.run(self.root, once=True)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(service.status(self.root)["state"], "failed")
        with FileLock(str(self.root / "private/service.lock"), timeout=0):
            pass

    def test_unknown_or_injected_health_is_not_returned(self):
        (self.root / "private/service-status.json").write_text(
            json.dumps(
                {
                    "token": "SECRET",
                    "pid": 999999,
                    "state": "running",
                    "last_scan": {"result": "ok", "stderr": "SECRET"},
                }
            ),
            encoding="utf-8",
        )
        result = service.status(self.root)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(result["running"])
        self.assertEqual(result["last_scan"], {"result": "ok"})

    def test_lock_and_fresh_heartbeat_show_responsiveness(self):
        (self.root / "private/service-status.json").write_text(
            json.dumps(
                {
                    "heartbeat": datetime.now(timezone.utc).isoformat(),
                    "state": "running",
                }
            ),
            encoding="utf-8",
        )
        with FileLock(str(self.root / "private/service.lock"), timeout=0):
            result = service.status(self.root)
        self.assertTrue(result["running"])
        self.assertTrue(result["responsive"])

    def test_execute_uses_no_shell_and_hides_output(self):
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0
        with patch.object(
            service, "_python", return_value=Path(sys.executable)
        ), patch.object(service.subprocess, "Popen", return_value=process) as spawn:
            service._execute(self.root, "run", 10, threading.Event(), lambda: None)
        args, kwargs = spawn.call_args
        self.assertEqual(args[0][-1], "run")
        self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", kwargs)


class AutostartTests(TemporaryProject):
    sid = "S-1-5-21-123-456-789-1001"

    def test_windows_remove_requests_graceful_stop_and_preserves_data(self):
        raw = service._windows_xml(
            self.root, self.root / ".venv/Scripts/pythonw.exe", self.sid
        )

        def command(args, *, capture=False):
            return subprocess.CompletedProcess(
                args, 0, raw if "/Query" in args else b""
            )

        with patch.object(service.sys, "platform", "win32"), patch.object(
            service, "_windows_sid", return_value=self.sid
        ), patch.object(
            service, "_system", side_effect=command
        ) as system, patch.object(
            service,
            "status",
            side_effect=[
                {"running": True, "pid": 42},
                {"running": False},
                {"running": False},
            ],
        ):
            service.remove(self.root)
        self.assertTrue((self.root / "private/study.toml").exists())
        self.assertFalse((self.root / "private/service-stop.txt").exists())
        self.assertEqual(system.call_args.args[0][:2], ["schtasks", "/Delete"])

    def test_failed_windows_registration_or_start_is_not_success(self):
        for failed in ("/Create", "/Run"):

            def command(args, *, capture=False):
                return subprocess.CompletedProcess(
                    args, 1 if "/Query" in args or failed in args else 0, b""
                )

            with self.subTest(failed=failed), patch.object(
                service.sys, "platform", "win32"
            ), patch.object(
                service, "_python", return_value=Path(sys.executable)
            ), patch.object(
                service, "_windows_sid", return_value=self.sid
            ), patch.object(
                service, "_system", side_effect=command
            ):
                with self.assertRaises(ValueError):
                    service.install(self.root)

    def test_mac_invalid_plist_never_unloads_other_service(self):
        home = self.root / "home"
        destination = (
            home / "Library/LaunchAgents" / (service._label(self.root) + ".plist")
        )
        destination.parent.mkdir(parents=True)
        destination.write_text("invalid plist", encoding="utf-8")
        with patch.object(service.sys, "platform", "darwin"), patch.object(
            service.os, "getuid", return_value=501, create=True
        ), patch.object(service.Path, "home", return_value=home), patch.object(
            service, "_system", return_value=subprocess.CompletedProcess([], 0)
        ) as system:
            with self.assertRaises(ValueError):
                service.remove(self.root)
        self.assertFalse(any("bootout" in c.args[0] for c in system.call_args_list))
        self.assertTrue(destination.exists())

    def test_failed_mac_validation_or_bootstrap_is_not_success(self):
        for failed in ("plutil", "bootstrap"):

            def command(args, *, capture=False):
                querying_service = (
                    args[:2] == ["launchctl", "print"] and args[-1].count("/") > 1
                )
                return subprocess.CompletedProcess(
                    args, 1 if querying_service or failed in args else 0, b""
                )

            with self.subTest(failed=failed), patch.object(
                service.sys, "platform", "darwin"
            ), patch.object(
                service.os, "getuid", return_value=501, create=True
            ), patch.object(
                service.Path, "home", return_value=self.root / "home"
            ), patch.object(
                service, "_python", return_value=self.root / ".venv/bin/python"
            ), patch.object(
                service, "_system", side_effect=command
            ):
                with self.assertRaises(ValueError):
                    service.install(self.root)

    def test_windows_xml_is_per_user_without_admin_password_or_time_limit(self):
        raw = service._windows_xml(
            self.root, self.root / ".venv/Scripts/pythonw.exe", self.sid
        )
        tree = ET.fromstring(raw)

        def find(name):
            return tree.findtext(".//{" + service.TASK_NS + "}" + name)

        self.assertEqual(find("LogonType"), "InteractiveToken")
        self.assertEqual(find("RunLevel"), "LeastPrivilege")
        self.assertEqual(find("MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(find("ExecutionTimeLimit"), "PT0S")
        self.assertEqual(find("WorkingDirectory"), str(self.root))
        self.assertEqual(find("Arguments"), "-X utf8 -m agent_code.service run")
        self.assertNotIn("Password", raw.decode("utf-16"))
        self.assertTrue(service._owns_windows(raw, self.root, self.sid))
        self.assertFalse(service._owns_windows(raw, self.root / "other", self.sid))

    def test_mac_plist_has_correct_command_restart_and_no_secrets(self):
        with patch.dict(os.environ, {"DIGEST_SMTP_PASSWORD": "SECRET"}):
            raw = service._mac_plist(self.root, self.root / ".venv/bin/python")
        doc = plistlib.loads(raw)
        self.assertEqual(doc["ProgramArguments"][-2:], ["agent_code.service", "run"])
        self.assertEqual(doc["WorkingDirectory"], str(self.root))
        self.assertTrue(doc["KeepAlive"])
        self.assertEqual(doc["Umask"], 0o077)
        self.assertEqual(doc["StandardErrorPath"], "/dev/null")
        self.assertNotIn(b"SECRET", raw)

    def test_windows_install_calls_real_command_contract_without_registering_anything(
        self,
    ):
        commands = []

        def command(args, *, capture=False):
            commands.append(args)
            if args[0] == "whoami":
                return subprocess.CompletedProcess(
                    args, 0, f'"machine\\student","{self.sid}"'.encode()
                )
            return subprocess.CompletedProcess(args, 1 if "/Query" in args else 0, b"")

        with patch.object(service.sys, "platform", "win32"), patch.object(
            service, "_python", return_value=self.root / ".venv/Scripts/pythonw.exe"
        ), patch.object(service, "_system", side_effect=command):
            service.install(self.root)
        self.assertEqual(
            commands[-1], ["schtasks", "/Run", "/TN", service._label(self.root)]
        )
        create = next(c for c in commands if "/Create" in c)
        self.assertNotIn("/F", create)
        self.assertTrue((self.root / "private/service-task.xml").is_file())
        self.assertFalse(any("/RU" in c or "/RP" in c or "sudo" in c for c in commands))

    def test_windows_foreign_task_is_never_changed_or_deleted(self):
        def command(args, *, capture=False):
            if args[0] == "whoami":
                return subprocess.CompletedProcess(
                    args, 0, f'"test","{self.sid}"'.encode()
                )
            return subprocess.CompletedProcess(args, 0, b"<Task />")

        with patch.object(service.sys, "platform", "win32"), patch.object(
            service, "_python", return_value=Path(sys.executable)
        ), patch.object(service, "_system", side_effect=command) as system:
            with self.assertRaises(ValueError):
                service.install(self.root)
            with self.assertRaises(ValueError):
                service.remove(self.root)
        self.assertFalse(
            any(
                "/Create" in call.args[0] or "/Delete" in call.args[0]
                for call in system.call_args_list
            )
        )

    def test_mac_install_and_remove_only_target_own_plist(self):
        home = self.root / "home"
        commands = []
        loaded = [False]

        def command(args, *, capture=False):
            commands.append(args)
            if args[:2] == ["launchctl", "print"] and args[-1].count("/") > 1:
                return subprocess.CompletedProcess(args, 0 if loaded[0] else 1, b"")
            if "bootstrap" in args:
                loaded[0] = True
            if "bootout" in args:
                loaded[0] = False
            return subprocess.CompletedProcess(args, 0, b"")

        with patch.object(service.sys, "platform", "darwin"), patch.object(
            service.os, "getuid", return_value=501, create=True
        ), patch.object(service.Path, "home", return_value=home), patch.object(
            service, "_python", return_value=self.root / ".venv/bin/python"
        ), patch.object(
            service, "_system", side_effect=command
        ):
            service.install(self.root)
            path = (
                home / "Library/LaunchAgents" / (service._label(self.root) + ".plist")
            )
            self.assertTrue(path.is_file())
            service.remove(self.root)
            self.assertFalse(path.exists())
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(path)], commands)
        self.assertIn(
            ["launchctl", "bootout", "gui/501/" + service._label(self.root)], commands
        )
        self.assertIn(["plutil", "-lint", str(path)], commands)

    def test_mac_no_gui_fails_before_writing_plist(self):
        with patch.object(service.sys, "platform", "darwin"), patch.object(
            service.os, "getuid", return_value=501, create=True
        ), patch.object(
            service.Path, "home", return_value=self.root / "home"
        ), patch.object(
            service, "_python", return_value=Path(sys.executable)
        ), patch.object(
            service, "_system", return_value=subprocess.CompletedProcess([], 1)
        ):
            with self.assertRaisesRegex(ValueError, "графический"):
                service.install(self.root)
        self.assertFalse((self.root / "home").exists())


if __name__ == "__main__":
    unittest.main()  # Никакая проверка не устанавливает реальную задачу или LaunchAgent.
