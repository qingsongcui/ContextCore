"""
Behavioral tests for ContextCore runtime service management.
Tests launchd plist generation, autostart preferences, fail-closed PID identity verification,
ownership checks, launchd unload failure retention, and clean stop behavior.
"""

from __future__ import annotations

import errno
import json
import os
import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import runtime


def test_default_data_dir_resolution(tmp_path: Path) -> None:
    custom = tmp_path / "custom_data"
    assert runtime.resolve_data_dir(custom) == custom.resolve()

    with patch.dict(os.environ, {"CONTEXTCORE_DATA_DIR": str(tmp_path / "env_data")}):
        resolved = runtime.resolve_data_dir(None)
        assert resolved == (tmp_path / "env_data").resolve()


def test_autostart_desired_default_and_mutation(tmp_path: Path) -> None:
    # 1. Fresh directory defaults to True (PRD D047: 默认开机自启，可关闭)
    assert runtime.get_autostart_desired(tmp_path) is True

    # 2. Mutate to False - deactivates autostart by unlinking LaunchAgent plist
    fake_plist = tmp_path / "com.contextcore.test.plist"
    plist_data = {
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve", "--data-dir", str(tmp_path.resolve())],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(tmp_path.resolve())},
    }
    fake_plist.write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML))

    with patch.object(runtime.sys, "platform", "darwin"), patch("app.runtime.is_launchd_registered", return_value=False):
        res = runtime.set_autostart_desired(False, tmp_path, plist_path=fake_plist)
        assert res["ok"] is True
        assert res["desired"] is False
        assert res["uninstalled_from_launchd"] is True
        assert not fake_plist.exists()

    assert runtime.get_autostart_desired(tmp_path) is False

    # 3. Mutate back to True
    res2 = runtime.set_autostart_desired(True, tmp_path)
    assert res2["ok"] is True
    assert res2["desired"] is True
    assert runtime.get_autostart_desired(tmp_path) is True


def test_pid_file_lifecycle_and_data_dir(tmp_path: Path) -> None:
    pid_file = tmp_path / runtime.PID_FILE_NAME
    assert not pid_file.exists()

    with patch("app.runtime.get_process_cmdline", return_value="python3 -m app.cli serve"):
        with patch("app.runtime.get_process_start_time", return_value="Sat Oct 24 12:00:00 2026"):
            written_path = runtime.write_pid_file(99999, port=8420, host="127.0.0.1", data_dir=tmp_path)
            assert written_path.exists()

    info = runtime.read_pid_file(tmp_path)
    assert info is not None
    assert info["pid"] == 99999
    assert info["port"] == 8420
    assert info["host"] == "127.0.0.1"
    assert info["data_dir"] == str(tmp_path.resolve())
    assert info["app"] == "ContextCore"
    assert info["cmdline"] == "python3 -m app.cli serve"
    assert info["start_time"] == "Sat Oct 24 12:00:00 2026"

    assert runtime.remove_pid_file(tmp_path) is True
    assert not pid_file.exists()
    assert runtime.read_pid_file(tmp_path) is None


def test_is_port_bound_by_pid_exact_lsof() -> None:
    # PID <= 1 or port <= 0 fails closed
    assert runtime.is_port_bound_by_pid(0, 8420) is False
    assert runtime.is_port_bound_by_pid(1, 8420) is False
    assert runtime.is_port_bound_by_pid(1234, 0) is False

    # Exact line match 'p1234' succeeds
    mock_success = MagicMock(returncode=0, stdout="p1234\n")
    with patch("subprocess.run", return_value=mock_success):
        assert runtime.is_port_bound_by_pid(1234, 8420) is True

    # Substring match (e.g. line 'p12345' when checking pid 1234) must fail
    mock_mismatch = MagicMock(returncode=0, stdout="p12345\n")
    with patch("subprocess.run", return_value=mock_mismatch):
        assert runtime.is_port_bound_by_pid(1234, 8420) is False

    # Non-zero return code fails
    mock_fail = MagicMock(returncode=1, stdout="")
    with patch("subprocess.run", return_value=mock_fail):
        assert runtime.is_port_bound_by_pid(1234, 8420) is False


