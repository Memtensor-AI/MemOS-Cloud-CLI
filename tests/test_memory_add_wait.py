"""Tests for the add-then-get race fix: --wait polling + list alias."""
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from rich.console import Console

from memos_cli import main as cli_main
from memos_cli.commands import memory, memory_cmd
from memos_cli.config import MemOSConfig, PlatformConfig
from memos_cli.output import format_add_result


def _make_config() -> MemOSConfig:
    config = MemOSConfig(
        platform=PlatformConfig(api_key="test-key", base_url="https://example.test/api")
    )
    config.defaults.user_id = "user_1"
    config.defaults.conversation_id = "conversation_1"
    return config


class FakeBackend:
    """Backend double that returns a running task_id and completes on the 3rd poll."""

    def __init__(self, statuses: list[str] | None = None, task_id: str = "task-42") -> None:
        self.task_id = task_id
        self.statuses = list(statuses) if statuses is not None else ["running", "running", "completed"]
        self.add_calls: list[dict] = []
        self.status_calls: list[str] = []

    def add_memory(self, messages, **kwargs):
        self.add_calls.append({"messages": messages, **kwargs})
        return {
            "code": 0,
            "message": "add accepted",
            "data": {"task_id": self.task_id, "status": "running"},
        }

    def get_status(self, task_id: str):
        self.status_calls.append(task_id)
        status = self.statuses.pop(0) if self.statuses else "completed"
        return {"code": 0, "data": {"status": status, "task_id": task_id}}


class TaskIdExtractionTests(unittest.TestCase):
    def test_extract_task_id_from_data_field(self) -> None:
        response = {"data": {"task_id": "abc123", "status": "running"}}
        self.assertEqual(memory_cmd._extract_task_id(response), "abc123")

    def test_extract_task_id_from_top_level_taskId(self) -> None:
        response = {"taskId": "xyz"}
        self.assertEqual(memory_cmd._extract_task_id(response), "xyz")

    def test_extract_task_id_missing(self) -> None:
        self.assertIsNone(memory_cmd._extract_task_id({"data": {}}))
        self.assertIsNone(memory_cmd._extract_task_id({}))
        self.assertIsNone(memory_cmd._extract_task_id(None))

    def test_extract_status_normalizes_case(self) -> None:
        self.assertEqual(
            memory_cmd._extract_status({"data": {"status": "RUNNING"}}), "running"
        )
        self.assertEqual(memory_cmd._extract_status({"status": "Completed"}), "completed")
        self.assertEqual(memory_cmd._extract_status({}), "")


class CmdAddWaitPollingTests(unittest.TestCase):
    def test_cmd_add_polls_status_until_completed(self) -> None:
        config = _make_config()
        backend = FakeBackend(statuses=["running", "completed"])
        with patch.object(memory_cmd, "_load_backend", return_value=(config, backend)):
            with patch.object(memory_cmd, "time") as time_mod:
                # Freeze time so wait_timeout never expires while we drain statuses.
                time_mod.time.side_effect = [0, 0, 0, 0, 0, 0, 0]
                time_mod.sleep.return_value = None
                memory_cmd.cmd_add(
                    message_text="hello world",
                    message_option=None,
                    user_id=None,
                    agent_id=None,
                    app_id=None,
                    conversation_id=None,
                    tags_json=None,
                    info_json=None,
                    allow_public=None,
                    allow_knowledgebase_ids=None,
                    async_mode=None,
                    output_format="json",
                    detail="simple",
                    wait=True,
                    wait_timeout=30.0,
                )

        # Server was polled at least once and ultimately observed completion.
        self.assertGreaterEqual(len(backend.status_calls), 1)
        self.assertEqual(backend.status_calls[0], backend.task_id)

    def test_cmd_add_no_wait_skips_polling(self) -> None:
        config = _make_config()
        backend = FakeBackend()
        with patch.object(memory_cmd, "_load_backend", return_value=(config, backend)):
            memory_cmd.cmd_add(
                message_text="hi",
                message_option=None,
                user_id=None,
                agent_id=None,
                app_id=None,
                conversation_id=None,
                tags_json=None,
                info_json=None,
                allow_public=None,
                allow_knowledgebase_ids=None,
                async_mode=None,
                output_format="json",
                detail="simple",
                wait=False,
                wait_timeout=30.0,
            )

        self.assertEqual(backend.status_calls, [])
        self.assertEqual(len(backend.add_calls), 1)

    def test_cmd_add_wait_timeout_stops_polling(self) -> None:
        config = _make_config()
        # Always report running so we only stop via timeout.
        backend = FakeBackend(statuses=["running"] * 20)
        with patch.object(memory_cmd, "_load_backend", return_value=(config, backend)):
            with patch.object(memory_cmd, "time") as time_mod:
                # time.time is called: 1) start_time in cmd_add, 2) once before loop
                #    in _poll_task_status to set deadline, 3) inside the loop for
                #    each iteration's timeout check. Advance the clock past
                #    the timeout on the second polling iteration.
                time_mod.time.side_effect = [0.0, 0.0, 0.0, 0.5, 999.0, 999.0]
                time_mod.sleep.return_value = None
                memory_cmd.cmd_add(
                    message_text="x",
                    message_option=None,
                    user_id=None,
                    agent_id=None,
                    app_id=None,
                    conversation_id=None,
                    tags_json=None,
                    info_json=None,
                    allow_public=None,
                    allow_knowledgebase_ids=None,
                    async_mode=None,
                    output_format="json",
                    detail="simple",
                    wait=True,
                    wait_timeout=1.0,
                )

        # We should have polled a bounded number of times (not infinitely).
        self.assertLessEqual(len(backend.status_calls), 5)
        self.assertGreaterEqual(len(backend.status_calls), 1)


