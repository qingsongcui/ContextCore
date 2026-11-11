#!/usr/bin/env python3
"""Reusable release distribution and packaging smoke checker for ContextCore.

Verifies:
1. Clean-source default serve static discovery via real subprocess CLI on ephemeral ports
   (dist-staging mounted at root, --no-static serving API/health but 404 on static root,
   missing explicit --frontend-dir immediately exiting code 1).
2. Wheel build completeness (all non-Python assets, skills, evidence, extensions in wheel).
3. Installed wheel execution (load_evidence on context_handoff schema, render_skill_or_command,
   render_hooks_config in isolated site-packages).
4. Template fail-loud behavior when assets are absent (must raise FileNotFoundError/RuntimeError,
   never return empty content or silent success).
5. Immutable candidate release preparation & activation safeguards (manifest, launcher,
   symlink check before resolve, fail-closed live PID check).
6. Exact dependency synchronization across pyproject.toml, requirements.txt, and uv.lock.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import json
from email.parser import BytesParser
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile

SCRIPT_DIR = Path(__file__).resolve().parent
APP_ROOT = SCRIPT_DIR.parent
BACKEND_DIR = APP_ROOT / "backend"

# Ensure backend modules are importable
sys.path.insert(0, str(BACKEND_DIR))


def log(msg: str) -> None:
    print(f"[check_release_readiness] {msg}")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_http(host: str, port: int, path: str = "/health", timeout: float = 8.0) -> http.client.HTTPResponse:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=1.0)
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp
        except (OSError, http.client.HTTPException):
            time.sleep(0.1)
    raise TimeoutError(f"Server at {host}:{port} did not respond within {timeout}s")


def verify_dependencies(backend_dir: Path) -> None:
    """Compare declared runtime pins with the requirements and resolved lock."""
    root = backend_dir.resolve()
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    with (root / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)

    def pins(requirements):
        result = {}
        for raw in requirements:
            match = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[([A-Za-z0-9_,.-]+)\])?==([A-Za-z0-9_.+!-]+)",
                raw.strip(),
            )
            if not match:
                raise AssertionError(f"Runtime dependency must have an exact pin: {raw}")
            name, extras, version = match.groups()
            key = (re.sub(r"[-_.]+", "-", name.lower()), tuple(sorted((extras or "").split(","))))
            if key in result:
                raise AssertionError(f"Duplicate runtime dependency: {name}")
            result[key] = version
        return result

    def read_requirements(path, visited):
        path = path.resolve()
        if not path.is_relative_to(root) or path in visited:
            raise AssertionError("Requirements include escapes the backend or contains a cycle")
        visited = {*visited, path}
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.partition("#")[0].strip()
            if not line:
                continue
            if line.startswith("-r "):
                result.extend(read_requirements(path.parent / line[3:].strip(), visited))
            else:
                result.append(line)
        return result

    declared = pins(project["dependencies"])
    installed = pins(read_requirements(root / "requirements.txt", set()))
    if declared != installed:
        raise AssertionError("requirements.txt and pyproject runtime dependencies differ")
    resolved = {}
    for package in lock["package"]:
        name = re.sub(r"[-_.]+", "-", package["name"].lower())
        resolved.setdefault(name, set()).add(package["version"])
    for (name, _), version in declared.items():
        if version not in resolved.get(name, set()):
            raise AssertionError(f"Locked runtime dependency differs: {name}")
    if any(name == "pyinstaller" for name, _ in declared):
        raise AssertionError("PyInstaller belongs to the optional build environment, not runtime")
    log("Declared runtime pins, requirements and dependency lock agree.")


def verify_static_discovery_subprocess(backend_dir: Path, app_root: Path) -> None:
    log("Verifying real source CLI static discovery via subprocess HTTP...")
    env = os.environ.copy()

    with tempfile.TemporaryDirectory(prefix="cc-static-sub-") as tmp:
        tmp_path = Path(tmp)
        isolated_home = tmp_path / "home"
        isolated_home.mkdir()
        env["HOME"] = str(isolated_home)
        copied_backend = tmp_path / "app" / "backend"
        shutil.copytree(backend_dir / "app", copied_backend / "app",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env["PYTHONPATH"] = str(copied_backend)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Probe 1: Explicit missing frontend dir must fail immediately with exit code 1
        nonexistent = tmp_path / "does-not-exist"
        cmd_invalid = [
            sys.executable, "-m", "app.cli", "serve",
            "--host", "127.0.0.1",
            "--port", str(find_free_port()),
            "--data-dir", str(data_dir),
            "--frontend-dir", str(nonexistent),
        ]
        res = subprocess.run(cmd_invalid, env=env, capture_output=True, text=True)
        if res.returncode != 1:
            raise AssertionError(f"Expected CLI with missing frontend-dir to exit 1, got {res.returncode}")
        log("  Explicit missing frontend-dir correctly rejected.")

        # Default discovery must prefer the current build, never the stale dist.
        fake_frontend_root = tmp_path / "app" / "frontend" / "dist-staging"
        fake_frontend_root.mkdir(parents=True)
        marker = "MARKER_CHECK_DIST_STAGING_CONTENT"
        (fake_frontend_root / "index.html").write_text(f"<!doctype html><html><body>{marker}</body></html>\n", encoding="utf-8")
        stale_frontend = tmp_path / "app" / "frontend" / "dist"
        stale_frontend.mkdir()
        (stale_frontend / "index.html").write_text("STALE_BUILD_MUST_NOT_LOAD", encoding="utf-8")

        port_staging = find_free_port()
        cmd_staging = [
            sys.executable, "-m", "app.cli", "serve",
            "--host", "127.0.0.1",
            "--port", str(port_staging),
            "--data-dir", str(data_dir),
        ]
        proc = subprocess.Popen(cmd_staging, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            resp = wait_for_http("127.0.0.1", port_staging, path="/")
            if resp.status != 200:
                raise AssertionError(f"Expected root mount to return 200, got {resp.status}")
            body = resp.read().decode("utf-8")
            if marker not in body:
                raise AssertionError("Expected dist-staging index.html content in response body")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        log("  Staging frontend mount responds with 200 and expected index.html content.")

        # Probe 3: --no-static bypasses static mounting (root returns 404, health returns 200)
        port_nostatic = find_free_port()
        cmd_nostatic = [
            sys.executable, "-m", "app.cli", "serve",
            "--host", "127.0.0.1",
            "--port", str(port_nostatic),
            "--data-dir", str(data_dir),
            "--frontend-dir", str(fake_frontend_root),
            "--no-static",
        ]
        proc_no = subprocess.Popen(cmd_nostatic, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            resp_health = wait_for_http("127.0.0.1", port_nostatic, path="/health")
            if resp_health.status != 200:
                raise AssertionError(f"Expected /health to return 200, got {resp_health.status}")
            resp_root = wait_for_http("127.0.0.1", port_nostatic, path="/")
            if resp_root.status != 404:
                raise AssertionError(f"Expected root with --no-static to return 404, got {resp_root.status}")
        finally:
            proc_no.terminate()
            proc_no.wait(timeout=5)
        log("  --no-static correctly keeps API active while bypassing static mount.")


def verify_wheel_build_and_contents(backend_dir: Path) -> None:
    log("Verifying wheel package contents and execution...")
    with tempfile.TemporaryDirectory(prefix="cc-wheel-check-") as tmp:
        wheel_dir = Path(tmp) / "dist"
        wheel_dir.mkdir()

        cmd = ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)]
        result = subprocess.run(cmd, cwd=str(backend_dir), capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(f"Wheel build failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}")

        wheels = list(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"Expected 1 wheel file, found {len(wheels)}")
        whl_path = wheels[0]

        with zipfile.ZipFile(whl_path) as zf:
            namelist = set(zf.namelist())
            required_assets = [
                "app/integrations/host_capability_evidence.json",
                "app/integrations/omp_extension.ts",
                "app/integrations/opencode_plugin.ts",
                "app/integrations/skills/codex_skill.md",
                "app/integrations/skills/omp_command.md",
                "app/integrations/skills/cursor_skill.md",
                "app/integrations/skills/antigravity_skill.md",
                "app/integrations/skills/claude_code_skill.md",
                "app/integrations/skills/opencode_command.md",
                "app/services/semantic-runtime-wheels.json",
                "app/services/semantic-runtime-requirements.txt",
            ]
            for asset in required_assets:
                if not any(name.endswith(asset) for name in namelist):
                    raise AssertionError(f"Wheel archive missing required packaged asset: {asset}")
            metadata_files = [name for name in namelist if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise AssertionError("Wheel must contain exactly one package metadata record")
            metadata = BytesParser().parsebytes(zf.read(metadata_files[0]))
            declared_version = tomllib.loads((backend_dir / 'pyproject.toml').read_text())['project']['version']
            if metadata.get('Version') != declared_version:
                raise AssertionError('Wheel metadata differs from declared canonical Python version')
            if metadata.get("License-Expression") != "MIT":
                raise AssertionError("Wheel must declare its project license")
            for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
                members = [entry for entry in namelist if entry.endswith(f"/licenses/{name}")]
                if len(members) != 1 or zf.read(members[0]) != (backend_dir / name).read_bytes():
                    raise AssertionError(f"Wheel must retain its complete license notice: {name}")

        log(f"  Wheel {whl_path.name} contains all {len(required_assets)} non-Python assets.")

        # Install the artifact non-editably, with the canonical locked runtime.
        # An extracted wheel on PYTHONPATH can otherwise hide entrypoint bugs.
        installed = Path(tmp) / "installed"
        export = subprocess.run(
            ["uv", "export", "--frozen", "--no-dev", "--no-editable", "--no-emit-project", "--format", "requirements-txt"],
            cwd=backend_dir, capture_output=True, text=True,
        )
        if export.returncode:
            raise AssertionError("Cannot export the locked runtime for installed-wheel verification")
        requirements = Path(tmp) / "runtime.txt"
        requirements.write_text(export.stdout, encoding="utf-8")
        scripts = installed / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        console = scripts / ("contextcore.exe" if os.name == "nt" else "contextcore")
        commands = [
            ["uv", "venv", "--python", sys.executable, str(installed)],
            ["uv", "pip", "sync", "--python", str(python), "--require-hashes", str(requirements)],
            ["uv", "pip", "install", "--python", str(python), "--no-deps", str(whl_path)],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=tmp, capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(f"Isolated wheel installation failed:\n{result.stderr}")

        test_script = """