def test_verify_pid_identity_strict_fail_closed(tmp_path: Path) -> None:
    valid_cmdline = f"/usr/bin/python3 -m app.cli serve --host 127.0.0.1 --port 8420 --data-dir {tmp_path.resolve()}"
    valid_start = "Sat Oct 24 12:00:00 2026"

    # Missing mandatory ground truth fails closed
    assert runtime.verify_pid_identity(1234, expected_cmdline=None, expected_start_time=valid_start, data_dir=tmp_path) is False
    assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=None, data_dir=tmp_path) is False
    assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=valid_start, data_dir=None) is False

    # PID <= 1 fails closed
    assert runtime.verify_pid_identity(0, expected_cmdline=valid_cmdline, expected_start_time=valid_start, data_dir=tmp_path) is False
    assert runtime.verify_pid_identity(1, expected_cmdline=valid_cmdline, expected_start_time=valid_start, data_dir=tmp_path) is False

    # Process not alive fails closed
    with patch("os.kill", side_effect=OSError(3, "No such process")):
        assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=valid_start, data_dir=tmp_path) is False

    # Process alive but ps cmdline returns None/empty -> FAILS CLOSED
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=None):
            assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=valid_start, data_dir=tmp_path) is False

    # Cmdline missing '-m app.cli serve' token sequence -> FAILS CLOSED
    foreign_cmd = f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --data-dir {tmp_path.resolve()}"
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=foreign_cmd):
            assert runtime.verify_pid_identity(1234, expected_cmdline=foreign_cmd, expected_start_time=valid_start, data_dir=tmp_path) is False

    # Cmdline contains '-m app.cli serve' but wrong data_dir -> FAILS CLOSED
    wrong_dir_cmd = "python3 -m app.cli serve --data-dir /different/path"
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=wrong_dir_cmd):
            assert runtime.verify_pid_identity(1234, expected_cmdline=wrong_dir_cmd, expected_start_time=valid_start, data_dir=tmp_path) is False

    # Cmdline does not match expected_cmdline -> FAILS CLOSED
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=valid_cmdline):
            assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline + " --extra", expected_start_time=valid_start, data_dir=tmp_path) is False

    # ps lstart returns None/empty -> FAILS CLOSED
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=valid_cmdline):
            with patch("app.runtime.get_process_start_time", return_value=None):
                assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=valid_start, data_dir=tmp_path) is False

    # Start time mismatch (process recycled by OS) -> FAILS CLOSED
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=valid_cmdline):
            with patch("app.runtime.get_process_start_time", return_value="Sun Oct 25 09:00:00 2026"):
                assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=valid_start, data_dir=tmp_path) is False

    # Port not bound -> FAILS CLOSED
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=valid_cmdline):
            with patch("app.runtime.get_process_start_time", return_value=valid_start):
                with patch("app.runtime.is_port_bound_by_pid", return_value=False):
                    assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=valid_start, expected_port=8420, data_dir=tmp_path) is False

    # All criteria satisfied -> PASS
    with patch("os.kill", return_value=None):
        with patch("app.runtime.get_process_cmdline", return_value=valid_cmdline):
            with patch("app.runtime.get_process_start_time", return_value=valid_start):
                with patch("app.runtime.is_port_bound_by_pid", return_value=True):
                    assert runtime.verify_pid_identity(1234, expected_cmdline=valid_cmdline, expected_start_time=valid_start, expected_port=8420, data_dir=tmp_path) is True


def test_generate_launchd_plist_structure(tmp_path: Path) -> None:
    custom_python = Path("/opt/homebrew/bin/python3")
    plist_xml = runtime.generate_launchd_plist(
        data_dir=tmp_path,
        python_path=custom_python,
        port=8420,
        host="127.0.0.1",
        label="com.contextcore.test",
    )

    data = plistlib.loads(plist_xml.encode("utf-8"))
    assert data["Label"] == "com.contextcore.test"
    assert data["RunAtLoad"] is True

    keep_alive = data["KeepAlive"]
    assert isinstance(keep_alive, dict)
    assert keep_alive.get("SuccessfulExit") is False
    assert keep_alive.get("Crashed") is True

    args = data["ProgramArguments"]
    assert str(custom_python) in args[0]
    assert "-m" in args
    assert "app.cli" in args
    assert "serve" in args
    assert "--host" in args
    assert "127.0.0.1" in args
    assert "--port" in args
    assert "8420" in args
    assert "--data-dir" in args
    assert str(tmp_path.resolve()) in args
    assert not any("tauri" in arg.lower() for arg in args)

    env = data["EnvironmentVariables"]
    assert env["CONTEXTCORE_DATA_DIR"] == str(tmp_path.resolve())
    assert env["CONTEXTCORE_DB"] == str(tmp_path.resolve() / "contextcore.db")


def test_verify_plist_ownership(tmp_path: Path) -> None:
    plist_file = tmp_path / "test.plist"

    # Foreign plist label
    foreign_data = {"Label": "com.foreign.app", "ProgramArguments": ["/bin/sh"]}
    plist_file.write_bytes(plistlib.dumps(foreign_data, fmt=plistlib.FMT_XML))
    is_owner, reason = runtime.verify_plist_ownership(plist_file, expected_label="com.contextcore.service")
    assert is_owner is False
    assert "label" in reason.lower()

    # Right label but not ContextCore app.cli serve
    not_contextcore = {"Label": "com.contextcore.service", "ProgramArguments": ["python3", "-m", "other.module"]}
    plist_file.write_bytes(plistlib.dumps(not_contextcore, fmt=plistlib.FMT_XML))
    is_owner, reason = runtime.verify_plist_ownership(plist_file, expected_label="com.contextcore.service")
    assert is_owner is False
    assert "app.cli serve" in reason

    # Mismatched data_dir
    mismatch_data = {
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve"],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": "/different/data/dir"},
    }
    plist_file.write_bytes(plistlib.dumps(mismatch_data, fmt=plistlib.FMT_XML))
    is_owner, reason = runtime.verify_plist_ownership(plist_file, expected_label="com.contextcore.service", expected_data_dir=tmp_path)
    assert is_owner is False
    assert "data directory mismatch" in reason.lower()

    # Valid ContextCore plist
    valid_data = {
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve"],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(tmp_path.resolve())},
    }
    plist_file.write_bytes(plistlib.dumps(valid_data, fmt=plistlib.FMT_XML))
    is_owner, reason = runtime.verify_plist_ownership(plist_file, expected_label="com.contextcore.service", expected_data_dir=tmp_path)
    assert is_owner is True