class AddTyperEntrypointTests(unittest.TestCase):
    def test_typer_add_defaults_wait_true(self) -> None:
        with patch.object(memory, "cmd_add") as cmd_add:
            memory.add(
                "hello",
                message_option=None,
                user_id=None,
                output_format="json",
                wait=True,
                wait_timeout=30.0,
            )
        self.assertEqual(cmd_add.call_count, 1)
        kwargs = cmd_add.call_args.kwargs
        self.assertTrue(kwargs["wait"])
        self.assertEqual(kwargs["wait_timeout"], 30.0)

    def test_typer_add_passes_no_wait(self) -> None:
        with patch.object(memory, "cmd_add") as cmd_add:
            memory.add(
                "hi",
                message_option=None,
                user_id=None,
                output_format="json",
                wait=False,
                wait_timeout=5.0,
            )
        kwargs = cmd_add.call_args.kwargs
        self.assertFalse(kwargs["wait"])
        self.assertEqual(kwargs["wait_timeout"], 5.0)


class ListAliasTests(unittest.TestCase):
    @staticmethod
    def _resolved_name(cmd) -> str:
        return cmd.name or (cmd.callback.__name__ if cmd.callback else "")

    def test_list_command_registered(self) -> None:
        registered = {self._resolved_name(cmd) for cmd in cli_main.app.registered_commands}
        self.assertIn("list", registered)
        self.assertIn("get", registered)

    def test_list_command_delegates_to_same_callback_as_get(self) -> None:
        commands = {self._resolved_name(cmd): cmd for cmd in cli_main.app.registered_commands}
        self.assertIs(commands["list"].callback, commands["get"].callback)


class FormatAddResultOutputTests(unittest.TestCase):
    def _render(self, **kwargs) -> str:
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True, width=200)
        format_add_result(console, kwargs.pop("result"), output="text", **kwargs)
        return buf.getvalue()

    def test_completed_status_shows_added(self) -> None:
        text = self._render(
            result={"data": {"task_id": "t1", "status": "completed"}},
            task_id="t1",
            final_status="completed",
            waited=True,
        )
        self.assertIn("Memory added", text)
        self.assertIn("t1", text)

    def test_running_after_wait_shows_processing_hint(self) -> None:
        text = self._render(
            result={"data": {"task_id": "t2", "status": "running"}},
            task_id="t2",
            final_status="running",
            waited=True,
        )
        self.assertIn("still processing", text)
        self.assertIn("memos status t2", text)

    def test_no_wait_running_mentions_task_id(self) -> None:
        text = self._render(
            result={"data": {"task_id": "t3", "status": "running"}},
            task_id="t3",
            final_status="running",
            waited=False,
        )
        self.assertIn("accepted", text.lower())
        self.assertIn("t3", text)

    def test_failed_status_reports_failure(self) -> None:
        text = self._render(
            result={"data": {"task_id": "t4", "status": "failed"}},
            task_id="t4",
            final_status="failed",
            waited=True,
        )
        self.assertIn("failed", text.lower())
        self.assertIn("t4", text)


if __name__ == "__main__":
    unittest.main()