import sys
from pathlib import Path
import app
from importlib.metadata import version
from app.config import settings
assert Path(app.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), "App leaked from the source checkout"
assert settings.app_version == version('contextcore-backend'), 'Runtime version differs from installed wheel'

# Verify evidence loading from wheel
from app.integrations.host_capability_matrix import load_evidence
evidence = load_evidence()
assert isinstance(evidence, dict), "load_evidence must return a dict"
assert "context_handoff" in evidence, "context_handoff missing from evidence"
assert "codex" in evidence["context_handoff"], "codex missing from context_handoff evidence"
assert "omp" in evidence["context_handoff"], "omp missing from context_handoff evidence"

# Verify real template rendering from wheel
from app.integrations.templates import render_skill_or_command, render_hooks_config

codex_skill = render_skill_or_command("codex")
assert codex_skill["filename"] == "SKILL.md", "codex skill filename invalid"
assert len(codex_skill["content"].strip()) > 50, "codex skill content empty"

omp_cmd = render_skill_or_command("omp")
assert omp_cmd["filename"] == "contextcore.md", "omp command filename invalid"
assert len(omp_cmd["content"].strip()) > 50, "omp command content empty"

omp_hooks = render_hooks_config("omp")
assert omp_hooks["format"] == "ts", "omp hooks format must be ts"
assert len(omp_hooks["content"].strip()) > 50, "omp extension content empty"