def test_install_autostart_refuses_foreign_overwrite_and_handles_load_error(tmp_path: Path) -> None:
    target_plist = tmp_path / "com.foreign.plist"
    foreign_data = {"Label": "com.foreign.app", "ProgramArguments": ["/bin/foreign"]}
    target_plist.write_bytes(plistlib.dumps(foreign_data, fmt=plistlib.FMT_XML))

    with patch("sys.platform", "darwin"):
        # Refuses to overwrite foreign plist
        res = runtime.install_autostart(
            data_dir=tmp_path,
            plist_path=target_plist,
            register=False,
        )
        assert res["ok"] is False
        assert "refusing to overwrite" in res["error"].lower()

        # Load failure must fail closed (ok: False)
        valid_data = {
            "Label": "com.contextcore.service",
            "ProgramArguments": ["python3", "-m", "app.cli", "serve"],
            "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(tmp_path.resolve())},
        }
        target_plist.write_bytes(plistlib.dumps(valid_data, fmt=plistlib.FMT_XML))

        mock_sub = MagicMock(returncode=1, stderr="Permission denied", stdout="")
        with patch("subprocess.run", return_value=mock_sub):
            load_fail_res = runtime.install_autostart(
                data_dir=tmp_path,
                plist_path=target_plist,
                register=True,
            )
            assert load_fail_res["ok"] is False
            assert "launchctl load failed" in load_fail_res["error"]


def test_uninstall_autostart_unload_failure_retains_plist(tmp_path: Path) -> None:
    target_plist = tmp_path / "com.contextcore.test.plist"
    valid_data = {
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve"],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(tmp_path.resolve())},
    }
    target_plist.write_bytes(plistlib.dumps(valid_data, fmt=plistlib.FMT_XML))

    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=True):
            # Unload returns non-zero exit code
            mock_sub = MagicMock(returncode=3, stderr="Could not find specified service", stdout="")
            with patch("subprocess.run", return_value=mock_sub):
                res = runtime.uninstall_autostart(data_dir=tmp_path, plist_path=target_plist)
                # MUST NOT delete plist file and MUST NOT return ok: True
                assert res["ok"] is False
                assert "failed to unload launchd job" in res["error"].lower()
                assert target_plist.exists()


def test_uninstall_autostart_leaves_data_directory_intact(tmp_path: Path) -> None:
    marker = tmp_path / "contextcore.db"
    extra = tmp_path / "user-marker.txt"
    marker.write_text("user-records", encoding="utf-8")
    extra.write_text("keep-me", encoding="utf-8")
    plist_path = tmp_path / "com.contextcore.service.plist"
    _owned_plist(plist_path, tmp_path)
    with patch("sys.platform", "darwin"), patch("app.runtime.is_launchd_registered", return_value=False):
        result = runtime.uninstall_autostart(data_dir=tmp_path, plist_path=plist_path)
    assert result["ok"] is True
    assert result["uninstalled"] is True
    assert not plist_path.exists()
    assert marker.read_text(encoding="utf-8") == "user-records"
    assert extra.read_text(encoding="utf-8") == "keep-me"


def test_stop_service_fail_closed_and_timeout(tmp_path: Path) -> None:
    # 1. When service is not running
    res = runtime.stop_service(tmp_path, timeout=1.0)
    assert res["ok"] is True
    assert res["stopped"] is False

    # 2. When PID is present in file but fails fail-closed verification
    pid_file = tmp_path / runtime.PID_FILE_NAME
    pid_data = {
        "pid": 55555,
        "port": 8420,
        "host": "127.0.0.1",
        "data_dir": str(tmp_path.resolve()),
        "cmdline": "/bin/unrelated_daemon",
        "start_time": "Sat Oct 24 10:00:00 2026",
    }
    pid_file.write_text(json.dumps(pid_data), encoding="utf-8")

    # With PID identity failing, stop_service must fail closed and NOT send kill signals
    with patch("app.runtime.is_service_running", return_value=(True, 55555)):
        with patch("app.runtime.verify_pid_identity", return_value=False):
            with patch("os.kill") as mock_kill:
                stop_res = runtime.stop_service(tmp_path, timeout=1.0)
                assert stop_res["ok"] is False
                assert "failed fail-closed identity verification" in stop_res["error"]
                mock_kill.assert_not_called()

    # 3. Ordinary process timeout. Isolate plist and launchd so a same-label
    # LaunchAgent for another data directory cannot redirect this path.
    missing_plist = tmp_path / "not-installed.plist"
    with patch("app.runtime.is_launchd_registered", return_value=False):
        with patch("app.runtime.get_default_plist_path", return_value=missing_plist):
            with patch("app.runtime.is_service_running", return_value=(True, 55555)):
                with patch("app.runtime.verify_pid_identity", return_value=True):
                    with patch("os.kill", return_value=None), patch("app.runtime.subprocess.run", return_value=MagicMock(returncode=0, stdout="S", stderr="")):
                        timeout_res = runtime.stop_service(tmp_path, timeout=0.1, force=False)
                        assert timeout_res["ok"] is False
                        assert timeout_res["stopped"] is False
                        assert "did not stop within" in timeout_res["message"]
                        assert pid_file.exists()

    # 4. Launchd-managed stop whose plist belongs to another data directory
    # refuses before bootout or any signal. Uses a temporary plist, not the
    # machine LaunchAgent.
    other_dir = tmp_path / "other-service-data"
    other_dir.mkdir()
    foreign_plist = tmp_path / "com.contextcore.service.plist"
    foreign_plist.write_bytes(plistlib.dumps({
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve", "--data-dir", str(other_dir.resolve())],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(other_dir.resolve())},
    }, fmt=plistlib.FMT_XML))
    with patch("sys.platform", "darwin"):
        with patch("app.runtime.is_launchd_registered", return_value=True), patch("app.runtime._registered_launchd_pid", return_value=55555):
            with patch("app.runtime.get_default_plist_path", return_value=foreign_plist):
                with patch("app.runtime.is_service_running", return_value=(True, 55555)):
                    with patch("app.runtime.verify_pid_identity", return_value=True):
                        with patch("os.kill") as mock_kill:
                            with patch("subprocess.run") as mock_run:
                                denied = runtime.stop_service(tmp_path, timeout=0.1, force=False)
                                assert denied["ok"] is False
                                assert denied["stopped"] is False
                                assert "plist ownership verification failed" in denied["error"]
                                assert "message" not in denied
                                mock_kill.assert_not_called()
                                mock_run.assert_not_called()
                                assert pid_file.exists()


