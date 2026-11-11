"""Behavioral tests for release distribution, packaging, and static discovery contracts."""
from __future__ import annotations

import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from app import cli, runtime


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_http(host: str, port: int, path: str = "/health", timeout: float = 6.0) -> http.client.HTTPResponse:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=1.0)
            conn.request("GET", path)
            return conn.getresponse()
        except (OSError, http.client.HTTPException):
            time.sleep(0.1)
    raise TimeoutError(f"Server at {host}:{port} did not respond within {timeout}s")


def test_cli_serve_explicit_missing_frontend_fails(tmp_path: Path) -> None:
    """Providing an explicit --frontend-dir that does not exist must fail immediately."""
    parser = cli.create_parser()
    missing_dir = tmp_path / "nonexistent-frontend"
    args = parser.parse_args(["serve", "--data-dir", str(tmp_path), "--frontend-dir", str(missing_dir)])

    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        exit_code = cli.cmd_serve(args)
        assert exit_code == 1
        assert "Explicit frontend distribution is unavailable" in mock_stderr.getvalue()


def test_cli_serve_static_discovery_behavior(tmp_path: Path) -> None:
    """CLI serve mounts frontend distribution and responds with 200 on root path."""
    backend_dir = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    fake_frontend = tmp_path / "dist-staging"
    fake_frontend.mkdir()
    marker = "MARKER_TEST_DIST_STAGING_CONTENT"
    (fake_frontend / "index.html").write_text(f"<!doctype html><html><body>{marker}</body></html>\n", encoding="utf-8")

    port = find_free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir)
    home = (tmp_path / 'home').resolve()
    home.mkdir()
    env['HOME'] = str(home)

    cmd = [
        sys.executable, "-m", "app.cli", "serve",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--data-dir", str(data_dir),
        "--frontend-dir", str(fake_frontend),
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with wait_for_http("127.0.0.1", port, path="/") as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert marker in body
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
            raise


def test_cli_serve_no_static_flag_behavior(tmp_path: Path) -> None:
    """When --no-static is passed, API serves health but static root returns 404."""
    backend_dir = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    fake_frontend = tmp_path / "dist-staging"
    fake_frontend.mkdir()
    (fake_frontend / "index.html").write_text("<!doctype html><html><body>STATIC</body></html>\n", encoding="utf-8")

    port = find_free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir)
    home = (tmp_path / 'home').resolve()
    home.mkdir()
    env['HOME'] = str(home)

    cmd = [
        sys.executable, "-m", "app.cli", "serve",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--data-dir", str(data_dir),
        "--frontend-dir", str(fake_frontend),
        "--no-static",
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with wait_for_http("127.0.0.1", port, path="/health") as resp_health:
            assert resp_health.status == 200

        with wait_for_http("127.0.0.1", port, path="/") as resp_root:
            assert resp_root.status == 404
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
            raise


def test_release_preparation_and_activation_safeguards(tmp_path: Path) -> None:
    """prepare_release and activate_release must enforce atomic write, 0600 mode, and fail-closed PID checks."""
    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
    sys.path.insert(0, str(scripts_dir))
    import prepare_release

    fake_backend = tmp_path / "backend"
    fake_backend.mkdir()
    (fake_backend / "app").mkdir()
    (fake_backend / "app" / "main.py").write_text("# backend main\n", encoding="utf-8")
    (fake_backend / "requirements.txt").write_text("fastapi==0.125.0\n", encoding="utf-8")

    fake_frontend = tmp_path / "frontend"
    fake_frontend.mkdir()
    (fake_frontend / "index.html").write_text("<!doctype html><html></html>\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    record = prepare_release.prepare(fake_backend, fake_frontend, data_dir)
    assert record["api_contract"] == 2
    assert "release_id" in record
    assert Path(record["launcher_path"]).exists()

    # Activation writes current-release.json with 0o600 mode
    active_path = prepare_release.activate_release(record, data_dir)
    assert active_path.exists()
    assert (active_path.stat().st_mode & 0o777) == 0o600
    saved = json.loads(active_path.read_text(encoding="utf-8"))
    assert saved["release_id"] == record["release_id"]

    # PID safeguard test 1: running process with JSON metadata blocks activation
    pid_file = data_dir / "service.pid"
    my_pid = os.getpid()
    pid_file.write_text(json.dumps({
        "pid": my_pid,
        "port": 8420,
        "start_time": "2026-10-25T00:00:00Z",
        "cmdline": sys.argv,
        "data_dir": str(data_dir),
    }), encoding="utf-8")

    with pytest.raises(RuntimeError):
        prepare_release.activate_release(record, data_dir)

    # PID safeguard test 2: corrupted PID file fails closed
    pid_file.write_text("CORRUPTED_METADATA", encoding="utf-8")
    with pytest.raises(RuntimeError):
        prepare_release.activate_release(record, data_dir)

    for invalid_pid in (None, True, 0, "12345"):
        pid_file.write_text(json.dumps({"pid": invalid_pid}), encoding="utf-8")
        with pytest.raises(RuntimeError):
            prepare_release.activate_release(record, data_dir)
        assert json.loads(active_path.read_text()) == saved

    # PID safeguard test 3: symlink PID file fails closed
    pid_file.unlink()
    pid_file.symlink_to(tmp_path / "target")
    with pytest.raises(ValueError):
        prepare_release.activate_release(record, data_dir)


def test_standalone_preparer_uses_explicit_backend_without_default_tree(tmp_path):
    import hashlib
    import shutil

    script = tmp_path / "standalone" / "prepare_release.py"
    script.parent.mkdir()
    original_script = Path(__file__).resolve().parents[2] / "scripts" / "prepare_release.py"
    shutil.copy2(original_script, script)
    shutil.copy2(original_script.with_name("launch_release.py"), script.with_name("launch_release.py"))
    backend = tmp_path / "selected-source"
    original_app = Path(__file__).resolve().parents[1] / "app"
    shutil.copytree(original_app, backend / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    frontend = tmp_path / "selected-ui"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html><body>Explicit frontend</body></html>", encoding="utf-8")
    data = tmp_path / "prepared-data"
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--backend", str(backend),
         "--frontend", str(frontend), "--data-dir", str(data)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    candidate = Path(record["backend_dir"]).parent / "manifest.json"
    manifest = json.loads(candidate.read_text())
    assert manifest["files"]["app/main.py"] == hashlib.sha256((backend / "app" / "main.py").read_bytes()).hexdigest()
    assert Path(record["backend_dir"]).is_relative_to(data.resolve())
    assert not (data / "current-release.json").exists()