opencode_hooks = render_hooks_config("opencode")
assert opencode_hooks["format"] == "ts", "opencode hooks format must be ts"
assert len(opencode_hooks["content"].strip()) > 50, "opencode plugin content empty"

codex_hooks = render_hooks_config("codex")
assert codex_hooks["format"] == "json", "codex hooks format must be json"
assert isinstance(codex_hooks["data"], dict), "codex hooks data must be dict"

print("SUCCESS_ISOLATED_WHEEL_EXECUTION")
"""
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME", "CONTEXTCORE_DB", "CONTEXTCORE_DATA_DIR",
                              "CONTEXTCORE_TOKEN", "CONTEXTCORE_CREDENTIAL_PATH"}}
        exec_res = subprocess.run([str(python), "-I", "-c", test_script], cwd=tmp, env=env, capture_output=True, text=True)
        if exec_res.returncode != 0:
            raise AssertionError(f"Installed wheel execution probe failed:\n{exec_res.stderr}")
        if "SUCCESS_ISOLATED_WHEEL_EXECUTION" not in exec_res.stdout:
            raise AssertionError("Installed wheel execution probe did not complete")
        log("  Non-editable wheel loads evidence and renders templates without the source checkout.")
        verify_installed_console(console, Path(tmp), env, expected_version=declared_version)


def verify_installed_console(console: Path, root: Path, env: dict, expected_version: str | None = None) -> None:
    """Exercise the exact public entrypoint, including verified status and stop."""
    home, data, frontend = root / "home", root / "data", root / "frontend"
    home.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("<html><body>Installed wheel workspace</body></html>", encoding="utf-8")
    env = {**env, "HOME": str(home)}
    port = find_free_port()
    process = subprocess.Popen(
        [str(console), "serve", "--host", "127.0.0.1", "--port", str(port),
         "--data-dir", str(data), "--frontend-dir", str(frontend)],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        response = wait_for_http("127.0.0.1", port, "/")
        if response.status != 200 or b"Installed wheel workspace" not in response.read():
            raise AssertionError("Installed console did not serve the selected workspace")
        health = wait_for_http('127.0.0.1', port, '/health')
        if expected_version and json.loads(health.read()).get('version') != expected_version:
            raise AssertionError('Installed health version differs from wheel metadata')
        status = subprocess.run(
            [str(console), "status", "--json", "--data-dir", str(data)],
            cwd=root, env=env, capture_output=True, text=True, timeout=15,
        )
        state = json.loads(status.stdout) if status.returncode == 0 else {}
        if state.get("running") is not True or state.get("pid") != process.pid or state.get("port") != port:
            raise AssertionError("Installed console could not identify its own running service")
        stopped = subprocess.run(
            [str(console), "stop", "--data-dir", str(data)],
            cwd=root, env=env, capture_output=True, text=True, timeout=20,
        )
        if stopped.returncode:
            observed = subprocess.run(["ps", "-p", str(process.pid), "-o", "stat="],
                                      capture_output=True, text=True, check=False)
            detail = stopped.stderr.strip() or stopped.stdout.strip()
            raise AssertionError(
                f"Installed console stop failed (exit={stopped.returncode}, "
                f"state={observed.stdout.strip()!r}, child_exit={process.poll()}): {detail}"
            )
        process.wait(timeout=10)
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise AssertionError("Installed service still accepts connections after stop")
        log("  Installed console serves, reports its own PID/port and stops cleanly.")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def verify_template_fail_loud(backend_dir: Path) -> None:
    log("Verifying fail-loud behavior when assets are absent...")
    test_script = """
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