def _owned_plist(path: Path, data_dir: Path, label: str = "com.contextcore.service") -> None:
    path.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": ["python3", "-m", "app.cli", "serve", "--data-dir", str(data_dir.resolve())],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(data_dir.resolve())},
    }, fmt=plistlib.FMT_XML))


def test_autostart_status_and_summary(tmp_path: Path) -> None:
    missing = tmp_path / "missing.plist"
    with patch("app.runtime.get_default_plist_path", return_value=missing):
        with patch("app.runtime.is_launchd_registered") as registered:
            status = runtime.autostart_status(tmp_path, label="com.contextcore.test_status")
            assert status["desired"] is True
            assert status["autostart"] is True
            assert status["installed"] is False
            assert status["autostart_installed"] is False
            assert status["label_plist_present"] is False
            assert status["plist_ownership"] == "not_present"
            assert status["running"] is False
            assert status["platform"] == sys.platform
            assert status["data_dir"] == str(tmp_path.resolve())
            registered.assert_not_called()

            summary = runtime.get_service_status_summary(tmp_path)
            assert summary == {
                "running": False,
                "autostart": True,
                "autostart_installed": False,
                "platform": sys.platform,
            }

    other = tmp_path / "other-data"
    other.mkdir()
    foreign = tmp_path / "foreign.plist"
    _owned_plist(foreign, other)
    with patch("sys.platform", "darwin"):
        with patch("app.runtime.get_default_plist_path", return_value=foreign):
            with patch("app.runtime.is_launchd_registered", return_value=True) as registered:
                status = runtime.autostart_status(tmp_path)
                assert status["installed"] is False
                assert status["autostart_installed"] is False
                assert status["registered"] is False
                assert status["label_plist_present"] is True
                assert status["label_registered"] is True
                assert status["plist_owned_by_data_dir"] is False
                assert "data directory mismatch" in status["plist_ownership"].lower()
                registered.assert_called()

                summary = runtime.get_service_status_summary(tmp_path)
                assert summary["autostart_installed"] is False
                assert summary["running"] is False

    owned = tmp_path / "owned.plist"
    _owned_plist(owned, tmp_path)
    with patch("sys.platform", "darwin"):
        with patch("app.runtime.get_default_plist_path", return_value=owned):
            with patch("app.runtime.is_launchd_registered", return_value=True):
                status = runtime.autostart_status(tmp_path)
                assert status["installed"] is True
                assert status["autostart_installed"] is True
                assert status["registered"] is True
                assert status["label_registered"] is True
                assert status["plist_owned_by_data_dir"] is True
                assert status["plist_ownership"] == "owned"

                summary = runtime.get_service_status_summary(tmp_path)
                assert summary["autostart_installed"] is True

