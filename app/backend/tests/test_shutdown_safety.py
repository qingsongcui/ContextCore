"""
Unit tests for ContextCore shutdown safety and controller verification.

Covers:
- Metadata-only shutdown receipt lifecycle in data directory.
- POST /api/service/stop verification: confirmed requirement, PID identity check, pending receipt creation.
- Stderr/stdout log redirection (avoiding DEVNULL loss).
- Launchd owned plist verification before touching jobs.
- Explicit failure on launchctl bootout/unload errors (no false stopped misreport).
- Graceful stop polling, timeout fail-closed, and force kill identity re-verification.
- ServiceStatus stop_status and stop_error integration across autostart_status, summary, and /api/status.
- Never executing real launchctl commands (all OS interactions mocked).
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, mock_open, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from app import cli, runtime
from app.models import ServiceStopRequest, StatusResponse, ServiceStatus
from app.routers import status as status_router


# ============================================================================
# 1. Shutdown Receipt Lifecycle Tests
# ============================================================================

def test_shutdown_receipt_lifecycle(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Initial state: no receipt
    assert runtime.read_shutdown_receipt(data_dir) is None
    assert runtime.clear_shutdown_receipt(data_dir) is False

    # Write pending receipt
    path = runtime.write_shutdown_receipt(
        data_dir=data_dir,
        status="pending",
        pid=12345,
        stopped=False,
        ok=True,
        message="Stop pending",
    )
    assert path.exists()

    receipt = runtime.read_shutdown_receipt(data_dir)
    assert receipt is not None
    assert receipt["status"] == "pending"
    assert receipt["pid"] == 12345
    assert receipt["stopped"] is False
    assert receipt["ok"] is True
    assert receipt["completed_at"] is None
    assert receipt["requested_at"] is not None
    req_at = receipt["requested_at"]

    # Transition to stopped preserves requested_at and sets completed_at
    runtime.write_shutdown_receipt(
        data_dir=data_dir,
        status="stopped",
        pid=12345,
        stopped=True,
        ok=True,
        message="Service stopped gracefully",
    )
    receipt2 = runtime.read_shutdown_receipt(data_dir)
    assert receipt2 is not None
    assert receipt2["status"] == "stopped"
    assert receipt2["stopped"] is True
    assert receipt2["ok"] is True
    assert receipt2["requested_at"] == req_at
    assert receipt2["completed_at"] is not None

    # Clear receipt
    assert runtime.clear_shutdown_receipt(data_dir) is True
    assert runtime.read_shutdown_receipt(data_dir) is None


def test_shutdown_receipt_invalid_status_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid shutdown status"):
        runtime.write_shutdown_receipt(data_dir=tmp_path, status="unknown_status")


def test_shutdown_receipt_sanitizes_error_length(tmp_path: Path) -> None:
    huge_error = "E" * 1000
    runtime.write_shutdown_receipt(
        data_dir=tmp_path,
        status="failed",
        pid=999,
        ok=False,
        error=huge_error,
        stopped=False,
    )
    receipt = runtime.read_shutdown_receipt(tmp_path)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert len(receipt["error"]) == 500


# ============================================================================
# 2. Stop Endpoint (/api/service/stop) Tests
# ============================================================================

def test_stop_endpoint_requires_confirmed_true() -> None:
    req = ServiceStopRequest(confirmed=False)
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc_info:
        status_router.stop_service_endpoint(req, bg)
    assert exc_info.value.status_code == 422
    assert "requires confirmed=True" in exc_info.value.detail


def test_stop_endpoint_fails_if_process_not_verified(tmp_path: Path) -> None:
    req = ServiceStopRequest(confirmed=True)
    bg = BackgroundTasks()

    # Case A: Service reported not running
    with patch("app.runtime.resolve_data_dir", return_value=tmp_path):
        with patch("app.runtime.is_service_running", return_value=(False, None)):
            with pytest.raises(HTTPException) as exc_info:
                status_router.stop_service_endpoint(req, bg)
            assert exc_info.value.status_code == 503
            assert "Cannot verify the identity" in exc_info.value.detail

    # Case B: PID mismatch (running PID does not match current process PID)
    with patch("app.runtime.resolve_data_dir", return_value=tmp_path):
        with patch("app.runtime.is_service_running", return_value=(True, os.getpid() + 999)):
            with pytest.raises(HTTPException) as exc_info:
                status_router.stop_service_endpoint(req, bg)
            assert exc_info.value.status_code == 503
            assert "Cannot verify the identity" in exc_info.value.detail


def test_stop_endpoint_records_pending_receipt_and_redirects_logs(tmp_path: Path) -> None:
    req = ServiceStopRequest(confirmed=True)
    bg = BackgroundTasks()

    with patch("app.runtime.resolve_data_dir", return_value=tmp_path):
        with patch("app.runtime.is_service_running", return_value=(True, os.getpid())):
            res = status_router.stop_service_endpoint(req, bg)
            assert res["ok"] is True
            assert res["status"] == "stopping"

            # Pending receipt must be written immediately
            receipt = runtime.read_shutdown_receipt(tmp_path)
            assert receipt is not None
            assert receipt["status"] == "pending"
            assert receipt["pid"] == os.getpid()
            assert receipt["stopped"] is False

            # Verify background task execution
            assert len(bg.tasks) == 1
            task_fn = bg.tasks[0].func

            mock_popen = MagicMock()
            with patch("subprocess.Popen", mock_popen):
                task_fn()
                assert mock_popen.called
                args, kwargs = mock_popen.call_args
                # Assert log redirection: stdout and stderr are NOT DEVNULL
                assert kwargs["stdout"] != subprocess.DEVNULL
                assert kwargs["stderr"] != subprocess.DEVNULL
                assert kwargs["start_new_session"] is True
                assert (tmp_path / "logs" / "stop.log").exists()


def test_stop_endpoint_background_launch_failure_marks_receipt_failed(tmp_path: Path) -> None:
    req = ServiceStopRequest(confirmed=True)
    bg = BackgroundTasks()

    with patch("app.runtime.resolve_data_dir", return_value=tmp_path):
        with patch("app.runtime.is_service_running", return_value=(True, os.getpid())):
            status_router.stop_service_endpoint(req, bg)
            task_fn = bg.tasks[0].func

            with patch("subprocess.Popen", side_effect=OSError("Exec format error")):
                task_fn()

            receipt = runtime.read_shutdown_receipt(tmp_path)
            assert receipt is not None
            assert receipt["status"] == "failed"
            assert receipt["ok"] is False
            assert "Exec format error" in receipt["error"]


# ============================================================================
# 3. Stop Service Safety & Owned Plist Verification
# ============================================================================

def test_stop_service_when_not_running(tmp_path: Path) -> None:
    res = runtime.stop_service(tmp_path)
    assert res["ok"] is True
    assert res["stopped"] is False
    assert res["pid"] is None

    receipt = runtime.read_shutdown_receipt(tmp_path)
    assert receipt is not None
    assert receipt["status"] == "stopped"
    assert receipt["ok"] is True


def test_stop_service_refuses_when_pid_identity_verification_fails(tmp_path: Path) -> None:
    pid_file = tmp_path / runtime.PID_FILE_NAME
    pid_file.write_text(json.dumps({
        "pid": 77777,
        "port": 8420,
        "host": "127.0.0.1",
        "data_dir": str(tmp_path.resolve()),
        "cmdline": "/bin/other_app",
        "start_time": "Sat Oct 24 10:00:00 2026",
    }))

    with patch("app.runtime.is_service_running", return_value=(True, 77777)):
        with patch("app.runtime.verify_pid_identity", return_value=False):
            with patch("os.kill") as mock_kill:
                res = runtime.stop_service(tmp_path)
                assert res["ok"] is False
                assert res["stopped"] is False
                assert "failed fail-closed identity verification" in res["error"]
                mock_kill.assert_not_called()

                receipt = runtime.read_shutdown_receipt(tmp_path)
                assert receipt is not None
                assert receipt["status"] == "failed"
                assert receipt["stopped"] is False


def test_stop_service_launchd_refuses_missing_plist(tmp_path: Path) -> None:
    fake_plist = tmp_path / "com.contextcore.missing.plist"

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=True), patch("app.runtime._registered_launchd_pid", return_value=12345):
            with patch("app.runtime.get_default_plist_path", return_value=fake_plist):
                with patch("app.runtime.is_service_running", return_value=(True, 12345)):
                    with patch("app.runtime.verify_pid_identity", return_value=True):
                        with patch("subprocess.run") as mock_run:
                            with patch("os.kill") as mock_kill:
                                res = runtime.stop_service(tmp_path)
                                assert res["ok"] is False
                                assert res["stopped"] is False
                                mock_run.assert_not_called()
                                mock_kill.assert_not_called()

                                receipt = runtime.read_shutdown_receipt(tmp_path)
                                assert receipt is not None
                                assert receipt["status"] == "failed"


def test_stop_service_launchd_refuses_foreign_plist(tmp_path: Path) -> None:
    foreign_plist = tmp_path / "foreign.plist"
    foreign_data = {
        "Label": "com.other.service",
        "ProgramArguments": ["/bin/foreign", "serve"],
    }
    foreign_plist.write_bytes(plistlib.dumps(foreign_data, fmt=plistlib.FMT_XML))

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=True), patch("app.runtime._registered_launchd_pid", return_value=12345):
            with patch("app.runtime.get_default_plist_path", return_value=foreign_plist):
                with patch("app.runtime.is_service_running", return_value=(True, 12345)):
                    with patch("app.runtime.verify_pid_identity", return_value=True):
                        with patch("subprocess.run") as mock_run:
                            with patch("os.kill") as mock_kill:
                                res = runtime.stop_service(tmp_path)
                                assert res["ok"] is False
                                assert res["stopped"] is False
                                mock_run.assert_not_called()
                                mock_kill.assert_not_called()

                                receipt = runtime.read_shutdown_receipt(tmp_path)
                                assert receipt is not None
                                assert receipt["status"] == "failed"


def test_stop_service_launchd_bootout_error_fails_explicit_no_false_stopped(tmp_path: Path) -> None:
    valid_plist = tmp_path / "com.contextcore.service.plist"
    plist_data = {
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve", "--data-dir", str(tmp_path.resolve())],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(tmp_path.resolve())},
    }
    valid_plist.write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML))

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=True), patch("app.runtime._registered_launchd_pid", return_value=12345):
            with patch("app.runtime.get_default_plist_path", return_value=valid_plist):
                with patch("app.runtime.is_service_running", return_value=(True, 12345)):
                    with patch("app.runtime.verify_pid_identity", return_value=True):
                        # Both bootout and fallback unload return exit code 3
                        mock_sub = MagicMock(returncode=3, stderr="Could not find service", stdout="")
                        with patch("subprocess.run", return_value=mock_sub):
                            with patch("os.kill") as mock_kill:
                                res = runtime.stop_service(tmp_path)
                                # MUST fail explicitly and NOT report stopped
                                assert res["ok"] is False
                                assert res["stopped"] is False
                                # SIGTERM must NOT be sent if unload failed (KeepAlive could restart)
                                mock_kill.assert_not_called()

                                receipt = runtime.read_shutdown_receipt(tmp_path)
                                assert receipt is not None
                                assert receipt["status"] == "failed"
                                assert receipt["stopped"] is False


def test_stop_service_launchd_bootout_exception_fails_explicit(tmp_path: Path) -> None:
    valid_plist = tmp_path / "com.contextcore.service.plist"
    plist_data = {
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve", "--data-dir", str(tmp_path.resolve())],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(tmp_path.resolve())},
    }
    valid_plist.write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML))

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=True), patch("app.runtime._registered_launchd_pid", return_value=12345):
            with patch("app.runtime.get_default_plist_path", return_value=valid_plist):
                with patch("app.runtime.is_service_running", return_value=(True, 12345)):
                    with patch("app.runtime.verify_pid_identity", return_value=True):
                        with patch("subprocess.run", side_effect=OSError("launchctl execution denied")):
                            with patch("os.kill") as mock_kill:
                                res = runtime.stop_service(tmp_path)
                                assert res["ok"] is False
                                assert res["stopped"] is False
                                mock_kill.assert_not_called()


def test_stop_service_graceful_shutdown_success(tmp_path: Path) -> None:
    pid_file = tmp_path / runtime.PID_FILE_NAME
    pid_file.write_text(json.dumps({"pid": 22222, "data_dir": str(tmp_path)}))

    # Simulate process alive when queried, but exits when polled
    kill_calls = []

    def mock_os_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig == 0:
            # First poll or subsequent: raise OSError (process has exited)
            raise OSError(3, "No such process")

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=False):
            with patch("app.runtime.is_service_running", return_value=(True, 22222)):
                with patch("app.runtime.verify_pid_identity", return_value=True):
                    with patch("os.kill", side_effect=mock_os_kill):
                        res = runtime.stop_service(tmp_path, timeout=1.0)
                        assert res["ok"] is True
                        assert res["stopped"] is True
                        assert "stopped gracefully" in res["message"]
                        assert not pid_file.exists()

                        receipt = runtime.read_shutdown_receipt(tmp_path)
                        assert receipt is not None
                        assert receipt["status"] == "stopped"
                        assert receipt["stopped"] is True
                        assert receipt["ok"] is True


def test_stop_service_launchd_bootout_stops_process_before_sigterm(tmp_path: Path) -> None:
    pid_file = tmp_path / runtime.PID_FILE_NAME
    pid_file.write_text(json.dumps({"pid": 22223, "data_dir": str(tmp_path.resolve())}))
    plist_path = tmp_path / "com.contextcore.service.plist"
    plist_path.write_text("dummy")

    verify_calls = {"count": 0}

    def mock_verify(*args, **kwargs):
        verify_calls["count"] += 1
        return verify_calls["count"] == 1

    with (
        patch("sys.platform", "darwin"),
        patch("app.runtime.is_launchd_registered", return_value=True),
        patch("app.runtime._registered_launchd_pid", return_value=22223),
        patch("app.runtime.is_service_running", return_value=(True, 22223)),
        patch("app.runtime.verify_pid_identity", side_effect=mock_verify),
        patch("app.runtime.get_default_plist_path", return_value=plist_path),
        patch("app.runtime.verify_plist_ownership", return_value=(True, "owned")),
        patch("app.runtime.subprocess.run", return_value=type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()),
        patch("app.runtime._pid_definitively_absent", return_value=True),
        patch("os.kill") as mock_kill,
    ):
        res = runtime.stop_service(tmp_path, timeout=1.0)
        assert res["ok"] is True
        assert res["stopped"] is True
        assert not pid_file.exists()
        mock_kill.assert_not_called()
        receipt = runtime.read_shutdown_receipt(tmp_path)
        assert receipt is not None
        assert receipt["status"] == "stopped"
        assert receipt["ok"] is True


def test_stop_service_timeout_unforced_leaves_pid_records_failed(tmp_path: Path) -> None:
    pid_file = tmp_path / runtime.PID_FILE_NAME
    pid_file.write_text(json.dumps({"pid": 33333, "data_dir": str(tmp_path)}))

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=False):
            with patch("app.runtime.is_service_running", return_value=(True, 33333)):
                with patch("app.runtime.verify_pid_identity", return_value=True):
                    # os.kill(pid, 0) always succeeds (process stays alive)
                    with patch("os.kill", return_value=None):
                        res = runtime.stop_service(tmp_path, timeout=0.1, force=False)
                        assert res["ok"] is False
                        assert res["stopped"] is False
                        assert "did not stop within" in res["message"]
                        assert pid_file.exists()

                        receipt = runtime.read_shutdown_receipt(tmp_path)
                        assert receipt is not None
                        assert receipt["status"] == "failed"
                        assert receipt["stopped"] is False
                        assert "did not stop within" in receipt["error"]


def test_stop_service_force_kill_success(tmp_path: Path) -> None:
    pid_file = tmp_path / runtime.PID_FILE_NAME
    pid_file.write_text(json.dumps({"pid": 44444, "data_dir": str(tmp_path)}))

    kill_signals = []

    def mock_kill(pid: int, sig: int) -> None:
        kill_signals.append(sig)
        if sig == signal.SIGKILL:
            pass
        elif sig == 0 and signal.SIGKILL in kill_signals:
            # Exited after SIGKILL
            raise OSError(3, "No such process")

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=False):
            with patch("app.runtime.is_service_running", return_value=(True, 44444)):
                with patch("app.runtime.verify_pid_identity", return_value=True):
                    with patch("os.kill", side_effect=mock_kill):
                        res = runtime.stop_service(tmp_path, timeout=0.1, force=True)
                        assert res["ok"] is True
                        assert res["stopped"] is True
                        assert "force-stopped" in res["message"]
                        assert not pid_file.exists()

                        receipt = runtime.read_shutdown_receipt(tmp_path)
                        assert receipt is not None
                        assert receipt["status"] == "stopped"
                        assert receipt["stopped"] is True


# ============================================================================
# 4. Status Service Fields & /api/status Integration Tests
# ============================================================================

def test_autostart_status_and_summary_expose_stop_fields(tmp_path: Path) -> None:
    # 1. No receipt -> stop_status and stop_error are None; summary has no extra keys
    summary_empty = runtime.get_service_status_summary(tmp_path)
    assert "stop_status" not in summary_empty
    assert "stop_error" not in summary_empty

    # 2. Pending receipt -> stop_status="pending"
    runtime.write_shutdown_receipt(data_dir=tmp_path, status="pending", pid=101, stopped=False)
    with patch("app.runtime.is_service_running", return_value=(True, 101)):
        status_pending = runtime.autostart_status(tmp_path)
        assert status_pending["stop_status"] == "pending"
        assert status_pending["stop_error"] is None

        summary_pending = runtime.get_service_status_summary(tmp_path)
        assert summary_pending["stop_status"] == "pending"
        assert summary_pending.get("stop_error") is None

    # 3. Failed receipt -> stop_status="failed", stop_error exposed
    runtime.write_shutdown_receipt(
        data_dir=tmp_path,
        status="failed",
        pid=101,
        ok=False,
        error="Port timeout after SIGTERM",
        stopped=False,
    )
    with patch("app.runtime.is_service_running", return_value=(True, 101)):
        status_failed = runtime.autostart_status(tmp_path)
        assert status_failed["stop_status"] == "failed"
        assert status_failed["stop_error"] == "Port timeout after SIGTERM"

        summary_failed = runtime.get_service_status_summary(tmp_path)
        assert summary_failed["stop_status"] == "failed"
        assert summary_failed["stop_error"] == "Port timeout after SIGTERM"


def test_get_system_status_defaults_running_false_and_populates_stop_fields(tmp_path: Path) -> None:
    db_file = tmp_path / "test_status.db"
    with patch.dict(os.environ, {"CONTEXTCORE_DB": str(db_file)}):
        from app.database import create_schema, seed_data
        create_schema()
        seed_data()

        mock_summary = {
            "autostart": True,
            "autostart_installed": False,
            "platform": "darwin",
            "stop_status": "pending",
            "stop_error": None,
        }

        with patch("app.routers.status._get_runtime_service_summary", return_value=mock_summary):
            res = status_router.get_system_status()
            assert isinstance(res, StatusResponse)
            # running should default to False when key is absent
            assert res.service.running is False
            assert res.service.stop_status == "pending"
            assert res.service.stop_error is None

# ============================================================================
# 5. CLI Tests: Serve Receipt Clearing & Stop Stderr Output
# ============================================================================

def test_cli_stop_prints_error_to_stderr(tmp_path: Path) -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["stop", "--data-dir", str(tmp_path)])

    with patch("app.runtime.stop_service", return_value={"ok": False, "error": "Stop timed out", "stopped": False}):
        with patch("sys.stderr") as mock_stderr:
            code = cli.cmd_stop(args)
            assert code == 1
            mock_stderr.write.assert_called()


def test_cli_status_displays_shutdown_status_when_present(tmp_path: Path) -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["status", "--data-dir", str(tmp_path)])

    mock_status = {
        "platform": "darwin",
        "running": True,
        "pid": 1111,
        "port": 8420,
        "desired": True,
        "installed": True,
        "registered": True,
        "data_dir": str(tmp_path),
        "plist_path": "/fake/plist",
        "stop_status": "failed",
        "stop_error": "Process did not terminate",
    }

    with patch("app.runtime.autostart_status", return_value=mock_status):
        with patch("sys.stdout") as mock_stdout:
            code = cli.cmd_status(args)
            assert code == 0
            output = "".join(call_args[0][0] for call_args in mock_stdout.write.call_args_list)
            assert "Shutdown Status:     FAILED" in output
            assert "Process did not terminate" in output


# ============================================================================
# 6. Real Active SSE Stream + Graceful CLI Stop Regression
# ============================================================================

def test_event_bus_shutdown_and_restart() -> None:
    from app import event_bus
    import asyncio

    async def exercise():
        event_bus.reset_shutdown()
        q1 = await event_bus.subscribe()
        q2 = await event_bus.subscribe()
        assert len(event_bus._subscribers) == 2

        event_bus.notify_shutdown()
        assert len(event_bus._subscribers) == 0

        # Current subscribers receive shutdown sentinel
        item1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        item2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert item1.name == "__shutdown__"
        assert item2.name == "__shutdown__"

        # Late subscribers during shutdown receive shutdown sentinel immediately
        q3 = await event_bus.subscribe()
        item3 = await asyncio.wait_for(q3.get(), timeout=1.0)
        assert item3.name == "__shutdown__"

        # Reset shutdown for new lifecycle
        event_bus.reset_shutdown()
        q4 = await event_bus.subscribe()
        assert len(event_bus._subscribers) == 1
        event_bus.publish_event("content.changed", {"id": "restarted"})
        await asyncio.sleep(0.01)
        item4 = await asyncio.wait_for(q4.get(), timeout=1.0)
        assert item4.name == "content.changed"
        event_bus.unsubscribe(q4)

    asyncio.run(exercise())


def test_cli_stop_with_active_sse_stream_exits_cleanly(tmp_path: Path) -> None:
    import http.client
    import socket
    import time

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = {**os.environ, "HOME": str(home), "CONTEXTCORE_DATA_DIR": str(data)}
    backend_dir = Path(__file__).resolve().parents[1]
    server_log = tmp_path / "server.log"

    with server_log.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.cli", "serve", "--host", "127.0.0.1",
             "--port", str(port), "--data-dir", str(data), "--no-static"],
            cwd=backend_dir,
            env=env,
            stdout=log,
            stderr=log,
        )

        try:
            started = False
            for _ in range(100):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
                    conn.request("GET", "/health")
                    resp = conn.getresponse()
                    if resp.status == 200:
                        started = True
                        conn.close()
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.05)
            assert started, "Server failed to start within timeout"

            # Open real persistent SSE connection to /api/events
            sse_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
            sse_conn.request("GET", "/api/events")
            sse_resp = sse_conn.getresponse()
            assert sse_resp.status == 200
            first_chunk = sse_resp.read(30)
            assert b"event: connect" in first_chunk

            # Verify PID file exists and service reports running
            running, srv_pid = runtime.is_service_running(data)
            assert running is True
            assert srv_pid == proc.pid

            # Invoke graceful stop_service via CLI logic within existing 5.0s deadline
            t0 = time.monotonic()
            stop_res = runtime.stop_service(data_dir=data, timeout=5.0, force=False)
            elapsed = time.monotonic() - t0

            assert stop_res["ok"] is True
            assert stop_res["stopped"] is True
            assert elapsed < 5.0, f"stop_service took {elapsed}s, exceeded 5.0s deadline"

            # Verify process exited cleanly with code 0 without SIGKILL
            exit_code = proc.wait(timeout=2.0)
            assert exit_code == 0
            assert not (data / runtime.PID_FILE_NAME).exists()

            # Verify receipt
            receipt = runtime.read_shutdown_receipt(data)
            assert receipt is not None
            assert receipt["ok"] is True
            assert receipt["status"] == "stopped"

            # Restart verification: verify clean restart on same data directory
            with socket.socket() as sock2:
                sock2.bind(("127.0.0.1", 0))
                port2 = sock2.getsockname()[1]

            proc2 = subprocess.Popen(
                [sys.executable, "-m", "app.cli", "serve", "--host", "127.0.0.1",
                 "--port", str(port2), "--data-dir", str(data), "--no-static"],
                cwd=backend_dir,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                started2 = False
                for _ in range(100):
                    try:
                        conn2 = http.client.HTTPConnection("127.0.0.1", port2, timeout=1.0)
                        conn2.request("GET", "/health")
                        if conn2.getresponse().status == 200:
                            started2 = True
                            conn2.close()
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                    time.sleep(0.05)
                assert started2, "Server failed to restart"

                # Verify events endpoint works on restarted server
                conn_events = http.client.HTTPConnection("127.0.0.1", port2, timeout=5.0)
                conn_events.request("GET", "/api/events")
                resp_events = conn_events.getresponse()
                assert resp_events.status == 200
                chunk2 = resp_events.read(30)
                assert b"event: connect" in chunk2

                # Stop second server gracefully
                stop_res2 = runtime.stop_service(data_dir=data, timeout=5.0, force=False)
                assert stop_res2["ok"] is True
                proc2.wait(timeout=2.0)
            finally:
                if proc2.poll() is None:
                    proc2.kill()
                    proc2.wait(timeout=2.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)
