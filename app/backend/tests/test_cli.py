"""
Behavioral tests for ContextCore CLI commands.
Tests loopback enforcement, parameter parsing, JSON status formatting,
force flags, and clean termination behavior.
"""

from __future__ import annotations

import io
import os
import json
import plistlib
from pathlib import Path
from unittest.mock import patch

import pytest

from app import cli, runtime


def test_is_loopback_validation() -> None:
    # Valid loopback addresses
    assert cli.is_loopback("127.0.0.1") is True
    assert cli.is_loopback("localhost") is True
    assert cli.is_loopback("::1") is True
    assert cli.is_loopback(" 127.0.0.1 ") is True
    assert cli.is_loopback("127.0.0.42") is True

    # Link-local or remote bindings must be rejected
    assert cli.is_loopback("fe80::1%lo0") is False
    assert cli.is_loopback("0.0.0.0") is False
    assert cli.is_loopback("192.168.1.100") is False
    assert cli.is_loopback("example.com") is False


def test_cli_serve_rejects_non_loopback() -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["serve", "--host", "0.0.0.0"])

    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        exit_code = cli.cmd_serve(args)
        assert exit_code == 1
        assert "not a loopback address" in mock_stderr.getvalue()


def _status_plist(path: Path, data_dir: Path) -> None:
    payload = {
        "Label": "com.contextcore.service",
        "ProgramArguments": ["python3", "-m", "app.cli", "serve", "--data-dir", str(data_dir.resolve())],
        "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(data_dir.resolve())},
    }
    path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))


def test_cli_status_json_output(tmp_path: Path) -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["status", "--data-dir", str(tmp_path), "--json"])
    missing = tmp_path / "missing.plist"

    with patch.object(cli.runtime.sys, "platform", "darwin"):
        with patch("app.runtime.get_default_plist_path", return_value=missing):
            with patch("app.runtime.is_launchd_registered") as registered:
                with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    exit_code = cli.cmd_status(args)
                    assert exit_code == 0
                    output = json.loads(mock_stdout.getvalue())
                    assert output["desired"] is True
                    assert output["installed"] is False
                    assert output["registered"] is False
                    assert output["label_plist_present"] is False
                    assert output["plist_owned_by_data_dir"] is False
                    assert output["plist_ownership"] == "not_present"
                    assert output["running"] is False
                    assert output["data_dir"] == str(tmp_path.resolve())
                    registered.assert_not_called()

        other = tmp_path / "other-data"
        other.mkdir()
        foreign = tmp_path / "foreign.plist"
        _status_plist(foreign, other)
        with patch("app.runtime.get_default_plist_path", return_value=foreign):
            with patch("app.runtime.is_launchd_registered", return_value=True) as registered:
                with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    exit_code = cli.cmd_status(args)
                    assert exit_code == 0
                    output = json.loads(mock_stdout.getvalue())
                    assert output["installed"] is False
                    assert output["registered"] is False
                    assert output["label_plist_present"] is True
                    assert output["label_registered"] is True
                    assert output["plist_owned_by_data_dir"] is False
                    assert "data directory mismatch" in output["plist_ownership"].lower()
                    assert output["data_dir"] == str(tmp_path.resolve())
                    registered.assert_called()

        owned = tmp_path / "owned.plist"
        _status_plist(owned, tmp_path)
        with patch("app.runtime.get_default_plist_path", return_value=owned):
            with patch("app.runtime.is_launchd_registered", return_value=True):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    exit_code = cli.cmd_status(args)
                    assert exit_code == 0
                    output = json.loads(mock_stdout.getvalue())
                    assert output["installed"] is True
                    assert output["registered"] is True
                    assert output["label_registered"] is True
                assert output["plist_owned_by_data_dir"] is True
                assert output["plist_ownership"] == "owned"
                assert output["data_dir"] == str(tmp_path.resolve())


def test_cli_generate_plist(tmp_path: Path) -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["generate-plist", "--data-dir", str(tmp_path)])

    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        exit_code = cli.cmd_generate_plist(args)
        assert exit_code == 0
        output = mock_stdout.getvalue()
        assert "<?xml version=" in output
        assert "com.contextcore.service" in output
        assert "SuccessfulExit" in output


def test_cli_stop_not_running(tmp_path: Path) -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["stop", "--data-dir", str(tmp_path)])

    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        exit_code = cli.cmd_stop(args)
        assert exit_code == 0
        assert "not running" in mock_stdout.getvalue().lower()


def test_cli_stop_with_force_flag(tmp_path: Path) -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["stop", "--data-dir", str(tmp_path), "--force"])
    assert args.force is True


def test_cli_install_and_uninstall_have_no_force_bypass(tmp_path: Path) -> None:
    parser = cli.create_parser()
    # Install does not accept --force
    with pytest.raises(SystemExit):
        parser.parse_args(["install", "--force"])

    # Uninstall does not accept --force
    with pytest.raises(SystemExit):
        parser.parse_args(["uninstall", "--force"])

def test_cli_status_default_invocation(tmp_path: Path) -> None:
    parser = cli.create_parser()
    args = parser.parse_args(["status", "--json"])

    default_dir = tmp_path / "env_default"
    default_dir.mkdir(parents=True, exist_ok=True)

    with patch.dict(os.environ, {"CONTEXTCORE_DATA_DIR": str(default_dir)}):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            exit_code = cli.cmd_status(args)
            assert exit_code == 0
            output = json.loads(mock_stdout.getvalue())
            assert output["data_dir"] == str(default_dir.resolve())
            assert output["desired"] is True