def test_default_invocation_behavior(tmp_path: Path) -> None:
    """
    Test genuine PID verification when CLI serve is started without --data-dir (default invocation).
    Ensures that default user path does NOT cause status/stop to falsely report running=False.
    """
    default_dir = tmp_path / "default_contextcore"
    default_dir.mkdir(parents=True, exist_ok=True)

    with patch.dict(os.environ, {"CONTEXTCORE_DATA_DIR": str(default_dir)}):
        assert runtime.get_default_data_dir() == default_dir.resolve()
        assert runtime.resolve_data_dir(None) == default_dir.resolve()

        default_cmdline = "/usr/bin/python3 -m app.cli serve --host 127.0.0.1 --port 8420"
        default_start = "Sat Oct 24 14:00:00 2026"

        with patch("app.runtime.get_process_cmdline", return_value=default_cmdline):
            with patch("app.runtime.get_process_start_time", return_value=default_start):
                pid_path = runtime.write_pid_file(44444, port=8420, host="127.0.0.1", data_dir=None)
                assert pid_path.exists()
                assert pid_path.parent == default_dir.resolve()

                info = runtime.read_pid_file(None)
                assert info is not None
                assert info["pid"] == 44444
                assert info["cmdline"] == default_cmdline
                assert info["data_dir"] == str(default_dir.resolve())

        with patch("app.runtime.get_process_cmdline", return_value=default_cmdline):
            with patch("app.runtime.get_process_start_time", return_value=default_start):
                with patch("app.runtime.is_port_bound_by_pid", return_value=True):
                    with patch("os.kill", return_value=None):
                        running, running_pid = runtime.is_service_running(None)
                        assert running is True
                        assert running_pid == 44444

                        st = runtime.autostart_status(None)
                        assert st["running"] is True
                        assert st["pid"] == 44444

        other_dir = tmp_path / "other_contextcore"
        other_dir.mkdir(parents=True, exist_ok=True)
        with patch("app.runtime.get_process_cmdline", return_value=default_cmdline):
            with patch("app.runtime.get_process_start_time", return_value=default_start):
                with patch("app.runtime.is_port_bound_by_pid", return_value=True):
                    with patch("os.kill", return_value=None):
                        diff_check = runtime.verify_pid_identity(
                            44444,
                            expected_cmdline=default_cmdline,
                            expected_start_time=default_start,
                            expected_port=8420,
                            data_dir=other_dir,
                        )
                        assert diff_check is False


@pytest.mark.parametrize('probe', ['command', 'start', 'port'])
def test_status_transient_probe_failure_preserves_metadata_and_recovers(tmp_path, probe):
    pid = 45678
    command = f'python3 -m app.cli serve --data-dir {tmp_path.resolve()}'
    start = 'Sat Oct 24 14:00:00 2026'
    info = {'pid':pid,'port':8420,'cmdline':command,'start_time':start,'data_dir':str(tmp_path.resolve())}
    path = tmp_path / runtime.PID_FILE_NAME; original = json.dumps(info); path.write_text(original)
    with patch('app.runtime.os.kill') as kill, patch('app.runtime.get_process_cmdline',return_value=command) as cmd, patch('app.runtime.get_process_start_time',return_value=start) as lstart, patch('app.runtime.is_port_bound_by_pid',return_value=True) as port:
        target = {'command':cmd,'start':lstart,'port':port}[probe]
        target.return_value = False if probe == 'port' else None
        assert runtime.is_service_running(tmp_path) == (False,None)
        assert path.read_text() == original
        target.return_value = {'command':command,'start':start,'port':True}[probe]
        assert runtime.is_service_running(tmp_path) == (True,pid)
        assert path.read_text() == original
        assert all(call.args[1] == 0 for call in kill.call_args_list)


def test_status_recycled_pid_remains_fail_closed_and_stop_never_signals(tmp_path):
    pid = 45678
    command = f'python3 -m app.cli serve --data-dir {tmp_path.resolve()}'
    info = {'pid':pid,'port':8420,'cmdline':command,'start_time':'original lifecycle','data_dir':str(tmp_path.resolve())}
    path = tmp_path/runtime.PID_FILE_NAME; original=json.dumps(info);path.write_text(original)
    with patch('app.runtime.os.kill') as kill, patch('app.runtime.get_process_cmdline',return_value=command), patch('app.runtime.get_process_start_time',return_value='recycled lifecycle'), patch('app.runtime.is_launchd_registered',return_value=False):
        assert runtime.is_service_running(tmp_path) == (False,None)
        result = runtime.stop_service(tmp_path,force=True)
        assert result['ok'] is False and result['stopped'] is False
        assert 'identity' in result['error']
        assert path.read_text() == original
        assert all(call.args == (pid,0) for call in kill.call_args_list)


def test_status_never_cleans_dead_record_but_explicit_stop_may(tmp_path):
    import errno
    info = {'pid':45678,'port':8420,'cmdline':'python3 -m app.cli serve','start_time':'old','data_dir':str(tmp_path.resolve())}
    path=tmp_path/runtime.PID_FILE_NAME;path.write_text(json.dumps(info))
    with patch('app.runtime.os.kill',side_effect=ProcessLookupError(errno.ESRCH,'exited')) as kill, patch('app.runtime.is_launchd_registered',return_value=False):
        assert runtime.is_service_running(tmp_path) == (False,None)
        assert path.exists()
        result=runtime.stop_service(tmp_path)
        assert result['ok'] is True and result['stopped'] is False
        assert not path.exists()
        assert all(call.args[1] == 0 for call in kill.call_args_list)


def test_status_permission_failure_and_missing_identity_preserve_record(tmp_path):
    import errno
    info={'pid':45678,'port':8420,'cmdline':'python3 -m app.cli serve','start_time':'old','data_dir':str(tmp_path.resolve())}
    path=tmp_path/runtime.PID_FILE_NAME;original=json.dumps(info);path.write_text(original)
    with patch('app.runtime.os.kill',side_effect=PermissionError(errno.EPERM,'denied')), patch('app.runtime.is_launchd_registered',return_value=False):
        assert runtime.is_service_running(tmp_path) == (False,None)
        assert runtime.stop_service(tmp_path)['ok'] is False
        assert path.read_text() == original
    path.write_text(json.dumps({'pid':45678}))
    assert runtime.is_service_running(tmp_path) == (False,None)
    assert path.exists()



