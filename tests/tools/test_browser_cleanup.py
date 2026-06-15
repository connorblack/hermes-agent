"""Regression tests for browser session cleanup and screenshot recovery."""

import json
import os
import subprocess
from unittest.mock import patch


class TestScreenshotPathRecovery:
    def test_extracts_standard_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert (
            _extract_screenshot_path_from_text("Screenshot saved to /tmp/foo.png")
            == "/tmp/foo.png"
        )

    def test_extracts_quoted_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert (
            _extract_screenshot_path_from_text(
                "Screenshot saved to '/Users/david/.hermes/browser_screenshots/shot.png'"
            )
            == "/Users/david/.hermes/browser_screenshots/shot.png"
        )


class TestBrowserCleanup:
    def setup_method(self):
        from tools import browser_tool

        self.browser_tool = browser_tool
        self.orig_active_sessions = browser_tool._active_sessions.copy()
        self.orig_session_last_activity = browser_tool._session_last_activity.copy()
        self.orig_recording_sessions = browser_tool._recording_sessions.copy()
        self.orig_last_active_session_key = browser_tool._last_active_session_key.copy()
        self.orig_cleanup_done = browser_tool._cleanup_done

    def teardown_method(self):
        self.browser_tool._active_sessions.clear()
        self.browser_tool._active_sessions.update(self.orig_active_sessions)
        self.browser_tool._session_last_activity.clear()
        self.browser_tool._session_last_activity.update(self.orig_session_last_activity)
        self.browser_tool._recording_sessions.clear()
        self.browser_tool._recording_sessions.update(self.orig_recording_sessions)
        self.browser_tool._last_active_session_key.clear()
        self.browser_tool._last_active_session_key.update(self.orig_last_active_session_key)
        self.browser_tool._cleanup_done = self.orig_cleanup_done

    def test_cleanup_browser_clears_tracking_state(self):
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": "",
        }
        browser_tool._session_last_activity["task-1"] = 123.0

        with (
            patch("tools.browser_tool._maybe_stop_recording") as mock_stop,
            patch(
                "tools.browser_tool._run_browser_command",
                return_value={"success": True},
            ) as mock_run,
            patch("tools.browser_tool.os.path.exists", return_value=False),
        ):
            browser_tool.cleanup_browser("task-1")

        assert "task-1" not in browser_tool._active_sessions
        assert "task-1" not in browser_tool._session_last_activity
        mock_stop.assert_called_once_with("task-1")
        mock_run.assert_called_once_with("task-1", "close", [], timeout=10)

    def test_cleanup_camofox_managed_persistence_skips_close(self):
        """When camofox mode + managed persistence, soft_cleanup fires instead of close."""
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": "",
        }
        browser_tool._session_last_activity["task-1"] = 123.0

        with (
            patch("tools.browser_tool._is_camofox_mode", return_value=True),
            patch("tools.browser_tool._maybe_stop_recording") as mock_stop,
            patch(
                "tools.browser_tool._run_browser_command",
                return_value={"success": True},
            ),
            patch("tools.browser_tool.os.path.exists", return_value=False),
            patch(
                "tools.browser_camofox.camofox_soft_cleanup",
                return_value=True,
            ) as mock_soft,
            patch("tools.browser_camofox.camofox_close") as mock_close,
        ):
            browser_tool.cleanup_browser("task-1")

        mock_soft.assert_called_once_with("task-1")
        mock_close.assert_not_called()

    def test_cleanup_camofox_no_persistence_calls_close(self):
        """When camofox mode but managed persistence is off, camofox_close fires."""
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": "",
        }
        browser_tool._session_last_activity["task-1"] = 123.0

        with (
            patch("tools.browser_tool._is_camofox_mode", return_value=True),
            patch("tools.browser_tool._maybe_stop_recording") as mock_stop,
            patch(
                "tools.browser_tool._run_browser_command",
                return_value={"success": True},
            ),
            patch("tools.browser_tool.os.path.exists", return_value=False),
            patch(
                "tools.browser_camofox.camofox_soft_cleanup",
                return_value=False,
            ) as mock_soft,
            patch("tools.browser_camofox.camofox_close") as mock_close,
        ):
            browser_tool.cleanup_browser("task-1")

        mock_soft.assert_called_once_with("task-1")
        mock_close.assert_called_once_with("task-1")

    def test_emergency_cleanup_clears_all_tracking_state(self):
        browser_tool = self.browser_tool
        browser_tool._cleanup_done = False
        browser_tool._active_sessions["task-1"] = {"session_name": "sess-1"}
        browser_tool._active_sessions["task-2"] = {"session_name": "sess-2"}
        browser_tool._session_last_activity["task-1"] = 1.0
        browser_tool._session_last_activity["task-2"] = 2.0
        browser_tool._recording_sessions.update({"task-1", "task-2"})

        with patch("tools.browser_tool.cleanup_all_browsers") as mock_cleanup_all:
            browser_tool._emergency_cleanup_all_sessions()

        mock_cleanup_all.assert_called_once_with()
        assert browser_tool._active_sessions == {}
        assert browser_tool._session_last_activity == {}
        assert browser_tool._recording_sessions == set()
        assert browser_tool._cleanup_done is True

    def test_cleanup_session_kills_daemon_and_removes_socket_dir(self, tmp_path):
        browser_tool = self.browser_tool
        session_name = "h_deadline01"
        socket_dir = tmp_path / f"agent-browser-{session_name}"
        socket_dir.mkdir()
        (socket_dir / f"{session_name}.pid").write_text("12345")

        browser_tool._active_sessions["task-timeout"] = {
            "session_name": session_name,
            "bb_session_id": "",
        }
        browser_tool._session_last_activity["task-timeout"] = 123.0
        browser_tool._recording_sessions.add("task-timeout")
        browser_tool._last_active_session_key["task-timeout"] = "task-timeout"

        terminated = []

        def _terminate(pid):
            terminated.append(pid)

        with (
            patch("tools.browser_tool._socket_safe_tmpdir", return_value=str(tmp_path)),
            patch("tools.browser_tool._stop_cdp_supervisor") as mock_stop,
            patch(
                "tools.process_registry.ProcessRegistry._terminate_host_pid",
                side_effect=_terminate,
            ),
        ):
            browser_tool.cleanup_session("task-timeout")
            browser_tool.cleanup_session("task-timeout")

        assert terminated == [12345]
        assert not socket_dir.exists()
        assert "task-timeout" not in browser_tool._active_sessions
        assert "task-timeout" not in browser_tool._session_last_activity
        assert "task-timeout" not in browser_tool._recording_sessions
        assert "task-timeout" not in browser_tool._last_active_session_key
        assert mock_stop.call_count == 2

    def test_run_browser_command_timeout_cleans_session(self, tmp_path, monkeypatch):
        browser_tool = self.browser_tool
        session_name = "h_timeoutcmd1"
        recovered_session_name = "h_recovered01"

        browser_tool._active_sessions["task-timeout"] = {
            "session_name": session_name,
            "bb_session_id": "",
        }
        browser_tool._session_last_activity["task-timeout"] = 123.0

        popen_state = {"timeout": True}

        class _Popen:
            returncode = 0

            def __init__(self, *args, **kwargs):
                socket_dir = kwargs["env"]["AGENT_BROWSER_SOCKET_DIR"]
                os.makedirs(socket_dir, exist_ok=True)
                active_session = os.path.basename(socket_dir).removeprefix("agent-browser-")
                pid = "23456" if popen_state["timeout"] else "45678"
                with open(os.path.join(socket_dir, f"{active_session}.pid"), "w") as f:
                    f.write(pid)
                if not popen_state["timeout"]:
                    os.write(kwargs["stdout"], json.dumps({"success": True}).encode())
                self.should_timeout = popen_state["timeout"]
                self.killed = False

            def wait(self, timeout=None):
                if self.should_timeout and not self.killed:
                    raise subprocess.TimeoutExpired("agent-browser", timeout or 0)
                return 0

            def kill(self):
                self.killed = True

        def _get_or_create_session(task_id):
            if task_id not in browser_tool._active_sessions:
                browser_tool._active_sessions[task_id] = {
                    "session_name": recovered_session_name,
                    "bb_session_id": "",
                }
            return browser_tool._active_sessions[task_id]

        terminated = []

        monkeypatch.setattr(browser_tool.subprocess, "Popen", _Popen)
        monkeypatch.setattr(browser_tool, "_find_agent_browser", lambda: "/bin/agent-browser")
        monkeypatch.setattr(browser_tool, "_requires_real_termux_browser_install", lambda *_: False)
        monkeypatch.setattr(browser_tool, "_is_local_mode", lambda: False)
        monkeypatch.setattr(browser_tool, "_get_browser_engine", lambda: "auto")
        monkeypatch.setattr(browser_tool, "_get_session_info", _get_or_create_session)

        with (
            patch("tools.browser_tool._socket_safe_tmpdir", return_value=str(tmp_path)),
            patch("tools.browser_tool._stop_cdp_supervisor") as mock_stop,
            patch(
                "tools.process_registry.ProcessRegistry._terminate_host_pid",
                side_effect=lambda pid: terminated.append(pid),
            ),
        ):
            result = browser_tool._run_browser_command(
                "task-timeout", "open", ["about:blank"], timeout=1
            )
            popen_state["timeout"] = False
            recovered = browser_tool._run_browser_command(
                "task-timeout", "open", ["about:blank"], timeout=1
            )

        assert result["success"] is False
        assert "timed out" in result["error"]
        assert terminated == [23456]
        assert not (tmp_path / f"agent-browser-{session_name}").exists()
        assert recovered["success"] is True
        assert browser_tool._active_sessions["task-timeout"]["session_name"] == recovered_session_name
        assert (tmp_path / f"agent-browser-{recovered_session_name}").exists()
        mock_stop.assert_called_once_with("task-timeout")

    def test_run_browser_command_success_close_cleans_session(self, tmp_path, monkeypatch):
        browser_tool = self.browser_tool
        session_name = "h_closecmd01"

        browser_tool._active_sessions["task-close"] = {
            "session_name": session_name,
            "bb_session_id": "",
        }

        class _SuccessfulPopen:
            returncode = 0

            def __init__(self, *args, **kwargs):
                socket_dir = kwargs["env"]["AGENT_BROWSER_SOCKET_DIR"]
                os.makedirs(socket_dir, exist_ok=True)
                with open(os.path.join(socket_dir, f"{session_name}.pid"), "w") as f:
                    f.write("34567")
                os.write(kwargs["stdout"], json.dumps({"success": True}).encode())

            def wait(self, timeout=None):
                return 0

        terminated = []

        monkeypatch.setattr(browser_tool.subprocess, "Popen", _SuccessfulPopen)
        monkeypatch.setattr(browser_tool, "_find_agent_browser", lambda: "/bin/agent-browser")
        monkeypatch.setattr(browser_tool, "_requires_real_termux_browser_install", lambda *_: False)
        monkeypatch.setattr(browser_tool, "_is_local_mode", lambda: False)
        monkeypatch.setattr(browser_tool, "_get_browser_engine", lambda: "auto")
        monkeypatch.setattr(
            browser_tool,
            "_get_session_info",
            lambda task_id: browser_tool._active_sessions[task_id],
        )

        with (
            patch("tools.browser_tool._socket_safe_tmpdir", return_value=str(tmp_path)),
            patch("tools.browser_tool._stop_cdp_supervisor") as mock_stop,
            patch(
                "tools.process_registry.ProcessRegistry._terminate_host_pid",
                side_effect=lambda pid: terminated.append(pid),
            ),
        ):
            result = browser_tool._run_browser_command(
                "task-close", "close", [], timeout=1
            )

        assert result["success"] is True
        assert terminated == [34567]
        assert "task-close" not in browser_tool._active_sessions
        assert not (tmp_path / f"agent-browser-{session_name}").exists()
        mock_stop.assert_called_once_with("task-close")