from app.integrations import templates

# 1. Missing skill template must raise FileNotFoundError, never return empty dict or empty string
with tempfile.TemporaryDirectory() as empty_dir:
    with patch.object(templates, "SKILLS_DIR", Path(empty_dir)):
        try:
            res = templates.render_skill_or_command("codex")
            print(f"FAIL_LOUD_ERROR: render_skill_or_command returned without error: {res}")
            sys.exit(1)
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"FAIL_LOUD_ERROR: unexpected exception type: {type(exc)}: {exc}")
            sys.exit(1)

# 2. Missing OMP extension must raise FileNotFoundError
with tempfile.TemporaryDirectory() as empty_dir:
    with patch.object(templates, "OMP_EXTENSION_PATH", Path(empty_dir) / "missing.ts"):
        try:
            res = templates.render_hooks_config("omp")
            print(f"FAIL_LOUD_ERROR: render_hooks_config returned without error: {res}")
            sys.exit(1)
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"FAIL_LOUD_ERROR: unexpected exception type: {type(exc)}: {exc}")
            sys.exit(1)

# 3. Missing OpenCode plugin must raise FileNotFoundError
with tempfile.TemporaryDirectory() as empty_dir:
    with patch.object(templates, "OPENCODE_PLUGIN_PATH", Path(empty_dir) / "missing.ts"):
        try:
            res = templates.render_hooks_config("opencode")
            print(f"FAIL_LOUD_ERROR: render_hooks_config(opencode) returned without error: {res}")
            sys.exit(1)
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"FAIL_LOUD_ERROR: unexpected exception type: {type(exc)}: {exc}")
            sys.exit(1)