def _synthetic_pinned_release(data):
    import hashlib
    from app.frontend_release import _digest_tree
    ui_source = data/'frontend-staging';ui_source.mkdir(parents=True)
    (ui_source/'index.html').write_text('<html>synthetic pinned UI</html>')
    (ui_source/'asset.js').write_text('const fixture = true;')
    frontend_hash = _digest_tree(ui_source)
    ui = data/'frontend-releases'/frontend_hash;ui.parent.mkdir();ui_source.rename(ui)
    contents = {'app/main.py':'# synthetic backend\n','app/runtime.py':'# synthetic runtime\n','requirements.txt':'fastapi\n'}
    files = {name:hashlib.sha256(text.encode()).hexdigest() for name,text in contents.items()}
    backend_hash = hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    release_id=backend_hash[:16]+'-'+frontend_hash[:16]
    backend=data/'releases'/release_id/'backend'
    for name,text in contents.items():
        path=backend/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    record={'release_id':release_id,'backend_sha256':backend_hash,'frontend_sha256':frontend_hash,
            'frontend_dir':str(ui),'backend_dir':str(backend),'api_contract':2,'files':files}
    (backend.parent/'manifest.json').write_text(json.dumps(record))
    return record


@pytest.mark.parametrize('active', [False,True])
def test_autostart_install_retains_verified_pinned_service_and_ui(tmp_path,active):
    data=(tmp_path/'data').resolve();data.mkdir()
    record=_synthetic_pinned_release(data)
    backend=Path(record['backend_dir'])
    # A workspace reinstall follows the explicit active release; a process
    # already executing its pinned backend can recover from its own manifest.
    if active:
        (data/'current-release.json').write_text(json.dumps(record))
        current_file=tmp_path/'workspace/backend/app/runtime.py'
    else:
        current_file=backend/'app/runtime.py'
    plist=tmp_path/'launchagents/service.plist'
    with patch.object(runtime,'__file__',str(current_file)), patch('sys.platform','darwin'), patch('app.runtime.is_launchd_registered',return_value=False), patch('app.runtime.subprocess.run',return_value=MagicMock(returncode=0,stderr='',stdout='')) as launch:
        installed=runtime.install_autostart(data,python_path='/synthetic/venv/bin/python',plist_path=plist,register=True)
    assert installed['ok'] is True and installed['registered'] is True
    launch.assert_called_once()
    assert launch.call_args.args[0][1] == 'bootstrap'
    actual=plistlib.loads(plist.read_bytes())
    args=actual['ProgramArguments']
    assert args[args.index('--frontend-dir')+1] == record['frontend_dir']
    assert args[args.index('--data-dir')+1] == str(data)
    assert actual['WorkingDirectory'] == str(backend)
    assert actual['EnvironmentVariables']['PYTHONPATH'] == str(backend)
    assert actual['EnvironmentVariables']['CONTEXTCORE_RELEASE_ID'] == record['release_id']
    assert runtime.get_autostart_desired(data) is True


@pytest.mark.parametrize('corrupt', ['manifest','frontend','backend','other_data','symlink','active_mismatch'])
def test_autostart_damaged_pinned_release_never_rewrites_or_unregisters(tmp_path,corrupt):
    data=(tmp_path/'data').resolve();data.mkdir();record=_synthetic_pinned_release(data)
    backend=Path(record['backend_dir']);manifest=backend.parent/'manifest.json'
    if corrupt == 'manifest': manifest.write_text('{broken')
    elif corrupt == 'frontend': (Path(record['frontend_dir'])/'asset.js').write_text('external edit')
    elif corrupt == 'backend': (backend/'app/main.py').write_text('external edit')
    elif corrupt == 'other_data':
        record['frontend_dir']=str(tmp_path/'outside-ui');manifest.write_text(json.dumps(record))
    elif corrupt == 'symlink':
        target=Path(record['frontend_dir'])/'asset.js';target.unlink();target.symlink_to(backend/'app/main.py')
    else:
        # A damaged explicit active record cannot silently use a valid own manifest.
        (data/'current-release.json').write_text(json.dumps({**record,'release_id':'mismatch'}))
    plist=tmp_path/'installed.plist'
    original=plistlib.dumps({'Label':runtime.DEFAULT_LABEL,'ProgramArguments':['python','-m','app.cli','serve'],
                            'EnvironmentVariables':{'CONTEXTCORE_DATA_DIR':str(data)}})
    plist.write_bytes(original)
    with patch.object(runtime,'__file__',str(backend/'app/runtime.py')), patch('sys.platform','darwin'), patch('app.runtime.is_launchd_registered',return_value=True), patch('app.runtime.subprocess.run') as launch:
        result=runtime.install_autostart(data,plist_path=plist,register=True)
    assert result['ok'] is False and 'verification failed' in result['error']
    assert plist.read_bytes() == original
    launch.assert_not_called()
    assert not (data/runtime.SETTINGS_FILE_NAME).exists()