print("SUCCESS_FAIL_LOUD")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir)
    res = subprocess.run([sys.executable, "-c", test_script], env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Fail-loud probe failed (must raise explicit error on missing template assets):\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    if "SUCCESS_FAIL_LOUD" not in res.stdout:
        raise AssertionError(f"Fail-loud probe did not succeed:\n{res.stdout}")
    log("  Missing packaged template assets fail explicitly with FileNotFoundError.")


def verify_release_preparation_and_activation(scripts_dir: Path, backend_dir: Path) -> None:
    log("Verifying candidate release preparation and activation safeguards...")
    sys.path.insert(0, str(scripts_dir))
    import prepare_release

    with tempfile.TemporaryDirectory(prefix="cc-prep-test-") as tmp:
        tmp_path = Path(tmp)
        fake_backend = tmp_path / "backend"
        fake_backend.mkdir()
        (fake_backend / "app").mkdir()
        (fake_backend / "app" / "main.py").write_text("# main\n", encoding="utf-8")
        (fake_backend / "requirements.txt").write_text("fastapi==0.125.0\n", encoding="utf-8")

        fake_frontend = tmp_path / "frontend"
        fake_frontend.mkdir()
        (fake_frontend / "index.html").write_text("<!doctype html><html></html>\n", encoding="utf-8")
        (fake_frontend / "app.js").write_text("console.log('test');\n", encoding="utf-8")

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Run prepare
        record = prepare_release.prepare(fake_backend, fake_frontend, data_dir)
        if "release_id" not in record or "launcher_path" not in record:
            raise AssertionError("prepare() did not produce release_id or launcher_path")

        launcher_path = Path(record["launcher_path"])
        if not launcher_path.is_file():
            raise AssertionError(f"Prepared launcher file does not exist: {launcher_path}")

        # Activation on stopped service succeeds
        active_path = prepare_release.activate_release(record, data_dir)
        if not active_path.is_file():
            raise AssertionError("activate_release did not create current-release.json")
        if (active_path.stat().st_mode & 0o777) != 0o600:
            raise AssertionError(f"current-release.json has wrong permissions: {oct(active_path.stat().st_mode)}")

        # PID safeguard test 1: simulate running process with JSON metadata
        pid_file = data_dir / "service.pid"
        my_pid = os.getpid()
        pid_file.write_text(json.dumps({
            "pid": my_pid,
            "port": 8420,
            "start_time": "2026-10-25T00:00:00Z",
            "cmdline": sys.argv,
            "data_dir": str(data_dir),
        }), encoding="utf-8")

        try:
            prepare_release.activate_release(record, data_dir)
            raise AssertionError("activate_release() should have raised RuntimeError with live PID")
        except RuntimeError:
            pass

        # PID safeguard test 2: corrupted/unreadable PID file must fail closed
        pid_file.write_text("NOT_VALID_JSON", encoding="utf-8")
        try:
            prepare_release.activate_release(record, data_dir)
            raise AssertionError("activate_release() should have failed closed on unreadable PID file")
        except RuntimeError:
            pass

        # PID safeguard test 3: symlink PID file rejected immediately
        pid_file.unlink()
        pid_file.symlink_to(tmp_path / "fake-target")
        try:
            prepare_release.activate_release(record, data_dir)
            raise AssertionError("activate_release() should reject symlink PID file")
        except ValueError:
            pass

    log("  Release preparation, manifest verification, and activation safeguards passed.")


def main() -> int:
    log("Starting release readiness audit...")
    verify_dependencies(BACKEND_DIR)
    verify_static_discovery_subprocess(BACKEND_DIR, APP_ROOT)
    verify_release_preparation_and_activation(SCRIPT_DIR, BACKEND_DIR)
    verify_template_fail_loud(BACKEND_DIR)
    verify_wheel_build_and_contents(BACKEND_DIR)

    log("ALL RELEASE READINESS AUDIT CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