def test_autostart_without_release_metadata_preserves_development_behavior(tmp_path):
    with patch.object(runtime,'__file__',str(tmp_path/'workspace/backend/app/runtime.py')):
        actual=plistlib.loads(runtime.generate_launchd_plist(tmp_path/'data').encode())
    assert '--frontend-dir' not in actual['ProgramArguments']
    assert 'CONTEXTCORE_RELEASE_ID' not in actual['EnvironmentVariables']
    assert actual['WorkingDirectory'] == str(tmp_path/'workspace/backend')



def test_published_backend_missing_manifest_refuses_development_fallback(tmp_path):
    data=tmp_path/'selected-data';data.mkdir()
    # A published backend from another data directory is still a release, not
    # a workspace. No active pointer or adjacent manifest is available.
    backend=tmp_path/'other-data/releases/release-id/backend'
    (backend/'app').mkdir(parents=True)
    plist=tmp_path/'installed.plist'
    original=plistlib.dumps({'Label':runtime.DEFAULT_LABEL,'ProgramArguments':['python','-m','app.cli','serve'],
                            'EnvironmentVariables':{'CONTEXTCORE_DATA_DIR':str(data)}})
    plist.write_bytes(original)
    with patch.object(runtime,'__file__',str(backend/'app/runtime.py')), patch('sys.platform','darwin'), patch('app.runtime.is_launchd_registered',return_value=True), patch('app.runtime.subprocess.run') as launch:
        result=runtime.install_autostart(data,plist_path=plist,register=True)
    assert result['ok'] is False
    assert 'manifest is unavailable' in result['error']
    assert plist.read_bytes() == original
    launch.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("pid", True),
    ("cmdline", ["python", "-m", "app.cli", "serve"]),
    ("start_time", ["unexpected"]),
    ("data_dir", ["unexpected"]),
    ("port", "8420"),
])
def test_corrupt_pid_metadata_never_proves_running_or_signals(tmp_path, field, value):
    payload = {
        "pid": 12345,
        "cmdline": f"python -m app.cli serve --data-dir {tmp_path}",
        "start_time": "recorded-start",
        "data_dir": str(tmp_path),
        "port": 8420,
    }
    payload[field] = value
    (tmp_path / runtime.PID_FILE_NAME).write_text(json.dumps(payload))
    with patch("app.runtime.os.kill") as signal:
        assert runtime.is_service_running(tmp_path) == (False, None)
    signal.assert_not_called()


def test_installed_console_process_has_strict_runtime_identity(tmp_path):
    script = Path(sys.prefix) / ("Scripts/contextcore.exe" if os.name == "nt" else "bin/contextcore")
    command = f"{sys.executable} {script} serve --port 18420 --data-dir {tmp_path}"
    start = "synthetic-recorded-start"
    with patch("app.runtime.os.kill"), patch("app.runtime.get_process_cmdline", return_value=command), \
            patch("app.runtime.get_process_start_time", return_value=start), \
            patch("app.runtime.is_port_bound_by_pid", return_value=True):
        assert runtime.verify_pid_identity(
            12345, expected_cmdline=command, expected_start_time=start,
            expected_port=18420, data_dir=tmp_path,
        ) is True
    for foreign in (
        command.replace(str(script), str(tmp_path / "foreign" / script.name)),
        f"/bin/echo {command}",
        command.replace(" serve ", " serve-other "),
    ):
        with patch("app.runtime.os.kill"), patch("app.runtime.get_process_cmdline", return_value=foreign), \
                patch("app.runtime.get_process_start_time", return_value=start), \
                patch("app.runtime.is_port_bound_by_pid", return_value=True):
            assert runtime.verify_pid_identity(
                12345, expected_cmdline=foreign, expected_start_time=start,
                expected_port=18420, data_dir=tmp_path,
            ) is False


@pytest.mark.parametrize("job_pid", [99999, 0, None])
def test_shutdown_never_touches_another_registered_job(tmp_path, job_pid):
    pid = 55555
    command = f"{sys.executable} -m app.cli serve --data-dir {tmp_path}"
    (tmp_path / runtime.PID_FILE_NAME).write_text(json.dumps({
        "pid": pid, "port": 18420, "cmdline": command,
        "start_time": "synthetic-start", "data_dir": str(tmp_path),
    }))
    foreign = tmp_path / "other-service.plist"
    foreign.write_bytes(b"another service's unchanged configuration")
    ended = False
    signals = []

    def signal_target(target, sig):
        nonlocal ended
        signals.append((target, sig))
        if sig:
            ended = True
        elif ended:
            raise ProcessLookupError(errno.ESRCH, "No such process")

    with patch("sys.platform", "darwin"), \
            patch("app.runtime.is_service_running", return_value=(True, pid)), \
            patch("app.runtime.verify_pid_identity", return_value=True), \
            patch("app.runtime.is_launchd_registered", return_value=True), \
            patch("app.runtime._registered_launchd_pid", return_value=job_pid), \
            patch("app.runtime.get_default_plist_path", return_value=foreign), \
            patch("app.runtime.os.kill", side_effect=signal_target), \
            patch("app.runtime.subprocess.run") as launchctl:
        result = runtime.stop_service(tmp_path, timeout=0.1)
    launchctl.assert_not_called()
    assert foreign.read_bytes() == b"another service's unchanged configuration"
    if job_pid is None:
        assert result["ok"] is False and result["stopped"] is False
        assert signals == []
        assert (tmp_path / runtime.PID_FILE_NAME).exists()
    else:
        assert result["ok"] is True and result["stopped"] is True
        assert all(target == pid for target, _ in signals)
        assert any(sig for _, sig in signals)
        assert not (tmp_path / runtime.PID_FILE_NAME).exists()


@pytest.mark.parametrize("state,replace_record", [("Z", False), ("Z+", True), ("", False)])
def test_exited_zombie_is_stopped_without_forcing_or_removing_new_pid(tmp_path, state, replace_record):
    pid = 55555
    info = {"pid": pid, "port": 18420, "cmdline": f"python -m app.cli serve --data-dir {tmp_path}",
            "start_time": "synthetic-start", "data_dir": str(tmp_path)}
    path = tmp_path / runtime.PID_FILE_NAME
    path.write_text(json.dumps(info))
    replacement = {**info, "pid": 66666, "start_time": "next-instance"}
    signals = []

    def signal_target(target, sig):
        signals.append((target, sig))
        if sig == runtime.signal.SIGTERM and replace_record:
            path.write_text(json.dumps(replacement))

    with patch("app.runtime.is_service_running", return_value=(True, pid)), \
            patch("app.runtime.verify_pid_identity", return_value=True), \
            patch("app.runtime.is_launchd_registered", return_value=False), \
            patch("app.runtime.os.kill", side_effect=signal_target), \
            patch("app.runtime.subprocess.run", return_value=MagicMock(returncode=0, stdout=state, stderr="")):
        result = runtime.stop_service(tmp_path, timeout=0.02)
    assert all(target == pid and sig != runtime.signal.SIGKILL for target, sig in signals)
    if state:
        assert result["ok"] is True and result["stopped"] is True
        if replace_record:
            assert json.loads(path.read_text()) == replacement
        else:
            assert not path.exists()
    else:
        assert result["ok"] is False and result["stopped"] is False
        assert json.loads(path.read_text()) == info


def test_frozen_direct_service_identity_excludes_hook_and_other_executables(tmp_path, monkeypatch):
    import sys
    binary = tmp_path / 'Senrelay.app/Contents/Resources/service/senrelay-service'
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(binary))
    assert runtime.check_cli_serve_tokens(f'{binary} --port 18626 --data-dir /synthetic')
    assert runtime.check_cli_serve_tokens(f'{binary} --data-dir /synthetic --port 18626')
    assert not runtime.check_cli_serve_tokens(f'{binary} --hook codex stop')
    assert not runtime.check_cli_serve_tokens(f'{binary}-other --port 18626 --data-dir /synthetic')
    assert not runtime.check_cli_serve_tokens(f'{binary} -c arbitrary')


def test_check_cli_serve_tokens_accepts_macos_framework_python_app_launcher(tmp_path, monkeypatch):
    framework_ver = tmp_path / "Python.framework/Versions/3.11"
    bin_py = framework_ver / "bin/python3.11"
    app_py = framework_ver / "Resources/Python.app/Contents/MacOS/Python"
    bin_py.parent.mkdir(parents=True)
    app_py.parent.mkdir(parents=True)
    bin_py.touch()
    app_py.touch()

    venv_dir = tmp_path / "venv"
    venv_py = venv_dir / "bin/python"
    script_path = venv_dir / "bin/contextcore"
    venv_py.parent.mkdir(parents=True)
    venv_py.symlink_to(bin_py)
    script_path.touch()

    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime.sys, "executable", str(venv_py))
    monkeypatch.setattr(runtime.sys, "_base_executable", str(app_py), raising=False)
    monkeypatch.setattr(runtime.sys, "prefix", str(venv_dir))
    monkeypatch.setattr(runtime.sys, "base_prefix", str(framework_ver))

    assert runtime.check_cli_serve_tokens(
        f"{app_py.resolve()} {script_path.resolve()} serve --host 127.0.0.1 --port 18420"
    )


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX ps identity probe")
def test_process_identity_probe_handles_unicode_paths_under_ascii_locale(tmp_path):
    import subprocess
    backend = Path(__file__).resolve().parents[1]
    script = '''
import json,os,sys
from app import runtime
command = runtime.get_process_cmdline(os.getpid())
expected = os.fsencode(sys.argv[-1]).decode('utf-8')
first = runtime.get_process_start_time(os.getpid())
os.environ['LC_ALL'] = 'zh_CN.UTF-8'
second = runtime.get_process_start_time(os.getpid())
print(json.dumps({'command_matches': bool(command and expected in command),
                  'stable_start': bool(first and first == second)}))
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / '\u4e2d\u6587\u8def\u5f84')],
        env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C', 'PYTHONUTF8': '0',
             'PYTHONCOERCECLOCALE': '0', 'PYTHONPATH': str(backend)},
        capture_output=True, encoding='utf-8', timeout=10, check=True)
    assert json.loads(result.stdout) == {'command_matches': True, 'stable_start': True}
